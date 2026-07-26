"""What the machine did, read from the pipeline's own database.

The pipeline writes one row per run (`service_run`); this turns those rows into
the «Машина» block: one line per service in Russian, with the moment of the last
run and its counters.

The staleness bounds below are a copy of the pipeline's own numbers, not an
import: the two services talk through SQL and nothing else, and a stale-looking
constant is a much smaller price than a code dependency across that boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json

from collector.services.pipeline_db import PipelineUnavailable, fetch_all

SERVICE_TITLES = {
    "evaluator": "Оценка",
    "preparer": "Подготовка",
    "publisher": "Публикация",
    "evaluator-backfill": "Пересчёт решений",
    "notify-check": "Проверка на аварии",
    "notify-digest": "Сводка за день",
    "retention": "Чистка картинок",
}

# Twice the timer interval plus a little: past this an unfinished run is not
# «идёт», it is a process that died without closing its row.
STALE_AFTER = {
    "evaluator": timedelta(minutes=25),
    "preparer": timedelta(minutes=35),
    "publisher": timedelta(minutes=65),
    "evaluator-backfill": timedelta(minutes=30),
    "notify-check": timedelta(hours=2),
    "notify-digest": timedelta(hours=26),
    "retention": timedelta(hours=26),
}

# The counters travel as JSON keys the pipeline chose for itself; the screen is
# in Russian. An unknown key falls back to itself rather than being hidden — a
# number nobody named is still a number worth seeing.
COUNTER_TITLES = {
    "queue": "в очереди",
    "evaluated": "оценено",
    "selected": "отобрано",
    "failed": "не получилось",
    "without_topic": "без темы",
    "prepared": "подготовлено",
    "published": "опубликовано",
    "images": "картинок",
    "checked": "проверено",
    "removed": "удалено картинок",
    "freed_mb": "освобождено, МБ",
}

LAST_RUNS_SQL = """
SELECT service, status, started_at, finished_at, counters, error
FROM service_run
WHERE id IN (SELECT MAX(id) FROM service_run GROUP BY service)
ORDER BY service
"""

QUEUE_SQL = """
SELECT status, COUNT(*) AS count
FROM prepared_item
GROUP BY status
"""


@dataclass
class ServiceRun:
    service: str
    title: str
    status: str          # ok | failed | running | interrupted
    started_at: datetime | None
    finished_at: datetime | None
    counters: dict = field(default_factory=dict)
    error: str = ""

    @property
    def summary(self) -> str:
        """The counters as one Russian line, without inventing new vocabulary."""
        if self.status == "interrupted":
            return "прогон не закрылся, процесс прервали"
        if self.status == "failed":
            return self.error or "прогон завершился ошибкой"
        if self.status == "running":
            return "идёт"
        parts = [
            f"{COUNTER_TITLES.get(name, name)} {value}"
            for name, value in self.counters.items()
            if isinstance(value, int)
        ]
        return ", ".join(parts) if parts else "без изменений"


def _moment(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def last_runs() -> list[ServiceRun]:
    """The most recent run of every service. Raises PipelineUnavailable."""
    runs = []
    now = datetime.now(timezone.utc)
    for row in fetch_all(LAST_RUNS_SQL):
        started = _moment(row["started_at"])
        status = row["status"]
        if status == "running" and started is not None:
            limit = STALE_AFTER.get(row["service"], timedelta(hours=1))
            if now - started > limit:
                status = "interrupted"
        try:
            counters = json.loads(row["counters"] or "{}")
        except ValueError:
            counters = {}
        runs.append(
            ServiceRun(
                service=row["service"],
                title=SERVICE_TITLES.get(row["service"], row["service"]),
                status=status,
                started_at=started,
                finished_at=_moment(row["finished_at"]),
                counters=counters,
                error=row["error"] or "",
            )
        )
    return runs


def queue_counts() -> dict[str, int]:
    """How many news items sit in each pipeline state. Raises PipelineUnavailable."""
    return {row["status"]: row["count"] for row in fetch_all(QUEUE_SQL)}


def machine_block() -> tuple[list[ServiceRun], dict[str, int], str]:
    """Everything the dashboard needs, plus the reason it is empty when it is."""
    try:
        return last_runs(), queue_counts(), ""
    except PipelineUnavailable as exc:
        return [], {}, str(exc)
