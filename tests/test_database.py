import sqlite3

import pytest

import database as db
from reporting import attempts_to_csv, build_html_report, decision_details


def make_attempt(path, mission="phishing_email", answers=(), status=db.STATUS_COMPLETED, demo=False):
    attempt_id = db.create_attempt(path, mission, "1.0.0", "start", demo)
    for i, correct in enumerate(answers):
        db.record_decision(path, attempt_id, f"s{i}", "a", correct, 10 if correct else 0)
    if status != db.STATUS_IN_PROGRESS:
        db.finish_attempt(path, attempt_id, status)
    return attempt_id


def test_init_creates_tables(db_path):
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"attempts", "decisions"} <= tables


def test_init_is_idempotent(db_path):
    db.init_db(db_path)
    db.init_db(db_path)


def test_attempt_persistence_round_trip(db_path):
    attempt_id = db.create_attempt(db_path, "backup", "1.0.0", "bk_d1")
    assert db.record_decision(db_path, attempt_id, "bk_d1", "b", True, 10)
    db.update_visited(db_path, attempt_id, ["bk_d1", "bk_d2"])
    db.finish_attempt(db_path, attempt_id, db.STATUS_COMPLETED)
    summary = db.get_attempt(db_path, attempt_id)
    assert summary.mission_id == "backup" and summary.content_version == "1.0.0"
    assert summary.visited == ["bk_d1", "bk_d2"]
    assert summary.is_completed and summary.finished_at
    assert (summary.earned, summary.correct, summary.scored) == (10, 1, 1)
    decisions = db.get_decisions(db_path, attempt_id)
    assert [(d.scene_id, d.option_id, d.is_correct, d.points) for d in decisions] == [("bk_d1", "b", True, 10)]


def test_duplicate_decision_not_recorded_twice(db_path):
    attempt_id = db.create_attempt(db_path, "backup", "1.0.0", "bk_d1")
    assert db.record_decision(db_path, attempt_id, "bk_d1", "b", True, 10)
    assert not db.record_decision(db_path, attempt_id, "bk_d1", "a", False, 0)
    summary = db.get_attempt(db_path, attempt_id)
    assert summary.scored == 1 and summary.earned == 10


def test_closed_attempt_rejects_decisions(db_path):
    attempt_id = make_attempt(db_path, answers=[True])
    with pytest.raises(db.DatabaseError):
        db.record_decision(db_path, attempt_id, "late", "a", True, 10)


def test_incomplete_attempt(db_path):
    attempt_id = make_attempt(db_path, answers=[True, False], status=db.STATUS_STOPPED)
    summary = db.get_attempt(db_path, attempt_id)
    assert not summary.is_completed
    assert "Не завершена" in summary.status_label
    assert summary.score.maximum == 20
    # A stopped attempt cannot be reopened as completed.
    db.finish_attempt(db_path, attempt_id, db.STATUS_COMPLETED)
    assert db.get_attempt(db_path, attempt_id).status == db.STATUS_STOPPED
    latest, best = db.latest_and_best([summary])
    assert latest is None and best is None


def test_replay_creates_new_attempt(db_path):
    first = make_attempt(db_path, answers=[True, False])
    second = make_attempt(db_path, answers=[True, True])
    assert first != second
    attempts = db.list_attempts(db_path, mission_id="phishing_email")
    assert [a.id for a in attempts] == [first, second]
    assert db.get_attempt(db_path, first).earned == 10


def test_latest_and_best(db_path):
    make_attempt(db_path, answers=[True, False, False, False])
    best_id = make_attempt(db_path, answers=[True, True, True, False])
    latest_id = make_attempt(db_path, answers=[True, True, False, False])
    make_attempt(db_path, answers=[True, True, True, True], status=db.STATUS_STOPPED)
    latest, best = db.latest_and_best(db.list_attempts(db_path))
    assert latest.id == latest_id
    assert best.id == best_id


def test_demo_attempts_excluded_from_stats(db_path):
    make_attempt(db_path, answers=[True, True])
    make_attempt(db_path, mission="backup", answers=[False, False], demo=True)
    learner = db.list_attempts(db_path, demo=False)
    assert all(not a.is_demo for a in learner)
    stats = db.progress_stats(learner)
    assert stats.completed_missions == 1 and stats.scored_decisions == 2 and stats.accuracy == 100.0
    # Even if demo attempts are passed in, they are ignored.
    stats_all = db.progress_stats(learner + db.list_attempts(db_path, demo=True))
    assert stats_all == stats


def test_repeated_attempts_count_as_one_completed_mission(db_path):
    make_attempt(db_path, answers=[True])
    make_attempt(db_path, answers=[False])
    make_attempt(db_path, answers=[True], status=db.STATUS_STOPPED)
    stats = db.progress_stats(db.list_attempts(db_path))
    assert stats.completed_missions == 1
    assert stats.completed_attempts == 2
    assert stats.incomplete_attempts == 1
    assert stats.scored_decisions == 3 and stats.correct_decisions == 2


def test_empty_history(db_path):
    stats = db.progress_stats(db.list_attempts(db_path))
    assert stats.completed_attempts == 0 and stats.accuracy == 0.0


def test_delete_demo_attempts(db_path):
    keep = make_attempt(db_path, answers=[True])
    make_attempt(db_path, answers=[True], demo=True)
    assert db.delete_demo_attempts(db_path) == 1
    assert [a.id for a in db.list_attempts(db_path)] == [keep]
    assert db.list_attempts(db_path, demo=True) == []


def test_find_in_progress(db_path):
    make_attempt(db_path, answers=[True])
    open_id = make_attempt(db_path, answers=[True], status=db.STATUS_IN_PROGRESS)
    assert db.find_in_progress(db_path, "phishing_email", demo=False).id == open_id
    assert db.find_in_progress(db_path, "phishing_email", demo=True) is None


def test_csv_has_bom_and_russian_headers(db_path, content):
    make_attempt(db_path, answers=[True, False])
    data = attempts_to_csv(db.list_attempts(db_path), content)
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert "Миссия" in text.splitlines()[0]
    assert "Подозрительное письмо" in text


def test_html_report_escapes_content(db_path, content):
    attempt_id = db.create_attempt(db_path, "<script>alert(1)</script>", "1.0<b>", "start")
    db.record_decision(db_path, attempt_id, "<img src=x onerror=alert(1)>", "\"a'", False, 0)
    summary = db.get_attempt(db_path, attempt_id)
    details = decision_details(content, summary.mission_id, db.get_decisions(db_path, attempt_id))
    report = build_html_report([(summary, details)], content)
    assert "<script>" not in report
    assert "<img" not in report
    assert "&lt;script&gt;" in report
    assert "1.0&lt;b&gt;" in report


def test_html_report_contains_required_sections(db_path, content):
    attempt_id = db.create_attempt(db_path, "phishing_email", content.version, "pe_d1")
    db.record_decision(db_path, attempt_id, "pe_d1", "a", False, 0)
    db.finish_attempt(db_path, attempt_id, db.STATUS_STOPPED)
    summary = db.get_attempt(db_path, attempt_id)
    details = decision_details(content, summary.mission_id, db.get_decisions(db_path, attempt_id))
    report = build_html_report([(summary, details)], content)
    for expected in ("Подозрительное письмо", "Не завершена", "0 из 10", "Объяснение", "Рекомендация",
                     content.version, "cisa.gov", "Предпочтительное действие"):
        assert expected in report
    assert "<script" not in report and "http-equiv" not in report
