import achievements
import database as db

ALL = ["phishing_email", "account_security", "unexpected_attachment", "system_update", "backup", "data_handling"]


def attempt(i, mission, correct, scored, status=db.STATUS_COMPLETED, demo=False):
    return db.AttemptSummary(
        id=i,
        mission_id=mission,
        content_version="1.0.0",
        started_at=f"2026-10-12T10:{i:02d}:00",
        finished_at=None,
        status=status,
        is_demo=demo,
        visited=[],
        earned=correct * 10,
        correct=correct,
        scored=scored,
    )


def earned(attempts):
    return {a.id for a in achievements.evaluate(attempts, ALL) if a.earned}


def test_no_attempts_no_achievements():
    assert earned([]) == set()
    assert len(achievements.evaluate([], ALL)) == 4


def test_first_step():
    assert earned([attempt(1, "backup", 2, 4)]) == {"first_step"}


def test_incomplete_attempt_does_not_count():
    assert earned([attempt(1, "backup", 4, 4, status=db.STATUS_STOPPED)]) == set()


def test_flawless():
    assert "flawless" in earned([attempt(1, "backup", 4, 4)])
    assert "flawless" not in earned([attempt(1, "backup", 3, 4)])


def test_all_directions():
    attempts = [attempt(i, m, 1, 4) for i, m in enumerate(ALL)]
    assert "all_directions" in earned(attempts)
    assert "all_directions" not in earned(attempts[:-1])


def test_improvement_requires_higher_replay_score():
    assert "improvement" in earned([attempt(1, "backup", 2, 4), attempt(2, "backup", 3, 4)])
    assert "improvement" not in earned([attempt(1, "backup", 3, 4), attempt(2, "backup", 2, 4)])
    assert "improvement" not in earned([attempt(1, "backup", 2, 4), attempt(2, "phishing_email", 4, 4)])
    assert "improvement" not in earned([attempt(1, "backup", 2, 4), attempt(2, "backup", 2, 4)])


def test_demo_attempts_ignored():
    demo = [attempt(i, m, 4, 4, demo=True) for i, m in enumerate(ALL)]
    assert earned(demo) == set()
