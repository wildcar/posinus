"""«Эфир»: the queue, what went out, and how each platform is doing.

Everything here is read from the pipeline's database (never written) and joined
in Python with what the crawler knows — the headline, the source, the scores. Two
databases, two queries each, no cross-database SQL: `ATTACH` would need a writable
path to a file this process must not create, and the join is a dictionary lookup
over a few dozen rows anyway.

The three answers this module exists to give: what goes out next and when, what
went out and where to read it, and which platform is broken right now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from django.conf import settings

from collector.models import LatestEvaluationScore, NewsItem, PublicationPlan
from collector.services.pipeline_db import PipelineUnavailable, fetch_all

PLATFORM_TITLES = {"telegram": "Telegram", "site": "wildcar.ru", "vk": "ВКонтакте"}

# Weights behind «сила» — the order the queue would take if nobody touched it.
# Preparation time, which is what the publisher uses today, is the worst
# available signal: it says when the machine got round to the item, not how good
# the item is.
STRENGTH_WEIGHTS = {"positivity": 0.3, "interestingness": 0.2}
STRENGTH_HIGHLIGHT_WEIGHT = 0.5
HIGHLIGHT_AXES = (
    "pride_humanity", "pride_russia", "inspiration", "beauty",
    "interestingness", "surprise", "uniqueness",
)

QUEUE_SQL = """
SELECT p.news_id, p.retold_title, p.prepared_at, p.error,
       (SELECT COUNT(*) FROM illustration i WHERE i.news_id = p.news_id) AS images
FROM prepared_item p
WHERE p.status = 'prepared'
ORDER BY p.prepared_at ASC, p.news_id ASC
"""

FAILED_SQL = """
SELECT news_id, retold_title, error, prepared_at
FROM prepared_item
WHERE status = 'error'
ORDER BY prepared_at DESC
LIMIT 50
"""

PUBLISHED_SQL = """
SELECT p.news_id, p.retold_title, p.published_at,
       (SELECT COUNT(*) FROM illustration i WHERE i.news_id = p.news_id) AS images
FROM prepared_item p
WHERE p.status = 'published'
ORDER BY p.published_at DESC, p.news_id DESC
LIMIT ?
"""

PUBLICATIONS_SQL = """
SELECT news_id, platform, status, url, error, attempts, updated_at
FROM publication
"""

LAST_PUBLISHER_RUN_SQL = """
SELECT config, started_at
FROM service_run
WHERE service = 'publisher'
ORDER BY id DESC
LIMIT 1
"""


@dataclass
class QueueItem:
    news_id: int
    title: str
    prepared_at: datetime | None
    age_days: int | None
    strength: float
    images: int
    expected_at: datetime | None = None
    rank: int = 0
    hold_until: datetime | None = None
    note: str = ""

    @property
    def moved(self) -> bool:
        return self.rank != 0 or self.hold_until is not None


@dataclass
class PublishedItem:
    news_id: int
    title: str
    published_at: datetime | None
    images: int
    platforms: list[dict] = field(default_factory=list)


@dataclass
class PlatformCard:
    platform: str
    title: str
    ok_count: int = 0
    error_count: int = 0
    last_ok_at: datetime | None = None
    last_url: str = ""
    last_error: str = ""
    attempts_spent: int = 0
    given_up: int = 0


def _moment(raw) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def strength(scores: dict[str, int]) -> float:
    """One number for «how strong is this item», 0 to 10.

    Half of it is the best strong side, the rest is positivity and how
    interesting the item is. Deliberately crude: it only has to order a queue of
    dozens better than the timestamp does.
    """
    if not scores:
        return 0.0
    best_highlight = max((scores.get(axis, 0) for axis in HIGHLIGHT_AXES), default=0)
    value = STRENGTH_HIGHLIGHT_WEIGHT * best_highlight
    value += sum(weight * scores.get(axis, 0) for axis, weight in STRENGTH_WEIGHTS.items())
    return round(value, 1)


def _scores_for(news_ids: list[int]) -> dict[int, dict[str, int]]:
    if not news_ids:
        return {}
    rows = LatestEvaluationScore.objects.filter(
        selector_name=settings.POSINUS_MANUAL_SCORE_SELECTOR, news_id__in=news_ids
    ).values_list("news_id", "characteristic_key", "value")
    scores: dict[int, dict[str, int]] = {}
    for news_id, key, value in rows:
        scores.setdefault(news_id, {})[key] = value
    return scores


def _news_for(news_ids: list[int]) -> dict[int, NewsItem]:
    if not news_ids:
        return {}
    items = NewsItem.objects.filter(pk__in=news_ids).prefetch_related("occurrences__source")
    return {item.pk: item for item in items}


def publisher_settings() -> dict:
    """The publisher's own effective settings, as it recorded them on its last run.

    Read from `service_run` rather than kept in a second config: a copy of the
    interval on this side would be wrong the first time the owner edits the env
    file, and this screen puts times on the screen.
    """
    import json

    rows = fetch_all(LAST_PUBLISHER_RUN_SQL)
    if not rows:
        return {}
    try:
        return json.loads(rows[0]["config"] or "{}")
    except ValueError:
        return {}


def _next_slots(count: int, last_ok: datetime | None, config: dict) -> list[datetime]:
    """When the next `count` items would go out, at one per interval inside the window."""
    interval = timedelta(minutes=int(config.get("min_interval_minutes") or 120))
    window = str(config.get("window") or "")
    start_hour, end_hour, zone = _parse_window(window)
    now = datetime.now(timezone.utc)
    moment = max(now, (last_ok + interval) if last_ok else now)
    slots = []
    for _ in range(count):
        moment = _inside_window(moment, start_hour, end_hour, zone)
        slots.append(moment)
        moment = moment + interval
    return slots


def _parse_window(window: str):
    """«08:00-22:00 Europe/Moscow» → (start, end, zone name); empty means no window."""
    try:
        bounds, zone = window.split(" ", 1)
        start, end = bounds.split("-")
        start_h, start_m = (int(part) for part in start.split(":"))
        end_h, end_m = (int(part) for part in end.split(":"))
        return (start_h, start_m), (end_h, end_m), zone.strip()
    except (ValueError, AttributeError):
        return None, None, None


def _inside_window(moment: datetime, start, end, zone_name):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not start or not end or not zone_name:
        return moment
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return moment
    local = moment.astimezone(zone)
    opens = local.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
    closes = local.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
    if local < opens:
        return opens.astimezone(timezone.utc)
    if local >= closes:
        return (opens + timedelta(days=1)).astimezone(timezone.utc)
    return moment


def queue() -> tuple[list[QueueItem], dict]:
    """Prepared items in the order they will actually go out, with expected times."""
    rows = fetch_all(QUEUE_SQL)
    news_ids = [row["news_id"] for row in rows]
    scores = _scores_for(news_ids)
    news = _news_for(news_ids)
    now = datetime.now(timezone.utc)

    plans = {plan.news_item_id: plan for plan in PublicationPlan.objects.filter(news_item_id__in=news_ids)}

    items = []
    for row in rows:
        item = news.get(row["news_id"])
        plan = plans.get(row["news_id"])
        if plan is not None and plan.dropped_at:
            continue
        published = item.published_at or item.first_seen_at if item else None
        items.append(
            QueueItem(
                news_id=row["news_id"],
                title=row["retold_title"] or (item.title if item else f"Новость {row['news_id']}"),
                prepared_at=_moment(row["prepared_at"]),
                age_days=(now - published).days if published else None,
                strength=strength(scores.get(row["news_id"], {})),
                images=row["images"],
                rank=plan.rank if plan else 0,
                hold_until=plan.hold_until if plan else None,
                note=plan.note if plan else "",
            )
        )

    # The same order the publisher will take: the operator's hand first, then
    # strength, then preparation time as a stable tie-break. One rule, two
    # readers — the view `exchange_publication_order` is what the publisher reads.
    now_local = now
    items = [item for item in items if not (item.hold_until and item.hold_until > now_local)]
    items.sort(key=lambda item: (item.rank, -item.strength, item.prepared_at or now_local))

    config = publisher_settings()
    last_ok = None
    publications = fetch_all(PUBLICATIONS_SQL)
    for row in publications:
        if row["status"] == "ok":
            moment = _moment(row["updated_at"])
            if moment and (last_ok is None or moment > last_ok):
                last_ok = moment
    for item, slot in zip(items, _next_slots(len(items), last_ok, config)):
        item.expected_at = slot
    return items, config


def published(limit: int = 60) -> list[PublishedItem]:
    """What went out, newest first, with a live link per platform."""
    rows = fetch_all(PUBLISHED_SQL, (limit,))
    by_news: dict[int, PublishedItem] = {}
    for row in rows:
        by_news[row["news_id"]] = PublishedItem(
            news_id=row["news_id"],
            title=row["retold_title"] or f"Новость {row['news_id']}",
            published_at=_moment(row["published_at"]),
            images=row["images"],
        )
    for row in fetch_all(PUBLICATIONS_SQL):
        item = by_news.get(row["news_id"])
        if item is None:
            continue
        item.platforms.append(
            {
                "platform": row["platform"],
                "title": PLATFORM_TITLES.get(row["platform"], row["platform"]),
                "status": row["status"],
                "url": row["url"] or "",
                "error": row["error"] or "",
                "attempts": row["attempts"],
            }
        )
    for item in by_news.values():
        item.platforms.sort(key=lambda row: row["title"])
    return list(by_news.values())


def platforms(max_attempts: int = 8) -> list[PlatformCard]:
    """One card per platform: what works, what is broken, how many attempts are left."""
    cards: dict[str, PlatformCard] = {}
    for row in fetch_all(PUBLICATIONS_SQL):
        name = row["platform"]
        card = cards.setdefault(name, PlatformCard(platform=name, title=PLATFORM_TITLES.get(name, name)))
        moment = _moment(row["updated_at"])
        if row["status"] == "ok":
            card.ok_count += 1
            if moment and (card.last_ok_at is None or moment > card.last_ok_at):
                card.last_ok_at = moment
                card.last_url = row["url"] or ""
        else:
            card.error_count += 1
            card.attempts_spent += row["attempts"] or 0
            if row["attempts"] and row["attempts"] >= max_attempts:
                card.given_up += 1
            if row["error"]:
                card.last_error = row["error"]
    return sorted(cards.values(), key=lambda card: card.title)


def failed_preparations() -> list[dict]:
    """Items the preparer could not finish; they come back to the queue on their own."""
    return [
        {
            "news_id": row["news_id"],
            "title": row["retold_title"] or f"Новость {row['news_id']}",
            "error": row["error"] or "",
            "prepared_at": _moment(row["prepared_at"]),
        }
        for row in fetch_all(FAILED_SQL)
    ]


def held_items() -> list[dict]:
    """What the operator took out of the queue, and how to put it back."""
    now = datetime.now(timezone.utc)
    rows = []
    plans = PublicationPlan.objects.select_related("news_item").exclude(
        hold_until__isnull=True, dropped_at__isnull=True
    )
    for plan in plans:
        if plan.dropped_at:
            state = f"снята с очереди {plan.dropped_at:%d.%m %H:%M}"
        elif plan.hold_until and plan.hold_until > now:
            state = f"отложена до {plan.hold_until:%d.%m %H:%M}"
        else:
            continue
        rows.append({"news_id": plan.news_item_id, "title": plan.news_item.title, "state": state})
    return rows


def broadcast_state() -> tuple[dict, str]:
    """Everything the «Эфир» screen shows, or the reason it shows nothing."""
    try:
        items, config = queue()
        return {
            "queue": items,
            "config": config,
            "published": published(),
            "platforms": platforms(),
            "failed": failed_preparations(),
            "held": held_items(),
        }, ""
    except PipelineUnavailable as exc:
        return {}, str(exc)
