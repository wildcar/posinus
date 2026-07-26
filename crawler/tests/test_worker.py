from datetime import timedelta

import pytest
from django.utils import timezone

from collector.models import Source, SourceRuntimeState
from collector.services.crawler import lease_next_source, published_today


def test_published_today_accepts_only_current_date():
    assert published_today(timezone.now())
    assert published_today(timezone.now().replace(tzinfo=None))
    assert not published_today(None)
    assert not published_today(timezone.now() - timedelta(days=1))
    assert not published_today(timezone.now() + timedelta(days=1))


@pytest.mark.django_db
def test_expired_lease_is_recovered():
    source = Source.objects.create(name="Due", base_url="https://due.example/", domain="due.example")
    SourceRuntimeState.objects.create(source=source, next_run_at=timezone.now() - timedelta(hours=1), lease_until=timezone.now() - timedelta(minutes=1), lease_owner="dead")
    state = lease_next_source("new-worker")
    assert state.source_id == source.pk
    assert state.lease_owner == "new-worker"
    assert state.lease_until > timezone.now()


@pytest.mark.django_db
def test_daily_pass_purges_rejected_news_too(monkeypatch, tmp_path, settings):
    """The purge existed since July but the worker never called it.

    It lived only in the `maintenance` command, which nothing invokes, so
    rejected news kept its text forever. Owner switched it on 2026-07-26.
    """
    from django.core.management import call_command

    settings.POSINUS_BACKUP_DIR = tmp_path / "backups"
    called = []
    for name in ("evaluate_sources", "process_positive_discovery", "purge_old_content",
                 "purge_rejected_content", "create_backup"):
        monkeypatch.setattr(
            f"collector.management.commands.runworker.{name}",
            lambda *a, _name=name, **kw: called.append(_name),
        )
    monkeypatch.setattr("collector.management.commands.runworker.lease_next_source", lambda owner: None)

    call_command("runworker", "--once")

    assert "purge_rejected_content" in called
    assert called.index("purge_rejected_content") < called.index("create_backup")
