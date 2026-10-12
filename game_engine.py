"""Scenario engine: loads mission content, validates it and drives an attempt.

The engine has no dependency on Streamlit or the database so it can be tested
in isolation and reused by any interface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTENT_DIR = Path(__file__).resolve().parent / "content"
MISSIONS_PATH = CONTENT_DIR / "missions.json"
REFERENCES_PATH = CONTENT_DIR / "references.json"

DECISION = "decision"
RECOVERY = "recovery"
VERIFICATION = "verification"
SCENE_TYPES = {DECISION, RECOVERY, VERIFICATION}


class ContentError(ValueError):
    """Raised when mission content is malformed."""


class EngineError(RuntimeError):
    """Raised when an action is not allowed in the current attempt state."""


@dataclass(frozen=True)
class Artifact:
    kind: str
    fields: tuple[tuple[str, str], ...]
    body: str
    url: str | None = None


@dataclass(frozen=True)
class Option:
    id: str
    text: str
    preferred: bool
    consequence: str
    next: str | None


@dataclass(frozen=True)
class Scene:
    id: str
    type: str
    situation: str
    recommendation: str
    reference_id: str
    title: str = ""
    artifact: Artifact | None = None
    options: tuple[Option, ...] = ()
    explanation: str = ""
    steps: tuple[str, ...] = ()
    next: str | None = None

    @property
    def is_decision(self) -> bool:
        return self.type == DECISION

    def option(self, option_id: str) -> Option:
        for option in self.options:
            if option.id == option_id:
                return option
        raise EngineError(f"Вариант '{option_id}' не найден в сцене '{self.id}'.")

    @property
    def preferred_option(self) -> Option:
        return next(o for o in self.options if o.preferred)


@dataclass(frozen=True)
class Mission:
    id: str
    title: str
    summary: str
    start_scene: str
    scenes: dict[str, Scene]
    content_version: str

    def scene(self, scene_id: str) -> Scene:
        try:
            return self.scenes[scene_id]
        except KeyError as exc:
            raise EngineError(f"Сцена '{scene_id}' не найдена в миссии '{self.id}'.") from exc

    def successors(self, scene_id: str) -> list[str | None]:
        scene = self.scenes[scene_id]
        if scene.is_decision:
            return [o.next for o in scene.options]
        return [scene.next]

    def decision_count_bounds(self) -> tuple[int, int]:
        """Min and max number of scored decisions over all complete paths."""
        memo: dict[str, tuple[int, int]] = {}

        def walk(scene_id: str) -> tuple[int, int]:
            if scene_id in memo:
                return memo[scene_id]
            own = 1 if self.scenes[scene_id].is_decision else 0
            results = [walk(n) if n else (0, 0) for n in self.successors(scene_id)]
            bounds = (own + min(r[0] for r in results), own + max(r[1] for r in results))
            memo[scene_id] = bounds
            return bounds

        return walk(self.start_scene)


@dataclass(frozen=True)
class Reference:
    id: str
    title: str
    url: str
    publisher: str
    verified: bool


@dataclass
class Content:
    version: str
    missions: dict[str, Mission]
    references: dict[str, Reference]

    def mission(self, mission_id: str) -> Mission:
        try:
            return self.missions[mission_id]
        except KeyError as exc:
            raise EngineError(f"Миссия '{mission_id}' не найдена.") from exc


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ContentError(f"{where}: отсутствует поле '{key}'.")
    return data[key]


def _parse_artifact(raw: dict[str, Any] | None, where: str) -> Artifact | None:
    if raw is None:
        return None
    fields = tuple((str(label), str(value)) for label, value in raw.get("fields", []))
    return Artifact(
        kind=str(_require(raw, "kind", where)),
        fields=fields,
        body=str(raw.get("body", "")),
        url=raw.get("url"),
    )


def _parse_scene(raw: dict[str, Any], mission_id: str) -> Scene:
    scene_id = str(_require(raw, "id", f"Миссия {mission_id}"))
    where = f"Сцена {mission_id}/{scene_id}"
    scene_type = _require(raw, "type", where)
    if scene_type not in SCENE_TYPES:
        raise ContentError(f"{where}: неизвестный тип '{scene_type}'.")
    options = tuple(
        Option(
            id=str(_require(o, "id", where)),
            text=str(_require(o, "text", where)),
            preferred=bool(o.get("preferred", False)),
            consequence=str(_require(o, "consequence", where)),
            next=o.get("next"),
        )
        for o in raw.get("options", [])
    )
    return Scene(
        id=scene_id,
        type=scene_type,
        situation=str(_require(raw, "situation", where)),
        recommendation=str(_require(raw, "recommendation", where)),
        reference_id=str(_require(raw, "reference_id", where)),
        title=str(raw.get("title", "")),
        artifact=_parse_artifact(raw.get("artifact"), where),
        options=options,
        explanation=str(raw.get("explanation", "")),
        steps=tuple(str(s) for s in raw.get("steps", [])),
        next=raw.get("next"),
    )


def parse_content(missions_data: dict[str, Any], references_data: dict[str, Any]) -> Content:
    version = str(_require(missions_data, "content_version", "missions.json"))
    references = {
        r["id"]: Reference(
            id=r["id"],
            title=r["title"],
            url=r["url"],
            publisher=r.get("publisher", ""),
            verified=bool(r.get("verified", False)),
        )
        for r in references_data.get("references", [])
    }
    missions: dict[str, Mission] = {}
    for raw in _require(missions_data, "missions", "missions.json"):
        mission_id = str(_require(raw, "id", "missions.json"))
        if mission_id in missions:
            raise ContentError(f"Повторяющийся ID миссии: {mission_id}.")
        scenes: dict[str, Scene] = {}
        for raw_scene in _require(raw, "scenes", f"Миссия {mission_id}"):
            scene = _parse_scene(raw_scene, mission_id)
            if scene.id in scenes:
                raise ContentError(f"Миссия {mission_id}: повторяющийся ID сцены {scene.id}.")
            scenes[scene.id] = scene
        missions[mission_id] = Mission(
            id=mission_id,
            title=str(_require(raw, "title", mission_id)),
            summary=str(raw.get("summary", "")),
            start_scene=str(_require(raw, "start_scene", mission_id)),
            scenes=scenes,
            content_version=version,
        )
    content = Content(version=version, missions=missions, references=references)
    errors = validate_content(content)
    if errors:
        raise ContentError("Ошибки в содержимом миссий:\n" + "\n".join(errors))
    return content


def load_content(
    missions_path: Path = MISSIONS_PATH, references_path: Path = REFERENCES_PATH
) -> Content:
    try:
        missions_data = json.loads(Path(missions_path).read_text(encoding="utf-8"))
        references_data = json.loads(Path(references_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentError(f"Не удалось прочитать файлы содержимого: {exc}") from exc
    return parse_content(missions_data, references_data)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_mission(mission: Mission, reference_ids: set[str]) -> list[str]:
    errors: list[str] = []
    prefix = f"[{mission.id}]"
    if mission.start_scene not in mission.scenes:
        return [f"{prefix} стартовая сцена '{mission.start_scene}' не существует."]

    decision_ids = [s.id for s in mission.scenes.values() if s.is_decision]
    if len(decision_ids) < 4:
        errors.append(f"{prefix} меньше четырёх точек решения.")

    for scene in mission.scenes.values():
        where = f"{prefix} сцена {scene.id}:"
        if scene.reference_id not in reference_ids:
            errors.append(f"{where} неизвестный источник '{scene.reference_id}'.")
        if scene.is_decision:
            if not 3 <= len(scene.options) <= 4:
                errors.append(f"{where} должно быть 3–4 варианта ответа.")
            option_ids = [o.id for o in scene.options]
            if len(set(option_ids)) != len(option_ids):
                errors.append(f"{where} повторяющиеся ID вариантов.")
            if sum(o.preferred for o in scene.options) != 1:
                errors.append(f"{where} должен быть ровно один предпочтительный ответ.")
            if not scene.explanation:
                errors.append(f"{where} нет объяснения.")
            if scene.next is not None:
                errors.append(f"{where} у решения переход задаётся в вариантах, а не в сцене.")
        elif scene.options:
            errors.append(f"{where} у сцены без оценки не должно быть вариантов.")
        for target in mission.successors(scene.id):
            if target is not None and target not in mission.scenes:
                errors.append(f"{where} переход на несуществующую сцену '{target}'.")

    if errors:
        return errors

    # Cycle detection and reachability via depth-first search.
    state: dict[str, int] = {}  # 1 = in progress, 2 = finished

    def dfs(scene_id: str) -> None:
        state[scene_id] = 1
        for target in mission.successors(scene_id):
            if target is None:
                continue
            if state.get(target) == 1:
                errors.append(f"{prefix} цикл через сцену '{target}'.")
            elif target not in state:
                dfs(target)
        state[scene_id] = 2

    dfs(mission.start_scene)
    unreachable = set(mission.scenes) - set(state)
    if unreachable:
        errors.append(f"{prefix} недостижимые сцены: {sorted(unreachable)}.")
    if not errors and mission.decision_count_bounds()[0] < 4:
        errors.append(f"{prefix} есть путь короче четырёх решений.")
    return errors


def validate_content(content: Content) -> list[str]:
    errors: list[str] = []
    seen_scene_ids: set[str] = set()
    for mission in content.missions.values():
        duplicates = seen_scene_ids & set(mission.scenes)
        if duplicates:
            errors.append(f"[{mission.id}] ID сцен повторяются в разных миссиях: {sorted(duplicates)}.")
        seen_scene_ids |= set(mission.scenes)
        errors.extend(validate_mission(mission, set(content.references)))
    return errors


# --------------------------------------------------------------------------- #
# Attempt state
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DecisionRecord:
    scene_id: str
    option_id: str
    is_correct: bool
    points: int


@dataclass
class AttemptState:
    mission_id: str
    current_scene_id: str | None
    attempt_id: int | None = None
    visited: list[str] = field(default_factory=list)
    decisions: dict[str, DecisionRecord] = field(default_factory=dict)
    finished: bool = False

    @property
    def awaiting_continue(self) -> bool:
        """True when the current decision is answered and feedback is shown."""
        return self.current_scene_id is not None and self.current_scene_id in self.decisions


@dataclass(frozen=True)
class SubmitResult:
    record: DecisionRecord
    option: Option
    newly_recorded: bool


def start_attempt(mission: Mission, attempt_id: int | None = None) -> AttemptState:
    return AttemptState(
        mission_id=mission.id,
        current_scene_id=mission.start_scene,
        attempt_id=attempt_id,
        visited=[mission.start_scene],
    )


def restore_attempt(
    mission: Mission,
    attempt_id: int,
    visited: list[str],
    decisions: list[DecisionRecord],
    finished: bool,
) -> AttemptState:
    """Rebuild an attempt from persisted data (e.g. after a page reload)."""
    known = [s for s in visited if s in mission.scenes]
    state = AttemptState(
        mission_id=mission.id,
        current_scene_id=None if finished or not known else known[-1],
        attempt_id=attempt_id,
        visited=known,
        decisions={d.scene_id: d for d in decisions},
        finished=finished,
    )
    if not finished and not known:
        state.current_scene_id = mission.start_scene
        state.visited = [mission.start_scene]
    return state


def current_scene(state: AttemptState, mission: Mission) -> Scene | None:
    if state.finished or state.current_scene_id is None:
        return None
    return mission.scene(state.current_scene_id)


def submit_answer(
    state: AttemptState, mission: Mission, option_id: str, points_for_preferred: int = 10
) -> SubmitResult:
    """Record an answer for the current decision.

    A repeated submission for an already answered scene returns the original
    record and does not change the state (no double scoring).
    """
    scene = current_scene(state, mission)
    if scene is None:
        raise EngineError("Попытка уже завершена.")
    if not scene.is_decision:
        raise EngineError("В этой сцене не нужно выбирать ответ.")
    existing = state.decisions.get(scene.id)
    if existing is not None:
        return SubmitResult(existing, scene.option(existing.option_id), newly_recorded=False)
    option = scene.option(option_id)
    record = DecisionRecord(
        scene_id=scene.id,
        option_id=option.id,
        is_correct=option.preferred,
        points=points_for_preferred if option.preferred else 0,
    )
    state.decisions[scene.id] = record
    return SubmitResult(record, option, newly_recorded=True)


def advance(state: AttemptState, mission: Mission) -> Scene | None:
    """Move to the next scene. Returns the new scene or None if the mission ended."""
    scene = current_scene(state, mission)
    if scene is None:
        raise EngineError("Попытка уже завершена.")
    if scene.is_decision:
        record = state.decisions.get(scene.id)
        if record is None:
            raise EngineError("Сначала выберите и подтвердите ответ.")
        next_id = scene.option(record.option_id).next
    else:
        next_id = scene.next
    if next_id is None:
        state.current_scene_id = None
        state.finished = True
        return None
    state.current_scene_id = next_id
    state.visited.append(next_id)
    return mission.scene(next_id)


def decision_stage(state: AttemptState, mission: Mission) -> tuple[int, int]:
    """Return (current decision number, expected total decisions on this mission)."""
    visited_decisions = sum(1 for s in state.visited if s in mission.scenes and mission.scenes[s].is_decision)
    return max(visited_decisions, 1), mission.decision_count_bounds()[1]
