"""Regression tests for the world/persona bridge and the two-visit puzzle."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _shinsekai import PROJECT, WORLD_CODE

from plugins.nanami_world import PLUGIN_ID, PLUGIN_VERSION, persona_bridge, world_page
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.plugin import NanamiWorldPlugin
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService
from sdk.register import PluginCapabilityRegistry


class WorldPersonaTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.code = WORLD_CODE
        self.data = self.root / "world"
        self.persona = self.root / "persona"
        self.persona.mkdir()
        (self.persona / "pointers.yaml").write_text(
            "pointers:\n"
            "- id: scout\n  character_name: Scout\n  state_root: scout\n"
            "- id: helper\n  character_name: Helper\n  state_root: helper\n",
            encoding="utf-8",
        )
        self.settings = WorldSettings(persona_data_dir=str(self.persona))
        self.engine = self.open_world(self.data)
        self.heartbeat = self.heartbeat_for(self.engine)
        self.sid = "room_default"
        self.clock = datetime(2026, 7, 1, 12).timestamp()
        self.write_persona("scout", "traits.json", {"traits": {
            "needProfile": {"competence": 0.95, "relatedness": 0.95},
            "domainEfficacy": {"game": 0.12},
        }})
        self.write_persona("helper", "traits.json", {"traits": {
            "needProfile": {"competence": 0.05, "relatedness": 0.05},
        }})
        self.write_persona("scout", "social_traits.json", {"socialTraits": {
            "attraction": {"friendThreshold": 0.82},
        }})

    def open_world(self, data: Path) -> WorldEngine:
        return WorldEngine(code_dir=self.code, data_root=data, project_root=self.root)

    def heartbeat_for(self, engine: WorldEngine) -> WorldHeartbeat:
        return WorldHeartbeat(engine, lambda: self.settings, project_root=PROJECT)

    def write_persona(self, profile: str, name: str, data: dict) -> None:
        path = self.persona / profile / "state" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def mind(self, profile: str, actor_id: str) -> dict:
        path = self.persona / profile / "state" / "mind.json"
        return json.loads(path.read_text(encoding="utf-8"))[f"world:{self.sid}:{actor_id}"]

    def step_actor(self, engine: WorldEngine, actor_id: str, *, clock: float | None = None) -> dict:
        with patch("time.time", return_value=clock or self.clock):
            return self.heartbeat_for(engine).step(advance_minutes=0.01, use_llm=False, actor_id=actor_id)

    def test_nested_settings_and_shared_profile_actor_isolation(self) -> None:
        profile = persona_bridge.read_persona_behavior_profile(PROJECT, "Scout", custom_persona_dir=str(self.persona))
        self.assertEqual(profile["domain_efficacy_game"], 0.12)
        self.assertEqual(profile["friend_threshold"], 0.82)
        self.write_persona("scout", "mind.json", {
            "world:room_default:a": {"moodV": 0.25, "updatedAt": 1000},
            "world:room_default:b": {"moodV": -0.25, "updatedAt": 2000},
            "world:other:a": {"moodV": -1, "updatedAt": 3000},
            "shinsekai:main": {"moodV": 0.12, "updatedAt": 10},
        })
        for aid, expected in (("a", 0.25), ("b", -0.25), ("new", 0.12)):
            with self.subTest(actor=aid):
                state = persona_bridge.read_persona_state(
                    PROJECT, "Scout", space_id=self.sid, actor_id=aid, custom_persona_dir=str(self.persona),
                )
                self.assertEqual(state["mood_v"], expected)

    def test_same_room_first_action_follows_profile_when_seats_and_order_swap(self) -> None:
        for profile, competence in (("scout", 0.95), ("helper", 0.05)):
            self.write_persona(profile, "traits.json", {"traits": {"needProfile": {"competence": competence}}})
        scout = self.engine.import_actor(name="Scout", x=3, y=4)["actor_id"]
        helper = self.engine.import_actor(name="Helper", x=5, y=4)["actor_id"]
        chest = self.engine.place_from_blueprint("default_wooden_chest", self.sid, 4, 5)
        self.engine.place_from_blueprint("default_brass_key", self.sid, 4, 4)
        for scenario, order in enumerate(((scout, helper), (helper, scout))):
            for aid in order:
                branch_path = self.root / f"choice_{scenario}_{aid}"
                shutil.copytree(self.data, branch_path)
                branch = self.open_world(branch_path)
                if scenario:
                    branch.place_actor(scout, self.sid, 3, 5)
                    branch.place_actor(helper, self.sid, 3, 4)
                    branch.place_actor(scout, self.sid, 5, 4)
                with patch.object(persona_bridge, "emit_world_event_to_persona", return_value={"status": "unbound"}):
                    self.step_actor(branch, aid)
                ent = next(e for e in branch.visible_entities(aid) if e["entity_id"] == chest["entity_id"])
                self.assertEqual(ent["traits"]["container"]["is_locked"], aid == helper)

    def test_attention_changes_when_only_safety_changes(self) -> None:
        actor = self.engine.import_actor(name="Scout", x=4, y=4)
        chest = self.engine.place_from_blueprint("default_wooden_chest", self.sid, 4, 5)
        lamp = self.engine.place_from_blueprint("default_lamp", self.sid, 5, 4)
        visible = self.engine.visible_entities(actor["actor_id"])
        for safety, expected in ((0.05, chest), (0.95, lamp)):
            self.write_persona("scout", "traits.json", {"traits": {"needProfile": {"safety": safety}}})
            frame = persona_bridge.build_percept_frame(
                self.root / f"attention_{safety}", PROJECT, {"space_id": self.sid}, {}, actor, visible,
                custom_persona_dir=str(self.persona),
            )
            self.assertEqual(frame["focused_entities"][0]["entity_id"], expected["entity_id"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for persona event integration")
    def test_two_visits_and_durable_persona_feedback(self) -> None:
        scout = self.engine.import_actor(name="Scout", x=3, y=4)["actor_id"]
        helper = self.engine.import_actor(name="Helper", x=5, y=4)["actor_id"]
        observer = self.engine.import_actor(name="Observer", x=8, y=5)["actor_id"]
        chest = self.engine.place_from_blueprint("default_wooden_chest", self.sid, 4, 5)
        eid = chest["entity_id"]
        self.engine.execute_act(scout, eid, "unlock")
        blocked = self.engine.pending_persona_events()[0]
        self.assertEqual(blocked["payload"]["kind"], "action_blocked")
        lock = self.persona / "scout" / "state" / "mind.json.lock"
        lock.touch()
        try:
            self.heartbeat.deliver_persona_events()
            self.assertEqual(self.engine.pending_persona_events(), [blocked])
        finally:
            lock.unlink()
        self.heartbeat.deliver_persona_events()
        self.assertEqual(self.mind("scout", scout)["lastWorldEvent"]["kind"], "action_blocked")

        key = self.engine.place_from_blueprint("default_brass_key", self.sid, 5, 5)
        self.engine.execute_act(helper, key["entity_id"], "take")
        self.engine.set_actor_world_mood(helper, vitals_patch={"energy": 0.2})
        self.assertIn("暂时不想", self.heartbeat.invite_to_puzzle(scout, helper, eid))
        self.assertEqual(self.engine.puzzle_memories(scout, self.sid), [])
        self.engine.set_actor_world_mood(helper, vitals_patch={"energy": 0.8})
        registry = PluginCapabilityRegistry()
        registry.set_settings_ui_plugin_context(PLUGIN_ID, PLUGIN_VERSION)
        world_page.register_pages(
            registry, code_dir=self.code, engine=self.engine, heartbeat=self.heartbeat,
            settings_store=SimpleNamespace(get=lambda: self.settings),
        )
        canvas = next(page for page in registry.frontend_config_contributions if page.page_id == "world_canvas")
        interaction = next(action for action in canvas.actions if action.id == "entity_ui_interact")
        with patch.object(self.heartbeat, "deliver_persona_events"):
            result = interaction.run({"actor_id": scout, "target_id": helper, "entity_id": eid, "action": "invite_puzzle"})
        self.assertIn("Helper用手里的钥匙打开", result["feedback"])
        event = self.engine.pending_persona_events()[0]
        self.assertEqual(event["payload"]["kind"], "cooperative_solve")
        self.assertCountEqual(event["payload"]["witness_ids"], [scout, helper])
        self.assertEqual(self.engine.puzzle_memories(observer, self.sid), [])

        payload = {**event["payload"], "seq": event["seq"], "at": round(event["ts"] * 1000)}
        result = persona_bridge.emit_world_event_to_persona(
            PROJECT, "Scout", space_id=self.sid, actor_id=scout, event_payload=payload,
            custom_persona_dir=str(self.persona),
        )
        self.assertEqual(result["status"], "applied")
        before_retry = self.mind("scout", scout)
        # The persona saved the result, but the world never received the acknowledgment.
        restored = self.open_world(self.data)
        self.heartbeat_for(restored).deliver_persona_events()
        self.assertEqual(restored.pending_persona_events(), [])
        self.assertEqual(self.mind("scout", scout), before_retry)
        self.assertGreater(before_retry["lastWorldEvent"]["deltaMood"],
                           self.mind("helper", helper)["lastWorldEvent"]["deltaMood"])
        self.assertGreater(restored.puzzle_memories(scout, self.sid)[0]["appraisal"]["deltaMood"],
                           restored.puzzle_memories(helper, self.sid)[0]["appraisal"]["deltaMood"])

        chat = WorldChatService(
            restored, project_root=self.root, settings_provider=lambda: self.settings, adapter_provider=lambda: None,
        )
        question = "还记得上次开的箱子吗？"
        self.assertIn("Helper用手里的钥匙", chat.chat(scout, question)["reply"])
        self.assertIn("我用手里的钥匙", chat.chat(helper, question)["reply"])
        self.assertNotIn("手里的钥匙", chat.chat(observer, question)["reply"])
        other = restored.create_space(name="Other room")["space_id"]
        self.assertNotIn("手里的钥匙", chat.chat(scout, question, space_id=other)["reply"])
        plugin = NanamiWorldPlugin()
        plugin._engine = restored
        plugin._settings_store = SimpleNamespace(get=lambda: self.settings)
        plugin._session_character = "Scout"
        bound_context = SimpleNamespace(messages=[{"role": "user", "content": question}])
        plugin._on_before_chat(bound_context)
        self.assertIn("Helper用手里的钥匙", bound_context.messages[0]["content"])
        plugin._session_character = "Unknown"
        unbound_context = SimpleNamespace(messages=[{"role": "user", "content": question}])
        plugin._on_before_chat(unbound_context)
        self.assertNotIn("亲历的解谜记录", unbound_context.messages[0]["content"])

        second = restored.place_from_blueprint("default_wooden_chest", self.sid, 6, 4)["entity_id"]
        without_memory = self.root / "without_memory"
        shutil.copytree(self.data, without_memory)
        self.settings = replace(self.settings, episodic_memory_enabled=False)
        disabled = self.open_world(without_memory)
        self.assertNotIn("手里的钥匙", WorldChatService(
            disabled, project_root=self.root, settings_provider=lambda: self.settings, adapter_provider=lambda: None,
        ).chat(scout, question)["reply"])
        no_memory = self.step_actor(disabled, scout, clock=self.clock + 86400)
        self.assertFalse(any("手里的钥匙打开" in action for action in no_memory["autonomous_actions"]))
        unopened = next(e for e in disabled.visible_entities(scout) if e["entity_id"] == second)
        self.assertTrue(unopened["traits"]["container"]["is_locked"])
        self.settings = replace(self.settings, episodic_memory_enabled=True)
        with_memory = self.step_actor(restored, scout, clock=self.clock + 86400)
        self.assertTrue(any("Helper用手里的钥匙打开" in action for action in with_memory["autonomous_actions"]))
        opened = next(e for e in restored.visible_entities(scout) if e["entity_id"] == second)
        self.assertTrue(opened["traits"]["container"]["is_open"])

        drink = restored.place_from_blueprint("default_hot_drink", self.sid, 7, 6)
        restored.execute_act(scout, drink["entity_id"], "consume")
        energy_after = next(a for a in restored.list_actors() if a["actor_id"] == scout)["vitals"]["energy"]
        heartbeat = self.heartbeat_for(restored)
        heartbeat.deliver_persona_events()
        heartbeat.step(advance_minutes=0, use_llm=False)
        final_energy = next(a for a in restored.list_actors() if a["actor_id"] == scout)["vitals"]["energy"]
        self.assertEqual(final_energy, energy_after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
