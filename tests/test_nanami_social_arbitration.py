"""Brain-inspired social brain, speech-act, and Daw dynamic arbitration tests."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import persona_bridge, projector
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService


class SocialArbitrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.persona = self.root / "persona"
        self.persona.mkdir()
        self.settings = WorldSettings(heartbeat_use_llm=True, persona_data_dir=str(self.persona))
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)
        self.heartbeat = WorldHeartbeat(
            self.engine, lambda: self.settings, project_root=self.root,
            chat_service_provider=lambda: self.chat,
        )
        self.addCleanup(self.heartbeat.stop)
        self.live = self.heartbeat.live
        self.chat = WorldChatService(
            self.engine, project_root=self.root, settings_provider=lambda: self.settings,
            action_scheduler=self.live.queue_action,
        )

    def until(self, condition, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = condition()
            if res:
                return res
            time.sleep(0.03)
        self.fail("Condition did not settle before timeout")

    def test_repeated_social_turn_is_not_saved_or_applied(self) -> None:
        for generated_only in (True, False):
            with self.subTest(generated_only=generated_only):
                room = self.engine.create_space(name="对话测试房", width=10, height=8, wall_rows=0)["space_id"]
                a = self.engine.import_actor(name="甲", space_id=room, x=4, y=4)["actor_id"]
                b = self.engine.import_actor(name="乙", space_id=room, x=5, y=4)["actor_id"]
                snack = self.engine.place_from_blueprint("default_snack", room, 4, 5)
                first = {
                    "intent_a": "今晚的月色真美。", "intent_b": "是啊，又熬夜打游戏了吧？",
                    "memory_note": "聊到月色和游戏", "end_chat": False,
                }
                repeated = {
                    "intent_a": "今晚的月色真美！", "intent_b": "是啊。又熬夜打游戏了吧？",
                    "memory_note": "重复轮的总结", "speech_act": {"tone": "tense"},
                    "follow_up_a": {"entity_id": snack["entity_id"], "action": "consume"},
                    "end_chat": True,
                }
                with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=[first, repeated]) as model, \
                        patch.object(self.chat, "_mouth_speak", side_effect=lambda *args, **kwargs: args[2]):
                    result = self.chat.conduct_actor_social_exchange(a, b, generated_only=generated_only)
                self.assertEqual(model.call_count, 2)
                self.assertEqual(result["turns_count"], 1)
                self.assertEqual(result["turns"][0]["line_a"], first["intent_a"])
                self.assertIsNone(result["speech_act"])
                for aid in (a, b):
                    action, line_a, line_b = self.chat.get_history(aid, space_id=room)
                    self.assertIn("·1轮]", action["text"])
                    self.assertIn(first["memory_note"], action["text"])
                    self.assertTrue(line_a["text"].endswith(first["intent_a"]))
                    self.assertTrue(line_b["text"].endswith(first["intent_b"]))
                event = next(ev for ev in self.engine.read_recent_events() if ev["kind"] == "actor_social_talk" and ev["space_id"] == room)
                self.assertEqual(event["payload"]["turns_count"], 1)
                current_snack, = self.engine.visible_entities(a, entity_id=snack["entity_id"])
                self.assertEqual(current_snack["traits"]["consumable"]["uses_left"], snack["traits"]["consumable"]["uses_left"])

    def test_repeated_short_reply_does_not_end_a_new_social_turn(self) -> None:
        room = self.engine.create_space(name="对话测试房", width=10, height=8, wall_rows=0)["space_id"]
        a = self.engine.import_actor(name="甲", space_id=room, x=4, y=4)["actor_id"]
        b = self.engine.import_actor(name="乙", space_id=room, x=5, y=4)["actor_id"]
        rows = [
            {"intent_a": "今晚的月色真美。", "intent_b": "嗯。", "end_chat": False},
            {"intent_a": "窗户旁边也能看到。", "intent_b": "嗯。", "end_chat": True},
        ]
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=rows):
            result = self.chat.conduct_actor_social_exchange(a, b, generated_only=True)
        self.assertEqual(result["turns_count"], 2)

    def test_nighttime_escort_leads_to_rest_after_a_second_chat_at_the_dorm(self) -> None:
        escort = self.engine.import_actor(name="日向创")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=escort)["town_space_id"]
        dorm = f"{town}__guest"
        door = next(e for e in self.engine.snapshot_for_ui(town)["entities"]
                    if e["traits"].get("portal", {}).get("target_space_id") == dorm)
        bed = next(e for e in self.engine.snapshot_for_ui(dorm)["entities"]
                   if e.get("blueprint_id") == "default_bed")
        x, y = door["x"], door["y"]
        self.engine.place_actor(escort, town, x, y + 3)
        sleeper = self.engine.import_actor(name="七海千秋", space_id=town, x=x, y=y + 4)["actor_id"]
        now = datetime.now()
        night = now.replace(hour=23, minute=0, second=0, microsecond=0)
        self.engine._world["clock_offset_minutes"] = (night - now).total_seconds() / 60

        initial_chat = [
            {"intent_a": "七海，这么晚了你还不回去吗？", "intent_b": "嗯，现在准备回去了。", "end_chat": False},
            {"intent_a": "我送你到宿舍门口吧。", "intent_b": "嗯，谢谢日向君。", "end_chat": True,
             "speech_act": {"shared_place_id": dorm, "shared_drive": "social"}},
        ]
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=initial_chat):
            result = self.chat.conduct_actor_social_exchange(escort, sleeper, generated_only=True)
        self.assertEqual(result["speech_act"]["a"]["personal_goal"]["action"], "escort")
        self.assertEqual(result["speech_act"]["b"]["personal_goal"]["drive"], "rest")

        def decide(aid, modulation=None):
            scene = self.engine.cognitive_scene(aid)
            scene["engage"] = lambda *args: True
            if modulation:
                scene["modulation"] = modulation
            return self.heartbeat._cognitive.step(scene, self.settings, use_llm=False, talk=lambda *args: [])

        decide(sleeper)
        decide(escort)
        for aid in (sleeper, escort):
            frame = self.engine.live_actor_frame(aid)
            self.assertEqual(frame["space_id"], town)
            self.assertLessEqual(abs(frame["x"] - x) + abs(frame["y"] - y), 2)

        next_chat = {
            "intent_a": "日向君，今晚的星星很漂亮呢。", "intent_b": "是啊，夜空很清澈。", "end_chat": True,
            "speech_act": {"shared_place_id": dorm, "shared_drive": "social"},
        }
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=next_chat):
            second = self.chat.conduct_actor_social_exchange(sleeper, escort, generated_only=True)
        self.assertEqual(second["turns_count"], 1)
        decide(escort)
        decision = decide(sleeper)
        self.assertEqual(decision["drive"], "rest")
        self.assertEqual(self.engine.live_actor_frame(sleeper)["space_id"], dorm)

        reflection = {
            "id": 1, "at": self.engine.cognitive_scene(sleeper)["minutes"],
            "half_life": 12.0, "strength": 1.0, "commitment": {
                "id": "pfc:arcade", "source": "pfc", "priority": "high", "kind": "visit",
                "target": f"{town}__arcade", "drive": "game",
            },
        }
        self.assertEqual(decide(sleeper, reflection)["drive"], "rest")
        for _ in range(30):
            self.engine.tick_physics(advance_minutes=0.05)
            decide(sleeper)
            if self.engine.live_actor_frame(sleeper)["resting_on_id"]:
                break
        self.assertEqual(self.engine.live_actor_frame(sleeper)["resting_on_id"], bed["entity_id"])
        decide(escort)
        self.assertEqual(self.engine.live_actor_frame(escort)["space_id"], town)
        self.engine.tick_physics(advance_minutes=0.5)
        decide(sleeper)
        self.assertEqual(self.engine.live_actor_frame(sleeper)["resting_on_id"], bed["entity_id"])

    def test_van_interrupts_solo_walk_for_peer_and_respects_cold_stance(self) -> None:
        a1 = self.engine.import_actor(name="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=a1)["town_space_id"]
        a2 = self.engine.import_actor(name="日向创", space_id=town, x=19, y=15)["actor_id"]
        self.engine.place_actor(a1, town, 15, 15)
        self.engine.update_actor_autonomy(a1, {
            "cognition": {
                "active_intention": {
                    "kind": "explore", "key": f"explore:{town}:34:15:", "target": "34:15",
                    "space": town, "x": 34, "y": 15, "goal": "去东边街区走走",
                    "drive": "explore", "utility": 3.2, "action": "", "phase": "travel",
                },
            },
        })

        def model(system, user, **kwargs):
            if '"intent_a"' in system:
                return {"intent_a": "日向君，你也出来走走啊？", "intent_b": "嗯，刚好碰到了。", "end_chat": True}
            return {}

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=model), \
                patch.object(self.chat, "_mouth_speak", side_effect=lambda name, ctx, intent, *a, **kw: intent):
            self.live.control(a1, "window", "start", 4)
            self.until(lambda: any("刚好碰到了" in m["text"] for m in self.chat.get_history(a1, space_id=town)), timeout=7.0)
            actor_row = next(a for a in self.engine.list_actors() if a["actor_id"] == a1)
            arb = (actor_row.get("autonomy", {}).get("cognition") or {}).get("arbitration")
            self.assertEqual(arb, "社会脑/语言主导")
        self.live.control(a1, "window", "stop", 1)

        cold_engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world-cold", project_root=self.root)
        cold_hb = WorldHeartbeat(cold_engine, lambda: self.settings, project_root=self.root)
        self.addCleanup(cold_hb.stop)
        ca = cold_engine.import_actor(name="甲", host_character="甲")["actor_id"]
        ctown = cold_engine.create_default_town(move_actor_id=ca)["town_space_id"]
        cb = cold_engine.import_actor(name="乙", host_character="乙", space_id=ctown, x=18, y=15)["actor_id"]
        cold_engine.place_actor(ca, ctown, 15, 15)
        arcade = f"{ctown}__arcade"
        door = next(e for e in cold_engine.snapshot_for_ui(ctown)["entities"]
                    if e["traits"].get("portal", {}).get("target_space_id") == arcade)
        persona_bridge.save_world_actor_relation(cold_engine.data_root, ctown, "甲", "char:乙", {"known": True, "attitude_stance": "cold"})
        persona_bridge.save_world_actor_relation(cold_engine.data_root, ctown, "乙", "char:甲", {"known": True, "attitude_stance": "cold"})
        cold_engine.update_actor_autonomy(ca, {
            "cognition": {
                "portals": {
                    f"{ctown}:{arcade}": {
                        "space": ctown, "target": arcade, "name": "星海电玩厅",
                        "x": door["x"], "y": door["y"], "entity": door["entity_id"],
                        "source": "direct", "confidence": 1.0, "samples": [0.0],
                    },
                },
                "active_intention": {
                    "kind": "visit", "key": f"visit:{ctown}:{arcade}:", "target": arcade,
                    "space": ctown, "x": door["x"], "y": door["y"], "goal": "去「星海电玩厅」",
                    "drive": "game", "utility": 4.2, "action": "", "phase": "travel",
                },
            },
        })
        cold_hb.live.control(ca, "window", "start", 4)
        self.until(lambda: cold_engine.live_actor_frame(ca)["space_id"] == arcade, timeout=7.0)
        self.assertFalse(any(ev["kind"] == "actor_social_talk" for ev in cold_engine.read_recent_events(120)))

    def test_speech_act_creates_shared_commitment_and_beliefs_for_both_actors(self) -> None:
        a1 = self.engine.import_actor(name="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=a1)["town_space_id"]
        a2 = self.engine.import_actor(name="日向创", space_id=town, x=20, y=15)["actor_id"]
        self.engine.place_actor(a1, town, 19, 15)
        cafe = f"{town}__cafe"

        def model(system, user, **kwargs):
            if '"intent_a"' in system:
                return {
                    "intent_a": "听说微风咖啡馆新上了热茶，我们一起去坐坐吧？",
                    "intent_b": "好呀，现在过去正好。",
                    "speech_act": {
                        "shared_place_id": cafe,
                        "shared_drive": "food",
                        "shared_beliefs": [{"place_id": cafe, "domain": "food", "note": "有热茶"}],
                        "tone": "warm",
                    },
                    "end_chat": True,
                }
            return {}

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=model), \
                patch.object(self.chat, "_mouth_speak", side_effect=lambda name, ctx, intent, *a, **kw: intent):
            self.live.control(a1, "window", "start", 4)
            self.until(
                lambda: (
                    self.engine.live_actor_frame(a1)["space_id"] == cafe
                    and self.engine.live_actor_frame(a2)["space_id"] == cafe
                ),
                timeout=9.0,
            )
            actors = {a["actor_id"]: a for a in self.engine.list_actors()}
            for aid in (a1, a2):
                cog = actors[aid]["autonomy"]["cognition"]
                self.assertTrue(str(cog.get("applied_commitment_id") or "").startswith("sa:"), cog)
                self.assertIn(cafe, cog["places"])
                self.assertIn("food", cog["places"][cafe].get("affordances", {}))

    def test_user_chat_language_modulation_preempts_walk_and_obeys_sleep_guardrail(self) -> None:
        a1 = self.engine.import_actor(name="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=a1)["town_space_id"]
        library = f"{town}__library"
        arcade = f"{town}__arcade"
        door_arc = next(e for e in self.engine.snapshot_for_ui(town)["entities"]
                        if e["traits"].get("portal", {}).get("target_space_id") == arcade)
        self.engine.place_actor(a1, town, 19, 15)
        self.engine.update_actor_autonomy(a1, {
            "cognition": {
                "portals": {
                    f"{town}:{arcade}": {
                        "space": town, "target": arcade, "name": "星海电玩厅",
                        "x": door_arc["x"], "y": door_arc["y"], "entity": door_arc["entity_id"],
                        "source": "direct", "confidence": 1.0, "samples": [0.0],
                    },
                },
                "active_intention": {
                    "kind": "visit", "key": f"visit:{town}:{arcade}:", "target": arcade,
                    "space": town, "x": door_arc["x"], "y": door_arc["y"], "goal": "去「星海电玩厅」",
                    "drive": "game", "utility": 4.2, "action": "", "phase": "travel",
                },
            },
        })
        start_seq = self.engine.live_actor_frame(a1)["move_seq"]

        with patch("plugins.nanami_world.live_simulation.text_compiler._ask_llm_json", return_value={}):
            self.live.control(a1, "window", "start", 4)
            self.until(lambda: self.engine.live_actor_frame(a1)["move_seq"] >= start_seq + 2, timeout=4.0)
            with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", return_value={
                "world_actions": [],
                "cognitive_effect": {
                    "agenda": "听完话想去湖畔图书馆看看书",
                    "commit_place_id": library,
                    "commit_drive": "reading",
                    "beliefs": [{"place_id": library, "domain": "reading", "note": "很安静"}],
                },
                "emotion": "好奇",
                "intent": "好呀，那我这就去湖畔图书馆看看书。",
            }), patch.object(self.chat, "_mouth_speak", return_value="好呀，那我这就去湖畔图书馆看看书。"):
                self.chat.chat(a1, "待会儿去湖畔图书馆看看书吧")

            self.until(lambda: self.engine.live_actor_frame(a1)["space_id"] == library, timeout=9.0)
            ref_log = self.live.frame(a1, "window", -1)["reflection"]
            self.assertTrue(any(row["status"] == "preempted" for row in ref_log), ref_log)
        self.live.control(a1, "window", "stop", 1)

        sleepy = self.engine.import_actor(name="困倦角色", space_id=town, x=19, y=15)["actor_id"]
        self.engine.set_actor_world_mood(sleepy, space_id=town, vitals_patch={
            "sleep_pressure": 0.92, "energy": 0.12, "mood_v": 0.0, "source": "builtin",
        })
        with patch("plugins.nanami_world.world_chat.text_compiler._ask_llm_json", return_value={
            "world_actions": [],
            "cognitive_effect": {"agenda": "想去图书馆", "commit_place_id": library, "commit_drive": "reading"},
            "emotion": "困倦",
            "intent": "唔……好困，待会儿看看吧。",
        }), patch.object(self.chat, "_mouth_speak", return_value="唔……好困，待会儿看看吧。"):
            self.chat.chat(sleepy, "去图书馆看会儿书吧")
        self.heartbeat.step(actor_id=sleepy, advance_minutes=1, use_llm=False)
        sleepy_cog = next(a for a in self.engine.list_actors() if a["actor_id"] == sleepy)["autonomy"]["cognition"]
        self.assertEqual(sleepy_cog.get("arbitration"), "生理稳态主导")
        self.assertNotEqual((sleepy_cog.get("active_intention") or {}).get("target"), library)

    def test_cross_space_seek_peer_and_stale_location_prediction_error(self) -> None:
        (self.persona / "pointers.yaml").write_text(
            "pointers:\n- id: seeker\n  character_name: Seeker\n  state_root: seeker\n",
            encoding="utf-8",
        )
        pstate = self.persona / "seeker" / "state"
        pstate.mkdir(parents=True)
        (pstate / "traits.json").write_text(json.dumps({
            "traits": {
                "surgency": 0.95, "socialEfficacy": 0.90,
                "needProfile": {"relatedness": 0.98, "safety": 0.25, "competence": 0.4},
                "domainEfficacy": {"game": 0.2, "nonGame": 0.4},
            },
        }), encoding="utf-8")

        seeker = self.engine.import_actor(name="Seeker", host_character="Seeker")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=seeker)["town_space_id"]
        library = f"{town}__library"
        peer = self.engine.import_actor(name="Peer", space_id=library, x=9, y=6)["actor_id"]
        self.engine.update_actor_autonomy(peer, {"enabled": False})

        door_lib = next(e for e in self.engine.snapshot_for_ui(town)["entities"]
                        if e["traits"].get("portal", {}).get("target_space_id") == library)
        self.engine.place_actor(seeker, library, 4, 6)
        self.heartbeat.step(actor_id=seeker, advance_minutes=1, use_llm=False)
        self.engine.place_actor(seeker, town, 19, 15)
        s_actor = next(a for a in self.engine.list_actors() if a["actor_id"] == seeker)
        cog = dict(s_actor["autonomy"]["cognition"])
        cog["last_social_at"] = float(cog.get("last_social_at", 0)) - 60.0
        cog["portals"][f"{town}:{library}"] = {
            "space": town, "target": library, "name": "湖畔图书馆",
            "x": door_lib["x"], "y": door_lib["y"], "entity": door_lib["entity_id"],
            "source": "direct", "confidence": 1.0, "samples": [0.0],
        }
        cog["peers"][peer]["spoke_at"] = -1e9
        cog["active_intention"] = None
        self.engine.update_actor_autonomy(seeker, {"cognition": cog})

        def model(system, user, **kwargs):
            if '"intent_a"' in system:
                return {"intent_a": "终于在图书馆找到你啦！", "intent_b": "嗯，我刚好看一会儿书。", "end_chat": True}
            return {}

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=model), \
                patch.object(self.chat, "_mouth_speak", side_effect=lambda name, ctx, intent, *a, **kw: intent):
            self.live.control(seeker, "window", "start", 4)
            self.until(
                lambda: any("找到你啦" in m["text"] for m in self.chat.get_history(seeker, space_id=town)),
                timeout=9.0,
            )
            self.assertEqual(self.engine.live_actor_frame(seeker)["space_id"], library)
        self.live.control(seeker, "window", "stop", 1)

        # Case B: peer left the remembered library before seeker walks inside -> clears stale space & records prediction error
        cafe = f"{town}__cafe"
        self.engine.place_actor(peer, cafe, 5, 5)
        self.engine.place_actor(seeker, library, 5, 5)
        s_actor2 = next(a for a in self.engine.list_actors() if a["actor_id"] == seeker)
        cog2 = dict(s_actor2["autonomy"]["cognition"])
        cog2["peers"][peer]["space"] = library
        cog2["active_intention"] = {
            "kind": "seek_peer", "key": f"seek_peer:{library}:{peer}:",
            "target": peer, "target_space": library, "space": library,
            "x": 5, "y": 5, "goal": "去「湖畔图书馆」找Peer",
            "drive": "social", "utility": 8.0, "action": "", "phase": "travel",
        }
        self.engine.update_actor_autonomy(seeker, {"cognition": cog2})
        self.heartbeat.step(actor_id=seeker, advance_minutes=1, use_llm=False)
        after_cog = next(a for a in self.engine.list_actors() if a["actor_id"] == seeker)["autonomy"]["cognition"]
        self.assertEqual(after_cog["peers"][peer]["space"], "")
        self.assertIn("已经不在这里了", after_cog["prediction_error"])

    def test_saturated_town_never_freezes_and_same_town_distant_peers_meet(self) -> None:
        def _saturated_cognition(engine: WorldEngine, town: str) -> dict:
            snap = engine.snapshot_for_ui(town)
            board = next(e for e in snap["entities"] if "布告栏" in e["name"])
            tracks = {f"frontier|{town}:{gx}:{gy}": [1.0, 0.0] for gx in range(10) for gy in range(8)}
            tracks["action|explore"] = [2.6, 0.0]
            tracks["action|notice"] = [0.9, 0.0]
            places = {town: {"name": "樱町小镇", "kind": "town", "source": "direct", "visits": 19, "samples": [0.0]}}
            portals = {}
            for b in snap["space"].get("buildings") or ():
                pid = b.get("interior_space_id") or ""
                if not pid:
                    continue
                places[pid] = {"name": b["name"], "kind": "building", "source": "direct", "visits": 3, "samples": [0.0]}
                portals[f"{town}:{pid}"] = {
                    "space": town, "target": pid, "name": b["name"],
                    "x": b["door_x"], "y": b["door_y"], "source": "direct", "confidence": 1.0, "samples": [0.0],
                }
                tracks[f"place|{pid}"] = [2.3, 0.0]
            return {
                "places": places,
                "portals": portals,
                "tracks": tracks,
                "read": {board["entity_id"]: [[e["by"], e["text"]] for e in board["traits"]["inscribable"]["entries"][-5:]]},
                "active_intention": None,
                "last_social_at": -10.0,
            }

        # Subcase 1: single actor in fully saturated town still moves smoothly (no "四处观察" freeze) + llm_enabled flag
        self.settings = WorldSettings(heartbeat_use_llm=False, persona_data_dir=str(self.persona))
        solo = self.engine.import_actor(name="Solo")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=solo)["town_space_id"]
        self.engine.place_actor(solo, town, 27, 16)
        self.engine.update_actor_autonomy(solo, {"cognition": _saturated_cognition(self.engine, town)})
        start_seq = self.engine.live_actor_frame(solo)["move_seq"]
        self.live.control(solo, "window", "start", 4)
        self.until(lambda: self.engine.live_actor_frame(solo)["move_seq"] >= start_seq + 3, timeout=5.0)
        solo_after = next(a for a in self.engine.list_actors() if a["actor_id"] == solo)
        self.assertNotEqual(solo_after["autonomy"].get("current_goal"), "四处观察")
        self.assertIs(self.live.frame(solo, "window", -1)["llm_enabled"], False)
        self.live.control(solo, "window", "stop", 1)

        # Subcase 2: two close friends 25 tiles apart in saturated town cross town to meet and talk
        pair_engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world-pair", project_root=self.root)
        pair_settings = WorldSettings(heartbeat_use_llm=True, persona_data_dir=str(self.persona))
        pair_hb = WorldHeartbeat(
            pair_engine, lambda: pair_settings, project_root=self.root, chat_service_provider=lambda: pair_chat,
        )
        self.addCleanup(pair_hb.live._social.shutdown, wait=True)
        self.addCleanup(pair_hb.stop)
        pair_chat = WorldChatService(
            pair_engine, project_root=self.root, settings_provider=lambda: pair_settings,
            action_scheduler=pair_hb.live.queue_action,
        )
        nanami = pair_engine.import_actor(name="七海千秋", host_character="七海千秋")["actor_id"]
        ptown = pair_engine.create_default_town(move_actor_id=nanami)["town_space_id"]
        hinata = pair_engine.import_actor(name="日向创", host_character="日向创", space_id=ptown, x=2, y=12)["actor_id"]
        pair_engine.place_actor(nanami, ptown, 27, 16)
        persona_bridge.save_world_actor_relation(
            pair_engine.data_root, ptown, "七海千秋", "char:日向创",
            {"known": True, "rel_stage": "close", "attitude_stance": "warm", "warmth": 1.0, "affective_valence": 1.0},
        )
        persona_bridge.save_world_actor_relation(
            pair_engine.data_root, ptown, "日向创", "char:七海千秋",
            {"known": True, "rel_stage": "close", "attitude_stance": "warm", "warmth": 1.0, "affective_valence": 1.0},
        )
        sat_cog = _saturated_cognition(pair_engine, ptown)
        pair_engine.update_actor_autonomy(nanami, {"cognition": dict(sat_cog)})
        pair_engine.update_actor_autonomy(hinata, {"cognition": dict(sat_cog)})

        def model(system, user, **kwargs):
            if '"intent_a"' in system:
                nanami_line = "日向君，跑这么远总算找到你了。"
                hinata_line = "我也在找你呢，七海。"
                if system.startswith("你是「七海千秋」"):
                    return {"intent_a": nanami_line, "intent_b": hinata_line, "end_chat": True}
                return {"intent_a": hinata_line, "intent_b": nanami_line, "end_chat": True}
            return {}

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=model), \
                patch.object(pair_chat, "_mouth_speak", side_effect=lambda name, ctx, intent, *a, **kw: intent):
            pair_hb.live.control(nanami, "window", "start", 4)
            self.until(
                lambda: any("我也在找你呢，七海。" in m["text"] for m in pair_chat.get_history(nanami, space_id=ptown)),
                timeout=9.0,
            )

    def test_visit_distant_sleeping_peer_across_town_observes_quietly_without_omniscience(self) -> None:
        now = datetime.now()
        noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
        self.engine._world["clock_offset_minutes"] = (noon - now).total_seconds() / 60
        nanami = self.engine.import_actor(name="七海千秋", host_character="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=nanami)["town_space_id"]
        home = f"{town}__home"
        cushion = next(
            (e for e in self.engine.snapshot_for_ui(town)["entities"]
             if e["x"] == 35 and e["y"] == 21 and "restable" in e.get("traits", {})),
            None,
        ) or self.engine.place_from_blueprint("default_cat_cushion", town, 35, 21)
        hinata = self.engine.import_actor(name="日向创", host_character="日向创", space_id=town, x=35, y=21)["actor_id"]
        self.engine.execute_act(hinata, cushion["entity_id"], "rest")
        self.engine.update_actor_autonomy(hinata, {"enabled": False})

        self.engine.place_actor(nanami, town, 6, 8)
        self.assertNotIn(hinata, self.engine.visible_peer_ids(nanami))
        view_text = projector.project_view(self.engine, nanami)
        self.assertNotIn("休息中", view_text)

        self.engine.place_actor(nanami, home, 6, 4)
        self.engine.update_actor_autonomy(nanami, {"cognition": {"active_intention": None}})
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value={
            "world_actions": [],
            "cognitive_effect": {},
            "emotion": "平静",
            "intent": "好，我这就过去看看日向君。",
        }), patch.object(self.chat, "_mouth_speak", return_value="好，我这就过去看看日向君。"):
            res = self.chat.chat(nanami, "你去看看日向创吧")
        self.assertNotIn("不在同一空间", res["reply"])
        pending_mod = next(a for a in self.engine.list_actors() if a["actor_id"] == nanami)["autonomy"]["cognition"].get("pending_modulation") or {}
        self.assertEqual((pending_mod.get("commitment") or {}).get("target"), hinata)

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value={}):
            self.live.control(nanami, "window", "start", 4)
            self.until(
                lambda: (
                    (f_n := self.engine.live_actor_frame(nanami))
                    and (f_h := self.engine.live_actor_frame(hinata))
                    and f_n["space_id"] == town
                    and self.engine.chebyshev_distance(f_n, f_h) <= 1
                    and any(
                        ev["kind"] == "actor_autonomous_step" and ev["actor_id"] == nanami
                        and "正在休息" in str(ev.get("payload", {}).get("summary") or "")
                        for ev in self.engine.read_recent_events(80)
                    )
                ),
                timeout=12.0,
            )
            self.live.control(nanami, "window", "stop", 1)
            hinata_after = self.engine.live_actor_frame(hinata)
            self.assertEqual(hinata_after["posture"], "resting")
            cog = next(a for a in self.engine.list_actors() if a["actor_id"] == nanami)["autonomy"]["cognition"]
            self.assertEqual((cog.get("peers") or {}).get(hinata, {}).get("posture"), "resting")
            self.assertEqual((cog.get("modulation") or {}).get("strength", 1.0), 1.0)
            self.assertIn("没有打扰", cog.get("reflection") or "")

    def test_no_doorway_ping_pong_and_depleted_affordance_memory(self) -> None:
        nanami = self.engine.import_actor(name="七海千秋", host_character="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=nanami)["town_space_id"]
        home = f"{town}__home"
        self.engine.place_actor(nanami, home, 6, 4)
        self.heartbeat.step(actor_id=nanami, advance_minutes=0.2, use_llm=False)

        home_snap = self.engine.snapshot_for_ui(home)
        handheld = next(e for e in home_snap["entities"] if e["blueprint_id"] == "default_handheld")
        drink = next(e for e in home_snap["entities"] if e["blueprint_id"] == "default_hot_drink")
        hh_traits = dict(handheld["traits"])
        hh_traits["activity"] = {**hh_traits["activity"], "battery": 0}
        self.engine.update_entity_spec(handheld["entity_id"], traits=hh_traits)
        dr_traits = dict(drink["traits"])
        dr_traits["consumable"] = {**dr_traits["consumable"], "uses_left": 0}
        self.engine.update_entity_spec(drink["entity_id"], traits=dr_traits)
        self.heartbeat.step(actor_id=nanami, advance_minutes=0.2, use_llm=False)
        aff = next(a for a in self.engine.list_actors() if a["actor_id"] == nanami)["autonomy"]["cognition"]["places"][home]["affordances"]
        self.assertLess(aff["game"]["confidence"], 0.30)
        self.assertLess(aff["food"]["confidence"], 0.30)

        fresh = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world-door", project_root=self.root)
        hb2 = WorldHeartbeat(fresh, lambda: self.settings, project_root=self.root)
        self.addCleanup(hb2.stop)
        aid = fresh.import_actor(name="七海千秋", host_character="七海千秋")["actor_id"]
        t2 = fresh.create_default_town(move_actor_id=aid)["town_space_id"]
        h2 = f"{t2}__home"
        cafe = f"{t2}__cafe"
        snap2 = fresh.snapshot_for_ui(t2)
        d_home = next(e for e in snap2["entities"] if e["traits"].get("portal", {}).get("target_space_id") == h2)
        d_cafe = next(e for e in snap2["entities"] if e["traits"].get("portal", {}).get("target_space_id") == cafe)
        fresh.place_actor(aid, t2, 6, 8)
        now_min = float(snap2.get("minutes") or 0.0)
        if not now_min:
            now_min = float(fresh.cognitive_scene(aid)["minutes"])
        fresh.update_actor_autonomy(aid, {
            "cognition": {
                "places": {
                    t2: {"name": "樱町小镇", "kind": "town", "source": "direct", "visits": 2, "samples": [now_min]},
                    h2: {
                        "name": "七海的小屋", "kind": "building", "source": "direct", "visits": 2,
                        "arrived": now_min - 0.3, "last_left": now_min, "samples": [now_min],
                        "affordances": {"game": {"source": "direct", "confidence": 1.0, "samples": [now_min]}},
                    },
                    cafe: {
                        "name": "微风咖啡馆", "kind": "building", "source": "direct", "visits": 0, "samples": [now_min],
                        "affordances": {"food": {"source": "signboard", "confidence": 0.65, "samples": [now_min]}},
                    },
                },
                "portals": {
                    f"{t2}:{h2}": {"space": t2, "target": h2, "name": "七海的小屋", "x": d_home["x"], "y": d_home["y"], "entity": d_home["entity_id"], "source": "direct", "confidence": 1.0, "samples": [now_min]},
                    f"{t2}:{cafe}": {"space": t2, "target": cafe, "name": "微风咖啡馆", "x": d_cafe["x"], "y": d_cafe["y"], "entity": d_cafe["entity_id"], "source": "direct", "confidence": 1.0, "samples": [now_min]},
                },
                "tracks": {f"place|{h2}": [2.5, now_min]},
                "active_intention": None,
            },
        })
        fresh.set_actor_world_mood(aid, space_id=t2, vitals_patch={"energy": 0.85, "sleep_pressure": 0.15, "source": "builtin"})
        scene = fresh.cognitive_scene(aid, include_rooms=True)
        scene["engage"] = lambda *a: False
        decision = hb2._cognitive.step(scene, self.settings, use_llm=False, talk=lambda *a: [])
        self.assertNotEqual(decision["goal"], "去「七海的小屋」")

    def test_player_avatar_is_not_remembered_or_pursued_across_doorways(self) -> None:
        aid = self.engine.import_actor(name="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=aid)["town_space_id"]
        home = f"{town}__home"
        door = next(e for e in self.engine.snapshot_for_ui(town)["entities"]
                    if e["traits"].get("portal", {}).get("target_space_id") == home)
        player_id = self.engine.ensure_player_avatar(space_id=town)["actor_id"]
        self.engine.place_actor(player_id, town, door["x"], door["y"] + 2)
        self.engine.place_actor(aid, town, door["x"], door["y"] + 1)

        def decide():
            scene = self.engine.cognitive_scene(aid, include_rooms=True)
            scene["engage"] = lambda *args: True
            return self.heartbeat._cognitive.step(scene, self.settings, use_llm=False, talk=lambda *args: [])

        decide()
        cognition = self.engine._world["actors"][aid]["autonomy"]["cognition"]
        self.assertNotIn(player_id, cognition["peers"])

        self.engine.place_actor(player_id, home, 6, 4)
        self.engine.place_actor(aid, town, door["x"], door["y"] + 1)
        now = self.engine.cognitive_scene(aid)["minutes"]
        old_goal = {
            "kind": "seek_peer", "key": f"seek_peer:{town}:{player_id}:", "target": player_id,
            "target_space": home, "space": town, "goal": "去小屋找你", "drive": "social", "action": "",
        }
        self.engine.update_actor_autonomy(aid, {"cognition": {
            **cognition,
            "peers": {player_id: {"name": "你", "space": home, "seen_at": now, "posture": "standing"}},
            "active_intention": old_goal,
            "suspended_intention": old_goal,
        }})
        self.assertFalse(self.engine.inject_language_modulation(aid, {
            "commit_peer_id": player_id, "peer_beliefs": [{"actor_id": player_id, "place_id": home}],
        }))
        self.assertNotEqual(decide()["goal"], old_goal["goal"])
        cognition = self.engine._world["actors"][aid]["autonomy"]["cognition"]
        self.assertNotIn(player_id, cognition["peers"])
        for key in ("active_intention", "suspended_intention"):
            self.assertNotEqual((cognition.get(key) or {}).get("target"), player_id)

    def test_resting_actor_does_not_oscillate_expression_events(self) -> None:
        cfg_dir = self.root / "data" / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "characters.yaml").write_text(
            "- name: 七海千秋\n"
            "  character_brief: 测试\n"
            "  sprites:\n"
            "    - sprite/nanami/n1.png\n"
            "    - sprite/nanami/n2.png\n"
            "    - sprite/nanami/n3.png\n"
            "    - sprite/nanami/n4.png\n"
            "    - sprite/nanami/n5.png\n"
            "    - sprite/nanami/n6.png\n"
            '  emotion_tags: "sprite 01: 中性，平静\\nsprite 02: 疲倦，困倦，打哈欠\\nsprite 03: 微笑，开心\\nsprite 04: 沮丧，难过\\nsprite 05: 害羞，脸红\\nsprite 06: 筋疲力尽，困倦，流口水"\n',
            encoding="utf-8",
        )
        eng = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world-rest-expr", project_root=self.root)
        hb = WorldHeartbeat(eng, lambda: self.settings, project_root=self.root)
        self.addCleanup(hb.stop)
        room = eng.create_space(name="卧室", width=10, height=8, wall_rows=0)["space_id"]
        aid = eng.import_actor(name="七海千秋", host_character="七海千秋", space_id=room, x=4, y=4)["actor_id"]
        cushion = eng.place_from_blueprint("default_cat_cushion", room, 4, 4)
        eng.execute_act(aid, cushion["entity_id"], "rest")
        eng.set_actor_world_mood(aid, space_id=room, vitals_patch={"energy": 0.65, "sleep_pressure": 0.50, "source": "builtin"})
        baseline_seq = max((ev["seq"] for ev in eng.read_recent_events(500)), default=0)

        for _ in range(6):
            eng.tick_physics(advance_minutes=0.2)
            hb.step(actor_id=aid, advance_minutes=0.0, force_autonomy=True, use_llm=False)

        actor_now = next(a for a in eng.list_actors() if a["actor_id"] == aid)
        self.assertEqual(actor_now["expression"]["bucket"], "sleep_deep")
        new_expr_evs = [
            ev for ev in eng.read_recent_events(500)
            if ev["seq"] > baseline_seq and ev["kind"] == "actor_expression_changed"
        ]
        self.assertEqual(new_expr_evs, [])

    def test_en_route_peer_interception_and_misrouted_actor_id_and_egocentric_percept(self) -> None:
        hinata = self.engine.import_actor(name="日向创", host_character="日向创")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=hinata)["town_space_id"]
        arcade = f"{town}__arcade"
        nanami = self.engine.import_actor(name="七海千秋", host_character="七海千秋", space_id=arcade, x=6, y=5)["actor_id"]
        self.engine.update_actor_autonomy(nanami, {"enabled": False})
        self.engine.place_actor(hinata, town, 19, 15)

        # Egocentric outdoor view & L0 social salience
        self.engine.place_actor(nanami, town, 19, 13)
        view_near = projector.project_view(self.engine, hinata)
        self.assertIn("眼前/视线内的人物", view_near)
        self.assertNotIn("同屋的其他角色", view_near)
        scene_now = self.engine.cognitive_scene(hinata)
        l0 = persona_bridge.build_percept_frame(
            self.engine.data_root, self.root, scene_now["space"], scene_now["lighting"],
            scene_now["actor"], scene_now["visible"],
            actors_map={p["actor_id"]: p for p in scene_now["actors"]},
            visible_peer_ids=set(scene_now["visible_peer_ids"]),
            custom_persona_dir=str(self.persona),
        )
        self.assertIn("眼前人物", l0["prompt_block"])
        self.assertNotIn("室内光线明亮", l0["prompt_block"])

        # Move nanami into arcade, hinata to (6, 8), test misrouted world_act(entity_id=nanami) + location hint
        self.engine.place_actor(nanami, arcade, 6, 5)
        self.engine.place_actor(hinata, town, 6, 8)
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value={
            "world_actions": [{"tool": "world_act", "entity_id": nanami, "action": "interact"}],
            "cognitive_effect": {},
            "emotion": "好奇",
            "intent": "好，我去找七海。",
        }), patch.object(self.chat, "_mouth_speak", return_value="好，我去找七海。"):
            res1 = self.chat.chat(hinata, "去陪七海打会游戏吧")
        self.assertNotIn("没有编号为", res1["reply"])
        self.assertNotIn("没有编号为", " ".join(res1["actions_executed"]))

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value={
            "world_actions": [],
            "cognitive_effect": {},
            "emotion": "平静",
            "intent": "行，我去星海电玩厅找她。",
        }), patch.object(self.chat, "_mouth_speak", return_value="行，我去星海电玩厅找她。"):
            self.chat.chat(hinata, "她在星海电玩厅")
        cog_h = next(a for a in self.engine.list_actors() if a["actor_id"] == hinata)["autonomy"]["cognition"]
        pending_mod = cog_h.get("pending_modulation") or {}
        self.assertEqual((pending_mod.get("commitment") or {}).get("target"), nanami)
        self.assertEqual(pending_mod.get("peer_beliefs"), [{"actor_id": nanami, "place_id": arcade}])

        # Put nanami along the street on the way to arcade -> Hinata must intercept her en route without entering arcade!
        self.engine.place_actor(nanami, town, 19, 14)

        def model(system, user, **kwargs):
            if '"intent_a"' in system:
                return {"intent_a": "七海，原来你在路上啊，不用去电玩厅找了。", "intent_b": "嗯，刚好碰到了。", "end_chat": True}
            return {}

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=model), \
                patch.object(self.chat, "_mouth_speak", side_effect=lambda name, ctx, intent, *a, **kw: intent):
            self.live.control(hinata, "window", "start", 4)
            self.until(
                lambda: any("不用去电玩厅找了" in m["text"] for m in self.chat.get_history(hinata, space_id=town)),
                timeout=9.0,
            )
            self.assertEqual(self.engine.live_actor_frame(hinata)["space_id"], town)

    def test_saturated_town_ior_avoids_two_cell_loop_and_transient_door_yields(self) -> None:
        aid = self.engine.import_actor(name="日向创", host_character="日向创")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=aid)["town_space_id"]
        arcade = f"{town}__arcade"
        blocker = self.engine.import_actor(name="路人", space_id=town, x=29, y=24)["actor_id"]
        self.engine.place_actor(aid, town, 18, 12)

        # Distant actor standing at arcade door (29, 24) does not make route planning fail
        route = self.engine.plan_actor_route(aid, town, 29, 23, reach=1, stride=4, cardinal=True)
        self.assertGreater(len(route), 1)
        self.engine.place_actor(blocker, arcade, 5, 5)

        # Saturated town roaming with IOR should cover multiple blocks instead of oscillating in a 2-cell loop
        snap = self.engine.snapshot_for_ui(town)
        board = next(e for e in snap["entities"] if "布告栏" in e["name"])
        tracks = {f"frontier|{town}:{gx}:{gy}": [1.0, 0.0] for gx in range(10) for gy in range(8)}
        tracks["action|explore"] = [2.6, 0.0]
        tracks["action|notice"] = [0.9, 0.0]
        places = {town: {"name": "樱町小镇", "kind": "town", "source": "direct", "visits": 19, "samples": [0.0]}}
        portals = {}
        for b in snap["space"].get("buildings") or ():
            pid = b.get("interior_space_id") or ""
            if not pid:
                continue
            places[pid] = {"name": b["name"], "kind": "building", "source": "direct", "visits": 3, "samples": [0.0]}
            portals[f"{town}:{pid}"] = {
                "space": town, "target": pid, "name": b["name"],
                "x": b["door_x"], "y": b["door_y"], "source": "direct", "confidence": 1.0, "samples": [0.0],
            }
            tracks[f"place|{pid}"] = [3.0, 0.0]
        for dom in ("food", "rest", "game", "reading"):
            tracks[f"action|{dom}"] = [3.0, 0.0]
        self.engine.place_actor(aid, town, 15, 28)
        self.engine.update_actor_autonomy(aid, {
            "cognition": {
                "places": places,
                "portals": portals,
                "tracks": tracks,
                "read": {board["entity_id"]: [[e["by"], e["text"]] for e in board["traits"]["inscribable"]["entries"][-5:]]},
                "active_intention": None,
            },
        })
        visited_blocks: set[tuple[int, int]] = set()
        visited_cells: set[tuple[int, int]] = set()
        for _ in range(24):
            scene = self.engine.cognitive_scene(aid, include_rooms=True)
            scene["engage"] = lambda *a: True
            self.heartbeat._cognitive.step(scene, self.settings, use_llm=False, talk=lambda *a: [])
            f = self.engine.live_actor_frame(aid)
            if f["space_id"] == town:
                visited_cells.add((f["x"], f["y"]))
                visited_blocks.add((f["x"] // 4, f["y"] // 4))
        self.assertGreaterEqual(len(visited_blocks), 4, visited_blocks)
        self.assertGreaterEqual(len(visited_cells), 10, visited_cells)

    def test_hungry_actor_honors_shared_cafe_agreement_and_cafe_rest_skips_bed_search(self) -> None:
        now = datetime.now()
        noon = now.replace(hour=14, minute=0, second=0, microsecond=0)
        self.engine._world["clock_offset_minutes"] = (noon - now).total_seconds() / 60

        hinata = self.engine.import_actor(name="日向创", host_character="日向创")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=hinata)["town_space_id"]
        cafe = f"{town}__cafe"
        library = f"{town}__library"
        nanami = self.engine.import_actor(name="七海千秋", host_character="七海千秋", space_id=town, x=20, y=15)["actor_id"]
        self.engine.place_actor(hinata, town, 19, 15)

        # Deplete all consumables across the town realm to mirror a food-scarce runtime world.
        for sid_x in (town, cafe, library, f"{town}__shop", f"{town}__home", f"{town}__guest", f"{town}__arcade"):
            if sid_x not in self.engine._world["spaces"]:
                continue
            for ent in self.engine.snapshot_for_ui(sid_x)["entities"]:
                cons = (ent.get("traits") or {}).get("consumable")
                if isinstance(cons, dict):
                    traits = dict(ent["traits"])
                    traits["consumable"] = {**cons, "uses_left": 0}
                    self.engine.update_entity_spec(ent["entity_id"], traits=traits)

        now_min = float(self.engine.cognitive_scene(hinata)["minutes"])
        self.engine.update_actor_autonomy(hinata, {
            "hunger": 1.0,
            "cognition": {
                "applied_commitment_id": "ref:old_lib",
                "modulation": {
                    "id": 100,
                    "realm": town,
                    "at": now_min,
                    "half_life": 12.0,
                    "strength": 1.0,
                    "source": "pfc",
                    "commitment": {
                        "id": "ref:old_lib",
                        "source": "pfc",
                        "priority": "high",
                        "kind": "visit",
                        "target": library,
                        "drive": "reading",
                    },
                },
            },
        })

        cafe_chat = {
            "intent_a": "这雨下得不大，要不要去微风咖啡馆喝杯热饮？",
            "intent_b": "嗯，正好有点饿，过去坐坐吧。",
            "memory_note": "两人决定一起去微风咖啡馆喝热饮。",
            "speech_act": {
                "shared_place_id": cafe,
                "shared_drive": "food",
                "goal_b": {"kind": "visit", "target": cafe, "drive": "rest"},
                "tone": "warm",
            },
            "end_chat": True,
        }
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=cafe_chat):
            chat_res = self.chat.conduct_actor_social_exchange(hinata, nanami, generated_only=True)
        sa_a = (chat_res.get("speech_act") or {}).get("a") or {}
        sa_b = (chat_res.get("speech_act") or {}).get("b") or {}
        self.assertEqual(sa_a.get("shared_place_id"), cafe)
        self.assertEqual(sa_b.get("shared_place_id"), cafe)

        def decide(aid: str, modulation: dict | None = None) -> dict:
            scene = self.engine.cognitive_scene(aid, include_rooms=True)
            scene["engage"] = lambda *args: True
            if modulation is not None:
                scene["modulation"] = modulation
            return self.heartbeat._cognitive.step(scene, self.settings, use_llm=False, talk=lambda *args: [])

        res_h = decide(hinata)
        res_n = decide(nanami)
        for aid_x, res_x in ((hinata, res_h), (nanami, res_n)):
            cog_x = next(a for a in self.engine.list_actors() if a["actor_id"] == aid_x)["autonomy"]["cognition"]
            self.assertTrue(str(cog_x.get("applied_commitment_id") or "").startswith("sa:"), (aid_x, cog_x))
            self.assertEqual((cog_x.get("active_intention") or {}).get("target"), cafe, (aid_x, res_x))
            self.assertIn("一起去「微风咖啡馆」", res_x["goal"])

        cog_h = next(a for a in self.engine.list_actors() if a["actor_id"] == hinata)["autonomy"]["cognition"]
        self.assertIn("ref:old_lib", cog_h.get("applied_commitments") or ())
        self.assertEqual(cog_h["applied_commitments"][-1], cog_h["applied_commitment_id"])

        # Subsequent step must NOT replay stale ref:old_lib, and a fresh mid-walk pfc reflection must NOT hijack cafe.
        res_h2 = decide(hinata)
        self.assertIn("一起去「微风咖啡馆」", res_h2["goal"])
        fresh_pfc = {
            "id": 200,
            "realm": town,
            "at": float(self.engine.cognitive_scene(hinata)["minutes"]),
            "half_life": 12.0,
            "strength": 1.0,
            "source": "pfc",
            "commitment": {
                "id": "ref:new_lib",
                "source": "pfc",
                "priority": "high",
                "kind": "visit",
                "target": library,
                "drive": "reading",
            },
        }
        res_h3 = decide(hinata, fresh_pfc)
        self.assertIn("一起去「微风咖啡馆」", res_h3["goal"])

        for _ in range(18):
            if self.engine.live_actor_frame(hinata)["space_id"] != cafe:
                decide(hinata)
            if self.engine.live_actor_frame(nanami)["space_id"] != cafe:
                decide(nanami)
            if (
                self.engine.live_actor_frame(hinata)["space_id"] == cafe
                and self.engine.live_actor_frame(nanami)["space_id"] == cafe
            ):
                break
        self.assertEqual(self.engine.live_actor_frame(hinata)["space_id"], cafe)
        self.assertEqual(self.engine.live_actor_frame(nanami)["space_id"], cafe)

        # Case 2: Visiting cafe with drive="rest" must NOT trigger bedroom 5-corner _look_for_bed wandering.
        self.engine.update_actor_autonomy(nanami, {
            "cognition": {
                "active_intention": {
                    "kind": "visit",
                    "key": f"visit:{cafe}:{cafe}:",
                    "target": cafe,
                    "space": cafe,
                    "x": 6,
                    "y": 4,
                    "goal": "和日向创一起去「微风咖啡馆」",
                    "drive": "rest",
                    "utility": 8.0,
                    "action": "",
                    "phase": "activity",
                    "pfc_committed": True,
                    "source": "speech_act",
                },
            },
        })
        for _ in range(8):
            decide(nanami)
            cog_n = next(a for a in self.engine.list_actors() if a["actor_id"] == nanami)["autonomy"]["cognition"]
            intent_n = cog_n.get("active_intention") or {}
            self.assertNotIn("bed_search_index", intent_n)
            if self.engine.live_actor_frame(nanami)["posture"] == "resting":
                break
        self.assertEqual(self.engine.live_actor_frame(nanami)["posture"], "resting")

        # Case 3: Fallback hot-drink inference without LLM speech_act + hungry non-food pfc guardrail with food available.
        hungry = self.engine.import_actor(name="饿肚子测试", host_character="饿肚子测试", space_id=town, x=15, y=15)["actor_id"]
        snack = self.engine.place_from_blueprint("default_snack", town, 15, 16)
        door_lib = next(e for e in self.engine.snapshot_for_ui(town)["entities"]
                        if e["traits"].get("portal", {}).get("target_space_id") == library)
        self.engine.update_actor_autonomy(hungry, {
            "hunger": 0.95,
            "cognition": {
                "portals": {
                    f"{town}:{library}": {
                        "space": town, "target": library, "name": "湖畔图书馆",
                        "x": door_lib["x"], "y": door_lib["y"], "entity": door_lib["entity_id"],
                        "source": "direct", "confidence": 1.0, "samples": [0.0],
                    },
                },
            },
        })
        pfc_hungry_lib = {
            "id": 300,
            "realm": town,
            "at": float(self.engine.cognitive_scene(hungry)["minutes"]),
            "half_life": 12.0,
            "strength": 1.0,
            "source": "pfc",
            "commitment": {
                "id": "ref:lib_when_hungry",
                "source": "pfc",
                "priority": "high",
                "kind": "visit",
                "target": library,
                "drive": "reading",
            },
        }
        res_hungry = decide(hungry, pfc_hungry_lib)
        cog_hungry = next(a for a in self.engine.list_actors() if a["actor_id"] == hungry)["autonomy"]["cognition"]
        self.assertEqual(res_hungry["drive"], "food")
        self.assertNotEqual(cog_hungry.get("applied_commitment_id"), "ref:lib_when_hungry")
        self.assertNotEqual((cog_hungry.get("active_intention") or {}).get("target"), library)
        self.assertIsNotNone(snack)

        # Fallback speech_act drive inference from cafe hot-drink phrasing.
        pal_a = self.engine.import_actor(name="测试甲", space_id=town, x=10, y=15)["actor_id"]
        pal_b = self.engine.import_actor(name="测试乙", space_id=town, x=11, y=15)["actor_id"]
        fallback_chat = {
            "intent_a": "一起去微风咖啡馆喝热饮吧。",
            "intent_b": "好呀，走吧。",
            "memory_note": "决定一起去微风咖啡馆喝热饮。",
            "end_chat": True,
        }
        with patch("plugins.nanami_world.text_compiler._ask_llm_json", return_value=fallback_chat):
            fb_res = self.chat.conduct_actor_social_exchange(pal_a, pal_b, generated_only=True)
        fb_sa = (fb_res.get("speech_act") or {}).get("a") or {}
        self.assertEqual(fb_sa.get("shared_place_id"), cafe)
        self.assertEqual(fb_sa.get("shared_drive"), "food")


if __name__ == "__main__":
    unittest.main(verbosity=2)
