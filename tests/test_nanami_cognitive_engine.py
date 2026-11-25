"""Deterministic town autonomy acceptance tests using the real heartbeat and file bridge."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import projector
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings


PROFILES = {
    "gamer": (
        {"surgency": 0.12, "domainEfficacy": {"game": 1, "nonGame": 0.2},
         "needProfile": {"competence": 0.99, "relatedness": 0.15, "safety": 0.25}},
        {"novelty_weight": 0.65, "chronotype_offset": 2},
    ),
    "outgoing": (
        {"surgency": 0.99, "socialEfficacy": 0.96, "domainEfficacy": {"game": 0.15, "nonGame": 0.65},
         "needProfile": {"relatedness": 1, "safety": 0.1}},
        {"novelty_weight": 0.95, "chronotype_offset": 0},
    ),
    "cautious": (
        {"effortfulControl": 0.95, "surgency": 0.10, "domainEfficacy": {"game": 0.15, "nonGame": 0.98},
         "needProfile": {"competence": 0.5, "relatedness": 0.3, "safety": 0.98}},
        {"novelty_weight": 0.40, "chronotype_offset": -3},
    ),
}


class CognitiveEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.persona = self.root / "persona"
        self.persona.mkdir()
        clock = patch("time.time", return_value=datetime(2026, 7, 1, 16).timestamp())
        clock.start()
        self.addCleanup(clock.stop)
        self.settings = WorldSettings(allow_ai_diary=True, persona_data_dir=str(self.persona))

    def world(self, name: str) -> WorldEngine:
        return WorldEngine(code_dir=WORLD_CODE, data_root=self.root / f"world-{name}", project_root=self.root)

    def heartbeat(self, engine: WorldEngine) -> WorldHeartbeat:
        return WorldHeartbeat(engine, lambda: self.settings, project_root=self.root, adapter_provider=lambda: None)

    def step(self, heartbeat: WorldHeartbeat, aid: str, *, use_llm: bool = False) -> dict:
        return heartbeat.step(actor_id=aid, advance_minutes=5, use_llm=use_llm)["decisions"][0]

    def test_daytime_dorm_visit_does_not_put_a_mildly_tired_actor_to_bed(self) -> None:
        engine = self.world("daytime-sleep")
        aid = engine.import_actor(name="日向创")["actor_id"]
        town = engine.create_default_town(move_actor_id=aid)["town_space_id"]
        dorm = f"{town}__guest"
        engine.set_realm_initial_hour(town, 14.0)
        bed = next(e for e in engine.snapshot_for_ui(dorm)["entities"] if e["blueprint_id"] == "default_bed")
        engine.place_actor(aid, dorm, bed["x"], bed["y"] + 1)
        engine.set_actor_vitals(aid, {"energy": 0.61, "sleep_pressure": 0.27})
        engine.update_actor_autonomy(aid, {"cognition": {"active_intention": {
            "kind": "visit", "key": f"visit:{town}:{dorm}:", "target": dorm, "space": town,
            "x": bed["x"], "y": bed["y"], "goal": "去宿舍休息", "drive": "rest", "action": "", "phase": "activity",
        }}})
        heartbeat = self.heartbeat(engine)
        self.addCleanup(heartbeat.stop)
        heartbeat.step(actor_id=aid, advance_minutes=0.1, use_llm=False)
        self.assertEqual(engine.live_actor_frame(aid)["posture"], "standing")

        # Old saves may already contain the bed occupancy.
        engine.execute_act(aid, bed["entity_id"], "rest")
        heartbeat.step(actor_id=aid, advance_minutes=0.1, use_llm=False)
        self.assertEqual(engine.live_actor_frame(aid)["posture"], "standing")
        self.assertIsNone(engine.live_actor_frame(aid)["resting_on_id"])

        engine.set_actor_vitals(aid, {"energy": 0.20, "sleep_pressure": 0.78})
        for _ in range(4):
            heartbeat.step(actor_id=aid, advance_minutes=0.1, use_llm=False)
            if engine.live_actor_frame(aid)["posture"] == "resting":
                break
        self.assertEqual(engine.live_actor_frame(aid)["posture"], "resting")

    def test_exhausted_but_alert_actor_recovers_awake_and_keeps_world_vitals(self) -> None:
        engine = self.world("awake-rest")
        aid = engine.import_actor(name="哈贝特洛特", x=5, y=4)["actor_id"]
        engine.set_realm_initial_hour("room_default", 14.0)
        engine.place_from_blueprint("default_bed", "room_default", 6, 4)
        engine.set_actor_vitals(aid, {"energy": 0.20, "sleep_pressure": 0.01})
        heartbeat = self.heartbeat(engine)
        self.addCleanup(heartbeat.stop)

        with patch("plugins.nanami_world.heartbeat.persona_bridge.read_persona_state", return_value={
            "energy": 0.05, "sleep_pressure": 0.99, "mood_v": 0.35, "agenda": "want_company",
            "disengaged": False, "preferred_domains": ["game"], "updated_at": 1.0, "world_state": True,
        }):
            heartbeat.step(actor_id=aid, advance_minutes=0.1, use_llm=False)
        frame = engine.live_actor_frame(aid)
        self.assertEqual(frame["posture"], "standing")
        self.assertLess(frame["vitals"]["sleep_pressure"], 0.02)
        self.assertEqual(frame["vitals"]["mood_v"], 0.35)
        self.assertNotEqual(frame["expression"]["bucket"], "sleep_deep")
        decision = heartbeat.step(actor_id=aid, advance_minutes=0.1, use_llm=False)["decisions"][0]
        self.assertEqual(decision["goal"], "停下来歇一会儿")
        engine.tick_physics(advance_minutes=12)
        frame = engine.live_actor_frame(aid)
        self.assertEqual(frame["posture"], "standing")
        self.assertGreater(frame["vitals"]["energy"], 0.30)
        self.assertLess(frame["vitals"]["sleep_pressure"], 0.05)

    def test_story_explorer_finishes_a_walk_and_avoids_repeating_two_blocks(self) -> None:
        engine = self.world("story-walk")
        sid = engine.create_space(name="中庭", width=22, height=14, wall_rows=0,
                                  space_kind="town", isolated_persona=True)["space_id"]
        aid = engine.import_actor(name="丰川祥子", space_id=sid, x=12, y=8)["actor_id"]
        engine.set_actor_private_brief(aid, "在中庭等同伴的消息。", space_id=sid)
        engine.update_actor_autonomy(aid, {"cognition": {"active_intention": {
            "kind": "explore", "key": f"explore:{sid}:12:1:", "target": "12:1", "space": sid,
            "x": 12, "y": 1, "goal": "走到中庭北侧", "drive": "explore", "action": "", "phase": "travel",
        }}})
        heartbeat = self.heartbeat(engine)
        self.addCleanup(heartbeat.stop)
        cog = heartbeat._cognitive
        for _ in range(3):
            scene = engine.cognitive_scene(aid)
            scene["stride"] = 3
            decision = cog.step(scene, self.settings, use_llm=False, talk=lambda *_: [])
            self.assertEqual(decision["goal"], "走到中庭北侧")
        self.assertLessEqual(engine.live_actor_frame(aid)["y"], 3)
        engine.update_actor_autonomy(aid, {"cognition": {
            **engine.cognitive_scene(aid)["actor"]["autonomy"]["cognition"], "active_intention": None,
        }})
        seq = engine.live_actor_frame(aid)["move_seq"]
        for hour in (12.0, 23.0):
            engine.set_realm_initial_hour(sid, hour)
            engine.set_actor_private_brief(aid, "在中庭等同伴的消息。", space_id=sid, night_active=True)
            for _ in range(3):
                scene = engine.cognitive_scene(aid)
                scene["engage"] = lambda *args: False
                decision = cog.step(scene, self.settings, use_llm=False, talk=lambda *_: [])
                self.assertEqual(decision["goal"], "留意周围")
            self.assertEqual(engine.live_actor_frame(aid)["move_seq"], seq)
        scene = engine.cognitive_scene(aid)
        scene["engage"] = True
        scene["percept"] = {"memory_decay": 0.8}
        state = {"zones": {}, "tracks": {f"frontier|{sid}:3:2": (1.0, scene["minutes"])},
                 "recent_blocks": [f"{sid}:3:2", f"{sid}:3:1"] * 2}
        targets = cog._frontiers(state, scene)
        self.assertTrue(targets)
        self.assertFalse(any(x // 4 == 3 and y // 4 in (1, 2) for x, y, *_ in targets))

    def test_thirty_beats_different_personas(self) -> None:
        (self.persona / "pointers.yaml").write_text(
            "pointers:\n" + "".join(
                f"- id: {name}\n  character_name: {name}\n  state_root: {name}\n" for name in PROFILES
            ),
            encoding="utf-8",
        )
        distributions = {}
        starts = set()
        for name, (traits, perception) in PROFILES.items():
            with self.subTest(persona=name):
                profile = self.persona / name / "state"
                profile.mkdir(parents=True)
                (profile / "traits.json").write_text(json.dumps({"traits": traits}), encoding="utf-8")
                (profile / "world_perception.json").write_text(json.dumps(perception), encoding="utf-8")
                engine = self.world(name)
                aid = engine.import_actor(name=name, host_character=name)["actor_id"]
                town = engine.create_default_town(move_actor_id=aid)["town_space_id"]
                heartbeat = self.heartbeat(engine)
                actor = engine.list_actors()[0]
                starts.add((actor["x"], actor["y"]))
                snapshot = engine.snapshot_for_ui(town)
                lamps = {e["entity_id"] for e in snapshot["entities"] if e["blueprint_id"] == "default_street_lamp"}
                board = next(e["entity_id"] for e in snapshot["entities"] if "布告栏" in e["name"])
                public_actions = []
                for _ in range(30):
                    before = actor
                    self.step(heartbeat, aid)
                    actor = engine.list_actors()[0]
                    if actor["move_seq"] != before["move_seq"] and before["space_id"] == actor["space_id"]:
                        self.assertLessEqual(len(actor["last_path"]) - 1, 7)
                    if actor["last_act"] != before["last_act"] and actor["last_act"]["entity_id"] in lamps:
                        public_actions.append(actor["last_act"]["entity_id"])
                state = actor["autonomy"]["cognition"]
                zones = {z["zone_id"] for z in state["zones"].values() if z.get("visits")}
                events = engine.read_recent_events(1000)
                buildings = {
                    e["space_id"] for e in events if e["kind"] == "actor_moved" and e["actor_id"] == aid
                    and e["space_id"] != town and e["payload"].get("via_portal")
                }
                notes = [
                    e["payload"]["text"] for e in events if e["kind"] == "entity_inscribed"
                    and e["payload"].get("entity_id") == board
                ]
                self.assertGreaterEqual(len(zones), 3, zones)
                self.assertGreaterEqual(len(buildings), 2, buildings)
                self.assertLessEqual(len(public_actions), 1, public_actions)
                self.assertEqual(len(notes), len(set(notes)), notes)
                distributions[name] = buildings
                print(f"\n{name}: {len(zones)}/5 zones, {len(buildings)} buildings, "
                      f"{len(public_actions)} street-lamp actions, {len(notes)} board notes "
                      f"({', '.join(sorted(buildings))})")
        self.assertEqual(len(starts), 1)
        self.assertEqual(len({frozenset(dist) for dist in distributions.values()}), len(PROFILES))
        self.assertIn("town_sakura__arcade", distributions["gamer"])
        self.assertIn("town_sakura__cafe", distributions["outgoing"])
        self.assertIn("town_sakura__library", distributions["cautious"])

    def test_directory_learns_places_only_after_read_and_goal_resumes_after_reload(self) -> None:
        engine = self.world("resume")
        aid = engine.import_actor(name="Scout")["actor_id"]
        town = engine.create_default_town(move_actor_id=aid)["town_space_id"]
        heartbeat = self.heartbeat(engine)
        hidden = f"{town}__workshop"

        self.step(heartbeat, aid)
        state = engine.list_actors()[0]["autonomy"]["cognition"]
        self.assertNotIn(hidden, state["places"])
        self.assertEqual(state["active_intention"]["drive"], "notice")
        self.step(heartbeat, aid)
        state = engine.list_actors()[0]["autonomy"]["cognition"]
        self.assertEqual(state["places"][hidden]["source"], "signboard")
        self.assertEqual(len(state["zones"]), 5)
        self.step(heartbeat, aid)
        goal = engine.list_actors()[0]["autonomy"]["cognition"]["active_intention"]
        self.assertEqual(goal["kind"], "visit")

        engine = self.world("resume")
        heartbeat = self.heartbeat(engine)
        self.assertEqual(engine.list_actors()[0]["autonomy"]["cognition"]["active_intention"], goal)
        self.assertEqual(self.step(heartbeat, aid)["goal"], goal["goal"])
        entry = None
        for _ in range(8):
            self.step(heartbeat, aid)
            actor = engine.list_actors()[0]
            if actor["space_id"] != town:
                entry = actor
                break
        self.assertIsNotNone(entry)
        self.assertIn("记得的场所", projector.project_view(engine, aid))
        engine.place_actor(aid, "room_default", 5, 5)
        self.assertNotIn("cognition", engine.list_actors()[0]["autonomy"])
        engine.place_actor(aid, town, 19, 15)
        self.assertEqual(engine.list_actors()[0]["autonomy"]["cognition"], entry["autonomy"]["cognition"])

    def test_missing_door_corrects_belief_and_replans(self) -> None:
        engine = self.world("blocked")
        aid = engine.import_actor(name="Scout")["actor_id"]
        town = engine.create_default_town(move_actor_id=aid)["town_space_id"]
        heartbeat = self.heartbeat(engine)
        for _ in range(3):
            self.step(heartbeat, aid)
        state = engine.list_actors()[0]["autonomy"]["cognition"]
        target = state["active_intention"]["target"]
        key = f"{town}:{target}"
        original = state["portals"][key]
        door = next(e for e in engine.snapshot_for_ui(town)["entities"]
                    if e["traits"].get("portal", {}).get("target_space_id") == target)
        engine.remove_entity(door["entity_id"])
        for _ in range(9):
            self.step(heartbeat, aid)
            state = engine.list_actors()[0]["autonomy"]["cognition"]
            if state.get("prediction_error"):
                break
        self.assertIn(original["name"], state["prediction_error"])
        self.assertLess(state["portals"][key]["confidence"], original["confidence"])
        self.assertNotEqual(self.step(heartbeat, aid)["goal"], f"去「{original['name']}」")

    def test_strategy_is_not_called_during_segmented_walk(self) -> None:
        engine = self.world("strategy")
        town = engine.create_space(name="开放街区", width=40, height=30, wall_rows=0, space_kind="town")["space_id"]
        aid = engine.import_actor(name="Scout", space_id=town, x=20, y=15)["actor_id"]
        heartbeat = self.heartbeat(engine)
        with patch("plugins.nanami_world.cognitive_engine.text_compiler._ask_llm_json", return_value={"choice": 2}) as ask:
            first = self.step(heartbeat, aid, use_llm=True)
            ask.assert_called_once()
            self.assertRegex(first["actions"][0], "走了[5-7]格")
            commitment = engine.list_actors()[0]["autonomy"]["cognition"]["active_intention"]
            self.step(heartbeat, aid, use_llm=True)
            ask.assert_called_once()
            self.assertEqual(commitment["goal"], first["goal"])

    def test_notice_board_writes_only_fresh_unexpressed_experiences(self) -> None:
        engine = self.world("notice")
        town = engine.create_space(name="街边小院", width=12, height=12, wall_rows=0, space_kind="town")["space_id"]
        engine.paint_tiles(town, [
            {"x": x, "y": y, "tile_id": "wood_wall"}
            for x in range(3, 8) for y in range(3, 8) if x in (3, 7) or y in (3, 7)
        ])
        aid = engine.import_actor(name="Scout", space_id=town, x=5, y=5)["actor_id"]
        board = engine.place_from_blueprint("default_notebook", town, 5, 4, note_text="和阿青聊了几句！")
        engine.update_entity_spec(board["entity_id"], name="布告栏")
        heartbeat = self.heartbeat(engine)
        self.assertIn("阅读", self.step(heartbeat, aid)["goal"])

        first_peer = engine.import_actor(name="阿青", space_id=town, x=4, y=5)["actor_id"]
        engine.record_social_feedback(first_peer, town, "阿青前来聊天", peer_id=aid)
        engine.remove_actor(first_peer)
        for _ in range(10):
            self.step(heartbeat, aid)
        self.assertFalse(any(e["kind"] == "entity_inscribed" for e in engine.read_recent_events(100)))

        second_peer = engine.import_actor(name="小枫", space_id=town, x=4, y=5)["actor_id"]
        engine.record_social_feedback(second_peer, town, "小枫前来聊天", peer_id=aid)
        engine.remove_actor(second_peer)
        for _ in range(12):
            self.step(heartbeat, aid)
        notes = [e["payload"]["text"] for e in engine.read_recent_events(120) if e["kind"] == "entity_inscribed"]
        self.assertEqual(notes, ["和小枫聊了几句。"])
        state = engine.list_actors()[0]["autonomy"]["cognition"]
        self.assertNotIn("experience", state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
