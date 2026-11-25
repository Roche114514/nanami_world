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
                if "七海千秋" in system:
                    return {"candidates": ["顺着她的话承认自己在巡夜", "那个，日向君刚才看见桌上的笔记本了吗？"]}
                return {"candidates": ["顺着她的话回忆刚才路过桌角", "嗯，好像就在那边的桌角上。"]}
            return {}

        mind_calls: list[tuple[str, str, str]] = []

        def fake_mind(mind_mjs, state_root, actor_id, flag, text, *, space_id="room_default"):
            mind_calls.append((actor_id, flag, text))
            if flag == "--his":
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
        her_calls = [(aid, text) for aid, flag, text in mind_calls if flag == "--her"]
        self.assertEqual(len(her_calls), 2)

        # 3. Opaque realm suppresses logging raw internal thoughts into chat_history
        self.chat.append_autonomous_activity(
            a, action_summary="检查了「笔记本」", thought="这段心声绝不应该出现在公共对话框", emotion="平静", space_id=room,
        )
        hist_texts = [m["text"] for m in self.chat.get_history(a, space_id=room)]
        self.assertTrue(any("检查了「笔记本」" in t for t in hist_texts))
        self.assertFalse(any("这段心声绝不应该出现" in t for t in hist_texts))

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
