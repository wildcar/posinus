from datetime import timedelta
import sqlite3

import pytest
from django.test import override_settings
from django.utils import timezone

from collector.models import DiscoveryDomain, NewsTranslation, OperatorEvent, ReviewEvent, Source
from collector.services.ingest import ingest_article
from collector.services.maintenance import (
    create_backup,
    evaluate_sources,
    is_blocked_discovery_domain,
    process_positive_discovery,
    purge_old_content,
    purge_rejected_content,
)


def test_discovery_blocklist_matches_domains_and_subdomains():
    for blocked in ["vk.com", "invite.viber.com", "t.me", "apps.apple.com",
                    "www.rustore.ru", "youtu.be", "sub.ok.ru"]:
        assert is_blocked_discovery_domain(blocked), blocked
    for allowed in ["apple.com", "goodnewsnetwork.org", "ria.ru", "bbc.com", ""]:
        assert not is_blocked_discovery_domain(allowed), allowed


@pytest.mark.django_db
def test_positive_discovery_skips_blocked_domains():
    source = Source.objects.create(name="Src", base_url="https://src.example/", domain="src.example")
    item, _, _ = ingest_article(
        source=source, url="https://src.example/a", title="Great story",
        body=("Body text here. " * 30), links=["https://vk.com/share", "https://t.me/x"],
    )
    ReviewEvent.objects.create(news_item=item, decision="positive", selector_name="news-evaluator", idempotency_key="k1")
    process_positive_discovery()
    assert not DiscoveryDomain.objects.filter(domain__in=["vk.com", "t.me"]).exists()
    assert not Source.objects.filter(domain__in=["vk.com", "t.me"]).exists()


@pytest.mark.django_db
def test_active_source_is_paused_after_fifty_low_yield_reviews():
    source = Source.objects.create(name="Weak", base_url="https://weak.example/", domain="weak.example")
    for number in range(50):
        item, _, _ = ingest_article(source=source, url=f"https://weak.example/{number}", title=f"Ordinary item {number}", body=(f"Content number {number}. " * 30), language="en")
        ReviewEvent.objects.create(news_item=item, decision="not_positive", selector_name="selector", idempotency_key=str(number))
    evaluate_sources()
    source.refresh_from_db()
    assert source.status == Source.Status.PAUSED_LOW_YIELD
    assert OperatorEvent.objects.filter(event_type="source_status", source=source).exists()


@pytest.mark.django_db
def test_skipped_reviews_do_not_pause_source():
    source = Source.objects.create(name="Unknown", base_url="https://unknown.example/", domain="unknown.example")
    for number in range(50):
        item, _, _ = ingest_article(source=source, url=f"https://unknown.example/{number}", title=f"Item {number}", body=(f"Unique body {number}. " * 30), language="en")
        ReviewEvent.objects.create(news_item=item, decision="skipped", selector_name="selector", idempotency_key=str(number))
    evaluate_sources()
    source.refresh_from_db()
    assert source.status == Source.Status.ACTIVE


@pytest.mark.django_db
def test_retention_keeps_tombstone():
    source = Source.objects.create(name="Old", base_url="https://old.example/", domain="old.example")
    item, _, _ = ingest_article(source=source, url="https://old.example/1", title="Old news", body="Old news body. " * 30)
    type(item).objects.filter(pk=item.pk).update(first_seen_at=timezone.now() - timedelta(days=91))
    NewsTranslation.objects.create(
        news_item=item,
        title="Старый перевод",
        body_text="Старый полный текст",
        summary="Старый пересказ",
    )
    assert purge_old_content() == 1
    item.refresh_from_db()
    assert item.body_text == ""
    assert item.purged_at is not None
    assert item.occurrences.exists()
    assert not NewsTranslation.objects.filter(news_item=item).exists()


@pytest.mark.django_db
def test_rejected_news_purged_after_three_days():
    source = Source.objects.create(name="Rej", base_url="https://rej.example/", domain="rej.example")

    def add(url, days_old, decisions):
        item, _, _ = ingest_article(source=source, url=url, title=f"News {url}", body=(f"Body about {url} number. " * 30))
        type(item).objects.filter(pk=item.pk).update(first_seen_at=timezone.now() - timedelta(days=days_old))
        for i, decision in enumerate(decisions):
            ReviewEvent.objects.create(news_item=item, decision=decision, selector_name="news-evaluator", idempotency_key=f"{url}:{i}")
        return item

    rejected = add("https://rej.example/old-rejected", 4, ["not_positive"])
    selected = add("https://rej.example/old-selected", 4, ["not_positive", "positive"])
    skipped = add("https://rej.example/old-skipped", 4, ["skipped"])
    fresh_rejected = add("https://rej.example/fresh-rejected", 1, ["not_positive"])
    unreviewed = add("https://rej.example/old-unreviewed", 4, [])

    assert purge_rejected_content() == 1  # only the old item with a not_positive and no positive

    for item, purged in [(rejected, True), (selected, False), (skipped, False), (fresh_rejected, False), (unreviewed, False)]:
        item.refresh_from_db()
        assert (item.purged_at is not None) is purged, item.occurrences.first().url
    assert OperatorEvent.objects.filter(event_type="retention_rejected").exists()


@pytest.mark.django_db
def test_backup_is_valid(tmp_path, settings):
    settings.NEWSCRAWLER_BACKUP_DIR = tmp_path
    settings.DB_PATH = tmp_path / "source.sqlite3"
    source = sqlite3.connect(settings.DB_PATH)
    source.execute("CREATE TABLE sample(value TEXT)")
    source.execute("INSERT INTO sample VALUES ('ok')")
    source.commit()
    source.close()
    backup = create_backup()
    assert backup.exists()
    assert backup.stat().st_size > 0
