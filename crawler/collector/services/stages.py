"""Where a news item stands, as a pair of values rather than a point on a line.

The straight chain «собрана → оценена → отобрана → подготовлена → опубликована»
is a lie in two places. A news item can carry `not_positive` from the machine and
`positive` from the operator at the same time, and the second one really does go
to publication. Preparation and publication live in another database with two
independent statuses, so an item can be in Telegram and failing on VK at once.

So a stage is two things:

- **fate** — what the verdict says: not evaluated, rejected, selected by the
  machine, or selected by a human over a refusal;
- **progress** — how far it got: not prepared, queued, preparation failed,
  prepared, waiting to go out, partly published, published, taken off the queue.

The list shows progress in one word and marks the row when machine and human
disagree. Two extra queries per page of 50, not one per row: the first over the
crawler's latest verdicts, the second over the pipeline's statuses.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from collector.models import LatestReview
from collector.services.pipeline_db import PipelineUnavailable, fetch_all

FATE_TITLES = {
    "unreviewed": "не оценена",
    "rejected": "отклонена",
    "selected": "отобрана",
    "operator": "отобрана человеком",
}

PROGRESS_TITLES = {
    "none": "—",
    "queued": "ждёт подготовки",
    "failed": "ошибка подготовки",
    "prepared": "подготовлена",
    "partial": "вышла частично",
    "published": "опубликована",
    "expired": "снята с очереди",
}


@dataclass
class Stage:
    fate: str = "unreviewed"
    progress: str = "none"
    platforms: tuple[str, ...] = ()
    disagreement: bool = False   # the machine said no and a human said yes

    @property
    def fate_title(self) -> str:
        return FATE_TITLES.get(self.fate, self.fate)

    @property
    def title(self) -> str:
        """The one word the list column shows."""
        if self.progress != "none":
            return PROGRESS_TITLES.get(self.progress, self.progress)
        return self.fate_title


def _fates(news_ids: list[int]) -> dict[int, tuple[str, bool]]:
    """The verdict of each news item, and whether a human overrode the machine."""
    evaluator = settings.POSINUS_MANUAL_SCORE_SELECTOR
    machine: dict[int, str] = {}
    operator: dict[int, str] = {}
    rows = LatestReview.objects.filter(news_id__in=news_ids).values_list(
        "news_id", "selector_name", "decision"
    )
    for news_id, selector, decision in rows:
        if selector == evaluator:
            machine[news_id] = decision
        elif selector.startswith("operator:"):
            operator[news_id] = decision

    result: dict[int, tuple[str, bool]] = {}
    for news_id in news_ids:
        machine_verdict = machine.get(news_id)
        human_positive = operator.get(news_id) == "positive"
        if human_positive:
            result[news_id] = ("operator", machine_verdict == "not_positive")
        elif machine_verdict == "positive":
            result[news_id] = ("selected", False)
        elif machine_verdict == "not_positive":
            result[news_id] = ("rejected", False)
        else:
            result[news_id] = ("unreviewed", False)
    return result


def _progress(news_ids: list[int]) -> dict[int, tuple[str, tuple[str, ...]]]:
    """How far the pipeline took each item; empty when its database is unreachable."""
    if not news_ids:
        return {}
    placeholders = ",".join("?" * len(news_ids))
    try:
        prepared = {
            row["news_id"]: row["status"]
            for row in fetch_all(
                f"SELECT news_id, status FROM prepared_item WHERE news_id IN ({placeholders})",
                tuple(news_ids),
            )
        }
        posts: dict[int, list[tuple[str, str]]] = {}
        for row in fetch_all(
            f"SELECT news_id, platform, status FROM publication WHERE news_id IN ({placeholders})",
            tuple(news_ids),
        ):
            posts.setdefault(row["news_id"], []).append((row["platform"], row["status"]))
    except PipelineUnavailable:
        return {}

    result: dict[int, tuple[str, tuple[str, ...]]] = {}
    for news_id, status in prepared.items():
        published = tuple(platform for platform, state in posts.get(news_id, []) if state == "ok")
        if status == "published":
            progress = "published"
        elif status == "expired":
            # Waited past its date and was taken off the queue. Never silently:
            # this is where 112 items would otherwise just stop appearing.
            progress = "expired"
        elif status == "error":
            progress = "failed"
        elif published:
            progress = "partial"     # already public somewhere, still finishing
        else:
            progress = "prepared"
        result[news_id] = (progress, published)
    return result


def stages_for(news_ids: list[int]) -> dict[int, Stage]:
    """One Stage per news id, for a whole page of the list at once."""
    if not news_ids:
        return {}
    fates = _fates(news_ids)
    progress = _progress(news_ids)
    stages = {}
    for news_id in news_ids:
        fate, disagreement = fates.get(news_id, ("unreviewed", False))
        step, platforms = progress.get(news_id, ("none", ()))
        if step == "none" and fate in {"selected", "operator"}:
            step = "queued"          # selected, the preparer has not reached it yet
        stages[news_id] = Stage(fate=fate, progress=step, platforms=platforms, disagreement=disagreement)
    return stages
