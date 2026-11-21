"""M2 Player Avatar movement, front interaction, and AI isolation tests."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService
from plugins.nanami_world.world_page import CANVAS_PAGE_ID, register_pages


class M2PlayerAvatarTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=root / "world", project_root=root)
        self.settings = WorldSettings(heartbeat_use_llm=False)
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=root)
        self.addCleanup(self.heartbeat.stop)

    def test_player_spawn_step_move_and_interact_actor_door_entity(self) -> None:
        nanami = self.engine.import_actor(name="七海千秋", host_character="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=nanami)["town_space_id"]
        home = f"{town}__home"
        self.engine.place_actor(nanami, town, 10, 8)

        player = self.engine.ensure_player_avatar(space_id=town)
        self.assertEqual(player["actor_id"], "player_avatar")
        self.assertTrue(player["is_player"])
        self.assertFalse(player["autonomy"]["enabled"])

        # Move player next to Nanami and interact -> returns talk_actor
        self.engine.place_actor("player_avatar", town, 8, 8)
        step = self.engine.step_player_move("player_avatar", 1, 0)
        self.assertTrue(step["moved"])
        self.assertEqual((step["x"], step["y"]), (9, 8))
        self.assertEqual(step["facing"], "right")
        self.assertEqual(step["prompt"]["kind"], "actor")
        self.assertEqual(step["prompt"]["target_id"], nanami)

        talk_res = self.engine.player_interact_front("player_avatar")
        self.assertEqual(talk_res["kind"], "talk_actor")
        self.assertEqual(talk_res["target_actor_id"], nanami)

        # Move player to doorway (6, 8) facing up toward home door (6, 7) and press E -> enters home
        self.engine.place_actor("player_avatar", town, 6, 9)
        door_step = self.engine.step_player_move("player_avatar", 0, -1)
        self.assertEqual(door_step["prompt"]["kind"], "portal")
        enter_res = self.engine.player_interact_front("player_avatar")
        self.assertEqual(enter_res["kind"], "entered_space")
        self.assertEqual(enter_res["space_id"], home)

        # Heartbeat never hijacks player_avatar and Nanami never triggers AI-to-AI talk onto player_avatar
        self.engine.place_actor("player_avatar", town, 10, 9)
        self.heartbeat.step(advance_minutes=1.0, use_llm=False)
        p_after = next(a for a in self.engine.list_actors() if a["actor_id"] == "player_avatar")
        self.assertEqual((p_after["x"], p_after["y"]), (10, 9))
        self.assertFalse(p_after["autonomy"]["enabled"])
        self.assertNotIn("player_avatar", self.engine.live_realm_peers(nanami))

    def test_door_takes_priority_with_npc_on_threshold_or_approach(self) -> None:
        npc = self.engine.import_actor(name="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=npc)["town_space_id"]
        home = f"{town}__home"
        door = next(e for e in self.engine.snapshot_for_ui(town)["entities"]
                    if e["traits"].get("portal", {}).get("target_space_id") == home)
        self.engine.ensure_player_avatar(space_id=town)

        # The first interaction opens the door. The second tests an NPC on the open threshold.
        for blocker_y in (door["y"] + 1, door["y"]):
            with self.subTest(blocker_y=blocker_y):
                self.engine.place_actor(npc, town, door["x"], blocker_y)
                self.engine.place_actor("player_avatar", town, door["x"], blocker_y + 1)
                result = self.engine.player_interact_front(
                    "player_avatar", facing="up", space_id=town, target_kind="portal", target_id=door["entity_id"],
                )
                self.assertEqual((result["kind"], result["space_id"]), ("entered_space", home))
                npc_frame = self.engine.live_actor_frame(npc)
                self.assertEqual((npc_frame["space_id"], npc_frame["x"], npc_frame["y"]), (town, door["x"], blocker_y))
                late_move = self.engine.step_player_move("player_avatar", 0, 1, space_id=town)
                self.assertFalse(late_move["moved"])
                self.assertEqual(late_move["space_id"], home)

    def test_item_rpc_patches_only_target_and_never_redirects_to_npc(self) -> None:
        room = self.engine.create_space(name="互动测试房", width=10, height=8, wall_rows=0)["space_id"]
        self.engine.ensure_player_avatar(space_id=room)
        self.engine.place_actor("player_avatar", room, 4, 4)
        snack = self.engine.place_from_blueprint("default_snack", room, 5, 4)
        self.engine.place_from_blueprint("default_lamp", room, 2, 2)
        self.engine.import_actor(name="七海千秋", space_id=room, x=5, y=4)
        register = Mock()
        register_pages(register, code_dir=WORLD_CODE, engine=self.engine,
                       settings_store=Mock(get=Mock(return_value=self.settings)), heartbeat=self.heartbeat)
        config = next(call.args[0] for call in register.register_frontend_config_page.call_args_list
                      if call.args[0].page_id == CANVAS_PAGE_ID)
        interact = next(action.run for action in config.actions if action.id == "player_interact")
        values = {"space_id": room, "facing": "right", "target_kind": "entity", "target_id": snack["entity_id"]}

        with patch.object(self.engine, "snapshot_for_ui", side_effect=AssertionError("interaction must be lightweight")):
            first = interact(values)
            summary = config.load_values()
        self.assertEqual(summary["entity_count"], 2)
        self.assertEqual(first["interaction"]["action"], "consume")
        self.assertNotIn("snapshot", first)
        updated, = first["changes"]["entities"]
        self.assertEqual(updated["traits"]["consumable"]["uses_left"], 1)
        second = interact(values)
        self.assertEqual(second["changes"]["entities"], [])
        self.assertEqual(second["changes"]["removed_entity_ids"], [snack["entity_id"]])
        self.assertEqual(interact(values)["interaction"]["kind"], "none")
        self.assertEqual(interact({"space_id": room, "facing": "right"})["interaction"]["kind"], "talk_actor")

    def test_npc_pauses_route_during_user_chat_and_resumes_on_release(self) -> None:
        nanami = self.engine.import_actor(name="七海千秋", host_character="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=nanami)["town_space_id"]
        self.engine.place_actor(nanami, town, 8, 8)
        self.engine.ensure_player_avatar(space_id=town)
        self.engine.place_actor("player_avatar", town, 7, 8)

        self.heartbeat.live.control(nanami, "window", "start", 4)
        self.addCleanup(lambda: self.heartbeat.live.control(nanami, "window", "stop", 1))

        deadline = time.monotonic() + 2.5
        seq_moving = 0
        while time.monotonic() < deadline:
            frame = self.heartbeat.live.frame(nanami, "window", -1)
            seq_moving = int(frame["actor"].get("move_seq") or 0)
            if seq_moving > 0:
                break
            time.sleep(0.05)
        self.assertGreater(seq_moving, 0, "Expected NPC to start autonomous movement before chat hold")

        self.heartbeat.live.hold_for_user_chat(nanami, 20.0)
        time.sleep(0.05)
        held_frame = self.heartbeat.live.frame(nanami, "window", -1)
        self.assertEqual(held_frame["activity"]["phase"], "conversing")
        held_pos = (held_frame["actor"]["x"], held_frame["actor"]["y"])
        held_seq = int(held_frame["actor"].get("move_seq") or 0)

        time.sleep(0.25)
        still_frame = self.heartbeat.live.frame(nanami, "window", -1)
        self.assertEqual(still_frame["activity"]["phase"], "conversing")
        self.assertEqual((still_frame["actor"]["x"], still_frame["actor"]["y"]), held_pos)
        self.assertEqual(int(still_frame["actor"].get("move_seq") or 0), held_seq)

        self.heartbeat.live.release_user_chat(nanami)
        resume_deadline = time.monotonic() + 2.5
        resumed_seq = held_seq
        while time.monotonic() < resume_deadline:
            after_frame = self.heartbeat.live.frame(nanami, "window", -1)
            resumed_seq = int(after_frame["actor"].get("move_seq") or 0)
            if resumed_seq > held_seq:
                break
            time.sleep(0.05)
        self.assertGreater(resumed_seq, held_seq, "Expected NPC to resume movement after chat release")

    def test_greeting_opens_for_the_npc_and_waits_for_real_player_input(self) -> None:
        room = self.engine.create_space(name="对话测试房", width=10, height=8, wall_rows=0)["space_id"]
        player = self.engine.ensure_player_avatar(space_id=room)["actor_id"]
        self.engine.place_actor(player, room, 4, 4)
        npc = self.engine.import_actor(name="路人甲", space_id=room, x=5, y=4)["actor_id"]
        settings = WorldSettings(heartbeat_use_llm=True)
        chat = WorldChatService(self.engine, project_root=self.engine.project_root, settings_provider=lambda: settings)

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value={
            "greeting": "你好，刚好在这里碰到你。", "emotion": "平静", "intent_b": "伪造的主控回复",
        }) as model:
            result = chat.conduct_actor_social_exchange(npc, player)
        self.assertEqual(result["turns"], [])
        self.assertTrue(result["used_llm"])
        self.assertEqual(model.call_count, 1)
        self.assertNotIn("intent_b", model.call_args.args[0])
        self.assertEqual(chat.get_history(player, space_id=room), [])
        self.assertEqual(chat.get_history(npc, space_id=room), [])

        register = Mock()
        register_pages(register, code_dir=WORLD_CODE, engine=self.engine, settings_store=Mock(get=lambda: settings),
                       heartbeat=self.heartbeat, world_chat=chat)
        config = next(call.args[0] for call in register.register_frontend_config_page.call_args_list
                      if call.args[0].page_id == CANVAS_PAGE_ID)
        actions = {action.id: action.run for action in config.actions}
        pending = actions["get_world_snapshot"]({"space_id": room, "roaming": True})["player_greeting"]
        args = {"space_id": room, "greeting_id": pending["id"]}
        opened = actions["open_player_greeting"](args)["greeting"]
        self.assertEqual(opened["actor_id"], npc)
        line, = opened["chat_history"]
        self.assertEqual((line["role"], line["text"]), ("assistant", "你好，刚好在这里碰到你。"))
        self.assertNotIn("greeting", actions["open_player_greeting"](args))

        with self.assertRaisesRegex(ValueError, "主控的台词"):
            chat.conduct_actor_social_exchange(player, npc)
        with self.assertRaisesRegex(ValueError, "主控的台词"):
            chat.chat(player, "不可以生成这条回复")
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=None):
            chat.chat(npc, "你好，我自己来回答。", space_id=room)
        history = chat.get_history(npc, space_id=room)
        self.assertEqual([row["text"] for row in history if row["role"] == "user"], ["你好，我自己来回答。"])
        self.assertEqual(chat.get_history(player, space_id=room), [])

    def test_live_npc_greeting_expires_when_player_walks_away(self) -> None:
        root = self.engine.project_root
        chat = WorldChatService(self.engine, project_root=root, settings_provider=lambda: self.settings)
        heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=root, chat_service_provider=lambda: chat)
        self.addCleanup(heartbeat.stop)
        room = self.engine.create_space(name="安静房间", width=10, height=8, wall_rows=0)["space_id"]
        npc = self.engine.import_actor(name="路人乙", space_id=room, x=4, y=4)["actor_id"]
        player = self.engine.ensure_player_avatar(space_id=room)["actor_id"]
        self.engine.place_actor(player, room, 5, 4)
        now = datetime.now()
        self.engine._world["clock_offset_minutes"] = (now.replace(hour=12, minute=0, second=0) - now).total_seconds() / 60
        heartbeat.live.control(npc, "window", "start", 1)
        deadline = time.monotonic() + 3
        pending = chat.pending_player_greeting(room)
        while pending is None and time.monotonic() < deadline:
            time.sleep(0.03)
            pending = chat.pending_player_greeting(room)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["actor_id"], npc)
        frame = heartbeat.live.frame(npc, "window", -1)
        self.assertEqual(frame["activity"]["phase"], "conversing")
        self.assertFalse(frame["speech"])
        self.engine.place_actor(player, room, 8, 6)
        self.assertIsNone(chat.open_player_greeting(pending["id"], room))
        self.assertIsNone(chat.pending_player_greeting(room))
        self.assertEqual(chat.get_history(npc, space_id=room), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
