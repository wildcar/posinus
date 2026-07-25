from datetime import datetime, timezone as dt_timezone

import pytest
from django.contrib.auth import get_user_model

from collector.models import EvaluationScore, NewsItem, NewsOccurrence, ReviewEvent


@pytest.fixture
def operator(client, db):
    user = get_user_model().objects.create_user("operator", password="a-secure-test-password")
    client.force_login(user)
    return client


@pytest.fixture
def make_news(db):
    def factory(title, source, day, seed):
        item = NewsItem.objects.create(
            title=title,
            body_text=f"Body {seed}",
            language="ru",
            published_at=datetime(2026, 7, day, 12, 0, tzinfo=dt_timezone.utc),
            content_hash=seed,
            simhash=1,
        )
        NewsOccurrence.objects.create(
            news_item=item,
            source=source,
            url=f"https://{source.domain}/{seed}",
            normalized_url=f"https://{source.domain}/{seed}",
            canonical_url=f"https://{source.domain}/{seed}",
            extraction_method="rss",
        )
        return item

    return factory


@pytest.fixture
def make_review(db):
    def factory(item, scores, key, decision="positive", selector="evaluator"):
        event = ReviewEvent.objects.create(
            news_item=item, decision=decision, selector_name=selector, idempotency_key=key
        )
        for characteristic_key, value in scores.items():
            EvaluationScore.objects.create(review_event=event, characteristic_id=characteristic_key, value=value)
        return event

    return factory
