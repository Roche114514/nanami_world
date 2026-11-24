"""Phase 1 tests: unified venue semantics, hierarchical town generation, incremental building addition, and layout validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import schedule, text_compiler, town_builder, venue_semantics
from plugins.nanami_world.cognitive_engine import CognitiveEngine
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_page import CANVAS_PAGE_ID, register_pages


class TownGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = WorldSettings(heartbeat_use_llm=False, auto_compile_llm=False)
        self.clock_patch = patch("time.time", return_value=datetime(2026, 7, 1, 14, 0).timestamp())
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)

    def test_unified_venue_semantics_and_custom_building_integration(self) -> None:
        self.assertEqual(
            venue_semantics.normalize_venue_role("", category="shop", name="微风咖啡馆"),
            "cafe",
        )
        self.assertEqual(venue_semantics.restock_profile_for_building({"category": "shop", "name": "微风咖啡馆"}), "cafe")
        self.assertEqual(venue_semantics.restock_profile_for_building({"role": "shop", "name": "杂货工坊"}), "shop")
        self.assertEqual(venue_semantics.restock_profile_for_building({"role": "home", "name": "旅人宿舍"}), "")
        self.assertTrue(venue_semantics.is_lodging_building({"name": "汤宿旅馆"}))
        self.assertTrue(venue_semantics.is_food_building({"name": "雪见茶屋"}))
        self.assertTrue(
            venue_semantics.building_matches_schedule_categories(
                {"category": "shop", "name": "雪见茶屋", "role": "cafe"},
                ("cafe", "shop"),
            )
        )

        # Verify schedule + cognitive_engine + morning restocking honor custom-named venues via unified role.
        self.clock_patch.stop()
        early_patch = patch("time.time", return_value=datetime(2026, 7, 1, 5, 55).timestamp())
        early_patch.start()
        self.addCleanup(early_patch.stop)

        eng = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world-sem", project_root=self.root)
        aid = eng.import_actor(name="旅人测试")["actor_id"]
        spec = text_compiler.compile_town_spec_from_text("温泉雪见小镇", width=36, height=28, building_count=4, use_llm=False)["town_spec"]
        res = eng.generate_town(spec, move_actor_id=aid)
        tid = res["town_space_id"]
        self.assertTrue(res["validation"]["ok"], res["validation"])

        sc = eng.cognitive_scene(aid)
        sched = schedule.build_actor_schedule(sc["actor"], sc["actors"] and None, None, sc["realm_buildings"], home_space_id=sc["home_space_id"])
        self.assertTrue(sched["home_space_id"])
        midday = next(s for s in sched["slots"] if s["slot_id"] == "midday_meal")
        self.assertTrue(midday["preferred_place_id"])
        cafe_b = next(b for b in sc["realm_buildings"] if venue_semantics.is_food_building(b))
        home_b = next(b for b in sc["realm_buildings"] if venue_semantics.is_lodging_building(b))
        self.assertTrue(CognitiveEngine._place_is_lodging(home_b["interior_space_id"], sc))
        self.assertFalse(CognitiveEngine._place_is_lodging(cafe_b["interior_space_id"], sc))
        self.assertTrue(CognitiveEngine._place_is_food_venue(cafe_b["interior_space_id"], {"places": {}}, sc))

        # Deplete cafe & advance across 06:00 -> custom-named cafe ("雪见茶屋") restocks automatically.
        for ent in list(eng._world["entities"].values()):
            if ent.get("space_id") == cafe_b["interior_space_id"] and "consumable" in (ent.get("traits") or {}):
                ent["traits"]["consumable"]["uses_left"] = 0
        tick_res = eng.tick_physics(advance_minutes=10.0)
        self.assertIn(cafe_b["name"], tick_res["restocked"])

    def test_offline_multi_theme_hierarchical_town_generation_and_validation(self) -> None:
        aid = self.engine.import_actor(name="七海千秋")["actor_id"]
        cases = [
            ("海风海港小镇", 32, 24, 4, "seaside", "clear"),
            ("飘雪冬日温泉镇", 40, 30, 6, "snow_onsen", "snow"),
            ("赛博霓虹电玩都市", 48, 36, 8, "neon_market", "rain"),
            ("森林魔法学院小镇", 40, 30, 6, "forest_academy", "sakura"),
        ]
        for prompt, w, h, count, expected_theme, expected_weather in cases:
            with self.subTest(theme=expected_theme, size=(w, h), count=count):
                compiled = text_compiler.compile_town_spec_from_text(
                    prompt, width=w, height=h, building_count=count, use_llm=False,
                )
                self.assertFalse(compiled["used_llm"])
                spec = compiled["town_spec"]
                self.assertEqual(spec["theme"], expected_theme)
                self.assertEqual(len(spec["buildings"]), count)

                if expected_theme == "seaside":
                    spec["seed"] = 4
                gen = self.engine.generate_town(spec, move_actor_id=aid)
                tid = gen["town_space_id"]
                val = gen["validation"]
                self.assertTrue(val["ok"], f"{expected_theme} validation failed: {val['errors']}")
                self.assertEqual(val["building_count"], count)
                self.assertEqual(val["reachable_doors"], count)

                snap = self.engine.snapshot_for_ui(tid)
                bldgs = snap["space"]["buildings"]
                self.assertEqual(len(bldgs), count)
                self.assertTrue(any(venue_semantics.is_lodging_building(b) for b in bldgs))
                self.assertTrue(any(venue_semantics.is_food_building(b) for b in bldgs))
                self.assertIn(expected_weather, snap["weather"]["allowed"])

                board = next(e for e in snap["entities"] if "布告栏" in e["name"])
                board_text = board["traits"]["inscribable"]["entries"][0]["text"]
                self.assertIn(bldgs[0]["name"], board_text)

                # Verify actor can enter first building and exit back to town street seamlessly.
                first_b = bldgs[0]
                enter_info = self.engine.enter_portal(gen["buildings"][0]["exterior_door_id"], actor_id=aid)
                self.assertEqual(enter_info["target_space_id"], first_b["interior_space_id"])
                exit_info = self.engine.enter_portal(gen["buildings"][0]["interior_door_id"], actor_id=aid)
                self.assertEqual(exit_info["target_space_id"], tid)

    def test_mock_llm_town_generation_and_sanitizer_guards(self) -> None:
        dirty_llm = {
            "name": "奇幻测试小镇",
            "theme": "unknown_theme",
            "palette_id": "invalid_palette",
            "allowed_weathers": ["meteor", "rain", "clear"],
            "initial_weather": "tornado",
            "ground_tile": "water",  # unwalkable -> must be sanitized back to walkable
            "road_tile": "brick_wall",  # unwalkable -> must be sanitized back to walkable
            "border_tile": "grass",  # walkable -> must be sanitized to unwalkable border
            "buildings": [
                {"name": "同款馆", "role": "library", "zone_slot": "nw", "roof_color": "not_hex"},
                {"name": "同款馆", "role": "arcade", "zone_slot": "nw"},
                {"name": "同款馆", "role": "public", "zone_slot": "nw"},
                {"name": "同款馆", "role": "library", "zone_slot": "nw"},
            ],
        }
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=dirty_llm):
            compiled = text_compiler.compile_town_spec_from_text(
                "海边的奇幻小镇", width=36, height=28, building_count=4, use_llm=True,
            )
        self.assertTrue(compiled["used_llm"])
        spec = compiled["town_spec"]
        self.assertEqual(len(spec["buildings"]), 4)
        self.assertTrue(any(b["role"] == "home" for b in spec["buildings"]))
        self.assertTrue(any(venue_semantics.is_food_building(b) for b in spec["buildings"]))
        self.assertEqual(len({b["name"] for b in spec["buildings"]}), 4)
        self.assertIn(spec["initial_weather"], spec["allowed_weathers"])

        aid = self.engine.import_actor(name="测试者")["actor_id"]
        gen = self.engine.generate_town(spec, move_actor_id=aid)
        self.assertTrue(gen["validation"]["ok"], gen["validation"])

    def test_incremental_add_building_on_default_and_generated_towns(self) -> None:
        aid = self.engine.import_actor(name="日向创")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=aid)["town_space_id"]
        self.assertTrue(self.engine.validate_town(town)["ok"])

        added_lib = self.engine.add_town_building(
            town, name="海边二手书店", role="library", interior_ambient="飘着旧纸与阳光气味的街角二手书店。",
        )
        self.assertTrue(added_lib["validation"]["ok"], added_lib["validation"])
        self.assertEqual(added_lib["role"], "library")

        added_cafe = self.engine.add_town_building(
            town, name="烘焙咖啡角", role="cafe",
        )
        self.assertTrue(added_cafe["validation"]["ok"], added_cafe["validation"])
        self.assertEqual(added_cafe["role"], "cafe")

        snap = self.engine.snapshot_for_ui(town)
        self.assertEqual(len(snap["space"]["buildings"]), 8)
        board = next(e for e in snap["entities"] if "布告栏" in e["name"])
        notes = [e["text"] for e in board["traits"]["inscribable"]["entries"]]
        self.assertTrue(any("海边二手书店" in t for t in notes))
        self.assertTrue(any("烘焙咖啡角" in t for t in notes))

        # Actor can route from central spawn to the newly added cafe door & enter/exit seamlessly.
        route = self.engine.plan_actor_route(aid, town, added_cafe["door_x"], added_cafe["door_y"], reach=1, stride=2, cardinal=True)
        self.assertGreater(len(route), 1)
        enter_res = self.engine.enter_portal(added_cafe["exterior_door_id"], actor_id=aid)
        self.assertEqual(enter_res["target_space_id"], added_cafe["interior_space_id"])
        exit_res = self.engine.enter_portal(added_cafe["interior_door_id"], actor_id=aid)
        self.assertEqual(exit_res["target_space_id"], town)

        # Also increment a generated town and verify morning restocking finds the new cafe.
        gen_spec = text_compiler.compile_town_spec_from_text("海风海港小镇", width=36, height=28, building_count=4, use_llm=False)["town_spec"]
        gen_spec["seed"] = 5
        gen = self.engine.generate_town(gen_spec, move_actor_id=aid)
        gen_add = self.engine.add_town_building(gen["town_space_id"], name="港区烘焙坊", role="cafe")
        self.assertTrue(gen_add["validation"]["ok"], gen_add["validation"])

    def test_town_generation_and_add_building_rpcs(self) -> None:
        configs = []
        fake_hb = SimpleNamespace(
            deliver_persona_events=lambda: None,
            step=lambda **kw: {},
            live=SimpleNamespace(queue_action=lambda *a, **kw: (True, ""), frame=lambda *a, **kw: {}, control=lambda *a, **kw: {}),
        )
        register_pages(
            SimpleNamespace(register_frontend_config_page=configs.append),
            code_dir=WORLD_CODE,
            engine=self.engine,
            settings_store=SimpleNamespace(get=lambda: self.settings),
            heartbeat=fake_hb,
        )
        actions = {rule.id: rule.run for rule in next(c for c in configs if c.page_id == CANVAS_PAGE_ID).actions}

        gen_res = actions["generate_town"]({
            "prompt_text": "温暖的雪见温泉小镇",
            "width": 32,
            "height": 24,
            "building_count": 4,
            "use_llm": False,
        })
        self.assertTrue(gen_res["ok"], gen_res)
        town_sid = gen_res["town"]["town_space_id"]
        self.assertTrue(gen_res["town"]["validation"]["ok"])

        add_res = actions["add_town_building"]({
            "town_space_id": town_sid,
            "name": "山脚茶亭",
            "role": "cafe",
        })
        self.assertTrue(add_res["ok"], add_res)
        self.assertTrue(add_res["validation"]["ok"])

        val_res = actions["validate_town"]({"town_space_id": town_sid})
        self.assertTrue(val_res["ok"])
        self.assertTrue(val_res["validation"]["ok"])
        self.assertEqual(val_res["validation"]["building_count"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
