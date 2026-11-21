"""Real-time autonomy runs independently of frame reads and slow model calls."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import PLUGIN_ID, PLUGIN_VERSION, world_page
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService
from sdk.register import PluginCapabilityRegistry


class LiveSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=root / "world", project_root=root)
        persona = root / "persona"
        persona.mkdir()
        self.settings = WorldSettings(heartbeat_use_llm=True, persona_data_dir=str(persona))
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=root,
                                        chat_service_provider=lambda: self.chat)
        self.addCleanup(self.heartbeat.stop)
        self.live = self.heartbeat.live
        self.chat = WorldChatService(self.engine, project_root=root, settings_provider=lambda: self.settings,
                                     action_scheduler=self.live.queue_action)

    def until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(0.025)
        self.fail("Real-time condition did not arrive")

    def test_walking_does_not_wait_for_frames_or_reflection(self) -> None:
        room = self.engine.create_space(name="场景", width=28, height=25, wall_rows=0)["space_id"]
        aid = self.engine.import_actor(name="Scout", space_id=room, x=14, y=12)["actor_id"]
        start = self.engine.live_actor_frame(aid)
        entered = threading.Event()
        release = threading.Event()

        def slow_reflection(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"agenda": "去看看周围", "domain_bias": {"explore": 10, "unknown": 1}}

        with patch("plugins.nanami_world.live_simulation.text_compiler._ask_llm_json", side_effect=slow_reflection):
            try:
                self.live.control(aid, "window", "start", 1)
                self.assertTrue(entered.wait(2))
                self.until(lambda: self.engine.live_actor_frame(aid)["move_seq"] >= start["move_seq"] + 4)
                frame = self.live.frame(aid, "window", 0)
                self.assertEqual(frame["reflection"][-1]["status"], "requested")
                self.assertLessEqual(len(frame["actor"]["last_path"]), 12)
                self.assertEqual(self.engine.snapshot_for_ui(room)["clock_offset_minutes"], 0.0)
                release.set()
                self.until(lambda: any(event["status"] == "received"
                                       for event in self.live.frame(aid, "window", -1)["reflection"]))
                self.until(lambda: self.engine.list_actors()[0]["autonomy"]["cognition"].get("modulation"))
                self.assertEqual(self.engine.list_actors()[0]["autonomy"]["cognition"]["modulation"]["domains"],
                                 {"explore": 0.6})
            finally:
                release.set()

    def test_last_tile_then_portal_without_another_request(self) -> None:
        self.settings = WorldSettings(heartbeat_use_llm=False, persona_data_dir=self.settings.persona_data_dir)
        aid = self.engine.import_actor(name="Scout")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=aid)["town_space_id"]
        cafe = f"{town}__cafe"
        self.engine.place_actor(aid, town, 26, 9)
        goal = {"kind": "visit", "key": "cafe", "target": cafe, "space": town, "action": "",
                "drive": "food", "phase": "travel", "goal": "去咖啡馆"}
        self.engine.update_actor_autonomy(aid, {"cognition": {"active_intention": goal}})
        self.live.control(aid, "window", "start", 1)
        self.until(lambda: self.engine.live_actor_frame(aid)["space_id"] == cafe, timeout=2)

    def test_paused_interaction_commits_once_after_its_duration(self) -> None:
        self.settings = WorldSettings(heartbeat_use_llm=False, persona_data_dir=self.settings.persona_data_dir)
        room = self.engine.create_space(name="用餐", width=10, height=8, wall_rows=0)["space_id"]
        aid = self.engine.import_actor(name="Scout", space_id=room, x=5, y=4)["actor_id"]
        snack = self.engine.place_from_blueprint("default_snack", room, 5, 3)
        eid = snack["entity_id"]
        initial = snack["traits"]["consumable"]["uses_left"]
        self.engine.update_actor_autonomy(aid, {"hunger": 1})
        self.live.control(aid, "window", "start", 4)
        self.until(lambda: self.live.frame(aid, "window", -1)["activity"]["phase"] == "engaging")

        def uses():
            for entity in self.engine.visible_entities(aid):
                if entity["entity_id"] == eid:
                    return entity["traits"]["consumable"]["uses_left"]
            return 0

        self.live.control(aid, "window", "speed", 0)
        time.sleep(0.15)
        self.assertEqual(uses(), initial)
        self.assertEqual(self.live.frame(aid, "window", -1)["activity"]["phase"], "paused")
        self.live.control(aid, "window", "speed", 4)
        self.until(lambda: uses() < initial)
        self.assertEqual(uses(), initial - 1)

    def test_player_followup_visits_named_sleeping_actor_without_auto_life(self) -> None:
        street = self.engine.create_space(name="街道", width=28, height=15, wall_rows=0)["space_id"]
        nanami = self.engine.import_actor(name="七海千秋", space_id=street, x=4, y=7)["actor_id"]
        hinata = self.engine.import_actor(name="日向创", space_id=street, x=13, y=7)["actor_id"]
        bed = self.engine.place_from_blueprint("default_cat_cushion", street, 14, 7)
        self.engine.execute_act(hinata, bed["entity_id"], "rest")
        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json",
                   return_value={"intent": "……那，我这就过去。", "world_actions": []}), \
                patch.object(self.chat, "_mouth_speak", return_value="……那，我这就过去。"):
            self.assertFalse(self.chat.chat(nanami, "日向创在马路中间睡着了")["action_queued"])
            self.assertTrue(self.chat.chat(nanami, "你去看看他吧")["action_queued"])
        self.until(lambda: self.live.frame(nanami, "viewer", -1).get("order_status") == "arrived", timeout=5)
        first, second = (self.engine.live_actor_frame(aid) for aid in (nanami, hinata))
        self.assertLessEqual(self.engine.chebyshev_distance(first, second), 1)
        self.assertEqual(second["posture"], "resting")

    def test_real_time_roster_and_generated_dialogue_for_two_actors(self) -> None:
        room = self.engine.create_space(name="安静的房间", width=12, height=10, wall_rows=0)["space_id"]
        first = self.engine.import_actor(name="七海千秋", space_id=room, x=5, y=4)["actor_id"]
        second = self.engine.import_actor(name="日向创", space_id=room, x=6, y=4)["actor_id"]

        def model(system, user, **kwargs):
            if '"intent_a"' in system:
                return {"intent_a": "日向君，刚才在看什么？", "intent_b": "我看看这附近的路。", "end_chat": True}
            return {}

        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", side_effect=model), \
                patch.object(self.chat, "_mouth_speak", side_effect=lambda name, ctx, intent, *a, **kw: intent):
            self.live.control(first, "window", "start", 1)
            self.until(lambda: self.live.managed(second))
            self.until(lambda: any("附近的路" in line["text"] for line in self.chat.get_history(first, space_id=room)),
                       timeout=6)
            self.assertTrue(any("刚才在看什么" in line["text"] for line in self.chat.get_history(second, space_id=room)))
            view = self.live.frame(first, "window", -1)
            self.assertIn(second, [peer["actor_id"] for peer in view["peers"]])
            registry = PluginCapabilityRegistry()
            registry.set_settings_ui_plugin_context(PLUGIN_ID, PLUGIN_VERSION)
            world_page.register_pages(
                registry, code_dir=WORLD_CODE, engine=self.engine, heartbeat=self.heartbeat, world_chat=self.chat,
                settings_store=SimpleNamespace(get=lambda: self.settings),
            )
            canvas = next(page for page in registry.frontend_config_contributions if page.page_id == "world_canvas")
            frame = next(action.run for action in canvas.actions if action.id == "get_live_frame")
            request = {"actor_id": first, "client": "window", "space_id": room}
            result = frame(request)
            self.assertIn("chat_history", result)
            self.assertIn("changes", result)
            self.assertNotIn("snapshot", result)
            request.update(history_after=result["chat_history"][-1]["id"], world_version=result["live"]["world_version"])
            unchanged = frame(request)
            self.assertNotIn("chat_history", unchanged)
            self.assertNotIn("changes", unchanged)

if __name__ == "__main__":
    unittest.main()
