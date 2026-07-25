"""The stop cock: the web writes a file, the pipeline reads it."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from collector.models import OperatorEvent
from collector.services import pipeline_mailbox


@pytest.fixture
def mailbox(settings, tmp_path):
    directory = tmp_path / "requests"
    directory.mkdir()
    settings.POSINUS_PIPELINE_REQUESTS_DIR = str(directory)
    return directory


@pytest.mark.django_db
def test_pause_until_cancelled_writes_the_file_and_logs_the_reason(operator, mailbox):
    response = operator.post(
        reverse("publication_pause"), {"duration": "forever", "reason": "день траура"}, follow=True
    )

    content = (mailbox / "pause").read_text(encoding="utf-8")
    assert response.status_code == 200
    assert "until=" not in content
    assert "reason=день траура" in content
    assert OperatorEvent.objects.filter(event_type="publication_paused").exists()


@pytest.mark.django_db
def test_pause_for_an_hour_carries_a_deadline(operator, mailbox):
    operator.post(reverse("publication_pause"), {"duration": "hour", "reason": ""})

    pause = pipeline_mailbox.read_pause()
    assert pause is not None
    assert timedelta(minutes=55) < pause.until - timezone.now() < timedelta(minutes=65)


@pytest.mark.django_db
def test_resume_removes_the_file(operator, mailbox):
    operator.post(reverse("publication_pause"), {"duration": "forever", "reason": "проверка"})
    operator.post(reverse("publication_resume"))

    assert not (mailbox / "pause").exists()
    assert pipeline_mailbox.read_pause() is None
    assert OperatorEvent.objects.filter(event_type="publication_resumed").exists()


@pytest.mark.django_db
def test_expired_pause_reads_as_running(mailbox):
    """The publisher deletes it on its next run; until then it must not read as a pause."""
    past = (timezone.now() - timedelta(minutes=1)).isoformat()
    (mailbox / "pause").write_text(f"until={past}\nreason=истекла\n", encoding="utf-8")

    assert pipeline_mailbox.read_pause() is None


@pytest.mark.django_db
def test_dashboard_survives_a_missing_mailbox(operator, settings, tmp_path):
    """There is no pipeline on a development machine, and the page must still open."""
    settings.POSINUS_PIPELINE_REQUESTS_DIR = str(tmp_path / "absent")

    response = operator.get(reverse("dashboard"))

    assert response.status_code == 200
    assert response.context["mailbox_error"]
    assert "Нет связи с конвейером" in response.content.decode()


@pytest.mark.django_db
def test_pause_reports_an_unwritable_mailbox_instead_of_failing(operator, settings, tmp_path):
    settings.POSINUS_PIPELINE_REQUESTS_DIR = str(tmp_path / "absent")

    response = operator.post(
        reverse("publication_pause"), {"duration": "forever", "reason": "х"}, follow=True
    )

    assert response.status_code == 200
    assert "Не получилось остановить публикации" in response.content.decode()
    assert not OperatorEvent.objects.filter(event_type="publication_paused").exists()


@pytest.mark.django_db
def test_pause_endpoints_ignore_get(operator, mailbox):
    assert operator.get(reverse("publication_pause")).status_code == 405
    assert operator.get(reverse("publication_resume")).status_code == 405


@pytest.mark.django_db
def test_run_request_lands_in_the_mailbox(mailbox):
    pipeline_mailbox.request_run("publisher")

    assert (mailbox / "run-publisher").exists()
    with pytest.raises(ValueError):
        pipeline_mailbox.request_run("nonsense")
