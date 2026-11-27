"""Tests for void tile digging/guardrails and multi-color Pixel Studio schema/engine."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

WORLD_DIR = Path(__file__).resolve().parents[1]
PLUGINS_PARENT = WORLD_DIR.parent
if str(PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_PARENT))

from nanami_world import projector
from nanami_world.engine import WorldEngine
from nanami_world.schema import TILE_CATALOG, normalize_custom_frames, normalize_space_tiles, normalize_sprite


class VoidAndPixelStudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name) / "data"
        self.engine = WorldEngine(code_dir=WORLD_DIR, data_root=self.data_root)
        self.space_id = self.engine.snapshot_for_ui()["active_space_id"]
        self.actor = self.engine.import_actor(name="七海千秋", space_id=self.space_id, x=5, y=5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_void_tile_catalog_and_persistence(self) -> None:
        self.assertIn("void", TILE_CATALOG)
        self.assertFalse(TILE_CATALOG["void"]["walkable"])
        self.assertFalse(TILE_CATALOG["void"]["is_wall"])
        normalized = normalize_space_tiles({"2,2": "void", "99,99": "void"}, 12, 9)
        self.assertEqual(normalized, {"2,2": "void"})

    def test_paint_void_removes_entities_clears_relief_and_blocks_placement_and_walk(self) -> None:
        # Place an entity (even non-solid / placement=any) on (2, 3)
        bp = self.engine.save_blueprint({
            "name": "测试摆件",
            "brief": "小摆件",
            "detail": "测试用。",
            "footprint": [1, 1],
            "placement": "any",
            "solid": False,
            "sprite": {
                "mode": "pixel_grid",
                "w": 16,
                "h": 16,
                "frames": {"default": ["." * 16 for _ in range(15)] + ["..1111.........."]},
            },
        })
        ent = self.engine.place_from_blueprint(bp["blueprint_id"], self.space_id, 2, 3)

        # Add elevation & stair at (2, 3) to verify void clears relief
        with self.engine._lock:
            sp = self.engine._world["spaces"][self.space_id]
            sp.setdefault("elevations", {})["2,3"] = 1
            sp.setdefault("stairs", {})["2,3"] = "ns"

        # Dig (2, 3) into void; also test edge (0, 2) with auto_expand=True does NOT expand
        res = self.engine.paint_tiles(
            self.space_id,
            [{"x": 2, "y": 3, "tile_id": "void"}, {"x": 0, "y": 2, "tile_id": "void"}],
            auto_expand=True,
        )
        self.assertEqual(res["expanded"], [])
        self.assertIn(ent["entity_id"], res["removed"])
        self.assertEqual(res["painted"], 2)

        snap = self.engine.snapshot_for_ui(self.space_id)
        space = snap["space"]
        self.assertEqual(space["tiles"].get("2,3"), "void")
        self.assertEqual(space["tiles"].get("0,2"), "void")
        self.assertNotIn("2,3", space.get("elevations") or {})
        self.assertNotIn("2,3", space.get("stairs") or {})
        self.assertEqual(self.engine.get_cell_tile(space, 2, 3)["tile_id"], "void")

        # Pickaxe demolish must NOT resurrect void back to wood_floor
        demo = self.engine.paint_tiles(
            self.space_id,
            [{"x": 2, "y": 3, "tile_id": ""}],
            demolish=True,
        )
        self.assertEqual(demo["painted"], 0)
        self.assertEqual(self.engine.snapshot_for_ui(self.space_id)["space"]["tiles"].get("2,3"), "void")

        # Cannot place any entity (even non-solid) on void
        with self.assertRaises(ValueError):
            self.engine.place_from_blueprint(bp["blueprint_id"], self.space_id, 2, 3)

        # Cannot walk on void, and BFS routes around it
        with self.engine._lock:
            sp = self.engine._world["spaces"][self.space_id]
            self.assertFalse(self.engine._is_floor_walkable_locked(sp, 2, 3))
            path = self.engine._bfs_locked(self.space_id, (1, 3), {(3, 3)})
            self.assertIsNotNone(path)
            self.assertNotIn((2, 3), path)

        # Projector must not list void terrain
        rendered = projector.project_terminal_data(self.engine, next(iter(self.engine._world["actors"])))
        self.assertNotIn("虚空", rendered["text"])

        # Painting normal floor back restores walkability and placement
        self.engine.paint_tiles(self.space_id, [{"x": 2, "y": 3, "tile_id": "wood_floor"}])
        with self.engine._lock:
            sp = self.engine._world["spaces"][self.space_id]
            self.assertTrue(self.engine._is_floor_walkable_locked(sp, 2, 3))
        ent2 = self.engine.place_from_blueprint(bp["blueprint_id"], self.space_id, 2, 3)
        self.assertEqual((ent2["x"], ent2["y"]), (2, 3))

    def test_paint_void_skips_actor_cell(self) -> None:
        actors = list(self.engine._world["actors"].values())
        self.assertTrue(actors)
        act = actors[0]
        ax, ay = int(act["x"]), int(act["y"])
        res = self.engine.paint_tiles(
            self.space_id,
            [{"x": ax, "y": ay, "tile_id": "void"}],
            auto_expand=False,
        )
        self.assertEqual(res["skipped_actor_cells"], [[ax, ay]])
        self.assertNotEqual(self.engine.snapshot_for_ui(self.space_id)["space"]["tiles"].get(f"{ax},{ay}"), "void")

    def test_multicolor_sprite_and_legacy_compatibility(self) -> None:
        palette = {
            "0": "#181425",
            "A": "#ff2a85",
            "Z": "#00f0ff",
            "9": "#ffe600",
        }
        top_row = "0AZ9" + "." * 12
        rows = [top_row] + ["." * 16 for _ in range(15)]
        bp = self.engine.save_blueprint({
            "name": "多色像素画",
            "brief": "一幅自绘多色像素画",
            "detail": "保存后应保持原始坐标与调色盘。",
            "footprint": [1, 1],
            "placement": "any",
            "sprite": {
                "mode": "pixel_grid",
                "w": 16,
                "h": 16,
                "frames": {"default": rows},
                "palette": palette,
                "preserve_layout": True,
            },
        })
        reloaded = WorldEngine(code_dir=WORLD_DIR, data_root=self.data_root)
        loaded_bp = reloaded.all_blueprints()[bp["blueprint_id"]]
        sp = loaded_bp["sprite"]
        self.assertEqual(sp["palette"], palette)
        self.assertTrue(sp["preserve_layout"])
        # Exact top row preserved (not shifted to bottom by align_bottom)
        self.assertEqual(sp["frames"]["default"][0], top_row)

        # Legacy 6-slot sprite without palette still bottom-aligns and sanitizes
        legacy = normalize_sprite({
            "mode": "pixel_grid",
            "w": 16,
            "h": 16,
            "frames": {"default": ["..11.." + "." * 10] + ["." * 16 for _ in range(15)]},
        })
        self.assertEqual(legacy["palette"], {})
        self.assertFalse(legacy["preserve_layout"])
        self.assertEqual(legacy["frames"]["default"][-1].count("1"), 2)

    def test_custom_skin_roundtrip_16x24_and_23x32(self) -> None:
        actor_id = next(iter(self.engine._world["actors"]))
        pal = {"0": "#1d1722", "1": "#fde8d8", "A": "#ff8fab", "B": "#5e86c4"}
        down_16 = ["....00000000...." if y < 6 else "....11AABB11...." for y in range(24)]
        cf = normalize_custom_frames({
            "w": 16,
            "h": 24,
            "down": down_16,
            "up": down_16,
            "right": down_16,
            "palette": pal,
        })
        self.assertEqual((cf["w"], cf["h"]), (16, 24))
        updated = self.engine.set_actor_appearance(actor_id, appearance={"custom_frames": cf})
        self.assertEqual(updated["appearance"]["custom_frames"]["palette"], pal)
        self.assertEqual(len(updated["appearance"]["custom_frames"]["up"]), 24)

        down_23 = [".....0000000000000....." for _ in range(32)]
        cf2 = normalize_custom_frames({
            "w": 23,
            "h": 32,
            "down": down_23,
            "palette": {"0": "#201721"},
        })
        self.assertEqual((cf2["w"], cf2["h"]), (23, 32))


if __name__ == "__main__":
    unittest.main()
