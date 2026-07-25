"""Replaying a selection profile over every scored news item.

The operator changes a threshold and immediately needs three answers: how many
news items the draft would pass, which ones it adds, and which ones missed by a
single point. All three come from one pivot of the stored scores plus arithmetic
in Python — the corpus is ~6000 items by 20 axes, and replaying a profile over it
costs a fraction of a second.

Nothing here writes anything.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from django.conf import settings

from collector.models import LatestEvaluationScore, NewsItem
from collector.services.selection import GATE_MAX, GATE_MIN, HIGHLIGHT_MIN, Bound, selects

# Enough near misses to read through, not so many that the page turns into a list.
NEAR_MISS_LIMIT = 50
ADDED_LIMIT = 50


@dataclass
class Outcome:
    """What a profile does to the corpus."""

    total: int
    passed: set[int]
    # Which condition stopped each rejected item first, by axis key ('' for
    # «no strong side reached its bound»).
    blocked_by: dict[str, int]

    @property
    def passed_count(self) -> int:
        return len(self.passed)

    @property
    def passed_share(self) -> float:
        return 100.0 * len(self.passed) / self.total if self.total else 0.0


def corpus_scores(selector_name: str | None = None) -> dict[int, dict[str, int]]:
    """Latest evaluation of every scored news item: {news_id: {axis: value}}.

    Only the configured evaluator counts. The operator's manual review copies
    those same scores under its own selector name, and counting both would make
    one evaluation look like two.
    """
    selector = selector_name or settings.POSINUS_MANUAL_SCORE_SELECTOR
    rows = LatestEvaluationScore.objects.filter(selector_name=selector).values_list(
        "news_id", "characteristic_key", "value"
    )
    scores: dict[int, dict[str, int]] = defaultdict(dict)
    for news_id, key, value in rows.iterator(chunk_size=5000):
        scores[news_id][key] = value
    return scores


def apply_profile(bounds: list[Bound], corpus: dict[int, dict[str, int]]) -> Outcome:
    """Run the rule over the corpus and record what stopped each rejected item."""
    gates = [b for b in bounds if b.kind in (GATE_MIN, GATE_MAX)]
    highlights = [b for b in bounds if b.kind == HIGHLIGHT_MIN]
    passed: set[int] = set()
    blocked: dict[str, int] = defaultdict(int)

    for news_id, scores in corpus.items():
        blocker = next((b for b in gates if not b.holds(scores.get(b.key, 0))), None)
        if blocker is not None:
            blocked[blocker.key] += 1
            continue
        if highlights and not any(b.holds(scores.get(b.key, 0)) for b in highlights):
            blocked[""] += 1
            continue
        passed.add(news_id)
    return Outcome(total=len(corpus), passed=passed, blocked_by=dict(blocked))


def near_misses(bounds: list[Bound], corpus: dict[int, dict[str, int]]) -> list[tuple[int, str]]:
    """Items that would pass if one axis moved by one point.

    This list is where the arguing happens: the percentages tell you how strict
    the profile is, the near misses tell you whether it is strict about the
    right thing.
    """
    gates = [b for b in bounds if b.kind in (GATE_MIN, GATE_MAX)]
    highlights = [b for b in bounds if b.kind == HIGHLIGHT_MIN]
    found: list[tuple[int, str]] = []

    for news_id, scores in corpus.items():
        failed = [b for b in gates if not b.holds(scores.get(b.key, 0))]
        if len(failed) > 1:
            continue
        if failed:
            bound = failed[0]
            gap = abs(scores.get(bound.key, 0) - bound.value)
            if gap != 1:
                continue
            if highlights and not any(b.holds(scores.get(b.key, 0)) for b in highlights):
                continue
            found.append((news_id, f"{bound.title.lower()} {scores.get(bound.key, 0)}, {bound.requirement}"))
            continue
        if not highlights or any(b.holds(scores.get(b.key, 0)) for b in highlights):
            continue  # it passed, or there is nothing to miss by
        closest = min(highlights, key=lambda b: b.value - scores.get(b.key, 0))
        score = scores.get(closest.key, 0)
        if closest.value - score == 1:
            found.append((news_id, f"{closest.title.lower()} {score}, {closest.requirement}"))
    return found


def titles_for(news_ids, limit: int) -> list[NewsItem]:
    """Headlines for a list of ids — the operator argues about news, not numbers."""
    ids = list(news_ids)[:limit]
    if not ids:
        return []
    items = NewsItem.objects.filter(pk__in=ids).only("id", "title", "published_at", "first_seen_at")
    return sorted(items, key=lambda item: item.published_at or item.first_seen_at, reverse=True)
