"""The selection rule, its explanation in the card, and the calibration screen."""

import pytest
from django.urls import reverse

from collector.models import OperatorEvent, SelectionBound, SelectionProfile, Source
from collector.services import calibration, selection


@pytest.fixture
def source(db):
    return Source.objects.create(name="Alpha", base_url="https://alpha.example/", domain="alpha.example")


@pytest.fixture
def profile(db):
    """The profile seeded by migration 0008 — the owner's rule."""
    return SelectionProfile.objects.get(name="default")


def bounds_of(profile):
    return selection.profile_bounds(profile)


@pytest.mark.django_db
def test_migration_seeded_the_active_profile(profile):
    kinds = {(b.characteristic_id, b.kind, b.value) for b in profile.bounds.all()}

    assert profile.is_active and profile.revision == 1
    assert ("positivity", "gate_min", 8) in kinds
    assert ("heroism", "gate_max", 4) in kinds
    assert ("uniqueness", "highlight_min", 9) in kinds
    assert len(kinds) == 11


@pytest.mark.django_db
def test_the_view_shows_exactly_the_active_profile(profile):
    """The pipeline reads this view; the crawler owns it."""
    from django.db import connection

    draft = SelectionProfile.objects.create(name="draft", is_active=False, revision=7)
    SelectionBound.objects.create(profile=draft, characteristic_id="cuteness", kind="highlight_min", value=3)

    with connection.cursor() as cursor:
        cursor.execute("SELECT DISTINCT profile_name, profile_revision FROM exchange_active_selection_profile")
        rows = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM exchange_active_selection_profile")
        count = cursor.fetchone()[0]

    assert rows == [("default", 1)]
    assert count == 11


@pytest.mark.django_db
def test_rule_matches_the_owners_definition(profile):
    bounds = bounds_of(profile)
    passing = {"positivity": 8, "heroism": 4, "clickbait": 4, "promo": 4, "uniqueness": 9}

    assert selection.selects(bounds, passing)  # bounds are inclusive
    assert not selection.selects(bounds, {**passing, "positivity": 7})
    assert not selection.selects(bounds, {**passing, "clickbait": 5})
    assert not selection.selects(bounds, {**passing, "uniqueness": 8})  # no strong side left


@pytest.mark.django_db
def test_explanation_names_the_failing_condition_and_the_near_miss(profile):
    verdict = selection.explain(
        profile.name, profile.revision, bounds_of(profile),
        {"positivity": 6, "heroism": 2, "clickbait": 3, "promo": 0, "interestingness": 7},
    )

    assert not verdict.passed
    assert "Позитивность 6 из 10, нужно от 8".lower() in verdict.summary.lower()
    assert "ни одна сильная сторона" in verdict.summary
    assert verdict.closest.key == "interestingness"
    assert [c.key for c in verdict.failed_gates] == ["positivity"]


@pytest.mark.django_db
def test_explanation_of_a_passing_item(profile):
    verdict = selection.explain(
        profile.name, profile.revision, bounds_of(profile),
        {"positivity": 9, "heroism": 1, "clickbait": 2, "promo": 0, "uniqueness": 9},
    )

    assert verdict.passed
    assert verdict.summary.startswith("Прошла:")
    assert verdict.closest is None


@pytest.mark.django_db
def test_card_shows_the_verdict(operator, source, make_news, make_review, profile):
    item = make_news("Explained news", source, day=10, seed="v1")
    make_review(item, {"positivity": 6, "interestingness": 7}, key="v1-e1", decision="not_positive")

    html = operator.get(reverse("news_detail", args=[item.pk])).content.decode()

    assert "Решение отбора" in html
    assert "Не прошла" in html
    assert "нужно от 8" in html
    assert "Профиль «default», редакция 1" in html


@pytest.mark.django_db
def test_corpus_check_counts_and_blockers(operator, source, make_news, make_review, profile):
    strong = make_news("Strong", source, day=10, seed="c1")
    make_review(strong, {"positivity": 9, "uniqueness": 9}, key="c1")
    weak = make_news("Weak", source, day=10, seed="c2")
    make_review(weak, {"positivity": 2, "uniqueness": 9}, key="c2", decision="not_positive")
    near = make_news("Near", source, day=10, seed="c3")
    make_review(near, {"positivity": 9, "uniqueness": 8}, key="c3", decision="not_positive")

    corpus = calibration.corpus_scores()
    outcome = calibration.apply_profile(bounds_of(profile), corpus)

    assert outcome.total == 3
    assert outcome.passed == {strong.pk}
    assert outcome.blocked_by == {"positivity": 1, "": 1}
    assert [news_id for news_id, _ in calibration.near_misses(bounds_of(profile), corpus)] == [near.pk]


@pytest.mark.django_db
def test_screen_compares_a_draft_with_the_profile_in_force(operator, source, make_news, make_review, profile):
    strong = make_news("Strong", source, day=10, seed="s1")
    make_review(strong, {"positivity": 9, "uniqueness": 9}, key="s1")
    near = make_news("Near miss", source, day=10, seed="s2")
    make_review(near, {"positivity": 9, "uniqueness": 8}, key="s2", decision="not_positive")

    response = operator.get(reverse("selection"), {"highlight_min__uniqueness": "8"})
    html = response.content.decode()

    assert response.context["current"].passed_count == 1
    assert response.context["draft_outcome"].passed_count == 2
    assert response.context["added_count"] == 1
    assert "Near miss" in html
    assert "Применить черновик" in html


@pytest.mark.django_db
def test_screen_without_a_draft_offers_no_apply(operator, profile):
    html = operator.get(reverse("selection")).content.decode()

    assert "Проверка на всех оценённых новостях" in html
    assert "Применить черновик" not in html


@pytest.mark.django_db
def test_unused_axes_are_named(operator, profile):
    html = operator.get(reverse("selection")).content.decode()

    assert "Оцениваются и нигде не используются" in html
    assert "Негативность" in html and "Конфликтность" in html


@pytest.mark.django_db
def test_apply_raises_the_revision_and_logs_the_change(operator, profile):
    payload = {f"{b.kind}__{b.key}": str(b.value) for b in bounds_of(profile)}
    payload["gate_min__positivity"] = "7"
    payload["highlight_min__pride_russia"] = ""  # condition dropped entirely

    response = operator.post(reverse("selection_apply"), payload, follow=True)

    profile.refresh_from_db()
    assert response.status_code == 200
    assert profile.revision == 2
    assert profile.bounds.get(characteristic_id="positivity", kind="gate_min").value == 7
    # an empty field removes the condition; a missing one leaves it as it was
    assert profile.bounds.count() == 10
    assert not profile.bounds.filter(characteristic_id="pride_russia").exists()
    assert OperatorEvent.objects.filter(event_type="selection_profile_changed").exists()


@pytest.mark.django_db
def test_apply_without_changes_keeps_the_revision(operator, profile):
    payload = {f"{b.kind}__{b.key}": str(b.value) for b in bounds_of(profile)}

    operator.post(reverse("selection_apply"), payload)

    profile.refresh_from_db()
    assert profile.revision == 1
    assert not OperatorEvent.objects.filter(event_type="selection_profile_changed").exists()


@pytest.mark.django_db
def test_rescore_drops_a_request_into_the_mailbox(operator, settings, tmp_path, profile):
    mailbox = tmp_path / "requests"
    mailbox.mkdir()
    settings.POSINUS_PIPELINE_REQUESTS_DIR = str(mailbox)

    operator.post(reverse("selection_rescore"))

    assert (mailbox / "run-evaluator-backfill").exists()
    assert OperatorEvent.objects.filter(event_type="selection_rescore_requested").exists()


@pytest.mark.django_db
def test_selection_write_endpoints_ignore_get(operator, profile):
    assert operator.get(reverse("selection_apply")).status_code == 405
    assert operator.get(reverse("selection_rescore")).status_code == 405
