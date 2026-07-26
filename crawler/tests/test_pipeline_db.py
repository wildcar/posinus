"""Reading the pipeline's database: read-only, non-blocking, and never fatal."""

import json
import sqlite3
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from collector.models import OperatorEvent
from collector.services import pipeline_status
from collector.services.maintenance import backup_pipeline_db
from collector.services.pipeline_db import PipelineUnavailable, connection, fetch_all, is_available

SCHEMA = """
CREATE TABLE prepared_item (
    news_id INTEGER PRIMARY KEY, status TEXT NOT NULL, retold_title TEXT,
    retold_body_md TEXT, model_id TEXT, prepared_at TEXT, published_at TEXT, error TEXT
);
CREATE TABLE service_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, status TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, counters TEXT NOT NULL DEFAULT '{}',
    config TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT ''
);
"""


@pytest.fixture
def pipeline_db(settings, tmp_path):
    """A stand-in for the pipeline-owned database, with its two relevant tables."""
    path = tmp_path / "evaluator.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    settings.POSINUS_PIPELINE_DB_PATH = str(path)
    return path


def add_run(path, service, status, started, finished=None, counters=None, error=""):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO service_run (service, status, started_at, finished_at, counters, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (service, status, started.isoformat(), finished.isoformat() if finished else None,
         json.dumps(counters or {}), error),
    )
    con.commit()
    con.close()


@pytest.mark.django_db
def test_missing_database_is_reported_not_raised_at_import(settings, tmp_path):
    settings.POSINUS_PIPELINE_DB_PATH = str(tmp_path / "absent.sqlite3")

    assert is_available() is False
    with pytest.raises(PipelineUnavailable):
        fetch_all("SELECT 1")


def test_the_connection_refuses_to_write(pipeline_db):
    with connection() as con:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO prepared_item (news_id, status) VALUES (1, 'prepared')")


def test_a_missing_table_reads_as_unavailable(pipeline_db):
    """A host running older pipeline code has no service_run table yet."""
    with pytest.raises(PipelineUnavailable):
        fetch_all("SELECT * FROM nothing_like_this")


def test_last_runs_report_counters_per_service(pipeline_db):
    now = timezone.now()
    add_run(pipeline_db, "preparer", "ok", now - timedelta(minutes=40), now - timedelta(minutes=39))
    add_run(pipeline_db, "preparer", "ok", now - timedelta(minutes=5), now - timedelta(minutes=4),
            {"prepared": 3, "failed": 0})
    add_run(pipeline_db, "publisher", "failed", now - timedelta(minutes=2), now, error="VK не отвечает")

    runs = {run.service: run for run in pipeline_status.last_runs()}

    assert runs["preparer"].status == "ok"
    assert runs["preparer"].counters == {"prepared": 3, "failed": 0}  # only the latest row
    assert "подготовлено 3" in runs["preparer"].summary  # counters speak Russian on screen
    assert runs["publisher"].status == "failed"
    assert runs["publisher"].summary == "VK не отвечает"


def test_an_old_running_row_reads_as_interrupted(pipeline_db):
    now = timezone.now()
    add_run(pipeline_db, "evaluator", "running", now - timedelta(minutes=3))
    add_run(pipeline_db, "publisher", "running", now - timedelta(hours=5))

    runs = {run.service: run for run in pipeline_status.last_runs()}

    assert runs["evaluator"].status == "running"
    assert runs["publisher"].status == "interrupted"
    assert "прервали" in runs["publisher"].summary


@pytest.mark.django_db
def test_dashboard_shows_the_machine_block(operator, pipeline_db):
    add_run(pipeline_db, "publisher", "ok", timezone.now(), timezone.now(), {"published": 2})

    html = operator.get(reverse("dashboard")).content.decode()

    assert "Машина" in html
    assert "Публикация" in html and "опубликовано 2" in html


@pytest.mark.django_db
def test_dashboard_survives_a_missing_pipeline_database(operator, settings, tmp_path):
    settings.POSINUS_PIPELINE_DB_PATH = str(tmp_path / "absent.sqlite3")

    response = operator.get(reverse("dashboard"))

    assert response.status_code == 200
    assert "Нет связи с базой конвейера" in response.content.decode()


@pytest.mark.django_db
def test_pipeline_database_is_backed_up_with_the_crawler(settings, tmp_path, pipeline_db):
    settings.POSINUS_BACKUP_DIR = tmp_path / "backups"

    copy = backup_pipeline_db()

    assert copy is not None and copy.exists()
    assert OperatorEvent.objects.filter(event_type="pipeline_backup_success").exists()
    con = sqlite3.connect(copy)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"prepared_item", "service_run"} <= tables


@pytest.mark.django_db
def test_backup_keeps_seven_copies(settings, tmp_path, pipeline_db):
    settings.POSINUS_BACKUP_DIR = tmp_path / "backups"
    (tmp_path / "backups").mkdir()
    for index in range(9):
        (tmp_path / "backups" / f"pipeline-2026070{index}-000000.sqlite3").write_text("old")

    backup_pipeline_db(keep=7)

    assert len(list((tmp_path / "backups").glob("pipeline-*.sqlite3"))) == 7


@pytest.mark.django_db
def test_absent_pipeline_database_is_not_a_backup_failure(settings, tmp_path):
    settings.POSINUS_PIPELINE_DB_PATH = str(tmp_path / "absent.sqlite3")
    settings.POSINUS_BACKUP_DIR = tmp_path / "backups"

    assert backup_pipeline_db() is None
    assert not OperatorEvent.objects.filter(event_type="pipeline_backup_failed").exists()


@pytest.mark.django_db
def test_fragment_answers_401_for_an_expired_session(client, pipeline_db):
    """A redirect would paste the login form inside the block; 401 stops the script."""
    response = client.get(reverse("dashboard_fragment"))

    assert response.status_code == 401


@pytest.mark.django_db
def test_fragment_repeats_the_live_blocks_and_skips_an_unchanged_repaint(operator, pipeline_db):
    add_run(pipeline_db, "publisher", "ok", timezone.now(), timezone.now(), {"published": 2})

    first = operator.get(reverse("dashboard_fragment"))
    version = first["X-Dashboard-Version"]
    again = operator.get(reverse("dashboard_fragment"), {"version": version})

    assert first.status_code == 200
    assert "Машина" in first.content.decode() and "Требует внимания" in first.content.decode()
    assert again.status_code == 204  # nothing changed, no repaint
