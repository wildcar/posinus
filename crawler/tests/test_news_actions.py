from unittest import mock

import pytest
from django.urls import reverse

from collector.models import EvaluationScore, NewsTranslation, ReviewEvent, Source
from collector.services.translation import translate_news


@pytest.fixture
def source(db):
    return Source.objects.create(name="Alpha", base_url="https://alpha.example/", domain="alpha.example")


@pytest.mark.django_db
def test_translate_action_persists_and_renders_translation(monkeypatch, operator, source, make_news):
    """End to end through the queue: the button asks, the worker translates."""
    from collector.services import jobs

    item = make_news("Original title", source, day=10, seed="action-translation")

    def fake_translate(news):
        return NewsTranslation.objects.create(
            news_item=news,
            title="Переведённый заголовок",
            body_text="Полный перевод новости.",
            summary="Краткий пересказ новости.",
            model_id="deepseek-chat",
        )

    monkeypatch.setattr("collector.services.translation.translate_news", fake_translate)
    queued = operator.post(reverse("news_translate", args=[item.pk]), follow=True)
    assert "Перевод готовится" in queued.content.decode()

    jobs.run_job(jobs.claim_next_job())
    response = operator.get(reverse("news_detail", args=[item.pk]))

    assert response.status_code == 200
    assert NewsTranslation.objects.filter(news_item=item).exists()
    html = response.content.decode()
    assert "Переведённый заголовок" in html
    assert "Краткий пересказ новости" in html
    assert "Перевести заново" in html


@pytest.mark.django_db
def test_translation_service_sends_configured_model(monkeypatch, settings, source, make_news):
    item = make_news("Original title", source, day=10, seed="service-translation")
    settings.POSINUS_TRANSLATION_PROVIDER = "configured-provider"
    settings.POSINUS_TRANSLATION_MODEL = "configured-model"
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
    operator, source, make_news, make_review
):
    item = make_news("Selected news", source, day=10, seed="action-selected")
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
    assert "Отправлена в публикацию" in second.content.decode()


@pytest.mark.django_db
def test_action_endpoints_ignore_get(operator, source, make_news):
    item = make_news("Untouched news", source, day=10, seed="action-get")

    translate_response = operator.get(reverse("news_translate", args=[item.pk]))
    select_response = operator.get(reverse("news_select", args=[item.pk]))

    assert translate_response.status_code == select_response.status_code == 405
    assert not NewsTranslation.objects.filter(news_item=item).exists()
    assert not item.translation_jobs.exists()
    assert not ReviewEvent.objects.filter(selector_name="operator:operator").exists()


@pytest.mark.django_db
def test_translation_is_queued_not_run_inside_the_request(operator, source, make_news):
    """The model can think for minutes; the request must not wait for it."""
    from collector.models import TranslationJob

    item = make_news("To translate", source, day=10, seed="job-1")

    with mock.patch("collector.services.translation.translate_news") as translate:
        response = operator.post(reverse("news_translate", args=[item.pk]), follow=True)

    translate.assert_not_called()
    job = TranslationJob.objects.get(news_item=item)
    assert job.status == TranslationJob.Status.QUEUED
    assert job.requested_by == "operator"
    assert "поставлен в очередь" in response.content.decode()


@pytest.mark.django_db
def test_a_second_click_does_not_queue_a_second_job(operator, source, make_news):
    from collector.models import TranslationJob

    item = make_news("Twice", source, day=10, seed="job-2")

    operator.post(reverse("news_translate", args=[item.pk]))
    response = operator.post(reverse("news_translate", args=[item.pk]), follow=True)

    assert TranslationJob.objects.filter(news_item=item).count() == 1
    assert "уже готовится" in response.content.decode()


@pytest.mark.django_db
def test_the_worker_runs_a_job_and_records_the_outcome(source, make_news):
    from collector.models import TranslationJob
    from collector.services import jobs

    item = make_news("Worker", source, day=10, seed="job-3")
    TranslationJob.objects.create(news_item=item)

    with mock.patch("collector.services.translation.translate_news") as translate:
        job = jobs.claim_next_job()
        assert job.status == TranslationJob.Status.RUNNING
        jobs.run_job(job)

    translate.assert_called_once()
    job.refresh_from_db()
    assert job.status == TranslationJob.Status.DONE
    assert job.finished_at is not None
    assert jobs.claim_next_job() is None


@pytest.mark.django_db
def test_a_failing_job_closes_with_its_error(source, make_news):
    from collector.models import TranslationJob
    from collector.services import jobs

    item = make_news("Broken", source, day=10, seed="job-4")
    TranslationJob.objects.create(news_item=item)

    with mock.patch("collector.services.translation.translate_news", side_effect=RuntimeError("роутер молчит")):
        jobs.run_job(jobs.claim_next_job())

    job = TranslationJob.objects.get(news_item=item)
    assert job.status == TranslationJob.Status.FAILED
    assert "роутер молчит" in job.error


@pytest.mark.django_db
def test_a_restart_does_not_leave_a_job_promising_a_translation(source, make_news):
    """Nobody is going to finish a job the previous process was running."""
    from collector.models import TranslationJob
    from collector.services import jobs

    item = make_news("Interrupted", source, day=10, seed="job-5")
    TranslationJob.objects.create(news_item=item, status=TranslationJob.Status.RUNNING)

    assert jobs.reset_interrupted_jobs() == 1
    job = TranslationJob.objects.get(news_item=item)
    assert job.status == TranslationJob.Status.FAILED
    assert "перезапуске" in job.error
