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
