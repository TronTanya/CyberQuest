"""Explainable scoring: 10 points per preferred answer, 0 otherwise.

The maximum is calculated only from the decision points that were actually
visited, so branches or recovery scenes cannot inflate or deflate a score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

POINTS_PREFERRED = 10
POINTS_OTHER = 0


class ScoredDecision(Protocol):
    is_correct: bool


@dataclass(frozen=True)
class ScoreSummary:
    earned: int
    maximum: int
    correct: int
    scored: int

    @property
    def percent(self) -> float:
        return round(100 * self.earned / self.maximum, 1) if self.maximum else 0.0

    @property
    def is_flawless(self) -> bool:
        return self.scored > 0 and self.correct == self.scored


def points_for(is_correct: bool) -> int:
    return POINTS_PREFERRED if is_correct else POINTS_OTHER


def summarize(decisions: Iterable[ScoredDecision]) -> ScoreSummary:
    """Score a set of unique decisions (one per visited decision scene)."""
    items = list(decisions)
    correct = sum(1 for d in items if bool(d.is_correct))
    return ScoreSummary(
        earned=correct * POINTS_PREFERRED,
        maximum=len(items) * POINTS_PREFERRED,
        correct=correct,
        scored=len(items),
    )


def format_score(summary: ScoreSummary) -> str:
    return f"{summary.earned} из {summary.maximum} ({summary.percent:g}%)"
