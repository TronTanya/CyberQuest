from game_engine import DecisionRecord, advance, current_scene, start_attempt, submit_answer
from scoring import ScoreSummary, format_score, points_for, summarize


def rec(scene_id, correct):
    return DecisionRecord(scene_id, "x", correct, points_for(correct))


def test_points_rule():
    assert points_for(True) == 10
    assert points_for(False) == 0


def test_summary_uses_only_visited_decisions():
    summary = summarize([rec("a", True), rec("b", False)])
    assert summary == ScoreSummary(earned=10, maximum=20, correct=1, scored=2)
    assert summary.percent == 50.0


def test_empty_summary_has_zero_percent():
    summary = summarize([])
    assert summary.maximum == 0 and summary.percent == 0.0
    assert not summary.is_flawless


def test_no_negative_points():
    summary = summarize([rec(str(i), False) for i in range(4)])
    assert summary.earned == 0 and summary.maximum == 40


def test_flawless():
    assert summarize([rec("a", True), rec("b", True)]).is_flawless


def test_format_score():
    assert format_score(ScoreSummary(30, 40, 3, 4)) == "30 из 40 (75%)"


def play(mission, choose):
    state = start_attempt(mission)
    while not state.finished:
        scene = current_scene(state, mission)
        if scene.is_decision:
            submit_answer(state, mission, choose(scene))
        advance(state, mission)
    return state


def test_branching_does_not_inflate_maximum(content):
    for mission in content.missions.values():
        good = summarize(play(mission, lambda s: s.preferred_option.id).decisions.values())
        bad = summarize(
            play(mission, lambda s: next(o.id for o in s.options if not o.preferred)).decisions.values()
        )
        assert good.maximum == bad.maximum == 40
        assert good.percent == 100.0
        assert bad.earned == 0


def test_recovery_scenes_add_no_scored_decisions(content):
    mission = content.mission("backup")
    state = play(mission, lambda s: "a")  # "a" leads to bk_r1 at bk_d3
    assert "bk_r1" in state.visited
    assert "bk_r1" not in state.decisions
    assert summarize(state.decisions.values()).scored == 4
