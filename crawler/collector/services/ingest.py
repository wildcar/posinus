from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from django.db import transaction
from django.utils import timezone

from collector.models import NewsItem, NewsOccurrence, OutboundLink, Source
from .db import retry_sqlite
from .text import content_hash, hamming_distance, normalize_url, simhash64, title_similarity


def _near_duplicate(title, language, published_at, fingerprint):
    since = (published_at or timezone.now()) - timedelta(hours=48)
    until = (published_at or timezone.now()) + timedelta(hours=48)
    candidates = NewsItem.objects.filter(language=language, first_seen_at__range=(since, until), purged_at__isnull=True).only("id", "title", "simhash")[:500]
    for candidate in candidates:
        if hamming_distance(candidate.simhash, fingerprint) <= 3 and title_similarity(candidate.title, title) >= 0.85:
            return candidate
    return None


@retry_sqlite()
@transaction.atomic
def ingest_article(*, source: Source, url: str, title: str, body: str, language: str = "", author: str = "", published_at=None, canonical_url: str = "", metadata=None, links=None, extraction_method="trafilatura", http_status=200):
    normalized_url = normalize_url(canonical_url or url)
    existing_occurrence = NewsOccurrence.objects.filter(normalized_url=normalized_url).select_related("news_item").first()
    if existing_occurrence:
        return existing_occurrence.news_item, existing_occurrence, False
    digest = content_hash(body)
    fingerprint = simhash64(body)
    item = NewsItem.objects.filter(content_hash=digest).first()
    if item is None:
        item = _near_duplicate(title, language, published_at, fingerprint)
    if item is None:
        item = NewsItem.objects.create(
            title=title.strip(), body_text=body.strip(), language=(language or "")[:16], author=author.strip(),
            published_at=published_at, content_hash=digest, simhash=fingerprint, metadata=metadata or {},
        )
    occurrence = NewsOccurrence.objects.create(
        news_item=item, source=source, url=url, normalized_url=normalized_url,
        canonical_url=canonical_url or url, published_at=published_at,
        extraction_method=extraction_method, http_status=http_status,
    )
    source_host = urlsplit(source.base_url).hostname or ""
    outbound = []
    for raw_link in links or []:
        absolute = urljoin(url, raw_link)
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            continue
        try:
            clean = normalize_url(absolute)
        except ValueError:
            continue
        outbound.append(OutboundLink(occurrence=occurrence, url=clean, domain=parts.hostname.lower(), is_external=parts.hostname.lower() != source_host.lower()))
    OutboundLink.objects.bulk_create(outbound, ignore_conflicts=True)
    return item, occurrence, True

