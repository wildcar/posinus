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


def ensure_runtime(source):
    state, _ = SourceRuntimeState.objects.get_or_create(source=source)
    return state


def published_today(value) -> bool:
    if value is None:
        return False
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return timezone.localdate(value) == timezone.localdate()


@retry_sqlite()
@transaction.atomic
def lease_next_source(owner=None, lease_minutes=20):
    now = timezone.now()
    owner = owner or f"{socket.gethostname()}:{uuid.uuid4()}"
    candidate = (SourceRuntimeState.objects.select_related("source")
                 .filter(next_run_at__lte=now)
                 .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
                 .filter(source__status__in=[Source.Status.ACTIVE, Source.Status.PROBATION])
                 .order_by("next_run_at").first())
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
    try:
        if _probation_limit_reached(source):
            run.status = CrawlRun.Status.SUCCESS
            return run
        endpoints = list(source.endpoints.filter(enabled=True).order_by("priority"))
        if not endpoints:
            endpoints = [_initial_endpoint(source)]
        seen = set()
        article_limit = 20 if source.status == Source.Status.PROBATION else 200
        for endpoint in endpoints:
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
        run.finished_at = timezone.now()
        run.save()
        finish_lease(source, run)
    return run


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
