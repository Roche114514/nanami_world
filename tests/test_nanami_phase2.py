"""Regression coverage for rest consolidation and shared arrivals."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import episodic_memory, projector
from plugins.nanami_world.cognitive_engine import cognitive_summary
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.plugin import NanamiWorldPlugin
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService


class Phase2Tests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = WorldSettings(heartbeat_use_llm=False)
        clock = patch("time.time", return_value=datetime(2026, 7, 1, 12).timestamp())
        self.now = clock.start()
        self.addCleanup(clock.stop)
        self.engine = self.open_world()
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=self.root)
        self.addCleanup(self.heartbeat.stop)

    def open_world(self) -> WorldEngine:
        return WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)

    def pair(self) -> tuple[str, str, str, str]:
        first = self.engine.import_actor(name="甲")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=first)["town_space_id"]
        self.engine.place_actor(first, town, 26, 8)
        second = self.engine.import_actor(name="乙", space_id=town, x=26, y=9)["actor_id"]
        return first, second, town, f"{town}__cafe"

    def step_actor(self, aid: str, *, live: bool = True) -> dict:
        scene = self.engine.cognitive_scene(aid, include_rooms=True)
        if live:
            scene["engage"] = lambda *_: True
        return self.heartbeat._cognitive.step(scene, self.settings, use_llm=False, talk=lambda *_: [])

    def memory(self, aid: str, engine: WorldEngine | None = None) -> dict:
        actor = (engine or self.engine).cognitive_scene(aid, include_rooms=True)["actor"]
        return actor["autonomy"]["cognition"]["episodic_memory"]

    def enter(self, aid: str, source: str, target: str) -> None:
        door = next(e for e in self.engine.snapshot_for_ui(source)["entities"]
                    if e["traits"].get("portal", {}).get("target_space_id") == target)
        result = self.engine.enter_portal(door["entity_id"], actor_id=aid)
        self.assertTrue(result["entered"], result)

    def test_first_arrival_waits_then_both_use_the_venue(self) -> None:
        first, second, town, cafe = self.pair()
        self.engine.record_social_feedback(
            first, town, "约好喝热饮", peer_id=second, speech_act={"shared_place_id": cafe, "shared_drive": "food"},
        )
        # A later ordinary exchange can replace the social signal without replacing the agreement.
        self.engine.record_social_feedback(first, town, "说了声好", peer_id=second)
        self.step_actor(first)
        self.assertEqual(self.engine.live_actor_frame(first)["space_id"], cafe)

        def stock() -> int:
            return sum(e["traits"]["consumable"]["uses_left"] for e in self.engine.snapshot_for_ui(cafe)["entities"]
                       if "consumable" in e["traits"] and e["z"] >= 0)

        before = stock()
        for _ in range(3):
            result = self.step_actor(first)
            self.assertEqual(result["cognition"]["phase"], "等候同伴")
        self.assertEqual(stock(), before)
        self.assertEqual(self.engine.live_actor_frame(second)["space_id"], town)
        self.assertEqual(self.open_world().cognitive_scene(first)["joint_visit"]["target"], cafe)

        for _ in range(6):
            if self.engine.cognitive_scene(second)["joint_visit"].get("met"):
                break
            self.step_actor(second)
        finished, companion = (second, first) if self.engine.cognitive_scene(second)["joint_visit"].get("done") else (first, second)
        for _ in range(8):
            if self.engine.cognitive_scene(finished)["joint_visit"].get("done"):
                break
            self.step_actor(finished)
        after_meeting = self.step_actor(finished)
        companion_name = self.engine.live_actor_frame(companion)["name"]
        self.assertEqual(after_meeting["cognition"]["goal"], f"陪{companion_name}在微风咖啡馆待一会儿")
        self.assertEqual(after_meeting["cognition"]["phase"], "结伴停留")

        for _ in range(12):
            for aid in (first, second):
                if self.engine.cognitive_scene(aid)["joint_visit"]:
                    self.step_actor(aid)
        self.assertEqual(stock(), before - 2)
        self.assertTrue(all(self.engine.cognitive_scene(aid)["joint_visit"] is None for aid in (first, second)))
        self.assertEqual(sum(event["kind"] == "actors_met_for_visit" for event in self.engine.read_recent_events(100)), 1)

    def test_wait_times_out_and_releases_both_participants(self) -> None:
        first, second, town, cafe = self.pair()
        self.engine.record_social_feedback(
            first, town, "约好去咖啡馆", peer_id=second,
            speech_act={"shared_place_id": cafe, "shared_drive": "social"},
        )
        self.engine.tick_physics(advance_minutes=5)
        self.step_actor(first, live=False)
        self.assertEqual(self.engine.live_actor_frame(first)["space_id"], cafe)
        self.step_actor(first)
        self.engine.tick_physics(advance_minutes=4.1)
        result = self.step_actor(first)
        self.assertTrue(any("还没能汇合" in line for line in result["actions"]))
        self.assertTrue(all(self.engine.cognitive_scene(aid)["joint_visit"] is None for aid in (first, second)))

    def test_live_meeting_starts_one_follow_up_despite_pair_cooldown(self) -> None:
        first, second, town, cafe = self.pair()
        live = self.heartbeat.live
        self.engine.record_social_feedback(
            first, town, "约好去咖啡馆坐坐", peer_id=second,
            speech_act={"shared_place_id": cafe, "shared_drive": "social"},
        )
        pair = tuple(sorted((first, second)))
        live._social_until[pair] = time.monotonic() + 45
        with patch.object(live, "talk", return_value=["两人在咖啡馆碰了面。"]) as talk:
            live.control(first, "phase2-test", "start", 4)
            deadline = time.monotonic() + 8
            while not talk.called and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(talk.called)
            live.control(first, "phase2-test", "pause", 4)
            follow_ups = [call for call in talk.call_args_list if "汇合" in call.args[2]]
            self.assertEqual(len(follow_ups), 1)
        self.assertTrue(all(self.engine.live_actor_frame(aid)["space_id"] == cafe for aid in pair))

    def test_declined_invitation_does_not_create_a_joint_visit(self) -> None:
        first, second, town, _ = self.pair()
        chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.settings)
        proposal = {
            "intent_a": "一起去微风咖啡馆喝热饮吧。",
            "intent_b": "嗯，我今天不想去咖啡馆。",
            "end_chat": True,
        }
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=proposal):
            result = chat.conduct_actor_social_exchange(first, second, generated_only=True)
        self.assertTrue(result["turns"])
        self.assertIn("不想去", result["turns"][0]["line_b"])
        self.assertTrue(all(self.engine.cognitive_scene(aid)["joint_visit"] is None for aid in (first, second)))

    def test_rest_consolidates_once_and_keeps_firsthand_impressions_after_reload(self) -> None:
        first, second, town, cafe = self.pair()
        self.enter(first, town, cafe)
        self.enter(second, town, cafe)
        self.engine.record_social_feedback(first, cafe, "聊起了街上的樱花", peer_id=second)
        food = next(e for e in self.engine.snapshot_for_ui(cafe)["entities"] if e["blueprint_id"] == "default_cake")
        sofa = next(e for e in self.engine.snapshot_for_ui(cafe)["entities"] if e["blueprint_id"] == "default_sofa")
        self.engine.execute_act(first, food["entity_id"], "consume")
        self.engine.tick_physics(advance_minutes=5)
        self.assertNotIn("archive", self.memory(first))
        self.engine.execute_act(first, sofa["entity_id"], "rest")
        self.engine.tick_physics(advance_minutes=1)
        self.assertNotIn("archive", self.memory(first))

        restored = self.open_world()
        restored.tick_physics(advance_minutes=1)
        restored.tick_physics(advance_minutes=10)
        memory = self.memory(first, restored)
        self.assertEqual(len(memory["archive"]), 1)
        self.assertCountEqual(
            [e["kind"] for e in memory["archive"][0]["episodes"]], ["exploration", "activity", "social"],
        )
        self.assertEqual(memory["people"][second]["summary"], "聊起了街上的樱花")
        self.assertIn(cafe, memory["places"])
        self.assertNotIn("archive", self.memory(second, restored))
        self.assertIn("休息后记住的亲历片段", projector.project_view(restored, first))
        chat = WorldChatService(restored, project_root=self.root, settings_provider=lambda: self.settings)
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=None) as ask:
            chat.chat(first, "你记得乙吗？", space_id="room_default")
            self.assertNotIn("聊起了街上的樱花", ask.call_args.args[0])
        restored.place_actor(first, "room_default", 5, 5)
        self.assertNotIn("聊起了街上的樱花", projector.project_view(restored, first, memory_cue="乙"))
        events = [event for event in restored.pending_persona_events() if event["payload"]["kind"] == "memory_consolidated"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["pending"], [first])
        self.assertEqual(events[0]["payload"]["epa"], {"e": 0.0, "p": 0.0, "a": 0.0})

    def test_elapsed_time_before_rest_does_not_count_toward_consolidation(self) -> None:
        actor = self.engine.import_actor(name="甲", x=3, y=4)["actor_id"]
        snack = self.engine.place_from_blueprint("default_snack", "room_default", 3, 5)
        bed = self.engine.place_from_blueprint("default_bed", "room_default", 4, 4)
        self.engine.execute_act(actor, snack["entity_id"], "consume")
        self.now.return_value += 300
        self.engine.execute_act(actor, bed["entity_id"], "rest")
        self.engine.tick_physics()
        self.assertNotIn("archive", self.memory(actor))
        self.now.return_value += 120
        self.engine.tick_physics()
        self.assertEqual(len(self.memory(actor)["archive"]), 1)

    def test_archive_matches_partial_place_names_and_summary_topics(self) -> None:
        memory = {}
        cafe = {"space_id": "cafe", "name": "微风咖啡馆"}
        room = {"space_id": "room", "name": "工作室"}
        episodic_memory.record(memory, 1, cafe, "activity", "尝了草莓蛋糕。")
        episodic_memory.record(memory, 2, cafe, "social", "谈到蓝莓松饼。", subject_id="guest", subject_name="甲")
        episodic_memory.consolidate(memory, 3)
        for index, note in enumerate(("画画。", "读书。", "唱歌。"), start=4):
            episodic_memory.record(memory, index, room, "activity", note)
        episodic_memory.consolidate(memory, 8)
        cognition = {"episodic_memory": memory}
        plain = episodic_memory.recall(cognition, "room", set())
        self.assertNotIn("尝了草莓蛋糕。", plain)
        self.assertIn("尝了草莓蛋糕。", episodic_memory.recall(cognition, "room", set(), query="上次去咖啡馆做了什么？"))
        self.assertEqual(episodic_memory.recall(cognition, "room", set(), query="还记得我提过蓝莓吗？")[0], "谈到蓝莓松饼。")
        self.assertEqual(episodic_memory.recall(cognition, "room", set(), query="草莓蛋糕味道怎么样？")[0], "尝了草莓蛋糕。")

    def test_both_player_chat_entries_recall_without_an_avatar(self) -> None:
        actor = self.engine.import_actor(name="甲", x=3, y=4)["actor_id"]
        peer = self.engine.import_actor(name="乙", x=8, y=4)["actor_id"]
        sid = "room_default"
        bed = self.engine.place_from_blueprint("default_bed", sid, 4, 4)["entity_id"]
        player_memory = "玩家说「曾在海边看见白鲸」，我说「想看看」。"
        self.engine.record_player_conversation(actor, sid, player_memory)
        self.engine.execute_act(actor, bed, "rest")
        self.engine.tick_physics(advance_minutes=2)
        self.engine.execute_act(actor, bed, "leave_rest")
        for note in ("我们聊了木琴。", "我们聊了电影。", "我们聊了月亮。"):
            self.engine.record_social_feedback(actor, sid, note, peer_id=peer)
        self.engine.execute_act(actor, bed, "rest")
        self.engine.tick_physics(advance_minutes=2)
        self.assertIsNone(self.engine.live_actor_frame("player_avatar"))
        self.assertNotIn(player_memory, projector.project_view(self.engine, actor))

        question = "你记得我们上次聊了什么吗？"
        chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.settings)
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=None) as ask:
            chat.chat(actor, question)
        self.assertIn(player_memory, ask.call_args.args[0])

        plugin = NanamiWorldPlugin()
        plugin._engine = self.engine
        plugin._settings_store = SimpleNamespace(get=lambda: self.settings)
        plugin._session_character = "甲"
        for enabled in (True, False):
            self.settings = replace(self.settings, episodic_memory_enabled=enabled)
            context = SimpleNamespace(messages=[{"role": "user", "content": question}])
            plugin._on_before_chat(context)
            self.assertEqual(player_memory in context.messages[0]["content"], enabled)

    def test_reflection_and_cognitive_status_use_consolidated_episodes(self) -> None:
        actor = self.engine.import_actor(name="甲", x=3, y=4)["actor_id"]
        sid = "room_default"
        bed = self.engine.place_from_blueprint("default_bed", sid, 4, 4)["entity_id"]
        note = "白鲸游过灯塔。"
        self.engine.record_player_conversation(actor, sid, note)
        self.engine.execute_act(actor, bed, "rest")
        self.engine.tick_physics(advance_minutes=2)
        self.engine.execute_act(actor, bed, "leave_rest")
        for enabled in (True, False):
            self.settings = replace(self.settings, episodic_memory_enabled=enabled)
            self.engine.update_actor_autonomy(actor, {"cognition": {"reflection": "想起了灯塔"}})
            scene = self.engine.cognitive_scene(actor, include_rooms=True)
            briefs = []
            scene["request_reflection"] = briefs.append
            self.heartbeat._cognitive.step(scene, self.settings, use_llm=False, talk=lambda *_: [])
            self.assertEqual(len(briefs), 1)
            prompt = self.heartbeat.live._format_reflection_brief(briefs[0])
            self.assertEqual(note in prompt, enabled)
            self.assertEqual(briefs[0]["memories"], [note] if enabled else [])
        actor_row = self.engine.cognitive_scene(actor, include_rooms=True)["actor"]
        self.assertEqual(cognitive_summary(actor_row)["memories"], [note])

    def test_custom_lodging_escort_uses_building_role(self) -> None:
        escort, guest, town, _ = self.pair()
        self.engine.add_town_building(town, name="静光台", role="home")
        self.engine.add_town_building(town, name="有间旅馆", role="cafe")
        places = self.engine.list_realm_places(town)
        lodging = next(pid for pid, name in places.items() if name == "静光台")
        cafe = next(pid for pid, name in places.items() if name == "有间旅馆")
        chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.settings)
        for pid, name in ((lodging, "静光台"), (cafe, "有间旅馆")):
            with self.subTest(name=name):
                proposal = {
                    "intent_a": f"这么晚了，我送你到{name}门口吧。", "intent_b": "嗯，谢谢你。", "end_chat": True,
                    "speech_act": {"shared_place_id": pid, "shared_drive": "social"},
                }
                with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=proposal):
                    result = chat.conduct_actor_social_exchange(escort, guest, generated_only=True)
                sides = result["speech_act"]
                if pid == lodging:
                    self.assertEqual(sides["a"]["personal_goal"]["action"], "escort")
                    self.assertEqual(sides["b"]["personal_goal"]["drive"], "rest")
                    self.assertIsNone(self.engine.cognitive_scene(escort)["joint_visit"])
                else:
                    self.assertNotIn("personal_goal", sides["a"])
                    self.assertEqual(self.engine.cognitive_scene(escort)["joint_visit"]["target"], cafe)

    def object_outing(self, blueprint: str) -> tuple[str, str, str, dict]:
        room = self.engine.create_space(name="小院", width=20, height=10, wall_rows=0)["space_id"]
        first = self.engine.import_actor(name="日向创", space_id=room, x=3, y=4)["actor_id"]
        second = self.engine.import_actor(name="七海千秋", space_id=room, x=4, y=4)["actor_id"]
        entity = self.engine.place_from_blueprint(blueprint, room, 11, 4)
        return first, second, room, entity

    def test_the_pair_holds_by_the_campfire_in_live_simulation(self) -> None:
        first, second, room, campfire = self.object_outing("default_campfire")
        chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.settings)
        proposal = {
            "intent_a": "日向君，要不要去篝火那边坐坐？", "intent_b": "嗯，好呀，正好晒晒太阳。",
        }
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=proposal) as model:
            result = chat.conduct_actor_social_exchange(second, first, generated_only=True)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["speech_act"]["a"]["shared_entity_id"], campfire["entity_id"])

        self.heartbeat.live.control(first, "campfire-test", "start", 4)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            visit = self.engine.cognitive_scene(first)["joint_visit"]
            if visit and visit.get("met"):
                break
            time.sleep(0.03)
        else:
            self.fail("The pair did not rendezvous at the campfire")
        time.sleep(0.55)
        for aid in (first, second):
            actor = self.engine.live_actor_frame(aid)
            self.assertEqual(actor["space_id"], room)
            self.assertLessEqual(self.engine.chebyshev_distance(actor, campfire), 1)
            self.assertEqual(actor["posture"], "standing")
        self.assertEqual(sum(e["kind"] == "actors_met_for_visit" for e in self.engine.read_recent_events(100)), 1)

    def test_multi_cell_heartbeat_approach_keeps_distinct_campfire_tiles(self) -> None:
        first, second, room, campfire = self.object_outing("default_campfire")
        self.engine.record_social_feedback(
            first, room, "约好去篝火边待一会儿", peer_id=second,
            speech_act={"shared_entity_id": campfire["entity_id"], "shared_action": "dwell"},
        )
        for _ in range(3):
            self.step_actor(first, live=False)
        for _ in range(3):
            self.step_actor(second, live=False)
        first_frame, second_frame = (self.engine.live_actor_frame(aid) for aid in (first, second))
        self.assertTrue(self.engine.cognitive_scene(first)["joint_visit"]["met"])
        self.assertNotEqual((first_frame["x"], first_frame["y"]), (second_frame["x"], second_frame["y"]))
        for actor in (first_frame, second_frame):
            self.assertLessEqual(self.engine.chebyshev_distance(actor, campfire), 1)

    def test_one_cushion_waits_for_both_and_reloaded_dialogue_suppresses_paraphrase(self) -> None:
        first, second, room, cushion = self.object_outing("default_cat_cushion")
        chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.settings)
        proposal = {
            "intent_a": "七海，坐垫那边有阳光，我们过去坐坐吧。",
            "intent_b": "嗯，走吧。",
            "memory_note": "两人决定一起去猫咪坐垫上休息。",
        }
        model_path = "plugins.nanami_world.text_compiler._ask_llm_json"
        with patch(model_path, return_value=proposal):
            result = chat.conduct_actor_social_exchange(first, second, generated_only=True)
        self.assertEqual(result["speech_act"]["b"]["shared_action"], "rest")
        original = self.engine.cognitive_scene(second)["joint_visit"]["id"]
        stored = chat.get_history(first, space_id=room)

        reloaded_chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.settings)
        paraphrase = {"intent_a": "嗯，走吧。", "intent_b": "坐垫那边有阳光，我们过去坐坐吧！", "end_chat": True}
        with patch(model_path, return_value=paraphrase):
            duplicate = reloaded_chat.conduct_actor_social_exchange(second, first)
        self.assertTrue(duplicate["duplicate"])
        different_words = {
            "intent_a": "要不要到猫咪坐垫旁边晒晒太阳？", "intent_b": "好呀，我正想找个地方歇一下。",
        }
        with patch(model_path, return_value=different_words):
            duplicate_plan = reloaded_chat.conduct_actor_social_exchange(first, second, generated_only=True)
        self.assertTrue(duplicate_plan["duplicate"])
        self.assertEqual(reloaded_chat.get_history(first, space_id=room), stored)
        self.assertEqual(self.engine.cognitive_scene(second)["joint_visit"]["id"], original)

        for _ in range(3):
            self.step_actor(first, live=False)
        self.assertEqual(self.engine.live_actor_frame(first)["posture"], "standing")
        self.assertEqual(self.engine.live_actor_frame(second)["posture"], "standing")
        self.assertLessEqual(self.engine.chebyshev_distance(self.engine.live_actor_frame(first), cushion), 1)
        self.assertFalse(self.engine.cognitive_scene(first)["joint_visit"].get("met"))
        for _ in range(3):
            self.step_actor(second, live=False)
        sitter = self.engine.live_actor_frame(second)
        companion = self.engine.live_actor_frame(first)
        self.assertEqual(sitter["resting_on_id"], cushion["entity_id"])
        self.assertEqual(companion["posture"], "standing")
        self.step_actor(first, live=False)
        self.assertEqual(self.engine.live_actor_frame(first)["move_seq"], companion["move_seq"])

    def test_live_personal_follow_up_keeps_the_action_for_the_motor(self) -> None:
        first, second, room, handheld = self.object_outing("default_handheld")
        chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.settings)
        proposal = {
            "intent_a": "我想试试掌机的手感。", "intent_b": "好，你先试试看。", "end_chat": True,
            "follow_up_a": {"entity_id": handheld["entity_id"], "action": "interact"},
        }
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=proposal):
            result = chat.conduct_actor_social_exchange(first, second, generated_only=True)
        self.assertEqual(result["speech_act"]["a"]["personal_goal"]["target"], handheld["entity_id"])
        self.assertEqual(self.engine.live_actor_frame(first)["x"], 3)
        for _ in range(4):
            if self.engine.cognitive_scene(first) is None:
                break
            self.step_actor(first, live=False)
        item = next(entity for entity in self.engine.snapshot_for_ui(room)["entities"]
                    if entity["entity_id"] == handheld["entity_id"])
        self.assertEqual(item["traits"]["activity"]["progress"], 1)


if __name__ == "__main__":
    unittest.main()
