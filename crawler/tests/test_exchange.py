import pytest
from django.db import IntegrityError, connection, transaction

from collector.models import EvaluationCharacteristic, ReviewEvent, Source
from collector.services.ingest import ingest_article

EXPECTED_AXES = [
    "positivity", "negativity",
    "heartwarming", "cuteness", "humor", "pride_humanity", "pride_russia", "heroism", "inspiration", "beauty",
    "interestingness", "surprise", "uniqueness", "memorability",
    "importance", "impact_scale", "usefulness",
    "clickbait", "controversy", "promo",
]

EXAMPLE_SCORES = {
    "positivity": 8, "negativity": 1,
    "heartwarming": 7, "cuteness": 9, "humor": 4,
    "pride_humanity": 2, "pride_russia": 6, "heroism": 0,
    "inspiration": 3, "beauty": 5,
    "interestingness": 7, "surprise": 5, "uniqueness": 6, "memorability": 6,
    "importance": 2, "impact_scale": 1, "usefulness": 4,
    "clickbait": 1, "controversy": 0, "promo": 0,
}


def _insert_scores(event_id, scores):
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO exchange_evaluation_scores (review_event_id, characteristic_key, value) VALUES (%s, %s, %s)",
            [(event_id, key, value) for key, value in scores.items()],
        )


@pytest.mark.django_db
def test_exchange_views_and_append_only_trigger():
    source = Source.objects.create(name="One", base_url="https://one.example/", domain="one.example")
    item, _, _ = ingest_article(source=source, url="https://one.example/a", title="A useful headline", body="Substantial article body. " * 30, language="en")
    with connection.cursor() as cursor:
        cursor.execute("SELECT news_id, primary_url, sources_json FROM exchange_news_for_selection WHERE news_id=%s", [item.pk])
        row = cursor.fetchone()
    assert row[0] == item.pk
    assert row[1] == "https://one.example/a"
    event = ReviewEvent.objects.create(news_item=item, decision="positive", score=0.9, selector_name="test", selector_version="1", idempotency_key="one")
    with connection.cursor() as cursor:
        cursor.execute("SELECT decision FROM exchange_latest_reviews WHERE news_id=%s", [item.pk])
        assert cursor.fetchone()[0] == "positive"
        with pytest.raises(Exception, match="append-only"):
            with transaction.atomic():
                cursor.execute("UPDATE exchange_review_events SET reason='changed' WHERE id=%s", [event.pk])


@pytest.mark.django_db
def test_review_idempotency():
    source = Source.objects.create(name="One", base_url="https://one.example/", domain="one.example")
    item, _, _ = ingest_article(source=source, url="https://one.example/a", title="A useful headline", body="Substantial article body. " * 30)
    ReviewEvent.objects.create(news_item=item, decision="positive", selector_name="test", idempotency_key="same")
    with pytest.raises(Exception):
        ReviewEvent.objects.create(news_item=item, decision="positive", selector_name="test", idempotency_key="same")


@pytest.mark.django_db
def test_evaluation_characteristics_seeded():
    keys = list(EvaluationCharacteristic.objects.order_by("position", "id").values_list("key", flat=True))
    assert keys == EXPECTED_AXES
    upper = set(
        EvaluationCharacteristic.objects.filter(threshold_direction="upper_bound").values_list("key", flat=True)
    )
    assert upper == {"negativity", "clickbait", "controversy", "promo"}
    row = EvaluationCharacteristic.objects.get(key="positivity")
    assert row.title and row.category and row.description and row.anchor_low and row.anchor_high


@pytest.mark.django_db
def test_evaluation_scores_latest_view_and_append_only():
    source = Source.objects.create(name="One", base_url="https://one.example/", domain="one.example")
    item, _, _ = ingest_article(source=source, url="https://one.example/a", title="A useful headline", body="Substantial article body. " * 30, language="en")
    first = ReviewEvent.objects.create(news_item=item, decision="positive", selector_name="evaluator", selector_version="1", idempotency_key="eval-1")
    _insert_scores(first.pk, EXAMPLE_SCORES)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT characteristic_key, value FROM exchange_latest_evaluation_scores WHERE news_id=%s AND selector_name=%s",
            [item.pk, "evaluator"],
        )
        latest = dict(cursor.fetchall())
    assert latest == EXAMPLE_SCORES

    corrected = dict(EXAMPLE_SCORES, positivity=3, negativity=6)
    second = ReviewEvent.objects.create(news_item=item, decision="not_positive", selector_name="evaluator", selector_version="1", idempotency_key="eval-2")
    _insert_scores(second.pk, corrected)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT characteristic_key, value FROM exchange_latest_evaluation_scores WHERE news_id=%s AND selector_name=%s",
            [item.pk, "evaluator"],
        )
        latest = dict(cursor.fetchall())
    assert latest == corrected

    with connection.cursor() as cursor:
        with pytest.raises(Exception, match="append-only"):
            with transaction.atomic():
                cursor.execute("UPDATE exchange_evaluation_scores SET value=0 WHERE review_event_id=%s", [first.pk])
        with pytest.raises(Exception, match="append-only"):
            with transaction.atomic():
                cursor.execute("DELETE FROM exchange_evaluation_scores WHERE review_event_id=%s", [first.pk])


@pytest.mark.django_db
def test_evaluation_score_constraints():
    source = Source.objects.create(name="One", base_url="https://one.example/", domain="one.example")
    item, _, _ = ingest_article(source=source, url="https://one.example/a", title="A useful headline", body="Substantial article body. " * 30)
    event = ReviewEvent.objects.create(news_item=item, decision="positive", selector_name="evaluator", idempotency_key="eval-1")

    with connection.cursor() as cursor:
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                cursor.execute(
                    "INSERT INTO exchange_evaluation_scores (review_event_id, characteristic_key, value) VALUES (%s, %s, %s)",
                    [event.pk, "positivity", 11],
                )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                cursor.executemany(
                    "INSERT INTO exchange_evaluation_scores (review_event_id, characteristic_key, value) VALUES (%s, %s, %s)",
                    [(event.pk, "positivity", 5), (event.pk, "positivity", 6)],
                )

    # The characteristic-key foreign key is deferred, so it is verified via an
    # explicit constraint check inside a rolled-back savepoint.
    sid = transaction.savepoint()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO exchange_evaluation_scores (review_event_id, characteristic_key, value) VALUES (%s, %s, %s)",
            [event.pk, "unknown_axis", 5],
        )
    with pytest.raises(IntegrityError):
        connection.check_constraints()
    transaction.savepoint_rollback(sid)
