"""The numbers and the problems the dashboard shows.

Two blocks live here. «Сейчас» is today's counters with a «обычно столько-то»
next to each: a bare number means nothing to an operator without a month of
experience, and «в очереди 4» and «в очереди 340» are different news.
«Требует внимания» is a list of things to do — if it is empty, the dashboard
says so in one line instead of showing a wall of zeros.

The word «ошибка» is split by meaning on purpose. A crawl error is «пропустили»,
a preparation error is «повторим сами», a publication error is «нужна ваша
помощь» — and only the last one, plus a preparation that keeps failing, is worth
the operator's attention.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from collector.models import LatestReview, NewsItem, Source
from collector.services.broadcast import PLATFORM_TITLES
from collector.services.pipeline_db import PipelineUnavailable, fetch_all

# A platform that failed this many times in a row needs a human, not a retry —
# but only if it failed recently. An old row with many attempts is a dead tail
# (prod has one from before the VK token was fixed), and a block that cries
# about a settled problem trains the operator to ignore it.
PLATFORM_ALERT_ATTEMPTS = 3
PLATFORM_ALERT_WINDOW_HOURS = 24
# A preparation that failed this many times is looping, not unlucky.
PREPARATION_ALERT_ATTEMPTS = 2
TYPICAL_WINDOW_DAYS = 14


@dataclass
class Counters:
    """One number plus what «обычно» looks like for it."""

    title: str
    value: int
    typical: str = ""
    note: str = ""
    url: str = ""


@dataclass
class Attention:
    """Something the operator has to decide about, with the action attached."""

    text: str
    hint: str = ""
    url: str = ""
    action_title: str = ""


def _typical(values: list[int]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    return f"обычно {low}" if low == high else f"обычно {low}–{high}"


def today_counters() -> list[Counters]:
    """Collected, evaluated and selected today, each against the last two weeks."""
    now = timezone.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = start - timedelta(days=TYPICAL_WINDOW_DAYS)

    collected_today = NewsItem.objects.filter(first_seen_at__gte=start).count()
    per_day = Counter(
        item.date()
        for item in NewsItem.objects.filter(first_seen_at__gte=since, first_seen_at__lt=start)
        .values_list("first_seen_at", flat=True)
    )
    unreviewed = NewsItem.objects.filter(purged_at__isnull=True, review_events__isnull=True).count()

    selector = settings.POSINUS_MANUAL_SCORE_SELECTOR
    selected_today = LatestReview.objects.filter(
        selector_name=selector, decision="positive", created_at__gte=start
    ).count()
    selected_per_day = Counter(
        moment.date()
        for moment in LatestReview.objects.filter(
            selector_name=selector, decision="positive", created_at__gte=since, created_at__lt=start
        ).values_list("created_at", flat=True)
    )

    return [
        Counters("Собрано сегодня", collected_today, _typical(list(per_day.values()))),
        Counters(
            "Ждут оценки", unreviewed,
            note="очередь оценщика", url="/news/?decision=unreviewed",
        ),
        Counters(
            "Отобрано сегодня", selected_today, _typical(list(selected_per_day.values())),
            url="/news/?decision=positive",
        ),
    ]


def pipeline_counters() -> list[Counters]:
    """Prepared, queued and published today — from the pipeline's own database."""
    rows = fetch_all("SELECT status, COUNT(*) AS count FROM prepared_item GROUP BY status")
    counts = {row["status"]: row["count"] for row in rows}
    published_today = fetch_all(
        "SELECT COUNT(*) AS count FROM prepared_item WHERE status = 'published' AND published_at >= ?",
        (timezone.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),),
    )
    return [
        Counters("В очереди на выход", counts.get("prepared", 0), url="/broadcast/"),
        Counters("Вышло сегодня", published_today[0]["count"] if published_today else 0, url="/broadcast/?tab=published"),
        Counters("Не удалось подготовить", counts.get("error", 0), note="повторим сами", url="/broadcast/"),
    ]


def attention() -> list[Attention]:
    """Everything that needs a decision, in the order it hurts.

    A platform that keeps refusing needs a human; a preparation that loops needs
    a look; a source on pause needs a verdict. Nothing else belongs here.
    """
    problems: list[Attention] = []

    try:
        fresh = (timezone.now() - timedelta(hours=PLATFORM_ALERT_WINDOW_HOURS)).isoformat()
        for row in fetch_all(
            "SELECT platform, COUNT(*) AS items, MAX(attempts) AS attempts, MAX(error) AS error "
            "FROM publication WHERE status = 'error' AND updated_at >= ? GROUP BY platform",
            (fresh,),
        ):
            if (row["attempts"] or 0) < PLATFORM_ALERT_ATTEMPTS:
                continue
            title = PLATFORM_TITLES.get(row["platform"], row["platform"])
            problems.append(
                Attention(
                    text=f"{title} не принимает посты: неудачных отправок {row['items']}, попыток до {row['attempts']}.",
                    hint=row["error"] or "",
                    url="/broadcast/?tab=platforms",
                    action_title="Посмотреть площадку",
                )
            )
        for row in fetch_all(
            "SELECT news_id, retold_title, error FROM prepared_item WHERE status = 'error' LIMIT 10"
        ):
            problems.append(
                Attention(
                    text=f"Не получается подготовить «{row['retold_title'] or row['news_id']}».",
                    hint=row["error"] or "",
                    url=f"/news/{row['news_id']}/",
                    action_title="Открыть новость",
                )
            )
    except PipelineUnavailable:
        problems.append(
            Attention(text="Нет связи с базой конвейера: подготовку и публикацию сейчас не видно.")
        )

    paused = Source.objects.filter(status=Source.Status.PAUSED_LOW_YIELD).annotate(
        news_count=Count("occurrences", distinct=True)
    )
    for source in paused[:5]:
        problems.append(
            Attention(
                text=f"Источник «{source.name}» на паузе: слишком мало позитивных новостей.",
                url=f"/sources/{source.pk}/",
                action_title="Открыть источник",
            )
        )
    return problems


def feed_mix(days: int = 30) -> dict:
    """What the channel has actually been made of lately.

    Two halves, and both are needed. Rubrics catch monotony: the twenty axes
    happily give a ninth rescued dog the same nine they gave the first, and the
    subscriber count notices a week later than this block does. Source shares
    catch the other failure — if one agency gives seventy percent of the posts,
    this is a relay, not a feed.
    """
    since = timezone.now() - timedelta(days=days)
    try:
        rows = fetch_all(
            "SELECT news_id FROM prepared_item WHERE status = 'published' AND published_at >= ?",
            (since.isoformat(),),
        )
    except PipelineUnavailable:
        return {}
    news_ids = [row["news_id"] for row in rows]
    if not news_ids:
        return {"total": 0, "per_day": 0.0, "sources": []}

    counts = Counter()
    topics = Counter()
    published = NewsItem.objects.filter(pk__in=news_ids).select_related(
        "topic_row__topic"
    ).prefetch_related("occurrences__source")
    for item in published:
        names = sorted({occ.source.name for occ in item.occurrences.all()})
        counts[names[0] if names else "неизвестно"] += 1
        row = getattr(item, "topic_row", None)
        topics[(row.topic_id, row.topic.title) if row else ("", "Не определена")] += 1

    total = len(news_ids)
    shares = [
        {"name": name, "count": count, "share": round(100 * count / total)}
        for name, count in counts.most_common(8)
    ]
    topic_shares = [
        {"key": key, "name": title, "count": count, "share": round(100 * count / total)}
        for (key, title), count in topics.most_common(10)
    ]
    return {
        "total": total,
        "per_day": round(total / days, 1),
        "sources": shares,
        "topics": topic_shares,
        "days": days,
    }
