from django.conf import settings
from django.db import transaction

from collector.models import EvaluationScore, LatestEvaluationScore, NewsItem, ReviewEvent


@transaction.atomic
def mark_selected(item: NewsItem, username: str) -> tuple[ReviewEvent, bool, int]:
    selector_name = f"operator:{username}"[:200]
    event, created = ReviewEvent.objects.get_or_create(
        selector_name=selector_name,
        idempotency_key=f"selected:{item.pk}",
        defaults={
            "news_item": item,
            "decision": ReviewEvent.Decision.POSITIVE,
            "score": 1.0,
            "reason": "Отобрано оператором",
            "selector_version": "operator-ui-v1",
        },
    )
    if not created:
        return event, False, event.evaluation_scores.count()
    latest_scores = LatestEvaluationScore.objects.filter(
        news_id=item.pk,
        selector_name=settings.NEWSCRAWLER_MANUAL_SCORE_SELECTOR,
    )
    scores = [
        EvaluationScore(
            review_event=event,
            characteristic_id=row.characteristic_key,
            value=row.value,
        )
        for row in latest_scores
    ]
    EvaluationScore.objects.bulk_create(scores)
    return event, True, len(scores)
