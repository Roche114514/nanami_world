"""Run the town perception experiment against isolated Shinsekai saves."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import persona_bridge, projector, text_compiler
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings

Cell = tuple[int, int]
DAY = datetime(2026, 7, 1, 12).timestamp()
NEXT_DAY = DAY + 86400
ITEM_LIMIT = 6
BYTE_LIMIT = 768


def position(item: dict[str, Any]) -> Cell:
    return item["x"], item["y"]


def distance(a: Cell, b: Cell) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def sight_line(start: Cell, end: Cell, opaque: set[Cell]) -> bool:
    """Trace grid cells, treating an opaque endpoint as itself visible."""
    x, y = start
    tx, ty = end
    dx, dy = abs(tx - x), -abs(ty - y)
    sx, sy = (1 if x < tx else -1), (1 if y < ty else -1)
    error = dx + dy
    while (x, y) != end:
        previous = x, y
        twice = 2 * error
        if twice >= dy:
            error += dy
            x += sx
        if twice <= dx:
            error += dx
            y += sy
        if x != previous[0] and y != previous[1]:
            if (x, previous[1]) in opaque and (previous[0], y) in opaque:
                return False
        if (x, y) != end and (x, y) in opaque:
            return False
    return True


@dataclass(frozen=True)
class SeenObject:
    id: str
    name: str
    cell: Cell
    cue: str = ""


@dataclass
class Observation:
    space_id: str
    origin: Cell
    visible_cells: set[Cell]
    objects: dict[str, SeenObject]
    salience: dict[str, float]

    def focus(self, goal_target: str = "") -> list[SeenObject]:
        ranked = sorted(
            self.objects.values(),
            key=lambda item: (-(self.salience[item.id] + 4 * (item.name == goal_target)), item.id),
        )
        return ranked[:ITEM_LIMIT]

    def render(self, goal_target: str = "") -> tuple[int, str]:
        lines: list[str] = []
        for item in self.focus(goal_target):
            line = f"[{item.id}] {item.name} {item.cell} {item.cue}".strip()
            if len("\n".join([*lines, line]).encode("utf-8")) > BYTE_LIMIT:
                break
            lines.append(line)
        return len(lines), "\n".join(lines)


def observe(engine: WorldEngine, actor_id: str) -> Observation:
    actor = next(actor for actor in engine.list_actors() if actor["actor_id"] == actor_id)
    origin = position(actor)
    sid = actor["space_id"]
    snapshot = engine.snapshot_for_ui(sid)
    space = snapshot["space"]
    doors = {
        position(entity): entity["traits"]["portal"]["is_open"]
        for entity in snapshot["entities"] if "portal" in entity["traits"]
    }
    opaque = {
        (x, y) for x in range(space["width"]) for y in range(space["height"])
        if engine.is_wall_cell(space, x, y) and not doors.get((x, y))
    }
    opaque.update(cell for cell, open_door in doors.items() if not open_door)
    lamps = {position(item) for item in snapshot["lighting"]["lamp_positions"]}
    night = snapshot["lighting"]["period"] == "night"
    radius = 7
    cells: set[Cell] = set()
    for x in range(max(0, origin[0] - radius), min(space["width"], origin[0] + radius + 1)):
        for y in range(max(0, origin[1] - radius), min(space["height"], origin[1] + radius + 1)):
            cell = x, y
            lit = not night or distance(origin, cell) <= 2 or any(
                distance(lamp, cell) <= 3 and sight_line(lamp, cell, opaque) for lamp in lamps
            )
            if lit and sight_line(origin, cell, opaque):
                cells.add(cell)
    objects: dict[str, SeenObject] = {}
    salience: dict[str, float] = {}
    for entity in engine.visible_entities(actor_id):
        held = entity["parent_id"] == f"actor:{actor_id}"
        cell = origin if held else position(entity)
        if cell not in cells:
            continue
        eid = entity["entity_id"]
        cue = ""
        if position(entity) in lamps and "switchable" in entity["traits"]:
            cue = "亮着"
        if distance(origin, cell) <= 2 and "暂停通行" in entity["brief"]:
            cue = "门上贴着暂停通行"
        objects[eid] = SeenObject(eid, entity["name"], cell, cue)
        salience[eid] = 1 / (1 + distance(origin, cell)) + (2 if cue == "亮着" else 0)
    return Observation(sid, origin, cells, objects, salience)


def observe_interaction(
    engine: WorldEngine, actor_id: str, entity_id: str, action: str,
) -> str | None:
    """Experimental action boundary, using the actor's latest observation."""
    if entity_id not in observe(engine, actor_id).objects:
        return None
    return engine.execute_act(actor_id, entity_id, action)


@dataclass(frozen=True)
class ObjectBelief:
    identity: str
    name: str
    space_id: str
    cell: Cell
    observed_at: float
    source: str = "亲眼看见"
    status: str = "last_seen"

    def inspect(self, frame: Observation, at: float) -> ObjectBelief:
        item = frame.objects.get(self.identity)
        if item is not None:
            return ObjectBelief(self.identity, item.name, frame.space_id, item.cell, at)
        if (
            frame.space_id == self.space_id and self.cell in frame.visible_cells
            and distance(frame.origin, self.cell) <= 1
        ):
            return ObjectBelief(self.identity, self.name, self.space_id, self.cell, self.observed_at,
                                status="not_at_last_location")
        return self


@dataclass(frozen=True)
class DeliveryGoal:
    destination: str
    entrance: str
    tea: str
    counter: Cell

    def next_step(self, frame: Observation) -> tuple[str, str]:
        if frame.space_id == self.destination:
            cup = next((item for item in frame.objects.values() if item.name == self.tea), None)
            return ("place", cup.id) if cup else ("look_for_tea", "")
        door = next((item for item in frame.focus(self.entrance) if item.name == self.entrance), None)
        if door is None:
            return "look_for_entrance", ""
        if door.cue == "门上贴着暂停通行":
            return "wait_and_ask", door.id
        return "enter", door.id


def object_id(entity: dict[str, Any]) -> str:
    return entity["entity_id"]


def configure_gate(
    engine: WorldEngine, gate: dict[str, Any], target: str, arrival: Cell, *, opened: bool = False,
) -> None:
    engine.update_entity_spec(object_id(gate), traits={"portal": {
        "target_space_id": target, "target_x": arrival[0], "target_y": arrival[1], "is_open": opened,
    }})


def new_world(root: Path, name: str, from_save: Path | None = None) -> WorldEngine:
    data = root / name
    if from_save is not None:
        shutil.copytree(from_save, data)
    return WorldEngine(code_dir=WORLD_CODE, data_root=data, project_root=root)


def route_fixture(root: Path) -> dict[str, Any]:
    world = new_world(root, "town")
    home = "room_default"
    square = world.create_space(name="广场", width=14, height=10)["space_id"]
    shop = world.create_space(name="工坊", width=14, height=10)["space_id"]
    world.paint_tiles(shop, [{"x": 7, "y": y, "tile_id": "brick_wall"} for y in range(1, 10) if y != 5])
    partition = world.place_from_blueprint("default_door", shop, 7, 5)
    world.update_entity_spec(object_id(partition), name="隔间门")
    table = world.place_from_blueprint("default_desk", shop, 9, 5)
    key = world.place_from_blueprint("default_brass_key", shop, 9, 5)
    assert key["parent_id"] == "entity:" + object_id(table)
    box = world.place_from_blueprint("default_box", shop, 10, 4)
    lamp = world.place_from_blueprint("default_lamp", square, 5, 4)
    home_gate = world.place_from_blueprint("default_door", home, 5, 3)
    square_gate = world.place_from_blueprint("default_door", square, 7, 4)
    world.update_entity_spec(object_id(square_gate), name="工坊入口")
    configure_gate(world, home_gate, square, (5, 5))
    configure_gate(world, square_gate, shop, (5, 5))
    tea = world.place_from_blueprint("default_hot_drink", home, 4, 4)
    world.update_entity_spec(object_id(tea), name="工坊的茶")
    actor = world.import_actor(name="七海千秋", space_id=shop, x=9, y=6)["actor_id"]
    witness = world.import_actor(name="工匠", space_id=shop, x=10, y=6)["actor_id"]
    return {
        "world": world, "actor": actor, "witness": witness, "square": square, "shop": shop,
        "key": object_id(key), "box": object_id(box), "partition": partition, "lamp": object_id(lamp),
        "tea": object_id(tea), "home_gate": object_id(home_gate), "square_gate": square_gate,
        "goal": DeliveryGoal(shop, "工坊入口", "工坊的茶", (5, 6)),
    }


def baseline_visibility(engine: WorldEngine, actor_id: str, eid: str) -> dict[str, Any] | None:
    projection = projector.project_terminal_data(engine, actor_id)
    return next((anchor for anchor in projection["anchors"] if anchor["entity_id"] == eid), None)


def baseline_action(engine: WorldEngine, actor_id: str, root: Path) -> str:
    heartbeat = WorldHeartbeat(engine, WorldSettings, project_root=root)
    result = heartbeat.step(advance_minutes=0.01, use_llm=False, actor_id=actor_id)
    return " / ".join(decision["goal"] for decision in result["decisions"])


def baseline_brain_input(engine: WorldEngine, actor_id: str, root: Path) -> str:
    prompts: list[str] = []

    def capture(_system: str, user: str, **_options: Any) -> None:
        prompts.append(user)

    with patch.object(text_compiler, "_ask_llm_json", side_effect=capture):
        heartbeat = WorldHeartbeat(engine, WorldSettings, project_root=root)
        heartbeat.step(advance_minutes=0.01, use_llm=True, actor_id=actor_id)
    assert len(prompts) == 1
    return prompts[0]


def search_action(frame: Observation, belief: ObjectBelief) -> tuple[str, Cell | None]:
    target = frame.objects.get(belief.identity)
    if target:
        return "inspect_seen_key", target.cell
    if belief.status == "last_seen":
        return "inspect_last_location", belief.cell
    return "ask_where_key_went", None


def trial_visibility_and_memory(root: Path, scene: dict[str, Any]) -> dict[str, Any]:
    world, actor, key = scene["world"], scene["actor"], scene["key"]
    shop, box, partition = scene["shop"], scene["box"], scene["partition"]
    with patch("time.time", return_value=DAY):
        day_one = observe(world, actor)
        item = day_one.objects[key]
        memory = ObjectBelief(key, item.name, shop, item.cell, DAY)
        path = root / "actor_memory.json"
        path.write_text(json.dumps(asdict(memory), ensure_ascii=False), encoding="utf-8")
        world.place_actor(actor, shop, 5, 5)
        closed = observe(world, actor)
        assert key not in closed.objects
        configure_gate(world, partition, "", (5, 5), opened=True)
        assert key in observe(world, actor).objects
        configure_gate(world, partition, "", (5, 5))

    # One copy for each comparison, so scans and actions do not change its starting save.
    baseline = new_world(root, "memory_baseline", world.data_root)
    prototype = new_world(root, "memory_prototype", world.data_root)
    unmoved = new_world(root, "memory_unmoved", world.data_root)
    with patch("time.time", return_value=NEXT_DAY):
        saved = json.loads(path.read_text(encoding="utf-8"))
        memory = ObjectBelief(**{**saved, "cell": tuple(saved["cell"])})
        for branch in (baseline, prototype, unmoved):
            branch.update_entity_spec(box, traits={"container": {"is_open": True, "capacity": 6}})
        for branch in (baseline, prototype):
            moved = branch.move_entity_ui(key, 10, 4, target_parent_id=f"entity:{box}")
            assert moved["parent_id"] == f"entity:{box}"
        baseline_view = baseline_visibility(baseline, actor, key)
        outside = observe(prototype, actor)
        assert baseline_view is not None and position(baseline_view) == (10, 4)
        assert key not in outside.objects
        assert key not in outside.render(memory.name)[1]
        same_view = observe(unmoved, actor)
        assert outside.render(memory.name) == same_view.render(memory.name)
        assert position(baseline_visibility(unmoved, actor, key)) == (9, 5)
        assert key in observe(prototype, scene["witness"]).objects
        assert observe_interaction(prototype, actor, key, "look") is None
        assert memory.inspect(outside, NEXT_DAY) == memory
        assert search_action(outside, memory) == ("inspect_last_location", (9, 5))

        baseline_snapshot = baseline.snapshot_for_ui(shop)
        raw_vis = baseline.visible_entities(actor)
        l0 = persona_bridge.build_percept_frame(
            baseline.data_root, root, baseline_snapshot["space"], baseline_snapshot["lighting"],
            next(person for person in baseline.list_actors() if person["actor_id"] == actor), raw_vis,
            advance_exposure=0.1,
        )
        assert l0["mental_map_count"] > 0
        book = json.loads((baseline.data_root / "perception_state.json").read_text(encoding="utf-8"))
        leaked_to_l0_map = key in book["worlds"][shop][actor]["mental_map"]
        llm_prompt = baseline_brain_input(baseline, actor, root)
        hidden_key_line = next(line for line in llm_prompt.splitlines() if f"[{key}]" in line)
        assert "(10, 4)" in hidden_key_line

        # A closed cabinet controls for container occlusion when inspecting the old tabletop.
        prototype.update_entity_spec(box, traits={"container": {"is_open": False, "capacity": 6}})
        configure_gate(prototype, partition, "", (5, 5), opened=True)
        prototype.execute_move(actor, 8, 5)
        at_table = observe(prototype, actor)
        assert at_table.origin == (8, 5)
        revised = memory.inspect(at_table, NEXT_DAY)
        assert revised.status == "not_at_last_location"
        assert revised.observed_at == DAY
        assert search_action(at_table, revised) == ("ask_where_key_went", None)
        prototype.execute_move(actor, 9, 4)
        assert observe_interaction(prototype, actor, box, "open")
        at_box = observe(prototype, actor)
        rediscovered = revised.inspect(at_box, NEXT_DAY)
        assert rediscovered.cell == (10, 4) and rediscovered.observed_at == NEXT_DAY
        assert search_action(at_box, rediscovered) == ("inspect_seen_key", (10, 4))

    with patch("time.time", return_value=NEXT_DAY + 11 * 3600):
        baseline.place_actor(actor, shop, 5, 5)
        configure_gate(baseline, partition, "", (5, 5), opened=True)
        assert key not in observe(baseline, actor).objects
        lamp = baseline.place_from_blueprint("default_lamp", shop, 9, 4)
        baseline.update_entity_spec(object_id(lamp), traits={"switchable": {"provides_light": True, "is_on": True}})
        assert key in observe(baseline, actor).objects
        configure_gate(baseline, partition, "", (5, 5))
        assert key not in observe(baseline, actor).objects

    return {
        "现有投影与LLM输入隔墙暴露": position(baseline_view),
        "现有认知地图隔墙记入钥匙": leaked_to_l0_map,
        "原型隔墙提示与交互": "均拦截",
        "隔墙挪动钥匙对原型输入的影响": "无变化",
        "原型复查前": {"地点": memory.cell, "状态": memory.status},
        "原型复查后": {"状态": revised.status, "下一步": "询问钥匙去向"},
        "原型打开柜子后": rediscovered.cell,
        "原型夜间": "远处未照亮时不可见，开灯且通视时可见，门关闭后再次遮挡",
    }


def trial_goal(root: Path, scene: dict[str, Any]) -> dict[str, Any]:
    route = new_world(root, "route", scene["world"].data_root)
    aid, tea = scene["actor"], scene["tea"]
    goal = scene["goal"]
    home = "room_default"
    gate = object_id(scene["square_gate"])
    with patch("time.time", return_value=NEXT_DAY):
        route.place_actor(aid, home, 4, 5)
        route.execute_act(aid, tea, "take")
        route.execute_act(aid, scene["home_gate"], "enter")
        assert observe(route, aid).space_id == scene["square"]
        quiet = new_world(root, "quiet", route.data_root)
        bright = new_world(root, "bright", route.data_root)
        blocked = new_world(root, "blocked", route.data_root)
        baseline_quiet = new_world(root, "baseline_quiet", route.data_root)
        baseline_bright = new_world(root, "baseline_bright", route.data_root)
        for lit in (bright, baseline_bright):
            lit.execute_act(aid, scene["lamp"], "turn_on")
            # Keep the observer's position constant across variants.
            lit.execute_move(aid, 5, 5)
        blocked.update_entity_spec(gate, brief="贴着暂停通行的告示", portal_target_space_id="")
        quiet_frame, bright_frame, blocked_frame = (observe(branch, aid) for branch in (quiet, bright, blocked))
        assert bright_frame.focus()[0].id == scene["lamp"]
        assert bright_frame.focus(goal.entrance)[0].id == gate
        assert goal.next_step(quiet_frame) == goal.next_step(bright_frame) == ("enter", gate)
        assert goal.next_step(blocked_frame) == ("wait_and_ask", gate)
        blocked.update_entity_spec(gate, brief="门牌写着工坊", portal_target_space_id=goal.destination)
        assert goal.next_step(observe(blocked, aid)) == ("enter", gate)
        action, target = goal.next_step(bright_frame)
        assert observe_interaction(bright, aid, target, action)
        arrived = observe(bright, aid)
        assert arrived.space_id == goal.destination
        assert goal.next_step(arrived) == ("place", tea)
        assert "放到了" in bright.execute_act(aid, tea, "place", f"{goal.counter[0]},{goal.counter[1]}")
        dropped = next(item for item in bright.visible_entities(aid) if item["entity_id"] == tea)
        assert position(dropped) == goal.counter and dropped["parent_id"] == f"space:{goal.destination}"
        return {
            "现有规则灭灯组": baseline_action(baseline_quiet, aid, root),
            "现有规则亮灯组": baseline_action(baseline_bright, aid, root),
            "原型亮灯最显著物": bright_frame.focus()[0].name,
            "原型送茶目标下的焦点": bright_frame.focus(goal.entrance)[0].name,
            "原型亮灯后的动作": "继续进工坊，茶已放在约定位置",
            "原型入口暂停通行": "等待并询问，恢复通行后继续原目标",
        }


def trial_scale(root: Path, scene: dict[str, Any]) -> dict[str, Any]:
    world = new_world(root, "scale", scene["world"].data_root)
    aid = scene["actor"]
    square, goal = scene["square"], scene["goal"]
    world.place_actor(aid, square, 5, 5)

    def read_size() -> dict[str, int]:
        # Measure static vision with equal local notification state in each variant.
        world.consume_diffs(square)
        existing = projector.project_terminal_data(world, aid)
        count, text = observe(world, aid).render(goal.entrance)
        return {
            "现有条目": len(existing["anchors"]),
            "现有文本字节": len(existing["text"].encode("utf-8")),
            "原型条目": count,
            "原型文本字节": len(text.encode("utf-8")),
        }

    with patch("time.time", return_value=NEXT_DAY):
        original = read_size()
        previous = "room_default"
        for index in range(24):
            sid = world.create_space(name=f"街区{index + 1:02}", width=12, height=9)["space_id"]
            world.place_from_blueprint("default_door", previous, 11, 5, portal_target_space_id=sid)
            world.place_from_blueprint("default_door", sid, 1, 5, portal_target_space_id=previous)
            world.place_from_blueprint("default_desk", sid, 5, 3)
            previous = sid
        extended = read_size()
        assert extended == original, (original, extended)
        assert len(world.snapshot_for_ui(square)["spaces"]) == 27
        for x in range(2, 7):
            for y in range(6, 9):
                world.place_from_blueprint("default_lamp", square, x, y)
        dense = read_size()
        assert dense["现有条目"] == original["现有条目"] + 15
        assert dense["原型条目"] == ITEM_LIMIT and dense["原型文本字节"] <= BYTE_LIMIT
        return {"原始三场景": original, "接入24个远处街区后": extended, "本地加15件物品后": dense}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nanami-town-experiment-") as folder:
        root = Path(folder)
        with patch("time.time", return_value=DAY):
            scene = route_fixture(root)
        results = {
            "视野与记忆": trial_visibility_and_memory(root, scene),
            "目标延续": trial_goal(root, scene),
            "规模": trial_scale(root, scene),
        }
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
