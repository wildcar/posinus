import pytest
from django.urls import reverse

from collector.models import EvaluationScore, NewsTranslation, ReviewEvent, Source
from collector.services.translation import translate_news


@pytest.fixture
def source(db):
    return Source.objects.create(name="Alpha", base_url="https://alpha.example/", domain="alpha.example")


@pytest.mark.django_db
def test_translate_action_persists_and_renders_translation(monkeypatch, operator, source, make_news):
    item = make_news("Original title", source, day=10, seed="action-translation")

    def fake_translate(news):
        return NewsTranslation.objects.create(
            news_item=news,
            title="Переведённый заголовок",
            body_text="Полный перевод новости.",
            summary="Краткий пересказ новости.",
            model_id="deepseek-chat",
        )

    monkeypatch.setattr("collector.views.translate_news", fake_translate)
    response = operator.post(reverse("news_translate", args=[item.pk]), follow=True)

    assert response.status_code == 200
    assert NewsTranslation.objects.filter(news_item=item).exists()
    html = response.content.decode()
    assert "Переведённый заголовок" in html
    assert "Краткий пересказ новости" in html
    assert "Перевести заново" in html


@pytest.mark.django_db
def test_translation_service_sends_configured_model(monkeypatch, settings, source, make_news):
    item = make_news("Original title", source, day=10, seed="service-translation")
    settings.NEWSCRAWLER_TRANSLATION_PROVIDER = "configured-provider"
    settings.NEWSCRAWLER_TRANSLATION_MODEL = "configured-model"
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return {
            "text": (
                "<<<TITLE>>>\nЗаголовок\n"
                "<<<SUMMARY>>>\nПересказ\n"
                "<<<BODY>>>\nПеревод с кавычкой «пример».\n"
                "<<<END>>>"
            ),
            "model_id": "actual-model",
        }

    monkeypatch.setattr("collector.services.translation.call_chat", fake_chat)
    translation = translate_news(item)

    assert captured["provider"] == "configured-provider"
    assert captured["model_id"] == "configured-model"
    assert translation.model_id == "actual-model"
    assert translation.body_text == "Перевод с кавычкой «пример»."


@pytest.mark.django_db
def test_translation_service_retries_invalid_format(monkeypatch, source, make_news):
    item = make_news("Original title", source, day=10, seed="service-retry")
    replies = iter(
        [
            {"text": "broken response", "model_id": "deepseek-chat"},
            {
                "text": (
                    "<<<TITLE>>>\nЗаголовок\n"
                    "<<<SUMMARY>>>\nПересказ\n"
                    "<<<BODY>>>\nИсправленный перевод.\n"
                    "<<<END>>>"
                ),
                "model_id": "deepseek-chat",
            },
        ]
    )
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return next(replies)

    monkeypatch.setattr("collector.services.translation.call_chat", fake_chat)
    translation = translate_news(item)

    assert len(calls) == 2
    assert "format was invalid" in calls[1]["messages"][-1]["content"]
    assert translation.body_text == "Исправленный перевод."


@pytest.mark.django_db
def test_select_action_snapshots_scores_and_is_idempotent(
    operator, settings, source, make_news, make_review
):
    item = make_news("Selected news", source, day=10, seed="action-selected")
    settings.NEWSCRAWLER_MANUAL_SCORE_SELECTOR = "evaluator"
    make_review(item, {"positivity": 8, "negativity": 1}, key="automatic")
    url = reverse("news_select", args=[item.pk])

    first = operator.post(url, follow=True)
    second = operator.post(url, follow=True)

    manual_events = ReviewEvent.objects.filter(
        news_item=item,
        selector_name="operator:operator",
        idempotency_key=f"selected:{item.pk}",
    )
    assert first.status_code == second.status_code == 200
    assert manual_events.count() == 1
    event = manual_events.get()
    assert event.decision == ReviewEvent.Decision.POSITIVE
    assert dict(
        EvaluationScore.objects.filter(review_event=event).values_list("characteristic_id", "value")
    ) == {"positivity": 8, "negativity": 1}
    assert item.occurrences.get().url == "https://alpha.example/action-selected"
    assert "Отобрано" in second.content.decode()


@pytest.mark.django_db
def test_action_endpoints_ignore_get(operator, source, make_news):
    item = make_news("Untouched news", source, day=10, seed="action-get")

    translate_response = operator.get(reverse("news_translate", args=[item.pk]))
    select_response = operator.get(reverse("news_select", args=[item.pk]))

    assert translate_response.status_code == select_response.status_code == 405
    assert not NewsTranslation.objects.filter(news_item=item).exists()
    assert not ReviewEvent.objects.filter(selector_name="operator:operator").exists()
