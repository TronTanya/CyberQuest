from game_view import load_world, validate_world, world_payload


def test_world_loads_and_matches_missions(content):
    world = load_world()
    assert validate_world(world, set(content.missions)) == []
    assert len(world.terminals) == 6
    assert {t.mission_id for t in world.terminals.values()} == set(content.missions)


def test_spawn_is_walkable():
    world = load_world()
    sx, sy = int(world.spawn[0]), int(world.spawn[1])
    assert world.tile_at(sx, sy) in {".", "S"}
    assert not world.is_blocked(sx, sy) or world.tile_at(sx, sy) == "S"


def test_spawn_near_each_mission():
    world = load_world()
    for terminal in world.terminals.values():
        x, y = world.spawn_near(terminal.mission_id)
        assert abs(x - terminal.x) < 0.01
        assert y > terminal.y
        assert not world.is_blocked(int(x), int(y))


def test_payload_contains_map_and_terminals(content):
    world = load_world()
    payload = world_payload(world, completed=["backup"], best_scores={"backup": 100.0})
    assert payload["world"]["width"] == world.width
    assert len(payload["world"]["tiles"]) == world.width * world.height
    assert len(payload["world"]["terminals"]) == 6
    assert payload["completed"] == ["backup"]
    assert payload["best_scores"]["backup"] == 100.0
