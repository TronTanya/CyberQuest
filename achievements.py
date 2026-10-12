"""Deterministic achievements calculated from stored learner attempts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from database import AttemptSummary


@dataclass(frozen=True)
class Achievement:
    id: str
    name: str
    description: str
    earned: bool


ACHIEVEMENT_TEXT = {
    "first_step": ("Первый шаг", "Завершите одну миссию."),
    "all_directions": ("Все направления", "Завершите все шесть миссий."),
    "flawless": ("Без ошибок", "Завершите миссию, выбрав предпочтительный ответ во всех решениях."),
    "improvement": ("Работа над ошибками", "Улучшите результат при повторном прохождении миссии."),
}


def _improved_on_replay(completed: list[AttemptSummary]) -> bool:
    by_mission: dict[str, list[AttemptSummary]] = {}
    for attempt in sorted(completed, key=lambda a: (a.started_at, a.id)):
        by_mission.setdefault(attempt.mission_id, []).append(attempt)
    for attempts in by_mission.values():
        best_so_far = attempts[0].score.percent
        for attempt in attempts[1:]:
            if attempt.score.percent > best_so_far:
                return True
            best_so_far = max(best_so_far, attempt.score.percent)
    return False


def evaluate(attempts: Iterable[AttemptSummary], all_mission_ids: Iterable[str]) -> list[Achievement]:
    completed = [a for a in attempts if a.is_completed and not a.is_demo]
    completed_missions = {a.mission_id for a in completed}
    required = set(all_mission_ids)
    earned = {
        "first_step": bool(completed),
        "all_directions": bool(required) and required <= completed_missions,
        "flawless": any(a.score.is_flawless for a in completed),
        "improvement": _improved_on_replay(completed),
    }
    return [
        Achievement(id=key, name=name, description=description, earned=earned[key])
        for key, (name, description) in ACHIEVEMENT_TEXT.items()
    ]
