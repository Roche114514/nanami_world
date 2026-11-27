"""Standalone tests for the world builder actions and its shared undo history."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WORLD_DIR = Path(__file__).resolve().parents[1]
PLUGINS_PARENT = WORLD_DIR.parent
if str(PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_PARENT))

from nanami_world import projector
from nanami_world.engine import WorldEngine
from nanami_world.schema import (
    MAX_ELEVATION,
    MIN_SPACE_HEIGHT,
    MIN_SPACE_WIDTH,
    normalize_space,
    normalize_space_cliff_styles,
    normalize_space_stair_styles,
)


class Step1WorldBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_root = Path(tmp.name) / "data"
        self.engine = WorldEngine(code_dir=WORLD_DIR, data_root=self.data_root)
        self.sid = self.engine.snapshot_for_ui()["active_space_id"]

    def snapshot(self, sid: str = "") -> dict:
        return self.engine.snapshot_for_ui(sid or self.sid)

    def space(self, sid: str = "") -> dict:
        return self.snapshot(sid)["space"]

    def entities(self, sid: str = "") -> dict:
        return {entity["entity_id"]: entity for entity in self.snapshot(sid)["entities"]}

    def paint(self, tile: str, *points: tuple[int, int], sid: str = "", **kwargs) -> dict:
        return self.engine.paint_tiles(
            sid or self.sid, [{"x": x, "y": y, "tile_id": tile} for x, y in points], **kwargs,
        )

    def relief(self, mode: str, *points: tuple[int, int], sid: str = "", **options) -> dict:
        return self.engine.paint_terrain_relief(
            sid or self.sid, [{"x": x, "y": y} for x, y in points], mode=mode, **options,
        )

    def test_relief_modes_and_step_orientation(self) -> None:
        for mode, expected in (
            ("raise", 1), ("raise", 2), ("raise", 3), ("lower", 2),
            ("set_0", 0), ("lower", 0), ("set_2", 2), ("set_1", 1),
        ):
            with self.subTest(mode=mode, expected=expected):
                self.relief(mode, (4, 4), (4, 4))
                space = self.space()
                self.assertEqual(self.engine.get_cell_elevation(space, 4, 4), expected)
                self.assertEqual("4,4" in space["elevations"], expected != 0)

        for mode, ns, ew in (
            ("stair_ns", True, False), ("stair_ew", False, True),
            ("stair_both", True, True), ("clear_stair", False, False),
        ):
            with self.subTest(mode=mode):
                self.relief(mode, (4, 4))
                space = self.space()
                self.assertEqual(self.engine.can_step_elevation(space, 4, 4, 4, 5), ns)
                self.assertEqual(self.engine.can_step_elevation(space, 4, 4, 5, 4), ew)
                self.assertEqual(space["stairs"].get("4,4"), None if mode == "clear_stair" else mode[6:])
        self.relief("set_2", (4, 4))
        self.relief("stair_both", (4, 4))
        self.assertFalse(self.engine.can_step_elevation(self.space(), 4, 4, 5, 4))

    def test_high_elevation_cap_and_multi_step_traversal(self) -> None:
        self.assertEqual(MAX_ELEVATION, 16)
        old_space = normalize_space({"elevations": {"1,2": 80, "2,2": -4, "3,2": 0}})
        self.assertEqual(old_space["elevations"], {"1,2": MAX_ELEVATION})
        self.assertEqual(old_space["cliff_styles"], {})
        self.assertEqual(old_space["stair_styles"], {})

        self.relief("set_level", (4, 2), level=MAX_ELEVATION + 5)
        self.assertEqual(self.relief("raise", (4, 2))["painted"], 0)
        self.assertEqual(self.space()["elevations"]["4,2"], 16)
        self.relief("lower", (4, 2))
        self.relief("set_level", (5, 2), level=16)
        self.relief("stair_ew", (5, 2))
        self.assertTrue(self.engine.can_step_elevation(self.space(), 4, 2, 5, 2))
        self.relief("lower", (4, 2))
        self.assertFalse(self.engine.can_step_elevation(self.space(), 4, 2, 5, 2))
        self.relief("set_level", (4, 2), level=-3)
        self.assertNotIn("4,2", self.space()["elevations"])

        for level in range(1, 5):
            self.relief("set_level", (2 + level, 4), level=level)
            self.relief("stair_ew", (2 + level, 4))
        player = self.engine.ensure_player_avatar(space_id=self.sid)
        aid = player["actor_id"]
        self.engine.place_actor(aid, self.sid, 2, 4)
        for level in range(1, 5):
            step = self.engine.step_player_move(aid, 1, 0, space_id=self.sid)
            self.assertTrue(step["moved"])
            self.assertEqual((step["x"], step["y"]), (2 + level, 4))
            self.assertEqual(self.engine.get_cell_elevation(self.space(), step["x"], step["y"]), level)
        self.assertFalse(self.engine.step_player_move(aid, 0, 1, space_id=self.sid)["moved"])

    def test_material_normalization_persistence_and_automatic_styles(self) -> None:
        raw = {" 3, 2 ": " WOOD ", "4,2": "auto", "5,2": "", "6,2": "unknown", "12,0": "wood", "-1,2": "wood"}
        self.assertEqual(normalize_space_cliff_styles(raw, 10, 8), {"3,2": "wood"})
        self.assertEqual(normalize_space_stair_styles({**raw, "5,2": "waterfall"}, 10, 8), {"3,2": "wood"})

        self.relief("set_level", (3, 3), level=11, cliff_style="earth")
        self.assertEqual(self.relief("set_cliff", (3, 3), (4, 3), (4, 0), cliff_style="brick")["skipped_cells"], [[4, 3], [4, 0]])
        self.relief("stair_both", (3, 3), stair_style="stone")
        self.assertEqual(self.relief("stair_both", (3, 3), stair_style="wood")["painted"], 1)
        self.relief("raise", (3, 3))
        self.assertEqual(self.relief("stair_both", (3, 3))["painted"], 0)
        persisted = WorldEngine(code_dir=WORLD_DIR, data_root=self.data_root).snapshot_for_ui(self.sid)["space"]
        self.assertEqual(persisted["cliff_styles"], {"3,3": "brick"})
        self.assertEqual(persisted["stair_styles"], {"3,3": "wood"})
        self.assertEqual(persisted["elevations"], {"3,3": 12})
        self.assertEqual(persisted["stairs"], {"3,3": "both"})

        self.relief("set_cliff", (3, 3), cliff_style="auto")
        self.relief("stair_both", (3, 3), stair_style="")
        self.assertFalse(self.space()["cliff_styles"])
        self.assertFalse(self.space()["stair_styles"])
        self.assertEqual(self.space()["elevations"], {"3,3": 12})
        self.assertEqual(self.space()["stairs"], {"3,3": "both"})
        self.engine.undo_build(self.sid)
        self.engine.undo_build(self.sid)
        self.assertEqual(self.space(), persisted)
        self.engine.redo_build(self.sid)
        self.assertFalse(self.space()["cliff_styles"])
        self.assertEqual(self.space()["stair_styles"], {"3,3": "wood"})

    def test_grounding_stair_erasure_and_void_clear_materials(self) -> None:
        self.relief("raise", (4, 4), cliff_style="sand")
        self.relief("stair_ns", (4, 4), stair_style="brick")
        before = self.space()
        self.relief("lower", (4, 4), cliff_style="sand")
        self.assertEqual(self.space()["elevations"], {})
        self.assertEqual(self.space()["cliff_styles"], {})
        self.assertEqual(self.space()["stair_styles"], {"4,4": "brick"})
        self.relief("clear_stair", (4, 4), stair_style="brick")
        self.assertEqual(self.space()["stairs"], {})
        self.assertEqual(self.space()["stair_styles"], {})
        self.engine.undo_build(self.sid)
        self.engine.undo_build(self.sid)
        self.assertEqual(self.space(), before)

        self.paint("void", (4, 4))
        for field in ("elevations", "stairs", "cliff_styles", "stair_styles"):
            self.assertEqual(self.space()[field], {})
        self.engine.undo_build(self.sid)
        self.assertEqual(self.space(), before)
        self.engine.redo_build(self.sid)
        self.assertEqual(self.space()["tiles"]["4,4"], "void")
        self.assertEqual(self.space()["cliff_styles"], {})
        self.assertEqual(self.space()["stair_styles"], {})

    def test_relief_guardrails_match_placement(self) -> None:
        self.paint("void", (2, 2))
        self.paint("wood_wall", (3, 2))
        wide = self.engine.save_blueprint({"blueprint_id": "wide_seat", "name": "双格座", "footprint": [2, 1], "placement": "floor", "solid": False})
        solid = self.engine.save_blueprint({"blueprint_id": "blocking_rock", "name": "挡路石", "footprint": [1, 1], "placement": "any", "solid": True})
        self.engine.place_from_blueprint(wide["blueprint_id"], self.sid, 5, 4)
        self.engine.place_from_blueprint(solid["blueprint_id"], self.sid, 7, 4)

        invalid = [(-1, 4), (2, 2), (3, 2), (4, 0), (5, 4), (6, 4)]
        res = self.relief("set_2", *invalid, (4, 4))
        self.assertEqual(res["skipped_cells"], [list(point) for point in invalid])
        self.assertEqual(self.space()["elevations"], {"4,4": 2})

        invalid += [(7, 4)]
        res = self.relief("stair_both", *invalid, (4, 5))
        self.assertEqual(res["skipped_cells"], [list(point) for point in invalid])
        self.assertEqual(self.space()["stairs"], {"4,5": "both"})
        with self.assertRaisesRegex(ValueError, "台阶通道"):
            self.engine.place_from_blueprint(solid["blueprint_id"], self.sid, 4, 5)
        with self.assertRaisesRegex(ValueError, "不同高度"):
            self.engine.place_from_blueprint(wide["blueprint_id"], self.sid, 3, 4)

    def test_unified_history_across_build_operations(self) -> None:
        other = self.engine.create_space(name="Other", switch_active=False)["space_id"]
        self.paint("stone_tile", (3, 3))
        self.relief("raise", (3, 3))
        self.relief("stair_ew", (3, 3))
        # An unrestricted decoration can share a stair cell.
        bp = self.engine.save_blueprint({"name": "小饰品", "placement": "any", "solid": False})
        entity_id = self.engine.place_from_blueprint(bp["blueprint_id"], self.sid, 3, 3)["entity_id"]
        self.engine.move_entity_ui(entity_id, 4, 3)
        self.engine.update_entity_spec(entity_id, name="新饰品")
        self.engine.remove_entity(entity_id)
        self.assertTrue(self.snapshot()["build_history"]["can_undo"])
        self.assertFalse(self.snapshot(other)["build_history"]["can_undo"])

        self.engine.undo_build(self.sid)
        self.assertEqual(self.entities()[entity_id]["name"], "新饰品")
        self.engine.undo_build(self.sid)
        self.assertEqual(self.entities()[entity_id]["name"], "小饰品")
        self.engine.undo_build(self.sid)
        self.assertEqual(self.entities()[entity_id]["x"], 3)
        self.engine.undo_build(self.sid)
        self.assertNotIn(entity_id, self.entities())
        self.engine.redo_build(self.sid)
        self.assertIn(entity_id, self.entities())
        self.assertEqual(self.snapshot()["build_history"]["redo_label"], "移动物品")

        result = self.paint("void", (3, 3))
        self.assertEqual(result["removed"], [entity_id])
        self.assertFalse(self.snapshot()["build_history"]["can_redo"])
        self.assertEqual(self.space()["elevations"], {})
        self.assertEqual(self.space()["stairs"], {})
        self.engine.undo_build(self.sid)
        self.assertEqual(self.space()["tiles"]["3,3"], "stone_tile")
        self.assertEqual(self.space()["elevations"]["3,3"], 1)
        self.assertEqual(self.space()["stairs"]["3,3"], "ew")
        self.assertIn(entity_id, self.entities())
        self.engine.redo_build(self.sid)
        self.assertNotIn(entity_id, self.entities())
        self.assertEqual(self.space()["tiles"]["3,3"], "void")

        self.engine.undo_build(self.sid)
        self.paint("", (3, 3), demolish=True)
        self.assertNotIn(entity_id, self.entities())
        self.engine.undo_build(self.sid)
        self.assertIn(entity_id, self.entities())
        for _ in range(4):
            self.engine.undo_build(self.sid)
        self.assertEqual(self.space()["tiles"], {})
        self.assertEqual(self.space()["elevations"], {})
        for _ in range(4):
            self.engine.redo_build(self.sid)
        self.assertEqual(self.space()["stairs"], {"3,3": "ew"})
        self.assertIn(entity_id, self.entities())

    def test_history_cap_and_no_op(self) -> None:
        for step in range(42):
            self.relief("set_1" if step % 2 == 0 else "set_2", (5, 3))
        self.relief("set_2", (5, 3))
        for _ in range(40):
            self.engine.undo_build(self.sid)
        self.assertFalse(self.snapshot()["build_history"]["can_undo"])
        self.assertEqual(self.space()["elevations"]["5,3"], 2)
        for _ in range(40):
            self.engine.redo_build(self.sid)
        self.assertFalse(self.snapshot()["build_history"]["can_redo"])

    def test_zones_persist_and_project_inside_linked_space(self) -> None:
        sid = self.engine.create_space(name="Workshop", parent_space_id=self.sid, switch_active=False)["space_id"]
        actor = self.engine.import_actor(name="七海千秋", space_id=sid, x=4, y=4)
        actor_id = actor["actor_id"]
        zone = self.engine.upsert_space_zone(sid, {
            "name": "阅读角", "x": 3, "y": 3, "w": 3, "h": 3, "color": "#abcdef",
            "description": "靠窗阅读的位置", "enclosed": True,
        })["zone"]
        self.assertEqual(self.engine.get_zone_at(self.space(sid), 4, 4), zone)
        self.assertIn("阅读角（靠窗阅读的位置）", projector.project_terminal_data(self.engine, actor_id)["text"])
        self.assertEqual(self.space(sid)["zones"], [zone])

        updated = self.engine.upsert_space_zone(sid, {"zone_id": zone["zone_id"], "name": "自习区"})["zone"]
        self.assertEqual((updated["color"], updated["enclosed"]), ("#abcdef", True))
        self.assertIn("自习区", projector.project_terminal_data(self.engine, actor_id)["text"])
        self.engine.delete_space_zone(sid, zone["zone_id"])
        self.assertIsNone(self.engine.get_zone_at(self.space(sid), 4, 4))
        self.assertNotIn("自习区", projector.project_terminal_data(self.engine, actor_id)["text"])
        self.engine.undo_build(sid)
        self.assertEqual(self.space(sid)["zones"], [updated])
        self.engine.undo_build(sid)
        self.assertEqual(self.space(sid)["zones"], [zone])
        self.engine.redo_build(sid)
        self.assertEqual(self.space(sid)["zones"], [updated])
        reloaded = WorldEngine(code_dir=WORLD_DIR, data_root=self.data_root)
        self.assertEqual(reloaded.snapshot_for_ui(sid)["space"]["zones"], [updated])

    def assert_portal_pair(self, result: dict) -> None:
        sid, target = result["space_id"], result["target_space_id"]
        departing = self.entities(sid)[result["entity_id"]]
        returning = self.entities(target)[result["return_entity_id"]]
        for entry, peer, destination in ((departing, returning, target), (returning, departing, sid)):
            trait = entry["traits"]["portal"]
            self.assertEqual(trait["target_space_id"], destination)
            self.assertEqual(trait["paired_entity_id"], peer["entity_id"])
            self.assertLessEqual(abs(trait["target_x"] - peer["x"]) + abs(trait["target_y"] - peer["y"]), 1)
            self.assertTrue(self.engine.get_cell_tile(self.space(destination), trait["target_x"], trait["target_y"])["walkable"])

    def test_link_existing_portal_and_actor_round_trip(self) -> None:
        target = self.engine.create_space(name="Existing", switch_active=False)["space_id"]
        original = self.engine.place_from_blueprint("default_door", self.sid, 4, 3)
        actor = self.engine.import_actor(name="七海千秋", space_id=self.sid, x=4, y=4)
        result = self.engine.link_portal_bidirectional(entity_id=original["entity_id"], target_space_id=target)
        self.assert_portal_pair(result)
        actor_id = actor["actor_id"]
        self.assertEqual(self.engine.enter_portal(result["entity_id"], actor_id=actor_id)["target_space_id"], target)
        self.assertEqual(self.engine.enter_portal(result["return_entity_id"], actor_id=actor_id)["target_space_id"], self.sid)
        self.engine.undo_build(target)
        self.assertEqual(self.entities()[original["entity_id"]]["traits"]["portal"]["target_space_id"], "")
        self.assertNotIn(result["return_entity_id"], self.entities(target))
        self.engine.redo_build(self.sid)
        self.assert_portal_pair(result)

    def test_link_new_room_and_building_floors(self) -> None:
        room = self.engine.link_portal_bidirectional(
            self.sid, blueprint_id="wood_door", x=0, y=0, mode="new_room", new_space_name="Library",
            width=11, height=8, palette_id="warm_stardew",
        )
        self.assert_portal_pair(room)
        new_room = self.space(room["target_space_id"])
        self.assertEqual((new_room["name"], new_room["width"], new_room["height"]), ("Library", 11, 8))
        self.assertEqual(new_room["parent_space_id"], self.sid)
        self.assertEqual(self.engine.enter_portal(room["return_entity_id"], observe_only=True)["target_space_id"], self.sid)
        self.engine.undo_build(room["target_space_id"])
        self.assertFalse(self.engine.has_space(room["target_space_id"]))
        self.engine.redo_build(self.sid)
        self.assert_portal_pair(room)
        actor = self.engine.import_actor(name="Scout", space_id=self.sid, x=0, y=1)
        aid = actor["actor_id"]
        outbound = self.engine.enter_portal(room["entity_id"], actor_id=aid)
        returning = self.engine.enter_portal(room["return_entity_id"], actor_id=aid)
        self.assertEqual((outbound["x"], outbound["y"]), (0, 1))
        self.assertEqual((returning["x"], returning["y"]), (0, 1))

        town = self.engine.create_space(name="Town", space_kind="town", switch_active=False)["space_id"]
        source = self.engine.create_space(name="Shop", parent_space_id=town, switch_active=False)["space_id"]
        self.engine.place_actor(aid, source, 4, 3)
        for floor, x, label in ((-1, 4, "B1"), (2, 6, "2F")):
            result = self.engine.link_portal_bidirectional(source, x=x, y=4, mode="new_floor", floor_index=floor)
            self.assert_portal_pair(result)
            target = self.space(result["target_space_id"])
            self.assertEqual((target["space_kind"], target["floor_index"], target["floor_label"]), ("building", floor, label))
            self.assertEqual(target["parent_space_id"], source)
            self.assertEqual(target["building_group_id"], self.space(source)["building_group_id"])
            self.assertEqual(self.entities(source)[result["entity_id"]]["blueprint_id"], "default_staircase")
            self.assertEqual(self.engine.enter_portal(result["entity_id"], actor_id=aid)["target_space_id"], target["space_id"])
            self.assertEqual(self.engine.enter_portal(result["return_entity_id"], actor_id=aid)["target_space_id"], source)
        self.assertEqual(self.space(source)["space_kind"], "building")
        self.assertEqual(self.space(source)["parent_space_id"], town)
        self.assertEqual([floor["floor_label"] for floor in self.snapshot(source)["building_floors"]], ["2F", "1F", "B1"])
        with self.assertRaisesRegex(ValueError, "已经存在"):
            self.engine.link_portal_bidirectional(source, x=8, y=4, mode="new_floor", floor_index=2)

    def test_portal_move_and_removal_update_the_return_connection(self) -> None:
        target = self.engine.create_space(name="Target", switch_active=False)["space_id"]
        pair = self.engine.link_portal_bidirectional(self.sid, x=4, y=3, target_space_id=target)
        eid, return_id = pair["entity_id"], pair["return_entity_id"]
        self.engine.move_entity_ui(eid, 7, 3)
        self.assert_portal_pair(pair)
        self.assertEqual(self.entities(target)[return_id]["traits"]["portal"]["target_x"], 7)
        self.engine.undo_build(target)
        self.assertEqual(self.entities(target)[return_id]["traits"]["portal"]["target_x"], 4)
        self.engine.redo_build(self.sid)

        self.engine.remove_entity(eid)
        portal = self.entities(target)[return_id]["traits"]["portal"]
        self.assertEqual(portal["target_space_id"], "")
        self.assertNotIn("paired_entity_id", portal)
        self.engine.undo_build(target)
        self.assert_portal_pair(pair)

    def test_link_conflict_does_not_create_source_or_history(self) -> None:
        target = self.engine.create_space(name="Void", width=8, height=6, switch_active=False)["space_id"]
        self.paint("void", *((x, y) for x in range(8) for y in range(6)), sid=target)
        with self.assertRaisesRegex(ValueError, "回程"):
            self.engine.link_portal_bidirectional(self.sid, x=4, y=4, target_space_id=target)
        self.assertFalse(self.entities())
        self.assertFalse(self.snapshot()["build_history"]["can_undo"])

    def test_cross_space_history_and_relink(self) -> None:
        target = self.engine.create_space(name="Target", switch_active=False)["space_id"]
        other = self.engine.create_space(name="Other", switch_active=False)["space_id"]
        old = self.engine.link_portal_bidirectional(self.sid, x=4, y=3, target_space_id=target)
        new = self.engine.link_portal_bidirectional(entity_id=old["entity_id"], target_space_id=other)
        self.assert_portal_pair(new)
        self.assertEqual(self.entities(target)[old["return_entity_id"]]["traits"]["portal"]["target_space_id"], "")
        self.relief("raise", (2, 3), sid=other)
        self.assertFalse(self.snapshot()["build_history"]["can_undo"])
        self.engine.undo_build(other)
        self.engine.undo_build(self.sid)
        self.assert_portal_pair(old)
        self.assertNotIn(new["return_entity_id"], self.entities(other))
        self.engine.redo_build(target)
        self.assert_portal_pair(new)
        self.engine.redo_build(other)
        self.assertEqual(self.space(other)["elevations"], {"2,3": 1})

    def test_trim_shifts_geometry_and_inbound_portal_atomically(self) -> None:
        sid = self.engine.create_space(name="Trim", width=16, height=12, buildings=[{
            "x": 4, "y": 3, "w": 2, "h": 2, "door_x": 4, "door_y": 4,
        }], switch_active=False)["space_id"]
        self.assertFalse(self.engine.trim_space_void(sid)["trimmed"])
        self.paint("void", *((x, y) for x in range(16) for y in range(12)
                             if not (4 <= x <= 9 and 3 <= y <= 7)), sid=sid)
        self.paint("stone_tile", (4, 4), sid=sid)
        self.relief("set_level", (5, 4), sid=sid, level=10, cliff_style="stone")
        self.relief("stair_ew", (5, 4), sid=sid, stair_style="wood")
        zone = self.engine.upsert_space_zone(sid, {"name": "花园", "x": 4, "y": 3, "w": 6, "h": 5})["zone"]
        entity = self.engine.place_from_blueprint("default_lamp", sid, 6, 4)
        actor = self.engine.import_actor(name="七海千秋", space_id=sid, x=7, y=5)
        inbound = self.engine.place_from_blueprint("default_door", self.sid, 2, 3, portal_target_space_id=sid)
        actor_id, entity_id, inbound_id = actor["actor_id"], entity["entity_id"], inbound["entity_id"]
        self.engine.update_entity_spec(inbound_id, traits={"portal": {"target_space_id": sid, "target_x": 0, "target_y": 11}})
        before = self.space(sid)

        result = self.engine.trim_space_void(sid)
        self.assertEqual((result["width"], result["height"]), (MIN_SPACE_WIDTH, MIN_SPACE_HEIGHT))
        ox, oy = result["offset"]
        space = self.space(sid)
        self.assertGreater(ox, 0)
        self.assertGreater(oy, 0)
        self.assertEqual(space["tiles"][f"{4 - ox},{4 - oy}"], "stone_tile")
        self.assertEqual(self.engine.get_cell_tile(space, 8 - ox, 6 - oy)["tile_id"], "wood_floor")
        shifted = f"{5 - ox},{4 - oy}"
        self.assertEqual(space["elevations"], {shifted: 10})
        self.assertEqual(space["stairs"], {shifted: "ew"})
        self.assertEqual(space["cliff_styles"], {shifted: "stone"})
        self.assertEqual(space["stair_styles"], {shifted: "wood"})
        self.assertEqual(space["zones"][0], {**zone, "x": 4 - ox, "y": 3 - oy})
        self.assertEqual(space["buildings"][0]["door_y"], 4 - oy)
        self.assertEqual(self.entities(sid)[entity_id]["x"], 6 - ox)
        trimmed_actors = {a["actor_id"]: a for a in self.snapshot(sid)["actors"]}
        self.assertEqual((trimmed_actors[actor_id]["x"], trimmed_actors[actor_id]["y"]), (7 - ox, 5 - oy))
        pt = self.entities()[inbound_id]["traits"]["portal"]
        self.assertEqual((pt["target_x"], pt["target_y"]), (0, MIN_SPACE_HEIGHT - 1))

        self.engine.undo_build(self.sid)
        self.assertEqual(self.space(sid), before)
        self.assertEqual(self.entities()[inbound_id]["traits"]["portal"]["target_y"], 11)
        self.assertEqual(self.entities(sid)[entity_id]["x"], 6)
        self.assertEqual(next(a["y"] for a in self.snapshot(sid)["actors"] if a["actor_id"] == actor_id), 5)
        self.engine.redo_build(sid)
        self.assertEqual(self.space(sid), space)

    def test_trim_preserves_geometry_on_an_explicit_void_cell(self) -> None:
        sid = self.engine.create_space(name="Trim markers", width=16, height=10, buildings=[{
            "x": 12, "y": 3, "w": 2, "h": 2, "door_x": 12, "door_y": 4,
        }], switch_active=False)["space_id"]
        self.engine.upsert_space_zone(sid, {"name": "入口", "x": 2, "y": 3, "w": 1, "h": 1})
        self.paint("void", *((x, y) for x in range(16) for y in range(10) if (x, y) != (8, 5)), sid=sid)
        trim = self.engine.trim_space_void(sid, margin=1)
        self.assertEqual((trim["width"], trim["height"]), (14, MIN_SPACE_HEIGHT))
        x, y = trim["offset"]
        self.assertEqual(x, 1)
        self.assertEqual(self.space(sid)["zones"][0]["x"], 2 - x)
        self.assertEqual(self.space(sid)["buildings"][0]["door_y"], 4 - y)

    def test_redo_void_waits_for_actor_to_leave(self) -> None:
        self.paint("void", (4, 4))
        self.engine.undo_build(self.sid)
        actor = self.engine.import_actor(name="七海千秋", space_id=self.sid, x=4, y=4)
        self.assertFalse(self.snapshot()["build_history"]["can_redo"])
        with self.assertRaises(ValueError):
            self.engine.redo_build(self.sid)
        self.assertEqual(self.engine.get_cell_tile(self.space(), 4, 4)["tile_id"], "wood_floor")
        self.engine.place_actor(actor["actor_id"], self.sid, 5, 4)
        self.engine.redo_build(self.sid)
        self.assertEqual(self.space()["tiles"]["4,4"], "void")

    def test_expand_and_edge_paint_restore_shifted_contents(self) -> None:
        entity = self.engine.place_from_blueprint("default_lamp", self.sid, 4, 4)
        self.engine.upsert_space_zone(self.sid, {"name": "标记区", "x": 3, "y": 3, "w": 2, "h": 2})
        self.relief("set_level", (5, 4), level=14, cliff_style="waterfall")
        self.relief("stair_both", (5, 4), stair_style="sand")
        before = self.space()
        result = self.paint("stone_tile", (0, 3), auto_expand=True)
        self.assertEqual(result["expanded"], ["west"])
        self.assertEqual(self.space()["zones"][0]["x"], 7)
        self.assertEqual(self.entities()[entity["entity_id"]]["x"], 8)
        self.assertEqual(self.space()["cliff_styles"], {"9,4": "waterfall"})
        self.assertEqual(self.space()["stair_styles"], {"9,4": "sand"})
        self.engine.undo_build(self.sid)
        self.assertEqual(self.space(), before)
        self.engine.redo_build(self.sid)
        self.assertEqual(self.space()["tiles"]["4,3"], "stone_tile")
        expanded = self.engine.expand_space(self.sid, "north", 2, fill_tile_id="void")
        self.assertEqual(expanded["height"], before["height"] + 2)
        self.assertEqual(expanded["elevations"], {"9,6": 14})
        self.assertEqual(expanded["cliff_styles"], {"9,6": "waterfall"})
        self.assertEqual(expanded["stair_styles"], {"9,6": "sand"})
        self.engine.undo_build(self.sid)
        self.assertEqual(self.space()["height"], before["height"])
        self.assertEqual(self.space()["cliff_styles"], {"9,4": "waterfall"})
        self.assertEqual(self.space()["stair_styles"], {"9,4": "sand"})

    def test_stamp_persistence_paste_conflicts_and_atomic_history(self) -> None:
        stamp = self.engine.save_build_stamp({
            "name": "石台角落", "width": 4, "height": 2,
            "tiles": {"0,0": "stone_tile", "1,0": "wood_floor", "2,0": "stone_tile", "0,1": "void"},
            "elevations": {"0,0": 11}, "stairs": {"0,0": "ew"},
            "cliff_styles": {"0,0": "brick", "1,0": "auto"}, "stair_styles": {"0,0": "sand", "0,1": "waterfall"},
            "entities": [{"blueprint_id": "default_lamp", "x": x, "y": 0} for x in (1, 2, 3)]
                        + [{"blueprint_id": "missing", "x": 0, "y": 0}],
        })
        reloaded = WorldEngine(code_dir=WORLD_DIR, data_root=self.data_root)
        self.assertEqual(reloaded.snapshot_for_ui(self.sid)["stamps"], [stamp])
        self.engine = reloaded
        origin_x = self.space()["width"] - 3
        occupied = self.engine.place_from_blueprint("default_lamp", self.sid, origin_x + 2, 2)["entity_id"]
        before = self.space()
        result = self.engine.paste_build_stamp(self.sid, origin_x, 2, stamp["stamp_id"])
        self.assertEqual(result, {"space_id": self.sid, "pasted_tiles": 4, "pasted_entities": 1, "skipped_entities": 3})
        after = self.space()
        self.assertEqual(after["tiles"][f"{origin_x},2"], "stone_tile")
        self.assertEqual(after["tiles"][f"{origin_x},3"], "void")
        self.assertEqual(after["elevations"], {f"{origin_x},2": 11})
        self.assertEqual(after["stairs"], {f"{origin_x},2": "ew"})
        self.assertEqual(after["cliff_styles"], {f"{origin_x},2": "brick"})
        self.assertEqual(after["stair_styles"], {f"{origin_x},2": "sand"})
        pasted_entities = set(self.entities())
        self.engine.undo_build(self.sid)
        self.assertEqual(self.space(), before)
        self.assertEqual(set(self.entities()), {occupied})
        self.engine.redo_build(self.sid)
        self.assertEqual(self.space(), after)
        self.assertEqual(set(self.entities()), pasted_entities)
        self.engine.delete_build_stamp(stamp["stamp_id"])
        reloaded = WorldEngine(code_dir=WORLD_DIR, data_root=self.data_root)
        self.assertEqual(reloaded.snapshot_for_ui(self.sid)["stamps"], [])
        self.assertTrue((self.data_root / "stamps.json").is_file())

    def test_compound_build_rolls_back_if_save_fails(self) -> None:
        before = self.space()
        with patch("nanami_world.engine._atomic_write_json", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.engine.link_portal_bidirectional(self.sid, x=4, y=4, mode="new_floor")
        self.assertEqual(self.space(), before)
        self.assertFalse(self.entities())
        self.assertEqual(len(self.snapshot()["spaces"]), 1)
        self.assertFalse(self.snapshot()["build_history"]["can_undo"])


if __name__ == "__main__":
    unittest.main()
