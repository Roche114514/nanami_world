"""Real-time autonomy runs independently of frame reads and slow model calls."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

from _shinsekai import PROJECT, WORLD_CODE

from plugins.nanami_world import PLUGIN_ID, PLUGIN_VERSION, world_page
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.live_simulation import LiveActor
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

    def test_chat_presence_requires_a_nearby_player_in_roam_and_rejects_observer(self) -> None:
        registry = PluginCapabilityRegistry()
        registry.set_settings_ui_plugin_context(PLUGIN_ID, PLUGIN_VERSION)
        world_page.register_pages(
            registry, code_dir=WORLD_CODE, engine=self.engine, heartbeat=self.heartbeat, world_chat=self.chat,
            settings_store=SimpleNamespace(get=lambda: self.settings),
        )
        canvas = next(page for page in registry.frontend_config_contributions if page.page_id == "world_canvas")
        presence = next(action.run for action in canvas.actions if action.id == "user_chat_presence")
        room = self.engine.create_space(name="对话房间", width=14, height=10, wall_rows=0)["space_id"]
        aid = self.engine.import_actor(name="七海千秋", space_id=room, x=4, y=4)["actor_id"]
        actor = LiveActor("viewer", self.engine.live_actor_frame(aid), float("inf"), 0)
        self.live._actors[aid] = actor

        self.engine.set_external_overlay(room, {"title": "观剧", "view_only": True})
        presence({"actor_id": aid, "active": True})
        self.assertEqual(actor.user_chat_until, 0)
        self.engine.set_external_overlay(room, None)
        pid = self.engine.ensure_player_avatar(space_id=room)["actor_id"]
        request = {"actor_id": aid, "player_id": pid, "roaming": True}
        self.engine.place_actor(pid, room, 10, 4)
        presence(request)
        self.assertEqual(actor.user_chat_until, 0)
        self.engine.place_actor(pid, room, 5, 4)
        presence({**request, "roaming": False})
        self.assertEqual(actor.user_chat_until, 0)
        presence(request)
        self.assertGreater(actor.user_chat_until, time.monotonic())
        presence({"actor_id": aid, "active": False})
        self.assertEqual(actor.user_chat_until, 0)
        self.engine.set_external_overlay(room, {"title": "观剧", "view_only": True})
        presence(request)
        self.assertEqual(actor.user_chat_until, 0)

    def test_story_social_waits_for_focus_release_and_ignores_small_talk_cooldown(self) -> None:
        room = self.engine.create_space(name="剧情讨论", width=12, height=10, wall_rows=0)["space_id"]
        actor, peer, third = [self.engine.import_actor(name=name, space_id=room, x=x, y=y)["actor_id"]
                              for name, x, y in (("甲", 4, 4), ("乙", 5, 4), ("丙", 5, 5))]
        bench = self.engine.place_from_blueprint("default_cat_cushion", room, 7, 5)
        self.engine.record_social_feedback(actor, room, "约好去坐坐", peer_id=third, speech_act={
            "shared_entity_id": bench["entity_id"], "shared_drive": "social",
        })
        self.assertTrue(self.engine.cognitive_scene(actor)["joint_visit"])
        self.engine.propose_actor_commitment(actor, {"kind": "social", "target": peer, "reason": "追问草稿"}, preempt=True)
        self.assertFalse(self.engine.cognitive_scene(actor)["joint_visit"])
        self.assertFalse(self.engine.cognitive_scene(third, include_rooms=True)["joint_visit"])
        for aid in (actor, peer, third):
            self.live._actors[aid] = LiveActor("viewer", self.engine.live_actor_frame(aid), float("inf"), 0, speed=1)
        self.live.set_dialogue_focus(room, [{"scene_id": "other", "actor_ids": [peer, third], "topic": "另一段对话", "revision": 0}])
        self.live._social_until[tuple(sorted((actor, peer)))] = time.monotonic() + 40.0
        with patch.object(self.live, "_request"), patch.object(self.live._social, "submit") as dispatch:
            self.live._advance(actor, self.live._actors[actor], self.settings)
            dispatch.assert_not_called()
            intention = self.engine.cognitive_scene(actor)["actor"]["autonomy"]["cognition"]["active_intention"]
            self.assertEqual(intention["source"], "persona_choice")
            self.assertEqual(intention["target"], peer)
            self.live.set_dialogue_focus(room, [])
            self.assertFalse(self.live._meet(actor, peer, "随便聊聊"))
            self.live._advance(actor, self.live._actors[actor], self.settings)
            dispatch.assert_called_once()
            self.assertEqual(dispatch.call_args.args[2:5], (actor, peer, "追问草稿"))

    def test_refused_ambient_social_blocks_cross_space_seek_and_immediate_exit(self) -> None:
        town = self.engine.create_default_town()["town_space_id"]
        self.engine.set_realm_initial_hour(town, 12.0)
        library = f"{town}__library"
        actor = self.engine.import_actor(name="甲", space_id=library, x=5, y=4)["actor_id"]
        peer = self.engine.import_actor(name="乙", space_id=library, x=6, y=4)["actor_id"]
        door = next(e for e in self.engine.snapshot_for_ui(town)["entities"]
                    if e["traits"].get("portal", {}).get("target_space_id") == library)
        self.engine.update_actor_autonomy(actor, {"cognition": {
            "last_social_at": -60.0,
            "active_intention": {
                "kind": "social", "key": f"social:{library}:{peer}:", "target": peer,
                "space": library, "x": 6, "y": 4, "drive": "social", "action": "",
                "phase": "approach", "goal": "和乙聊一聊",
            },
            "portals": {f"{town}:{library}": {
                "space": town, "target": library, "entity": door["entity_id"], "name": "湖畔图书馆",
                "x": door["x"], "y": door["y"], "source": "direct", "confidence": 1.0, "samples": [0.0],
            }},
        }})
        cognition = self.live.cognition
        candidates = cognition._candidates
        observed = []

        def collect(*args, **kwargs):
            rows = candidates(*args, **kwargs)
            observed.append(rows)
            return rows

        def step():
            scene = self.engine.cognitive_scene(actor, include_rooms=True)
            scene["engage"] = lambda *args: False
            cognition.step(scene, self.settings, use_llm=False, talk=lambda *_: [])

        with patch.object(cognition, "_candidates", side_effect=collect):
            step()
            self.assertFalse(any(row["kind"] == "exit" for row in observed[-1]))
            state = self.engine.cognitive_scene(actor)["actor"]["autonomy"]["cognition"]
            self.assertIn(f"peer:{peer}", state["blocked"])
            self.engine.place_actor(actor, town, 19, 15)
            step()
            self.assertFalse(any(row["kind"] == "seek_peer" and row["target"] == peer for row in observed[-1]))

            state = self.engine.cognitive_scene(actor)["actor"]["autonomy"]["cognition"]
            state["blocked"].pop(f"peer:{peer}")
            state["active_intention"] = None
            self.engine.update_actor_autonomy(actor, {"cognition": state})
            step()
            self.assertTrue(any(row["kind"] == "seek_peer" and row["target"] == peer for row in observed[-1]))

    def test_npc_playback_keeps_both_speakers_busy_until_the_last_bubble(self) -> None:
        room = self.engine.create_space(name="三人对白", width=12, height=10, wall_rows=0)["space_id"]
        a, b, c = (self.engine.import_actor(name=name, space_id=room, x=x, y=y)["actor_id"]
                   for name, x, y in (("甲", 5, 4), ("乙", 6, 4), ("丙", 5, 5)))
        for aid in (a, b, c):
            self.live._actors[aid] = LiveActor("viewer", self.engine.live_actor_frame(aid), float("inf"), 0)
        self.live._chatting.update((a, b))
        with patch.object(self.chat, "conduct_actor_social_exchange", return_value={
            "turns": [{"line_a": "我先看看这边。", "line_b": "我再去确认一下。"}],
        }):
            self.live._speak(self.chat, a, b, "核对情况")
        end = self.live._actors[b].speech_until
        self.assertLess(self.live._actors[a].speech_queue[-1][2], end)
        with patch.object(self.live._social, "submit"), patch(
            "plugins.nanami_world.live_simulation.time.monotonic", return_value=end - 0.1,
        ):
            self.assertFalse(self.live._meet(c, a, "跟甲打招呼"))
            self.assertFalse(self.live._meet(c, b, "跟乙打招呼"))
        with patch.object(self.live._social, "submit"), patch(
            "plugins.nanami_world.live_simulation.time.monotonic", return_value=end,
        ):
            self.assertFalse(self.live._meet(a, b, "马上再聊"))
            self.assertTrue(self.live._meet(c, a, "跟甲打招呼"))

    def test_talking_and_persona_reflection_do_not_drain_world_physical_energy(self) -> None:
        room = self.engine.create_space(name="长谈测试", width=12, height=10, wall_rows=0)["space_id"]
        first = self.engine.import_actor(name="哈贝特洛特", space_id=room, x=5, y=4)["actor_id"]
        second = self.engine.import_actor(name="丰川祥子", space_id=room, x=6, y=4)["actor_id"]
        topics = iter("苹果 雨伞 山脉 钢琴 电灯 蓝莓 告别 宫殿 沙发 灯塔 桃花 菜单 车站 路牌 阳光 笔记 木桥 小河 绿茶 琴谱 门票 风筝 星星 沙滩 钟楼 橱柜".split())

        def dialogue(*args, **kwargs):
            topic = next(topics)
            return {"intent_a": f"聊聊{topic}？", "intent_b": f"{topic}挺有意思。"}

        original = self.engine.live_actor_frame(first)["vitals"]
        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", side_effect=dialogue), \
                patch.object(self.chat, "_mouth_speak", side_effect=lambda name, ctx, intent, *a, **kw: intent), \
                patch.object(self.chat, "_run_mind_mjs", return_value=None):
            for _ in range(13):
                exchange = self.chat.conduct_actor_social_exchange(first, second, generated_only=True, use_mouth=True)
                self.assertEqual(exchange["turns_count"], 2)
        self.assertEqual(self.engine.live_actor_frame(first)["vitals"]["energy"], original["energy"])
        self.assertGreater(self.engine.live_actor_frame(first)["vitals"]["mood_v"], original["mood_v"])

        with patch("plugins.nanami_world.live_simulation.persona_bridge.emit_world_event_to_persona",
                   return_value={"ok": True}) as emit, patch(
            "plugins.nanami_world.live_simulation.persona_bridge.read_persona_state",
            side_effect=[{"world_state": True, "energy": 0.7}, {"world_state": True, "energy": 0.55}],
        ):
            self.live._record_thought(first, {"realm": room, "host": "哈贝特洛特", "experience": "聊天"},
                                      {"thought": "谈得差不多了。", "agenda": "暂歇"})
            emit.assert_called_once()
        self.assertEqual(self.engine.live_actor_frame(first)["vitals"]["energy"], original["energy"])

    def test_observer_clock_keeps_running_through_a_dialogue_and_actors_resume(self) -> None:
        room = self.engine.create_space(name="观剧场景", width=12, height=10, wall_rows=0)["space_id"]
        first = self.engine.import_actor(name="甲", space_id=room, x=5, y=4)["actor_id"]
        second = self.engine.import_actor(name="乙", space_id=room, x=6, y=4)["actor_id"]
        entered, release = threading.Event(), threading.Event()

        def conversation(*_args, **_kwargs):
            entered.set()
            release.wait(8)
            return {"turns": [{"line_a": "我先看看这边。", "line_b": "我再去确认一下。"}]}

        with patch.object(self.chat, "conduct_actor_social_exchange", side_effect=conversation):
            self.live.set_background_roster(room, [first, second])
            self.live.control(first, "viewer", "start", 4)
            self.live._meet(first, second, "核对情况", user_requested=True)
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue({first, second} <= self.live._chatting)
                during = self.engine._world["simulation_minutes"]
                self.until(lambda: self.engine._world["simulation_minutes"] > during, timeout=4.5)
                self.assertTrue(self.live.queue_action(first, {"tool": "world_move", "x": 2, "y": 4})[0])
            finally:
                release.set()
            self.until(lambda: self.engine.live_actor_frame(first)["x"] == 2, timeout=5)
            self.assertFalse({first, second} & self.live._chatting)
            self.live.control(first, "viewer", "stop", 1)
            self.assertTrue(self.live.managed(first))

    def test_three_layer_social_mouth_intent_sanitize_and_opaque_thought_hiding(self) -> None:
        # 1. Third-person intent leaks are cleaned and heavily penalized against natural spoken dialogue
        self.assertEqual(self.chat._sanitize_spoken_line("应一声，说展柜合着呢"), "展柜合着呢")
        bad_score = self.chat._score_mouth_candidate("顺着她的话承认自己在巡夜", [], [])
        bad_score_2 = self.chat._score_mouth_candidate("应一声，说展柜合着呢", [], [])
        good_score = self.chat._score_mouth_candidate("展柜还合着呢，我刚才路过看了一眼。", [], [])
        self.assertGreater(good_score, bad_score)
        self.assertGreater(good_score, bad_score_2)

        # 2. conduct_actor_social_exchange(..., generated_only=True, use_mouth=True) engages _mouth_speak and mind.mjs --her
        room = self.engine.create_space(name="三层对话室", width=12, height=10, wall_rows=0, opaque_minds=True)["space_id"]
        a = self.engine.import_actor(name="七海千秋", space_id=room, x=4, y=4)["actor_id"]
        b = self.engine.import_actor(name="日向创", space_id=room, x=5, y=4)["actor_id"]

        def fake_llm(system, user, **kwargs):
            if '"intent_a"' in system:
                return {"intent_a": "询问对方有没有看到桌上的笔记本", "intent_b": "回应说刚才似乎瞥见过", "end_chat": True}
            if '"candidates"' in system:
                if "「七海千秋」的发声" in system:
                    return {"candidates": ["顺着她的话承认自己在巡夜", "那个，日向君刚才看见桌上的笔记本了吗？"]}
                return {"candidates": ["顺着她的话回忆刚才路过桌角", "嗯，好像就在那边的桌角上。"]}
            return {}

        mind_calls: list[tuple[str, str, str]] = []

        def fake_mind(mind_mjs, state_root, actor_id, flag, text, *, space_id="room_default"):
            mind_calls.append((actor_id, flag, text))
            if flag in ("--his", "--idle"):
                return {"card": f"{actor_id} 心境微紧"}
            return None

        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", side_effect=fake_llm), \
                patch.object(self.chat, "_run_mind_mjs", side_effect=fake_mind), \
                patch.object(self.chat, "_mouth_speak", wraps=self.chat._mouth_speak) as mouth_spy:
            res = self.chat.conduct_actor_social_exchange(a, b, reason="核对笔记", generated_only=True, use_mouth=True)

        self.assertEqual(mouth_spy.call_count, 2)
        self.assertEqual(mouth_spy.call_args_list[0].kwargs.get("mind_card"), f"{a} 心境微紧")
        self.assertEqual(mouth_spy.call_args_list[1].kwargs.get("mind_card"), f"{b} 心境微紧")
        self.assertEqual(res["turns"][0]["line_a"], "那个，日向君刚才看见桌上的笔记本了吗？")
        self.assertEqual(res["turns"][0]["line_b"], "嗯，好像就在那边的桌角上。")
        self.assertEqual(mind_calls[:2], [(a, "--idle", ""), (b, "--idle", "")])
        his_calls = [(aid, text) for aid, flag, text in mind_calls if flag == "--his"]
        self.assertEqual(his_calls, [(a, res["turns"][0]["line_b"]), (b, res["turns"][0]["line_a"])])
        her_calls = [(aid, text) for aid, flag, text in mind_calls if flag == "--her"]
        self.assertEqual(len(her_calls), 2)

        # 3. Opaque realm suppresses logging raw internal thoughts into chat_history
        self.chat.append_autonomous_activity(
            a, action_summary="检查了「笔记本」", thought="这段心声绝不应该出现在公共对话框", emotion="平静", space_id=room,
        )
        hist_texts = [m["text"] for m in self.chat.get_history(a, space_id=room)]
        self.assertTrue(any("检查了「笔记本」" in t for t in hist_texts))
        self.assertFalse(any("这段心声绝不应该出现" in t for t in hist_texts))

    def test_story_dialogue_waits_for_peer_and_then_preempts_ambient_routes(self) -> None:
        room = self.engine.create_space(name="中庭", width=18, height=10, wall_rows=0)["space_id"]
        a = self.engine.import_actor(name="七海千秋", space_id=room, x=3, y=4)["actor_id"]
        b = self.engine.import_actor(name="日向创", space_id=room, x=12, y=4)["actor_id"]
        now = time.monotonic()
        first = LiveActor("test", self.engine.live_actor_frame(a), float("inf"), now, speed=4, route=[(2, 4)])
        second = LiveActor("test", self.engine.live_actor_frame(b), float("inf"), now, speed=4, route=[(13, 4)])
        self.live._actors.update({a: first, b: second})
        self.live.set_dialogue_focus(room, [{
            "scene_id": "paper_note",
            "actor_ids": [a, b],
            "topic": "谈谈纸条上的话",
            "revision": 0,
        }])
        second.user_chat_until = now + 3
        with patch("plugins.nanami_world.live_simulation.time.monotonic", return_value=now):
            self.live._advance(a, first, self.settings)
        self.assertEqual(first.phase, "waiting")
        self.assertEqual(first.route, [])
        self.assertEqual(self.engine.live_actor_frame(a)["x"], 3)

        now += 4
        first.engagement = second.engagement = ("ambient", "interact")
        with patch.object(self.live._social, "submit") as dispatch:
            for _ in range(40):
                now += 0.25
                with patch("plugins.nanami_world.live_simulation.time.monotonic", return_value=now):
                    for aid, actor in ((a, first), (b, second)):
                        if actor.next_at <= now:
                            self.live._advance(aid, actor, self.settings)
                if dispatch.called:
                    break
            self.assertTrue(dispatch.called, "两名当事人应会合并生成关键对白")
            self.assertEqual(dispatch.call_args.args[2:4], (a, b))
            self.assertEqual(dispatch.call_args.kwargs["focus"].scene_id, "paper_note")
        self.assertEqual((second.frame["x"], second.frame["y"]), (12, 4))

    def test_npc_exchange_retries_false_arrival_and_commits_only_pair_dialogue(self) -> None:
        room = self.engine.create_space(name="庭院", width=16, height=12, wall_rows=0)["space_id"]
        elsewhere = self.engine.create_space(name="休息室", width=12, height=10, wall_rows=0, parent_space_id=room)["space_id"]
        habetrot = self.engine.import_actor(name="哈贝特洛特", space_id=room, x=4, y=4)["actor_id"]
        hinata = self.engine.import_actor(name="日向创", space_id=room, x=5, y=4)["actor_id"]
        nanami = self.engine.import_actor(name="七海千秋", space_id=elsewhere, x=4, y=4)["actor_id"]
        self.engine.import_actor(name="远处的人", space_id=elsewhere, x=4, y=4)
        self.engine.set_actor_private_brief(
            hinata, "当前决定：之后在掌机上打两局。\n见面想表达：七海！正好，要不要来一局？\n亲见实证：看过草稿。"
        )
        self.engine.set_actor_private_brief(habetrot, "见面想表达（对「七海千秋」）：来给咱讲讲这一关。")

        planner_calls = []

        def fake_llm(system, user, **kwargs):
            if '"intent_a"' in system:
                planner_calls.append(user)
                return {
                    "intent_a": "商量日向创之后怎么开口",
                    "intent_b": "回应刚才的话",
                    "memory_note": "日向创直接邀请七海玩游戏。",
                    "end_chat": True,
                }
            if "发声嘴巴" in system and "「哈贝特洛特」的发声" in system:
                options = ["七海都站到跟前了，你还磨蹭啥？", "趁人就在跟前赶紧开口啊"]
                if len(planner_calls) > 1:
                    options += ["要是碰到七海，你先问她想玩什么。"]
                return {"candidates": options}
            return {"candidates": [
                "七海，来一局吧。", "（对七海说）有空吗，陪我打一局？", "我先去找她，见面再问。",
            ]}

        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", side_effect=fake_llm):
            result = self.chat.conduct_actor_social_exchange(habetrot, hinata, generated_only=True, use_mouth=True)

        self.assertEqual(len(planner_calls), 2)
        self.assertIn("【重写本轮】", planner_calls[1])
        self.assertIn("日向创眼前：无", planner_calls[0])
        self.assertNotIn("远处的人", planner_calls[0])
        self.assertNotIn("七海！正好", planner_calls[0])
        self.assertNotIn("来给咱讲讲这一关", planner_calls[0])
        self.assertIn("亲见实证：看过草稿。", planner_calls[0])
        self.assertEqual(result["turns"][0]["line_a"], "要是碰到七海，你先问她想玩什么。")
        self.assertEqual(result["turns"][0]["line_b"], "我先去找她，见面再问。")
        events = [e["payload"] for e in self.engine.read_recent_events() if e["kind"] == "dialogue_exchange"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["speaker_id"], events[0]["partner_id"]), (habetrot, hinata))
        self.assertNotIn("直接邀请七海", events[0]["summary"])
        self.assertEqual(self.chat.get_history(nanami, space_id=elsewhere), [])
        self.assertNotIn("站到跟前了", str(self.chat.get_history(hinata, space_id=room)))

    def test_visible_bystander_cannot_be_silently_added_as_listener(self) -> None:
        room = self.engine.create_space(name="庭院", width=10, height=10, wall_rows=0)["space_id"]
        a = self.engine.import_actor(name="哈贝特洛特", space_id=room, x=4, y=4)["actor_id"]
        b = self.engine.import_actor(name="日向创", space_id=room, x=5, y=4)["actor_id"]
        self.engine.import_actor(name="七海千秋", space_id=room, x=4, y=5)
        bad = {"line_a": "七海都站到跟前了。", "line_b": "七海，来一局吧。", "end_chat": True}
        good = {"line_a": "七海都站到跟前了。", "line_b": "我待会儿就去找她。", "end_chat": True}
        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", side_effect=[bad, bad]):
            invalid = self.chat.conduct_actor_social_exchange(a, b, generated_only=True)
        self.assertEqual(invalid["turns"], [])
        self.assertTrue(invalid["invalid_dialogue"])
        self.assertEqual(self.chat.get_history(a, space_id=room), [])
        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", return_value=good):
            valid = self.chat.conduct_actor_social_exchange(a, b, generated_only=True)
        self.assertEqual(valid["turns"][0]["line_a"], good["line_a"])

    def test_night_skip_at_2330_curfew_wakes_and_restores_residents(self) -> None:
        room = self.engine.create_space(name="夜晚宿舍", width=12, height=10, wall_rows=0)["space_id"]
        bed_a = self.engine.place_from_blueprint("default_bed", room, 2, 2)["entity_id"]
        bed_b = self.engine.place_from_blueprint("default_bed", room, 8, 2)["entity_id"]
        a = self.engine.import_actor(name="住客甲", space_id=room, x=2, y=3)["actor_id"]
        b = self.engine.import_actor(name="住客乙", space_id=room, x=8, y=3)["actor_id"]
        self.engine.set_realm_initial_hour(room, 23.6)

        # Neither asleep -> no night skip
        self.assertIsNone(self.engine.try_skip_night(room))
        # Only one asleep (the other still active at night) -> no night skip
        self.engine.execute_act(a, bed_a, "rest")
        self.assertIsNone(self.engine.try_skip_night(room))
        # Both asleep, but mid-conversation -> no night skip
        self.engine.execute_act(b, bed_b, "rest")
        self.assertIsNone(self.engine.try_skip_night(room, chatting_ids={b}))

        self.engine.record_social_feedback(a, room, "睡前聊起明早见面的安排", peer_id=b)
        skip = self.engine.try_skip_night(room, consolidate_memory=True)
        self.assertIsNotNone(skip)
        self.assertEqual(skip["to_clock"], "07:00")
        lt = self.engine.get_lighting(room)
        self.assertGreaterEqual(float(lt["hour_float"]), 6.9)
        self.assertLessEqual(float(lt["hour_float"]), 7.1)
        for aid in (a, b):
            fr = self.engine.live_actor_frame(aid)
            self.assertEqual(fr["posture"], "standing")
            self.assertIsNone(fr["resting_on_id"])
            self.assertGreaterEqual(float(fr["vitals"]["energy"]), 0.94)
            self.assertLessEqual(float(fr["vitals"]["sleep_pressure"]), 0.08)
        # Second immediate call refuses to double-skip same night
        self.assertIsNone(self.engine.try_skip_night(room))

    def test_cinema_focus_across_spaces_and_camera_ui_invariants(self) -> None:
        town_res = self.engine.create_default_town()
        town = town_res["town_space_id"]
        cafe = f"{town}__cafe"
        idle_a = self.engine.import_actor(name="路人丙", space_id=town, x=15, y=15)["actor_id"]
        spk_1 = self.engine.import_actor(name="七海千秋", space_id=cafe, x=4, y=4)["actor_id"]
        spk_2 = self.engine.import_actor(name="日向创", space_id=cafe, x=5, y=4)["actor_id"]

        entered, release = threading.Event(), threading.Event()

        def conv(*_a, **_kw):
            entered.set()
            release.wait(6)
            return {"turns": [{"line_a": "这里的茶杯还是温的。", "line_b": "我刚好看过吧台。"}]}

        with patch.object(self.chat, "conduct_actor_social_exchange", side_effect=conv):
            self.live.control(idle_a, "cam_viewer", "start", 4)
            self.until(lambda: self.live.managed(spk_1) and self.live.managed(spk_2), timeout=5)
            self.live._meet(spk_1, spk_2, "核对线索", user_requested=True)
            try:
                self.assertTrue(entered.wait(2))
                during_view = self.live.frame(idle_a, "cam_viewer", -1)
                cf_during = during_view.get("cinema_focus") or {}
                self.assertIn(cf_during.get("actor_id"), (spk_1, spk_2))
                self.assertEqual(cf_during.get("space_id"), cafe)
                self.assertGreaterEqual(int(cf_during.get("priority") or 0), 2)
            finally:
                release.set()
            speaking_focus = self.until(
                lambda: (
                    (f := (self.live.frame(idle_a, "cam_viewer", -1).get("cinema_focus") or {}))
                    and f.get("priority") == 3 and f
                ),
                timeout=4,
            )
            self.assertEqual(speaking_focus["space_id"], cafe)
            self.assertIn(speaking_focus["actor_id"], (spk_1, spk_2))
            self.live.control(idle_a, "cam_viewer", "stop", 1)

        html = (PROJECT / "plugins" / "nanami_world" / "web" / "world.html").read_text(encoding="utf-8")
        self.assertIn("camera-mode-switch", html)
        for cam_mode in ('data-cam="free"', 'data-cam="follow"', 'data-cam="cinema"'):
            self.assertIn(cam_mode, html)
        self.assertIsNone(re.search(r"\b(?:window\.)?(?:alert|confirm|prompt)\s*\(", html))

    def test_park_bench_and_chairs_sit_awake_and_never_act_as_sleep_beds(self) -> None:
        from plugins.nanami_world import projector, schema
        from plugins.nanami_world.outdoor_designer import synthesize_outdoor_blueprint

        room = self.engine.create_space(name="广场长椅测试", width=12, height=10, wall_rows=0)["space_id"]
        bench_bp = synthesize_outdoor_blueprint("park_bench")
        self.assertEqual(bench_bp["traits"]["restable"]["rest_mode"], "sit")
        saved_bench = self.engine.save_blueprint(bench_bp)
        bench_ent = self.engine.place_from_blueprint(saved_bench["blueprint_id"], room, 4, 4)
        self.assertEqual(bench_ent["traits"]["restable"]["rest_mode"], "sit")

        sofa_bp = schema.normalize_blueprint({
            "name": "丝绒听音长椅",
            "traits": {"restable": {"energy_regen_per_min": 0.015, "comfort": 0.85, "occupied_by": None}},
            "tags": ["furniture", "sofa", "rest"],
        })
        self.assertEqual(sofa_bp["traits"]["restable"]["rest_mode"], "sit")

        chair_bp = schema.normalize_blueprint({
            "name": "靠背木椅",
            "traits": {"restable": {"energy_regen_per_min": 0.011, "comfort": 0.72, "occupied_by": None}},
            "tags": ["furniture", "chair", "rest"],
        })
        self.assertEqual(chair_bp["traits"]["restable"]["rest_mode"], "sit")

        bed_bp = self.engine.all_blueprints()["default_bed"]
        self.assertEqual(bed_bp["traits"]["restable"]["rest_mode"], "sleep")

        hinata = self.engine.import_actor(name="日向创", space_id=room, x=4, y=5)["actor_id"]
        nanami = self.engine.import_actor(name="七海千秋", space_id=room, x=5, y=4)["actor_id"]

        # Sitting on bench sets posture="sitting" (awake!), never sleeping
        res = self.engine.execute_act(hinata, bench_ent["entity_id"], "rest")
        self.assertIn("坐了下来", res)
        self.assertNotIn("睡觉", res)
        hinata_frame = self.engine.live_actor_frame(hinata)
        self.assertEqual(hinata_frame["posture"], "sitting")
        self.assertEqual(hinata_frame["resting_on_id"], bench_ent["entity_id"])

        raw_h = self.engine._world["actors"][hinata]
        self.assertNotEqual(raw_h["expression"]["tag"], "熟睡")
        self.assertIn("正坐着", projector.project_view(self.engine, hinata))
        self.assertIn("正坐在上面", " ".join(self.engine.entity_state_tags(self.engine._world["entities"][bench_ent["entity_id"]])))

        # Peer can talk normally with seated actor (NOT blocked as asleep)
        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", return_value={
            "intent_a": "日向君，坐在这里吹风吗？",
            "intent_b": "嗯，我有些事想跟你商量。",
            "end_chat": True,
        }):
            talk_res = self.chat.conduct_actor_social_exchange(nanami, hinata, reason="在长椅边说话", generated_only=True)
        self.assertTrue(talk_res["turns"])
        self.assertEqual(talk_res["turns"][0]["line_b"], "嗯，我有些事想跟你商量。")

        # Late night: sitting on a park bench NEVER counts as sleeping in bed
        self.engine.set_realm_initial_hour(room, 23.6)
        self.assertIsNone(self.engine.try_skip_night(room))

        # Sleepy actor looking for a bed in heartbeat NEVER picks a park bench
        bed_ent = self.engine.place_from_blueprint("default_bed", room, 8, 2)
        self.engine.place_actor(nanami, room, 7, 2)
        self.engine.set_actor_vitals(nanami, {"energy": 0.15, "sleep_pressure": 0.90})
        self.heartbeat.step(actor_id=nanami, advance_minutes=0.2, use_llm=False)
        self.heartbeat.step(actor_id=nanami, advance_minutes=0.2, use_llm=False)
        nanami_after = self.engine.live_actor_frame(nanami)
        self.assertEqual(nanami_after.get("resting_on_id"), bed_ent["entity_id"])
        self.assertEqual(nanami_after.get("posture"), "resting")
        # Even without any bed present, a sleepy actor must NOT go sleep on a free park bench
        self.engine.execute_act(hinata, bench_ent["entity_id"], "leave_rest")
        self.engine.remove_entity(bed_ent["entity_id"])
        sleepy_c = self.engine.import_actor(name="困倦旅人", space_id=room, x=3, y=4)["actor_id"]
        self.engine.set_actor_vitals(sleepy_c, {"energy": 0.12, "sleep_pressure": 0.92})
        self.heartbeat.step(actor_id=sleepy_c, advance_minutes=0.2, use_llm=False)
        self.heartbeat.step(actor_id=sleepy_c, advance_minutes=0.2, use_llm=False)
        self.assertNotEqual(self.engine.live_actor_frame(sleepy_c).get("resting_on_id"), bench_ent["entity_id"])
        self.assertNotEqual(self.engine.live_actor_frame(sleepy_c).get("posture"), "resting")

    def test_cafe_lounge_sofa_sits_awake_and_persona_choice_wakes_sleeper(self) -> None:
        from plugins.nanami_world import schema

        cafe_sofa_bp = schema.normalize_blueprint({
            "name": "靠窗卡座沙发",
            "brief": "暖布垫包裹的舒适小憩卡座。",
            "traits": {"restable": {"energy_regen_per_min": 0.016, "comfort": 0.88, "occupied_by": None}},
            "tags": ["furniture", "sofa", "rest"],
        })
        self.assertEqual(cafe_sofa_bp["traits"]["restable"]["rest_mode"], "sit")
        self.assertEqual(self.engine.all_blueprints()["default_sofa"]["traits"]["restable"]["rest_mode"], "sleep")

        room = self.engine.create_space(name="自习休眠测试", width=12, height=10, wall_rows=0)["space_id"]
        saved_cafe_sofa = self.engine.save_blueprint(cafe_sofa_bp)
        sofa_ent = self.engine.place_from_blueprint(saved_cafe_sofa["blueprint_id"], room, 4, 4)
        bed_ent = self.engine.place_from_blueprint("default_bed", room, 8, 2)
        nanami = self.engine.import_actor(name="七海千秋", space_id=room, x=4, y=5)["actor_id"]
        hinata = self.engine.import_actor(name="日向创", space_id=room, x=2, y=5)["actor_id"]

        # Lounge/booth sofa sets posture="sitting", never sleeping
        res_sofa = self.engine.execute_act(nanami, sofa_ent["entity_id"], "rest")
        self.assertIn("坐了下来", res_sofa)
        self.assertEqual(self.engine.live_actor_frame(nanami)["posture"], "sitting")

        # Even if actor fell asleep on a real bed, committing a story persona_choice wakes them up
        self.engine.execute_act(nanami, bed_ent["entity_id"], "rest")
        self.assertEqual(self.engine.live_actor_frame(nanami)["posture"], "resting")
        self.engine.set_actor_vitals(nanami, {"energy": 0.78, "sleep_pressure": 0.15})
        self.engine.propose_actor_commitment(
            nanami,
            {"commitment_id": "fate:wake_test", "kind": "social", "target": hinata, "reason": "去找日向创说话", "priority": "high"},
            space_id=room,
            preempt=True,
        )
        for _ in range(8):
            self.heartbeat.step(actor_id=nanami, advance_minutes=0.2, use_llm=False)
            if self.engine.live_actor_frame(nanami)["posture"] != "resting":
                break
        self.assertNotEqual(self.engine.live_actor_frame(nanami)["posture"], "resting")


if __name__ == "__main__":
    unittest.main()
