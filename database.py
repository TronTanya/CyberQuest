"""SQLite persistence for attempts and decisions (parameterized queries only)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from scoring import POINTS_PREFERRED, ScoreSummary

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "cyberquest.db"

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUSES = {STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_STOPPED}

STATUS_LABELS = {
    STATUS_COMPLETED: "Завершена",
    STATUS_STOPPED: "Не завершена (остановлена)",
    STATUS_IN_PROGRESS: "Не завершена (в процессе)",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id      TEXT    NOT NULL,
    content_version TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL CHECK (status IN ('in_progress', 'completed', 'stopped')),
    is_demo         INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    visited_scenes  TEXT    NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    scene_id    TEXT    NOT NULL,
    option_id   TEXT    NOT NULL,
    is_correct  INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
    points      INTEGER NOT NULL CHECK (points >= 0),
    answered_at TEXT    NOT NULL,
    UNIQUE (attempt_id, scene_id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_mission ON attempts(mission_id, is_demo);
"""


class DatabaseError(RuntimeError):
    """Raised for invalid persistence operations."""


@dataclass(frozen=True)
class DecisionRow:
    scene_id: str
    option_id: str
    is_correct: bool
    points: int
    answered_at: str


@dataclass(frozen=True)
class AttemptSummary:
    id: int
    mission_id: str
    content_version: str
    started_at: str
    finished_at: str | None
    status: str
    is_demo: bool
    visited: list[str]
    earned: int
    correct: int
    scored: int

    @property
    def score(self) -> ScoreSummary:
        return ScoreSummary(
            earned=self.earned,
            maximum=self.scored * POINTS_PREFERRED,
            correct=self.correct,
            scored=self.scored,
        )

    @property
    def is_completed(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def create_attempt(
    db_path: Path | str,
    mission_id: str,
    content_version: str,
    start_scene: str,
    is_demo: bool = False,
) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO attempts (mission_id, content_version, started_at, status, is_demo, visited_scenes)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (mission_id, content_version, now_iso(), STATUS_IN_PROGRESS, int(is_demo), json.dumps([start_scene])),
        )
        return int(cur.lastrowid)


def update_visited(db_path: Path | str, attempt_id: int, visited: list[str]) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET visited_scenes = ? WHERE id = ? AND status = ?",
            (json.dumps(visited, ensure_ascii=False), attempt_id, STATUS_IN_PROGRESS),
        )


def record_decision(
    db_path: Path | str,
    attempt_id: int,
    scene_id: str,
    option_id: str,
    is_correct: bool,
    points: int,
) -> bool:
    """Insert a decision. Returns False if this scene was already answered."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT status FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise DatabaseError(f"Попытка {attempt_id} не найдена.")
        if row["status"] != STATUS_IN_PROGRESS:
            raise DatabaseError(f"Попытка {attempt_id} уже закрыта.")
        cur = conn.execute(
            "INSERT OR IGNORE INTO decisions (attempt_id, scene_id, option_id, is_correct, points, answered_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (attempt_id, scene_id, option_id, int(is_correct), int(points), now_iso()),
        )
        return cur.rowcount == 1


def finish_attempt(db_path: Path | str, attempt_id: int, status: str) -> None:
    if status not in (STATUS_COMPLETED, STATUS_STOPPED):
        raise DatabaseError(f"Недопустимый итоговый статус: {status}.")
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET status = ?, finished_at = ? WHERE id = ? AND status = ?",
            (status, now_iso(), attempt_id, STATUS_IN_PROGRESS),
        )


_SUMMARY_SQL = """
SELECT a.id, a.mission_id, a.content_version, a.started_at, a.finished_at, a.status,
       a.is_demo, a.visited_scenes,
       COALESCE(SUM(d.points), 0)      AS earned,
       COALESCE(SUM(d.is_correct), 0)  AS correct,
       COUNT(d.id)                     AS scored
FROM attempts a
LEFT JOIN decisions d ON d.attempt_id = a.id
"""


def _to_summary(row: sqlite3.Row) -> AttemptSummary:
    try:
        visited = json.loads(row["visited_scenes"])
    except (TypeError, json.JSONDecodeError):
        visited = []
    return AttemptSummary(
        id=row["id"],
        mission_id=row["mission_id"],
        content_version=row["content_version"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        is_demo=bool(row["is_demo"]),
        visited=list(visited),
        earned=int(row["earned"]),
        correct=int(row["correct"]),
        scored=int(row["scored"]),
    )


def get_attempt(db_path: Path | str, attempt_id: int) -> AttemptSummary | None:
    with connect(db_path) as conn:
        row = conn.execute(_SUMMARY_SQL + " WHERE a.id = ? GROUP BY a.id", (attempt_id,)).fetchone()
    return _to_summary(row) if row else None


def list_attempts(
    db_path: Path | str,
    *,
    demo: bool = False,
    mission_id: str | None = None,
) -> list[AttemptSummary]:
    """Attempts in chronological order. Learner and demo attempts are never mixed."""
    sql = _SUMMARY_SQL + " WHERE a.is_demo = ?"
    params: list[object] = [int(demo)]
    if mission_id is not None:
        sql += " AND a.mission_id = ?"
        params.append(mission_id)
    sql += " GROUP BY a.id ORDER BY a.started_at, a.id"
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_to_summary(r) for r in rows]


def get_decisions(db_path: Path | str, attempt_id: int) -> list[DecisionRow]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT scene_id, option_id, is_correct, points, answered_at FROM decisions"
            " WHERE attempt_id = ? ORDER BY id",
            (attempt_id,),
        ).fetchall()
    return [
        DecisionRow(r["scene_id"], r["option_id"], bool(r["is_correct"]), int(r["points"]), r["answered_at"])
        for r in rows
    ]


def find_in_progress(db_path: Path | str, mission_id: str, demo: bool) -> AttemptSummary | None:
    attempts = [a for a in list_attempts(db_path, demo=demo, mission_id=mission_id) if a.status == STATUS_IN_PROGRESS]
    return attempts[-1] if attempts else None


def delete_demo_attempts(db_path: Path | str) -> int:
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM attempts WHERE is_demo = 1")
        return cur.rowcount


# --------------------------------------------------------------------------- #
# Aggregation (pure functions over stored summaries)
# --------------------------------------------------------------------------- #

def latest_and_best(attempts: list[AttemptSummary]) -> tuple[AttemptSummary | None, AttemptSummary | None]:
    """Latest and best completed attempt. Ties for best go to the earlier attempt."""
    completed = [a for a in attempts if a.is_completed]
    if not completed:
        return None, None
    ordered = sorted(completed, key=lambda a: (a.started_at, a.id))
    best = ordered[0]
    for attempt in ordered[1:]:
        if attempt.score.percent > best.score.percent:
            best = attempt
    return ordered[-1], best


@dataclass(frozen=True)
class ProgressStats:
    completed_missions: int
    completed_attempts: int
    incomplete_attempts: int
    scored_decisions: int
    correct_decisions: int

    @property
    def accuracy(self) -> float:
        return round(100 * self.correct_decisions / self.scored_decisions, 1) if self.scored_decisions else 0.0


def progress_stats(attempts: list[AttemptSummary]) -> ProgressStats:
    learner = [a for a in attempts if not a.is_demo]
    completed = [a for a in learner if a.is_completed]
    return ProgressStats(
        completed_missions=len({a.mission_id for a in completed}),
        completed_attempts=len(completed),
        incomplete_attempts=len(learner) - len(completed),
        scored_decisions=sum(a.scored for a in learner),
        correct_decisions=sum(a.correct for a in learner),
    )
