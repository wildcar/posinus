import logging
import socket
import uuid
from datetime import timedelta
from urllib.parse import urlsplit
from xml.etree import ElementTree

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from collector.models import CrawlRun, NewsOccurrence, Source, SourceEndpoint, SourceRuntimeState
from .db import retry_sqlite
from .fetch import candidate_urls, discover_endpoints, extract_article, fetch_url, url_matches
from .ingest import ingest_article

logger = logging.getLogger(__name__)

# A probation crawl exists to sample whether a site publishes today-dated news,
# not to mirror it. The 20-saved-articles cap never triggers on a site that
# saves nothing, so a giant discovered domain used to be walked whole — five
# hours and ten thousand pages for zero articles. Budget the sample instead.
PROBATION_PAGE_BUDGET = 40
PROBATION_TIME_BUDGET = timedelta(minutes=10)

# An active source is the product, so its budget is far wider than a sample —
# but it is a budget. The 200-saved-articles cap has the same blind spot as the
# probation one: a site that saves nothing never reaches it, and the run ends
# only when the whole site has been walked. On 2026-08-16 four such runs held
# the worker for 12 hours between them (АСИ 236 min / 9195 pages / 0 saved,
# Goednieuwssite 199 / 5439 / 0, My Modern Met 184 / 8239 / 0) while all 56
# active sources sat 26–45 hours overdue and the news flow stopped.
#
# The trade is measured, not guessed. Over the three weeks to 2026-08-16 active
# runs of 20 minutes or less returned 968 articles for 6.3 worker-hours, while
# runs over an hour returned 390 for 166 hours — 154 articles per hour against
# 2.3. Cutting the long tail costs a little yield per run and buys back the two
# days a starved source now waits for its turn.
#
# The time budget stays at the lease length on purpose: a run that outlives its
# own lease lets the next loop lease the same source again, which is how source
# 18 came to hold two live runs at once on 2026-08-16.
ACTIVE_PAGE_BUDGET = 1500
ACTIVE_TIME_BUDGET = timedelta(minutes=20)

# Curated active sources are the product; probation sources are an experiment.
# Without a priority the experiment starved the product for four days (2026-08),
# and with a strict priority the product would starve the experiment forever.
# Probation gets every fifth lease while actives are also due.
ACTIVE_LEASES_PER_PROBATION = 4


def ensure_runtime(source):
    state, _ = SourceRuntimeState.objects.get_or_create(source=source)
    return state


def published_today(value) -> bool:
    if value is None:
        return False
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return timezone.localdate(value) == timezone.localdate()


def _last_runs_all_active():
    recent = list(CrawlRun.objects.order_by("-id")
                  .values_list("source__status", flat=True)[:ACTIVE_LEASES_PER_PROBATION])
    return (len(recent) == ACTIVE_LEASES_PER_PROBATION
            and all(status == Source.Status.ACTIVE for status in recent))


@retry_sqlite()
@transaction.atomic
def lease_next_source(owner=None, lease_minutes=20):
    now = timezone.now()
    owner = owner or f"{socket.gethostname()}:{uuid.uuid4()}"

    def oldest_due(*statuses):
        return (SourceRuntimeState.objects.select_related("source")
                .filter(next_run_at__lte=now)
                .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
                .filter(source__status__in=statuses)
                .order_by("next_run_at").first())

    candidate = oldest_due(Source.Status.PROBATION) if _last_runs_all_active() else None
    if candidate is None:
        candidate = oldest_due(Source.Status.ACTIVE) or oldest_due(Source.Status.PROBATION)
    if not candidate:
        return None
    updated = SourceRuntimeState.objects.filter(pk=candidate.pk).filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now)).update(
        lease_owner=owner, lease_until=now + timedelta(minutes=lease_minutes), last_started_at=now,
    )
    if not updated:
        return None
    candidate.refresh_from_db()
    return candidate


def _probation_limit_reached(source):
    if source.status != Source.Status.PROBATION:
        return False
    count = NewsOccurrence.objects.filter(source=source, fetched_at__gte=source.probation_started_at or source.created_at).count()
    if count < 20:
        return False
    source.status = Source.Status.PROBATION_WAITING
    source.save(update_fields=["status", "updated_at"])
    return True


def _initial_endpoint(source):
    result = fetch_url(source.base_url, playwright=source.use_playwright)
    discovered = discover_endpoints(source, result.body, result.url)
    endpoint = SourceEndpoint.objects.filter(source=source, enabled=True).order_by("priority").first()
    if not endpoint:
        endpoint = SourceEndpoint.objects.create(source=source, kind=SourceEndpoint.Kind.HTML, url=source.base_url)
    return endpoint


def crawl_source(source: Source):
    run = CrawlRun.objects.create(source=source)
    errors = []
    budget_exhausted = ""
    try:
        if _probation_limit_reached(source):
            run.status = CrawlRun.Status.SUCCESS
            return run
        endpoints = list(source.endpoints.filter(enabled=True).order_by("priority"))
        if not endpoints:
            endpoints = [_initial_endpoint(source)]
        seen = set()
        probation = source.status == Source.Status.PROBATION
        article_limit = 20 if probation else 200
        page_budget = PROBATION_PAGE_BUDGET if probation else ACTIVE_PAGE_BUDGET
        deadline = timezone.now() + (PROBATION_TIME_BUDGET if probation else ACTIVE_TIME_BUDGET)
        for endpoint in endpoints:
            if timezone.now() >= deadline:
                budget_exhausted = budget_exhausted or "time"
            if budget_exhausted:
                break
            try:
                result = fetch_url(endpoint.url, etag=endpoint.etag, last_modified=endpoint.last_modified, delay=source.download_delay_seconds)
                endpoint.etag = result.headers.get("ETag", endpoint.etag)
                endpoint.last_modified = result.headers.get("Last-Modified", endpoint.last_modified)
                endpoint.save(update_fields=["etag", "last_modified"])
                candidates = candidate_urls(endpoint, result)
                if endpoint.kind == SourceEndpoint.Kind.SITEMAP:
                    try:
                        body = result.body
                        import gzip
                        if body[:2] == b"\x1f\x8b":
                            body = gzip.decompress(body)
                        root = ElementTree.fromstring(body)
                        if root.tag.endswith("sitemapindex"):
                            nested = []
                            for sitemap_url, _ in candidates[:20]:
                                nested_result = fetch_url(sitemap_url, delay=source.download_delay_seconds)
                                nested.extend(candidate_urls(endpoint, nested_result))
                            candidates = nested
                    except Exception as exc:
                        errors.append({"url": endpoint.url, "reason": f"nested sitemap: {exc}"})
                for url, hinted_date in candidates:
                    # Checked before the skip conditions below, so a candidate
                    # list that is walked without a single fetch still ends on
                    # time — source 18 spent 19 minutes and 0 pages that way.
                    if run.fetched_count >= page_budget:
                        budget_exhausted = "pages"
                        break
                    if timezone.now() >= deadline:
                        budget_exhausted = "time"
                        break
                    if run.saved_count >= article_limit or not url or url in seen or not url_matches(source, url):
                        continue
                    seen.add(url)
                    host = (urlsplit(url).hostname or "").lower()
                    if host != source.domain and not host.endswith("." + source.domain):
                        continue
                    if hinted_date and not published_today(hinted_date):
                        continue
                    try:
                        page = fetch_url(url, playwright=source.use_playwright, delay=source.download_delay_seconds)
                        run.fetched_count += 1
                        article = extract_article(source, page, hinted_date)
                        if not published_today(article["published_at"]):
                            run.rejected_count += 1
                            errors.append({"url": url, "reason": "not published on the current date"})
                            continue
                        if len(article["title"].strip()) < 5 or len(article["body"].strip()) < 200:
                            run.rejected_count += 1
                            errors.append({"url": url, "reason": "missing title or body shorter than 200 characters"})
                            continue
                        _, _, created = ingest_article(source=source, **article, extraction_method="playwright" if source.use_playwright else "trafilatura", http_status=page.status)
                        run.saved_count += int(created)
                    except Exception as exc:
                        run.error_count += 1
                        errors.append({"url": url, "reason": str(exc)[:500]})
            except Exception as exc:
                run.error_count += 1
                errors.append({"url": endpoint.url, "reason": str(exc)[:500]})
        run.status = CrawlRun.Status.PARTIAL if run.error_count else CrawlRun.Status.SUCCESS
        if run.error_count and not run.fetched_count:
            run.status = CrawlRun.Status.FAILED
    except Exception as exc:
        logger.exception("Source crawl failed", extra={"source_id": source.pk})
        run.status = CrawlRun.Status.FAILED
        run.error_count += 1
        errors.append({"reason": str(exc)[:500]})
    finally:
        run.details = {"errors": errors[:100]}
        if budget_exhausted:
            run.details["budget_exhausted"] = budget_exhausted
        run.finished_at = timezone.now()
        # Releasing the lease matters more than recording the run. When the run
        # row failed to save — the pipeline holds the crawler database open, and
        # a write can lose the race — the source kept its old `next_run_at` and
        # became the permanent head of the oldest-due queue: source 18 was
        # leased again 23 minutes after an abandoned run on 2026-08-16, while
        # 56 actives waited two days behind it. Save with the retry the rest of
        # the queue writes already use, and free the lease either way.
        try:
            _save_run(run)
        finally:
            finish_lease(source, run)
    return run


@retry_sqlite()
def _save_run(run):
    run.save()


@retry_sqlite()
@transaction.atomic
def finish_lease(source, run):
    state = SourceRuntimeState.objects.select_for_update().get(source=source)
    state.lease_until = None
    state.lease_owner = ""
    state.last_finished_at = timezone.now()
    state.next_run_at = timezone.now() + timedelta(minutes=source.interval_minutes)
    if run.status == CrawlRun.Status.FAILED:
        state.consecutive_failures += 1
        state.last_error = (run.details.get("errors") or [{}])[-1].get("reason", "Unknown error")
        delay = min(24 * 60, source.interval_minutes * 2 ** min(state.consecutive_failures, 5))
        state.next_run_at = timezone.now() + timedelta(minutes=delay)
    else:
        state.consecutive_failures = 0
        state.last_error = ""
    state.save()
