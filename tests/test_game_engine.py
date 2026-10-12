import copy
import json

import pytest

from game_engine import (
    MISSIONS_PATH,
    REFERENCES_PATH,
    ContentError,
    EngineError,
    advance,
    current_scene,
    parse_content,
    restore_attempt,
    start_attempt,
    submit_answer,
    validate_content,
)

EXPECTED_TITLES = [
    "Подозрительное письмо",
    "Защита аккаунта",
    "Неожиданное вложение",
    "Обновление системы",
    "Резервная копия",
    "Работа с данными",
]


def raw_content():
    missions = json.loads(MISSIONS_PATH.read_text(encoding="utf-8"))
    references = json.loads(REFERENCES_PATH.read_text(encoding="utf-8"))
    return missions, references


def all_paths(mission):
    """Enumerate every scene path from start to an ending (content is a DAG)."""
    paths = []

    def walk(scene_id, path):
        path = path + [scene_id]
        for target in set(mission.successors(scene_id)):
            if target is None:
                paths.append(path)
            else:
                walk(target, path)

    walk(mission.start_scene, [])
    return paths


def test_six_missions_with_expected_titles(content):
    assert [m.title for m in content.missions.values()] == EXPECTED_TITLES


def test_content_has_no_validation_errors(content):
    assert validate_content(content) == []


def test_mission_and_scene_ids_are_unique():
    missions, _ = raw_content()
    mission_ids = [m["id"] for m in missions["missions"]]
    assert len(mission_ids) == len(set(mission_ids))
    scene_ids = [s["id"] for m in missions["missions"] for s in m["scenes"]]
    assert len(scene_ids) == len(set(scene_ids))


def test_decisions_have_required_fields(content):
    for mission in content.missions.values():
        decisions = [s for s in mission.scenes.values() if s.is_decision]
        assert len(decisions) >= 4, mission.id
        for scene in decisions:
            assert 3 <= len(scene.options) <= 4
            assert sum(o.preferred for o in scene.options) == 1
            assert scene.situation and scene.explanation and scene.recommendation
            assert scene.reference_id in content.references
            assert len({o.id for o in scene.options}) == len(scene.options)
            assert all(o.consequence for o in scene.options)


def test_all_references_point_to_existing_scenes(content):
    for mission in content.missions.values():
        assert mission.start_scene in mission.scenes
        for scene_id in mission.scenes:
            for target in mission.successors(scene_id):
                assert target is None or target in mission.scenes


def test_every_path_terminates_with_at_least_four_decisions(content):
    for mission in content.missions.values():
        paths = all_paths(mission)
        assert paths, mission.id
        for path in paths:
            assert len(path) == len(set(path)), f"cycle in {mission.id}: {path}"
            decisions = [s for s in path if mission.scenes[s].is_decision]
            assert len(decisions) >= 4


def test_all_scenes_reachable(content):
    for mission in content.missions.values():
        reached = {s for path in all_paths(mission) for s in path}
        assert reached == set(mission.scenes), mission.id


def test_each_mission_has_meaningful_branch(content):
    for mission in content.missions.values():
        branching = []
        for scene in mission.scenes.values():
            if not scene.is_decision:
                continue
            preferred_next = scene.preferred_option.next
            risky_targets = {o.next for o in scene.options if not o.preferred}
            if preferred_next and mission.scenes[preferred_next].type == "verification" and any(
                t and mission.scenes[t].type == "recovery" for t in risky_targets
            ):
                branching.append(scene.id)
        assert branching, f"{mission.id} has no recovery/verification branch"
        # Consequences differ per option, so the branch is not identical text.
        for scene_id in branching:
            consequences = [o.consequence for o in mission.scenes[scene_id].options]
            assert len(set(consequences)) == len(consequences)


def test_cycle_is_rejected():
    missions, references = raw_content()
    broken = copy.deepcopy(missions)
    last = broken["missions"][0]["scenes"][-1]
    for option in last["options"]:
        option["next"] = broken["missions"][0]["start_scene"]
    with pytest.raises(ContentError, match="цикл"):
        parse_content(broken, references)


def test_unreachable_scene_is_rejected():
    missions, references = raw_content()
    broken = copy.deepcopy(missions)
    extra = copy.deepcopy(broken["missions"][0]["scenes"][1])
    extra["id"] = "orphan_scene"
    broken["missions"][0]["scenes"].append(extra)
    with pytest.raises(ContentError, match="недостижимые"):
        parse_content(broken, references)


def test_missing_scene_reference_is_rejected():
    missions, references = raw_content()
    broken = copy.deepcopy(missions)
    broken["missions"][0]["scenes"][0]["options"][0]["next"] = "does_not_exist"
    with pytest.raises(ContentError, match="несуществующую"):
        parse_content(broken, references)


def test_two_preferred_answers_rejected():
    missions, references = raw_content()
    broken = copy.deepcopy(missions)
    for option in broken["missions"][0]["scenes"][0]["options"]:
        option["preferred"] = True
    with pytest.raises(ContentError, match="ровно один"):
        parse_content(broken, references)


def test_selected_option_determines_consequence_and_next_scene(content):
    mission = content.mission("phishing_email")
    state = start_attempt(mission)
    result = submit_answer(state, mission, "b")
    assert result.option.consequence == mission.scene("pe_d1").option("b").consequence
    assert not result.record.is_correct and result.record.points == 0
    assert advance(state, mission).id == "pe_d2"


def test_risky_answer_leads_to_recovery_then_rejoins(content):
    mission = content.mission("phishing_email")
    state = start_attempt(mission)
    submit_answer(state, mission, "a")
    scene = advance(state, mission)
    assert scene.id == "pe_r1" and scene.type == "recovery"
    with pytest.raises(EngineError):
        submit_answer(state, mission, "a")  # recovery scenes are not scored
    assert advance(state, mission).id == "pe_d2"
    assert list(state.decisions) == ["pe_d1"]


def test_preferred_answer_leads_to_verification(content):
    mission = content.mission("phishing_email")
    state = start_attempt(mission)
    result = submit_answer(state, mission, "c")
    assert result.record.is_correct and result.record.points == 10
    assert advance(state, mission).type == "verification"
    assert advance(state, mission).id == "pe_d2"


def test_duplicate_submission_does_not_change_answer(content):
    mission = content.mission("account_security")
    state = start_attempt(mission)
    first = submit_answer(state, mission, "b")
    second = submit_answer(state, mission, "a")
    assert first.newly_recorded and not second.newly_recorded
    assert second.record == first.record
    assert len(state.decisions) == 1


def test_cannot_advance_without_answer(content):
    mission = content.mission("backup")
    state = start_attempt(mission)
    with pytest.raises(EngineError):
        advance(state, mission)


def test_full_playthrough_finishes(content):
    for mission in content.missions.values():
        state = start_attempt(mission)
        steps = 0
        while not state.finished:
            scene = current_scene(state, mission)
            if scene.is_decision:
                submit_answer(state, mission, scene.preferred_option.id)
            advance(state, mission)
            steps += 1
            assert steps < 50
        assert all(d.is_correct for d in state.decisions.values())
        with pytest.raises(EngineError):
            advance(state, mission)


def test_restore_attempt_resumes_at_last_visited_scene(content):
    mission = content.mission("phishing_email")
    state = start_attempt(mission, attempt_id=7)
    record = submit_answer(state, mission, "a").record
    advance(state, mission)
    restored = restore_attempt(mission, 7, list(state.visited), [record], finished=False)
    assert restored.current_scene_id == "pe_r1"
    assert restored.decisions == {"pe_d1": record}
