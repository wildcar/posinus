import sqlite3
from datetime import timedelta

import pytest
from django.utils import timezone

from collector.models import CrawlRun, Source, SourceEndpoint, SourceRuntimeState
from collector.services.crawler import (
    ACTIVE_LEASES_PER_PROBATION,
    ACTIVE_PAGE_BUDGET,
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
def test_active_crawl_keeps_the_wider_budget_of_the_product(monkeypatch):
    """An active source is worth far more pages than a probation sample."""
    count = PROBATION_PAGE_BUDGET + 20
    source = _crawlable_source(monkeypatch, "cur.example", Source.Status.ACTIVE, count)
    run = crawl_source(source)
    assert run.fetched_count == count
    assert "budget_exhausted" not in run.details


@pytest.mark.django_db
def test_active_crawl_stops_at_the_page_budget(monkeypatch):
    """The 200-saved-articles cap has the probation cap's blind spot: a site
    that saves nothing never reaches it, so the run ends only once the whole
    site has been walked. Four such runs held the worker for 12 hours on
    2026-08-16 while all 56 active sources sat up to 45 hours overdue."""
    source = _crawlable_source(monkeypatch, "huge.example", Source.Status.ACTIVE, ACTIVE_PAGE_BUDGET * 2)
    run = crawl_source(source)
    assert run.fetched_count == ACTIVE_PAGE_BUDGET
    assert run.details["budget_exhausted"] == "pages"


@pytest.mark.django_db
def test_active_crawl_stops_at_the_time_budget(monkeypatch):
    source = _crawlable_source(monkeypatch, "slowactive.example", Source.Status.ACTIVE, 10)
    monkeypatch.setattr("collector.services.crawler.ACTIVE_TIME_BUDGET", timedelta(0))
    run = crawl_source(source)
    assert run.fetched_count == 0
    assert run.details["budget_exhausted"] == "time"


@pytest.mark.django_db
def test_budget_ends_a_run_that_fetches_nothing(monkeypatch):
    """Source 18 spent 19 minutes on candidates it skipped without one fetch,
    so the budget is read before the skip conditions, not after them."""
    source = _crawlable_source(monkeypatch, "skipall.example", Source.Status.ACTIVE, 10)
    monkeypatch.setattr("collector.services.crawler.url_matches", lambda source, url: False)
    monkeypatch.setattr("collector.services.crawler.ACTIVE_TIME_BUDGET", timedelta(0))
    run = crawl_source(source)
    assert run.details["budget_exhausted"] == "time"


@pytest.mark.django_db
def test_lease_is_released_even_when_the_run_row_cannot_be_saved(monkeypatch):
    """A run whose row fails to save must still free its source.

    On 2026-08-16 source 18 kept a `next_run_at` from two days earlier after an
    abandoned run, which made it the permanent head of the oldest-due queue: it
    was leased again 23 minutes later while 56 actives waited behind it.
    """
    source = _crawlable_source(monkeypatch, "lockme.example", Source.Status.ACTIVE, 1)
    monkeypatch.setattr(
        "collector.services.crawler._save_run",
        lambda run: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        crawl_source(source)
    state = SourceRuntimeState.objects.get(source=source)
    assert state.lease_until is None
    assert state.next_run_at > timezone.now()


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
