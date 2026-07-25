"""The selection rule, read from the same place the pipeline reads it.

The thresholds live in `exchange_selection_profile` / `exchange_selection_bound`
and the pipeline's evaluator loads them through the
`exchange_active_selection_profile` view. This module is the crawler-side reader
of the same rows, so the screen that explains a verdict and the service that
issues it can never disagree.

Nothing here writes a review event. A verdict is a pure function of the scores,
which is also what lets the calibration screen replay a draft over the whole
corpus without touching anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from collector.models import SelectionBound, SelectionProfile

GATE_MIN = SelectionBound.Kind.GATE_MIN
GATE_MAX = SelectionBound.Kind.GATE_MAX
HIGHLIGHT_MIN = SelectionBound.Kind.HIGHLIGHT_MIN

# Reading order of the conditions: «нужно от» before «не больше», highlights
# last, and inside a group the reference order of the axes.
_KIND_ORDER = {GATE_MIN: 0, GATE_MAX: 1, HIGHLIGHT_MIN: 2}


@dataclass(frozen=True)
class Bound:
    """One threshold, detached from the database row it came from.

    A draft the operator is trying out is the same thing as a stored profile —
    a list of these — so every calculation below works on both.
    """

    key: str
    title: str
    kind: str
    value: int
    position: int = 0

    @property
    def requirement(self) -> str:
        return f"не больше {self.value}" if self.kind == GATE_MAX else f"нужно от {self.value}"

    def holds(self, score: int) -> bool:
        return score <= self.value if self.kind == GATE_MAX else score >= self.value


@dataclass
class Condition:
    """A bound checked against one news item's score."""

    bound: Bound
    value: int

    @property
    def title(self) -> str:
        return self.bound.title

    @property
    def key(self) -> str:
        return self.bound.key

    @property
    def requirement(self) -> str:
        return self.bound.requirement

    @property
    def passed(self) -> bool:
        return self.bound.holds(self.value)

    @property
    def shortfall(self) -> int:
        """How far the score is from its bound; 0 when the condition holds."""
        if self.passed:
            return 0
        return self.value - self.bound.value if self.bound.kind == GATE_MAX else self.bound.value - self.value


@dataclass
class Verdict:
    """Why a news item passed the profile, or why it did not."""

    profile_name: str
    revision: int
    gates: list[Condition] = field(default_factory=list)
    highlights: list[Condition] = field(default_factory=list)
    passed: bool = False
    summary: str = ""
    closest: Condition | None = None

    @property
    def failed_gates(self) -> list[Condition]:
        return [c for c in self.gates if not c.passed]

    @property
    def passed_highlights(self) -> list[Condition]:
        return [c for c in self.highlights if c.passed]


def active_profile() -> SelectionProfile | None:
    return SelectionProfile.objects.filter(is_active=True).first()


def profile_bounds(profile: SelectionProfile) -> list[Bound]:
    rows = profile.bounds.select_related("characteristic")
    return sort_bounds(
        Bound(
            key=row.characteristic_id,
            title=row.characteristic.title,
            kind=row.kind,
            value=row.value,
            position=row.characteristic.position,
        )
        for row in rows
    )


def sort_bounds(bounds) -> list[Bound]:
    return sorted(bounds, key=lambda b: (_KIND_ORDER.get(b.kind, 9), b.position, b.key))


def selects(bounds: list[Bound], scores: dict[str, int]) -> bool:
    """The rule itself: every gate holds and at least one highlight reaches its bound.

    A missing axis reads as 0, bounds are inclusive, and a profile without
    highlights needs only its gates — the same three sentences the evaluator
    implements.
    """
    highlights_present = False
    highlight_reached = False
    for bound in bounds:
        score = scores.get(bound.key, 0)
        if bound.kind == HIGHLIGHT_MIN:
            highlights_present = True
            highlight_reached = highlight_reached or bound.holds(score)
        elif not bound.holds(score):
            return False
    return highlight_reached if highlights_present else True


def explain(profile_name: str, revision: int, bounds: list[Bound], scores: dict[str, int]) -> Verdict:
    """The verdict with the reasoning the operator reads in the news card."""
    verdict = Verdict(profile_name=profile_name, revision=revision)
    for bound in bounds:
        condition = Condition(bound=bound, value=scores.get(bound.key, 0))
        if bound.kind == HIGHLIGHT_MIN:
            verdict.highlights.append(condition)
        else:
            verdict.gates.append(condition)

    highlight_ok = not verdict.highlights or bool(verdict.passed_highlights)
    verdict.passed = not verdict.failed_gates and highlight_ok
    if verdict.highlights and not verdict.passed_highlights:
        # The near miss is the interesting part: it is what a one-point change
        # to the profile would let through.
        verdict.closest = min(verdict.highlights, key=lambda c: (c.shortfall, -c.value))
    verdict.summary = _summarize(verdict)
    return verdict


def _summarize(verdict: Verdict) -> str:
    """One human sentence before the table of conditions."""
    if verdict.passed:
        parts = [f"{c.title.lower()} {c.value} из 10" for c in verdict.passed_highlights[:2]]
        strong = ", ".join(parts) if parts else "обязательные условия выполнены"
        return f"Прошла: {strong}."

    reasons = [f"{c.title.lower()} {c.value} из 10, {c.requirement}" for c in verdict.failed_gates]
    if verdict.highlights and not verdict.passed_highlights:
        reasons.append("ни одна сильная сторона не дотянула до своего порога")
    tail = ""
    if verdict.closest is not None:
        tail = f" Ближе всех была {verdict.closest.title.lower()} с {verdict.closest.value}."
    return "Не прошла: " + ", и ".join(reasons) + "." + tail
