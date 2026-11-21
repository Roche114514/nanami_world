"""Checks the loan outcome while changing only what the lender receives."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import PLUGIN_ID, PLUGIN_VERSION, world_page
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from sdk.register import PluginCapabilityRegistry


class KeyLoanTests(unittest.TestCase):
    def setUp(self) -> None:
        isolated = tempfile.TemporaryDirectory(prefix="nanami-loan-test-")
        self.addCleanup(isolated.cleanup)
        self.root = Path(isolated.name)
        clock = patch("time.time", return_value=datetime(2026, 7, 1, 12).timestamp())
        clock.start()
        self.addCleanup(clock.stop)
        self.code = WORLD_CODE
        self.sid = "room_default"
        self.persona = self.root / "persona"
        self.persona.mkdir()
        (self.persona / "pointers.yaml").write_text(
            "pointers:\n- id: lender\n  character_name: Lender\n  state_root: lender\n", encoding="utf-8",
        )
        self.traits = self.persona / "lender" / "state" / "traits.json"
        self.traits.parent.mkdir(parents=True)
        self.write_courtesy(0.9)
        self.settings = WorldSettings(persona_data_dir=str(self.persona))
        self.world = self.open_world("original")
        self.lender = self.world.import_actor(name="Lender", x=2, y=4)["actor_id"]
        self.borrower = self.world.import_actor(name="Borrower", x=3, y=4)["actor_id"]
        self.observer = self.world.import_actor(name="Observer", x=9, y=6)["actor_id"]
        self.key = self.world.place_from_blueprint("default_brass_key", self.sid, 2, 5)["entity_id"]
        self.world.execute_act(self.lender, self.key, "take")

    def write_courtesy(self, value: float) -> None:
        self.traits.write_text(json.dumps({"traits": {
            "social": {"normCompliance": value, "prosocial": value},
        }}), encoding="utf-8")

    def open_world(self, name: str) -> WorldEngine:
        return WorldEngine(code_dir=self.code, data_root=self.root / name, project_root=self.root)

    def copy_world(self, name: str) -> WorldEngine:
        shutil.copytree(self.world.data_root, self.root / name)
        return self.open_world(name)

    def heartbeat(self, world: WorldEngine) -> WorldHeartbeat:
        return WorldHeartbeat(world, lambda: self.settings, project_root=self.root)

    def key_loan_action(self, engine: WorldEngine, action_id: str = "key_loan"):
        registry = PluginCapabilityRegistry()
        registry.set_settings_ui_plugin_context(PLUGIN_ID, PLUGIN_VERSION)
        world_page.register_pages(
            registry, code_dir=self.code, engine=engine, heartbeat=self.heartbeat(engine),
            settings_store=SimpleNamespace(get=lambda: self.settings),
        )
        canvas = next(page for page in registry.frontend_config_contributions if page.page_id == "world_canvas")
        action = next(action for action in canvas.actions if action.id == action_id)
        return action.run

    def holder(self, world: WorldEngine) -> str:
        item = next(item for item in world.snapshot_for_ui(self.sid)["entities"] if item["entity_id"] == self.key)
        owning_actors = [actor for actor in world.list_actors() if actor["holding_id"] == self.key]
        self.assertEqual(len(owning_actors), 1)
        self.assertEqual(item["parent_id"], f"actor:{owning_actors[0]['actor_id']}")
        return owning_actors[0]["actor_id"]

    def borrowed_and_blocked(self) -> str:
        action = self.key_loan_action(self.world)
        lent = action({
            "operation": "lend", "actor_id": self.lender, "borrower_id": self.borrower, "item_id": self.key,
            "minutes": 30, "space_id": self.sid,
        })
        self.assertTrue(lent["ok"], lent)
        loan_id = self.world.key_loans_for(self.borrower)[0]["id"]
        self.assertEqual(self.holder(self.world), self.borrower)
        self.assertIn("坐标 (9, 4)", self.world.execute_move(self.borrower, 9, 4))
        self.world.paint_tiles(self.sid, [
            {"x": 6, "y": y, "tile_id": "brick_wall"} for y in range(self.world.snapshot_for_ui()["space"]["height"])
        ])
        blocked = action({"operation": "return", "actor_id": self.borrower, "loan_id": loan_id})
        self.assertIn("路被挡住", blocked["feedback"])
        self.assertEqual(self.holder(self.world), self.borrower)
        self.assertEqual(self.world.key_loans_for(self.borrower)[0]["status"], "open")
        return loan_id

    def test_message_arrival_changes_reaction_and_physical_return_settles_the_loan(self) -> None:
        original_traits = self.traits.read_bytes()
        loan_id = self.borrowed_and_blocked()
        action = self.key_loan_action(self.world)
        self.assertTrue(action({"operation": "send_extension", "actor_id": self.borrower, "loan_id": loan_id})["ok"])
        self.assertNotIn("notice", action({"operation": "list", "actor_id": self.lender})["loans"][0])
        self.assertEqual(action({"operation": "list", "actor_id": self.observer})["loans"], [])
        self.assertFalse(action({"operation": "deliver_extension", "actor_id": self.observer, "loan_id": loan_id})["ok"])

        for received, expected in ((False, "ask"), (True, "accept")):
            with self.subTest(received=received):
                branch = self.copy_world(f"received_{received}")
                if received:
                    delivery = self.key_loan_action(branch)({
                        "operation": "deliver_extension", "actor_id": self.borrower, "loan_id": loan_id,
                    })
                    self.assertTrue(delivery["ok"], delivery)
                    branch.deliver_key_loan_extension(self.borrower, loan_id)
                branch.tick_physics(advance_minutes=45)
                result = self.heartbeat(branch).step(
                    advance_minutes=0.01, use_llm=False, actor_id=self.lender,
                )
                reply = result["decisions"][0]["loan_response"]
                self.assertEqual(reply["choice"], expected)
                self.assertEqual(reply["evidence"], "extension_received" if received else "overdue")
                self.assertNotIn("reason", reply)
                self.assertIsNone(self.heartbeat(branch).respond_to_key_loan(self.lender))

                # Open one real passage. Return must traverse it and transfer the same key.
                branch.paint_tiles(self.sid, [{"x": 6, "y": 4, "tile_id": "wood_floor"}])
                outcome = branch.return_key(self.borrower, loan_id)
                self.assertEqual(outcome, {"outcome": "returned", "late": not received})
                self.assertEqual(branch.return_key(self.borrower, loan_id), outcome)
                self.assertEqual(self.holder(branch), self.lender)
                reopened = self.open_world(f"received_{received}")
                self.assertEqual(reopened.key_loans_for(self.lender)[0]["status"], "returned")
                events = reopened.read_recent_events()
                self.assertEqual(sum(event["kind"] == "key_returned" for event in events), 1)
                self.assertEqual(sum(event["kind"] == "key_extension_received" for event in events), int(received))
                blocked = next(event for event in events if event["kind"] == "key_return_blocked")
                self.assertEqual(blocked["payload"]["reason"], "route_blocked")
                self.assertEqual(blocked["payload"]["witness_ids"], [self.borrower])
        self.assertEqual(self.traits.read_bytes(), original_traits)
        self.assertFalse((self.traits.parent / "mind.json").exists())

    def test_same_received_request_uses_existing_courtesy_and_energy(self) -> None:
        loan_id = self.borrowed_and_blocked()
        self.world.send_key_loan_extension(self.borrower, loan_id)
        self.world.deliver_key_loan_extension(self.borrower, loan_id)
        for expected, courtesy, energy in (("accept", 0.9, 0.8), ("ask", 0.1, 0.8), ("wait", 0.9, 0.2)):
            with self.subTest(expected=expected):
                branch = self.copy_world(f"choice_{expected}")
                self.write_courtesy(courtesy)
                branch.set_actor_world_mood(self.lender, vitals_patch={"energy": energy})
                reaction = self.heartbeat(branch).respond_to_key_loan(self.lender)["loan_response"]
                self.assertEqual(reaction["choice"], expected)

    def test_wait_retries_after_world_time_advances_and_key_removal_closes_the_loan(self) -> None:
        loan_id = self.borrowed_and_blocked()
        self.world.send_key_loan_extension(self.borrower, loan_id)
        self.world.deliver_key_loan_extension(self.borrower, loan_id)
        self.world.set_actor_world_mood(self.lender, vitals_patch={"energy": 0.2})
        heartbeat = self.heartbeat(self.world)
        self.assertEqual(heartbeat.respond_to_key_loan(self.lender)["loan_response"]["choice"], "wait")
        self.world.set_actor_world_mood(self.lender, vitals_patch={"energy": 0.8})
        self.assertIsNone(heartbeat.respond_to_key_loan(self.lender))
        self.world.tick_physics(advance_minutes=10)
        self.assertEqual(heartbeat.respond_to_key_loan(self.lender)["loan_response"]["choice"], "accept")
        self.world.remove_entity(self.key)
        self.assertEqual(self.world.key_loans_for(self.borrower)[0]["status"], "cancelled")
        self.assertIsNone(heartbeat.respond_to_key_loan(self.lender))

    def test_canvas_snapshot_syncs_loans_and_events_for_the_selected_actor(self) -> None:
        loan_id = self.borrowed_and_blocked()
        self.world.send_key_loan_extension(self.borrower, loan_id)
        snapshot = self.key_loan_action(self.world, "get_world_snapshot")

        self.world.set_active_actor(self.borrower)
        borrower = snapshot({"space_id": self.sid})["snapshot"]
        self.assertIn("notice", borrower["key_loans"][0])
        self.assertTrue(any(event["kind"] == "key_extension_sent" for event in borrower["events"]))

        self.world.set_active_actor(self.lender)
        lender = snapshot({"space_id": self.sid})["snapshot"]
        self.assertNotIn("notice", lender["key_loans"][0])
        self.assertIsNone(lender["pending_key_loan"])
        self.assertTrue(all(event["kind"] not in {"key_extension_sent", "key_return_blocked"}
                            for event in lender["events"]))

        self.world.deliver_key_loan_extension(self.borrower, loan_id)
        lender = snapshot({"space_id": self.sid})["snapshot"]
        self.assertEqual(lender["pending_key_loan"]["evidence"], "extension_received")
        self.assertTrue(any(event["kind"] == "key_extension_received" for event in lender["events"]))

        self.world.set_active_actor(self.observer)
        observer = snapshot({"space_id": self.sid})["snapshot"]
        self.assertEqual(observer["key_loans"], [])
        self.assertIsNone(observer["pending_key_loan"])
        self.assertFalse(any(event["kind"].startswith("key_") for event in observer["events"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
