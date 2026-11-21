"""Tests for the multi-district town big-map and enterable building sub-worlds."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _shinsekai import PROJECT, WORLD_CODE

from plugins.nanami_world import PLUGIN_ID, PLUGIN_VERSION, projector, town_builder, world_page
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService
from sdk.register import PluginCapabilityRegistry


class TownFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="nanami-town-foundation-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "world"
        self.persona = self.root / "empty_persona"
        self.persona.mkdir()
        self.settings = WorldSettings(persona_data_dir=str(self.persona))
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.data, project_root=self.root)
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=PROJECT)
        self.clock = datetime(2026, 7, 1, 12).timestamp()

    def test_default_town_structure_reachability_and_roundtrip_entries(self) -> None:
        actor = self.engine.import_actor(name="七海千秋", host_character="七海千秋", x=5, y=5)
        aid = actor["actor_id"]

        with patch("time.time", return_value=self.clock):
            created = self.engine.create_default_town(move_actor_id=aid)
        self.assertTrue(created["created"])
        town_id = created["town_space_id"]
        self.assertEqual(town_id, town_builder.DEFAULT_TOWN_ID)
        self.assertEqual(len(created["building_space_ids"]), 6)

        # Idempotent second call must not duplicate spaces
        second = self.engine.create_default_town(move_actor_id=aid)
        self.assertFalse(second["created"])
        self.assertEqual(set(second["building_space_ids"]), set(created["building_space_ids"]))

        snap = self.engine.snapshot_for_ui(town_id)
        town_sp = snap["space"]
        self.assertEqual(town_sp["width"], 40)
        self.assertEqual(town_sp["height"], 30)
        self.assertEqual(town_sp["wall_rows"], 0)
        self.assertEqual(town_sp["space_kind"], "town")
        self.assertEqual(len(town_sp["zones"]), 5)
        self.assertEqual(len(town_sp["buildings"]), 6)
        self.assertEqual(snap["current_zone"]["zone_id"], "central_plaza")
        self.assertEqual(len(snap["child_spaces"]), 6)

        # Verify chat history & vitals continuity before entering buildings
        chat_svc = WorldChatService(
            self.engine, project_root=self.root, settings_provider=lambda: self.settings, adapter_provider=lambda: None,
        )
        chat_svc.chat(aid, "先去咖啡馆看看吧", space_id=town_id)
        self.engine.set_actor_world_mood(
            aid,
            space_id=town_id,
            vitals_patch={"energy": 0.61, "mood_v": 0.44},
            autonomy_patch={"curiosity": 0.77, "current_goal": "逛逛樱町小镇"},
        )

        # Pick up a snack from the park or place a portable brass key next to actor to check migration
        key = self.engine.place_from_blueprint("default_brass_key", town_id, 19, 14)
        self.engine.execute_act(aid, key["entity_id"], "take")

        # Verify BFS reachability from spawn to every exterior door + round-trip entry for all 6 buildings
        for bldg in town_sp["buildings"]:
            dx, dy = bldg["door_x"], bldg["door_y"]
            int_sid = bldg["interior_space_id"]
            with self.subTest(building=bldg["name"]):
                ext_door = next(
                    e for e in snap["entities"]
                    if e["x"] == dx and e["y"] == dy and "portal" in e["traits"]
                )
                self.assertEqual(ext_door["traits"]["portal"]["target_space_id"], int_sid)
                with self.engine._lock:
                    path = self.engine._bfs_locked(
                        town_id,
                        town_builder.DEFAULT_TOWN_SPAWN,
                        {(dx, dy)},
                        allow_cells={(dx, dy)},
                        ignore_actor_id=aid,
                    )
                self.assertIsNotNone(path, f"Door for {bldg['name']} must be reachable from plaza")

                entered = self.engine.enter_portal(ext_door["entity_id"], actor_id=aid)
                self.assertEqual(entered["mode"], "actor")
                self.assertEqual(entered["target_space_id"], int_sid)

                int_snap = self.engine.snapshot_for_ui(int_sid)
                int_sp = int_snap["space"]
                self.assertEqual(int_sp["space_kind"], "building")
                self.assertEqual(int_sp["parent_space_id"], town_id)
                self.assertEqual(int_snap["parent_space"]["space_id"], town_id)

                act_inside = next(a for a in int_snap["actors"] if a["actor_id"] == aid)
                self.assertEqual(act_inside["space_id"], int_sid)
                self.assertEqual(act_inside["facing"], "up")
                self.assertEqual(act_inside["holding_id"], key["entity_id"])
                self.assertAlmostEqual(act_inside["vitals"]["energy"], 0.61, places=2)
                self.assertAlmostEqual(act_inside["vitals"]["mood_v"], 0.44, places=2)
                self.assertGreaterEqual(len(chat_svc.get_history(aid, space_id=int_sid)), 2)

                term = projector.project_terminal_data(self.engine, aid)
                self.assertIn("樱町小镇", term["text"])

                exit_door = next(
                    e for e in int_snap["entities"]
                    if "portal" in e["traits"] and e["traits"]["portal"]["target_space_id"] == town_id
                )
                exited = self.engine.enter_portal(exit_door["entity_id"], actor_id=aid)
                self.assertEqual(exited["target_space_id"], town_id)

                town_after = self.engine.snapshot_for_ui(town_id)
                act_outside = next(a for a in town_after["actors"] if a["actor_id"] == aid)
                self.assertEqual(act_outside["space_id"], town_id)
                self.assertEqual((act_outside["x"], act_outside["y"]), (dx, dy + 1))
                self.assertEqual(act_outside["facing"], "down")
                self.assertEqual(act_outside["holding_id"], key["entity_id"])

                # Direct execute_act("enter") works from sidewalk right outside the door
                res_msg = self.engine.execute_act(aid, ext_door["entity_id"], "enter")
                self.assertIn(bldg["name"], res_msg)
                self.assertIn(town_sp["name"], self.engine.execute_act(aid, exit_door["entity_id"], "enter"))
                self.engine.execute_move(aid, *town_builder.DEFAULT_TOWN_SPAWN)

        # Independent room_default remains completely isolated from town realm
        self.assertEqual(chat_svc.get_history(aid, space_id="room_default"), [])

    def test_spatial_visibility_occlusion_and_night_lamps_on_town(self) -> None:
        actor = self.engine.import_actor(name="七海千秋", x=5, y=5)
        aid = actor["actor_id"]
        with patch("time.time", return_value=self.clock):
            self.engine.create_default_town(move_actor_id=aid)
            vis_plaza = {e["entity_id"] for e in self.engine.visible_entities(aid)}
            all_town = self.engine.snapshot_for_ui(town_builder.DEFAULT_TOWN_ID)["entities"]
            # Distant workshop door at (35, 7) is beyond 12 tiles from (19, 15)
            workshop_door = next(e for e in all_town if e["x"] == 35 and e["y"] == 7)
            self.assertNotIn(workshop_door["entity_id"], vis_plaza)
            # Nearby plaza lamp at (17, 13) is visible
            plaza_lamp = next(e for e in all_town if e["x"] == 17 and e["y"] == 13)
            self.assertIn(plaza_lamp["entity_id"], vis_plaza)

            # Wall occlusion check: place item north of home wall (y=2) while actor stands south of home at (6, 9)
            hidden_prop = self.engine.place_from_blueprint("default_snack", town_builder.DEFAULT_TOWN_ID, 6, 2)
            self.engine.place_actor(aid, town_builder.DEFAULT_TOWN_ID, 6, 9)
            vis_south = {e["entity_id"] for e in self.engine.visible_entities(aid)}
            self.assertNotIn(hidden_prop["entity_id"], vis_south)

        # Night lighting: actor at (19, 15) still sees lit plaza props near street lamps
        night_clock = datetime(2026, 7, 1, 23).timestamp()
        with patch("time.time", return_value=night_clock):
            self.engine.place_actor(aid, town_builder.DEFAULT_TOWN_ID, *town_builder.DEFAULT_TOWN_SPAWN)
            vis_night = {e["entity_id"] for e in self.engine.visible_entities(aid)}
            self.assertIn(plaza_lamp["entity_id"], vis_night)

    def test_observer_portal_custom_building_cascade_delete_and_replace(self) -> None:
        # Observer mode when no actor is in town_sakura
        town = self.engine.create_default_town(move_actor_id="")
        town_id = town["town_space_id"]
        snap = self.engine.snapshot_for_ui(town_id)
        cafe_door = next(e for e in snap["entities"] if e["x"] == 26 and e["y"] == 7)

        obs_enter = self.engine.enter_portal(cafe_door["entity_id"])
        self.assertEqual(obs_enter["mode"], "observer")
        self.assertEqual(obs_enter["target_space_id"], f"{town_id}__cafe")
        self.assertEqual(self.engine.snapshot_for_ui()["active_space_id"], f"{town_id}__cafe")

        # Dynamically build a 7th building sub-world on open northeast/southwest lot
        extra = self.engine.create_building_subworld(
            town_id,
            building_id="bldg_flower",
            name="花艺工坊",
            zone_id="south_east_arcade",
            ext_x=35,
            ext_y=19,
            ext_w=4,
            ext_h=4,
            door_x=36,
            door_y=22,
            icon="🌺",
            interior_items=[{"bp": "default_plant", "x": 3, "y": 2}],
        )
        self.assertEqual(len(self.engine.snapshot_for_ui(town_id)["child_spaces"]), 7)

        # Delete single building cleans parent town's buildings metadata
        self.engine.delete_space(extra["interior_space_id"])
        after_single = self.engine.snapshot_for_ui(town_id)
        self.assertEqual(len(after_single["child_spaces"]), 6)
        self.assertFalse(any(b["building_id"] == "bldg_flower" for b in after_single["space"]["buildings"]))

        # Actor inside cafe gets relocated cleanly when town is cascade-deleted
        aid = self.engine.import_actor(name="七海千秋", space_id=f"{town_id}__cafe", x=4, y=4)["actor_id"]
        del_res = self.engine.delete_space(town_id)
        self.assertEqual(len(del_res["deleted_descendants"]), 6)
        self.assertEqual(list(self.engine.snapshot_for_ui()["spaces"][0]["space_id"] for _ in [0]), ["room_default"])
        moved_act = next(a for a in self.engine.list_actors() if a["actor_id"] == aid)
        self.assertEqual(moved_act["space_id"], "room_default")

        # Test replace=True even when town is the ONLY remaining realm
        self.engine.create_default_town(move_actor_id=aid)
        self.engine.delete_space("room_default")
        rebuilt = self.engine.create_default_town(replace=True, move_actor_id=aid)
        self.assertTrue(rebuilt["created"])
        self.assertEqual(len(rebuilt["building_space_ids"]), 6)

    def test_town_rpcs_and_big_map_scenic_performance(self) -> None:
        registry = PluginCapabilityRegistry()
        registry.set_settings_ui_plugin_context(PLUGIN_ID, PLUGIN_VERSION)
        world_page.register_pages(
            registry,
            code_dir=WORLD_CODE,
            engine=self.engine,
            heartbeat=self.heartbeat,
            settings_store=SimpleNamespace(get=lambda: self.settings),
        )
        canvas = next(p for p in registry.frontend_config_contributions if p.page_id == "world_canvas")
        actions = {a.id: a.run for a in canvas.actions}

        res_town = actions["create_default_town"]({})
        self.assertTrue(res_town["ok"], res_town)
        town_snap = res_town["snapshot"]
        town_id = town_snap["active_space_id"]
        self.assertEqual(town_id, town_builder.DEFAULT_TOWN_ID)
        aid = town_snap["active_actor_id"]
        self.assertTrue(aid)

        # Move actor along the main avenue via move_actor RPC
        res_move = actions["move_actor"]({"space_id": town_id, "actor_id": aid, "x": 26, "y": 13})
        self.assertTrue(res_move["ok"], res_move)

        # Enter cafe via enter_portal RPC
        cafe_door = next(e for e in res_move["snapshot"]["entities"] if e["x"] == 26 and e["y"] == 7)
        res_enter = actions["enter_portal"]({"entity_id": cafe_door["entity_id"], "actor_id": aid})
        self.assertTrue(res_enter["ok"], res_enter)
        self.assertEqual(res_enter["snapshot"]["active_space_id"], f"{town_id}__cafe")
        self.assertEqual(res_enter["snapshot"]["parent_space"]["space_id"], town_id)

        # Create another building via create_building_subworld RPC
        res_bldg = actions["create_building_subworld"]({
            "town_space_id": town_id,
            "name": "街角面包房",
            "ext_x": 35,
            "ext_y": 19,
            "ext_w": 4,
            "ext_h": 4,
            "door_x": 36,
            "door_y": 22,
            "icon": "🥐",
        })
        self.assertTrue(res_bldg["ok"], res_bldg)
        self.assertEqual(len(res_bldg["snapshot"]["child_spaces"]), 7)

        # Performance sanity check on 40x30 town
        self.engine.place_actor(aid, town_id, *town_builder.DEFAULT_TOWN_SPAWN)
        actor = next(a for a in self.engine.list_actors() if a["actor_id"] == aid)
        space = self.engine.snapshot_for_ui(town_id)["space"]
        vis = self.engine.visible_entities(aid)
        t0 = time.perf_counter()
        scenic = self.heartbeat._pick_scenic_cell(actor, space, vis, [])
        elapsed = time.perf_counter() - t0
        self.assertIsNotNone(scenic)
        self.assertLess(elapsed, 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
