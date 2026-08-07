from datetime import timedelta

import pytest
from django.utils import timezone

from collector.models import CrawlRun, Source, SourceEndpoint, SourceRuntimeState
from collector.services.crawler import (
    ACTIVE_LEASES_PER_PROBATION,
    PROBATION_PAGE_BUDGET,
    crawl_source,
    lease_next_source,
    published_today,
)
from collector.services.fetch import FetchResult


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
def test_active_source_is_leased_before_an_older_probation_one():
    """Curated sources must never starve behind the discovery experiment.

    In August 2026 the probation backlog held the whole active set hostage for
    four days, and the news flow simply stopped.
    """
    probation = Source.objects.create(name="P", base_url="https://p.example/", domain="p.example", status=Source.Status.PROBATION)
    active = Source.objects.create(name="A", base_url="https://a.example/", domain="a.example")
    SourceRuntimeState.objects.create(source=probation, next_run_at=timezone.now() - timedelta(days=4))
    SourceRuntimeState.objects.create(source=active, next_run_at=timezone.now() - timedelta(hours=1))
    state = lease_next_source("worker")
    assert state.source_id == active.pk


@pytest.mark.django_db
def test_probation_gets_a_lease_after_a_streak_of_active_runs():
    probation = Source.objects.create(name="P", base_url="https://p.example/", domain="p.example", status=Source.Status.PROBATION)
    active = Source.objects.create(name="A", base_url="https://a.example/", domain="a.example")
    SourceRuntimeState.objects.create(source=probation, next_run_at=timezone.now() - timedelta(days=4))
    SourceRuntimeState.objects.create(source=active, next_run_at=timezone.now() - timedelta(hours=1))
    for _ in range(ACTIVE_LEASES_PER_PROBATION):
        CrawlRun.objects.create(source=active, finished_at=timezone.now())
    state = lease_next_source("worker")
    assert state.source_id == probation.pk


def _crawlable_source(monkeypatch, domain, status, page_count):
    """A source whose endpoint lists `page_count` article pages, none dated today."""
    source = Source.objects.create(name=domain, base_url=f"https://{domain}/", domain=domain, status=status)
    SourceEndpoint.objects.create(source=source, kind=SourceEndpoint.Kind.HTML, url=f"https://{domain}/")
    # finish_lease expects the runtime row that lease_next_source guarantees in production
    SourceRuntimeState.objects.create(source=source)
    monkeypatch.setattr(
        "collector.services.crawler.fetch_url",
        lambda url, **kwargs: FetchResult(url=url, status=200, body=b"", headers={}),
    )
    monkeypatch.setattr(
        "collector.services.crawler.candidate_urls",
        lambda endpoint, result: [(f"https://{domain}/{i}", None) for i in range(page_count)],
    )
    monkeypatch.setattr(
        "collector.services.crawler.extract_article",
        lambda source, page, hinted_date=None: {"title": "T", "body": "B", "published_at": None},
    )
    return source


@pytest.mark.django_db
def test_probation_crawl_stops_at_the_page_budget(monkeypatch):
    """The 20-saved-articles cap never fires on a site that saves nothing.

    good.is was walked for five hours and 9809 pages with zero articles saved;
    a probation run samples a site, so it stops after a fixed number of fetches.
    """
    source = _crawlable_source(monkeypatch, "giant.example", Source.Status.PROBATION, PROBATION_PAGE_BUDGET * 3)
    run = crawl_source(source)
    assert run.fetched_count == PROBATION_PAGE_BUDGET
    assert run.details["budget_exhausted"] == "pages"


@pytest.mark.django_db
def test_probation_crawl_stops_at_the_time_budget(monkeypatch):
    source = _crawlable_source(monkeypatch, "slow.example", Source.Status.PROBATION, 10)
    monkeypatch.setattr("collector.services.crawler.PROBATION_TIME_BUDGET", timedelta(0))
    run = crawl_source(source)
    assert run.fetched_count == 0
    assert run.details["budget_exhausted"] == "time"


@pytest.mark.django_db
def test_active_crawl_is_not_budgeted(monkeypatch):
    count = PROBATION_PAGE_BUDGET + 20
    source = _crawlable_source(monkeypatch, "cur.example", Source.Status.ACTIVE, count)
    run = crawl_source(source)
    assert run.fetched_count == count
    assert "budget_exhausted" not in run.details


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
