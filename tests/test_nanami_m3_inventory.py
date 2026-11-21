"""M3 tests for physical item transfer, player RPCs and NPC inventory decisions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import persona_bridge
from plugins.nanami_world.cognitive_engine import CognitiveEngine
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_page import register_pages


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.engine = self.open_world()
        self.room = self.engine.create_space(name="道具房", width=10, height=8, wall_rows=0)["space_id"]
        self.player = self.engine.ensure_player_avatar(space_id=self.room)["actor_id"]
        self.engine.place_actor(self.player, self.room, 4, 4)
        self.npc = self.engine.import_actor(name="测试角色", space_id=self.room, x=4, y=5)["actor_id"]
        self.settings = WorldSettings(heartbeat_use_llm=False)

    def open_world(self) -> WorldEngine:
        return WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)

    def claim(self, blueprint_id: str) -> str:
        existing = {i["entity_id"] for i in self.engine.inventory_state(self.player)["items"]}
        self.engine.inventory_action(self.player, "claim", blueprint_id=blueprint_id)
        added, = {i["entity_id"] for i in self.engine.inventory_state(self.player)["items"]} - existing
        return added

    def test_hotbar_item_remains_one_instance_across_use_box_map_and_portal(self) -> None:
        handheld = self.claim("default_handheld")
        snack = self.claim("default_snack")
        self.engine.inventory_action(self.player, "swap", entity_id=snack, slot=0)
        self.assertEqual(self.engine.inventory_state(self.player)["items"][0]["entity_id"], snack)
        for _ in range(2):
            self.engine.inventory_action(self.player, "use", entity_id=snack, verb="consume")
        self.assertNotIn(snack, self.engine._world["entities"])
        self.engine.inventory_action(self.player, "use", entity_id=handheld, verb="interact")

        box = self.engine.place_from_blueprint("default_box", self.room, 5, 4)
        box_id = box["entity_id"]
        result = self.engine.player_interact_front(self.player, facing="right")
        self.assertEqual(result["kind"], "container")
        self.engine.inventory_action(self.player, "deposit", entity_id=handheld, chest_id=box_id)
        self.assertEqual(self.engine.inventory_state(self.player, box_id)["chest"]["items"][0]["entity_id"], handheld)
        self.engine.inventory_action(self.player, "withdraw", entity_id=handheld, chest_id=box_id)
        self.engine.inventory_action(self.player, "place", entity_id=handheld, facing="up")
        self.engine.inventory_action(self.player, "pickup", entity_id=handheld)

        destination = self.engine.create_space(name="隔壁", width=10, height=8, wall_rows=0)["space_id"]
        self.engine.place_from_blueprint("default_door", self.room, 4, 3, portal_target_space_id=destination)
        self.assertEqual(self.engine.player_interact_front(self.player, facing="up")["kind"], "entered_space")
        reloaded = self.open_world()
        item = reloaded._world["entities"][handheld]
        self.assertEqual(item["space_id"], destination)
        self.assertEqual(item["traits"]["activity"]["progress"], 1)
        self.assertEqual(reloaded.inventory_state(self.player)["items"][0]["entity_id"], handheld)

    def test_transfer_rpc_only_moves_a_gift_if_accepted_and_records_recipient_relation(self) -> None:
        configs = []
        register_pages(SimpleNamespace(register_frontend_config_page=configs.append), code_dir=WORLD_CODE,
                       engine=self.engine, settings_store=SimpleNamespace(get=lambda: self.settings),
                       heartbeat=SimpleNamespace())
        action = next(rule.run for rule in configs[0].actions if rule.id == "player_inventory")
        item = self.claim("default_snack")
        gift = {"operation": "gift", "entity_id": item, "recipient_id": self.npc, "space_id": self.room}
        persona_bridge.save_world_actor_relation(self.engine.data_root, self.room, "测试角色", "user", {
            "known": True, "attitude_stance": "cold",
        })
        self.assertEqual(action(gift)["gift"]["status"], "declined")
        self.assertEqual(self.engine.inventory_state(self.player)["items"][0]["entity_id"], item)
        persona_bridge.save_world_actor_relation(self.engine.data_root, self.room, "测试角色", "user", {
            "known": True, "attitude_stance": "warm",
        })
        accepted = action(gift)
        self.assertEqual(accepted["gift"]["status"], "accepted")
        self.assertEqual(accepted["inventory"]["items"], [])
        edge = persona_bridge.read_actor_relations(self.root, "测试角色", space_id=self.room,
                                                   world_data_root=self.engine.data_root)["user"]
        self.assertGreater(edge["affective_valence"], 0)
        self.assertFalse(action(gift)["ok"])
        self.assertEqual(self.engine.give_item(self.npc, self.player, item)["status"], "offered")
        offer, = self.engine.gift_offers_for(self.player)
        self.assertEqual(self.engine.inventory_state(self.npc)["items"][0]["entity_id"], item)
        reply = action({"operation": "accept_gift", "offer_id": offer["offer_id"], "space_id": self.room})
        self.assertEqual(reply["inventory"]["items"][0]["entity_id"], item)
        self.assertFalse(action({"operation": "accept_gift", "offer_id": offer["offer_id"]})["ok"])

    def test_npc_can_use_food_and_offer_a_gift_based_on_relationship(self) -> None:
        cognitive = CognitiveEngine(self.engine, self.root)
        self.engine.update_actor_autonomy(self.npc, {"hunger": 0.82})

        def decide():
            scene = self.engine.cognitive_scene(self.npc, include_rooms=True)
            scene["engage"] = lambda *args: True
            return cognitive.step(scene, self.settings, use_llm=False, talk=lambda *args: [])

        self.assertIn("吃随身", decide()["goal"])
        carried, = self.engine.npc_pack(self.npc)["items"]
        self.assertEqual(carried["traits"]["consumable"]["uses_left"], 1)
        self.engine.update_actor_autonomy(self.npc, {"hunger": 0.1})
        persona_bridge.save_world_actor_relation(self.engine.data_root, self.room, "测试角色", "user", {
            "known": True, "attitude_stance": "warm", "warmth": 0.9,
        })
        noon = {**self.engine.cognitive_scene(self.npc, include_rooms=True)["lighting"], "hour": 12}
        with patch("plugins.nanami_world.engine.compute_lighting", return_value=noon):
            self.assertIn("送给", decide()["goal"])
        offer, = self.engine.gift_offers_for(self.player)
        self.assertEqual(offer["entity_id"], carried["entity_id"])
        self.engine.respond_to_gift(self.player, offer["offer_id"], accept=False)
        other = self.engine.import_actor(name="同伴", space_id=self.room, x=5, y=5)["actor_id"]
        gift = self.engine.give_item(self.npc, other, carried["entity_id"])
        self.assertEqual(gift["status"], "accepted")
        self.assertEqual(self.engine.inventory_state(other)["items"][0]["entity_id"], carried["entity_id"])

    def test_full_npc_pack_stores_item_in_its_box_and_respects_privacy(self) -> None:
        box = self.engine.place_from_blueprint("default_box", self.room, 5, 5)["entity_id"]
        self.engine.execute_act(self.player, box, "open")
        self.engine.inventory_action(self.player, "assign_chest", chest_id=box, owner_id=self.npc)
        for _ in range(8):
            key = self.engine.place_from_blueprint("default_brass_key", self.room, 3, 5)["entity_id"]
            self.engine.inventory_action(self.npc, "pickup", entity_id=key)
        scene = self.engine.cognitive_scene(self.npc, include_rooms=True)
        scene["engage"] = lambda *args: True
        cognitive = CognitiveEngine(self.engine, self.root)
        self.assertIn("存进", cognitive.step(scene, self.settings, use_llm=False, talk=lambda *args: [])["goal"])
        pack = self.engine.inventory_state(self.npc, box)
        self.assertEqual((len(pack["items"]), len(pack["chest"]["items"])), (7, 1))
        with self.assertRaisesRegex(ValueError, "私有箱"):
            self.engine.inventory_state(self.player, box)

    def test_load_migrates_legacy_player_hand_to_first_available_slot(self) -> None:
        first = self.claim("default_snack")
        held = self.engine.place_from_blueprint("default_handheld", self.room, 5, 4)["entity_id"]
        with self.engine._lock:
            self.engine._world["actors"][self.player]["holding_id"] = held
            self.engine._world["entities"][held].update(parent_id=f"actor:{self.player}", z=-1)
            self.engine._save_world_locked()
        loaded = self.open_world()
        self.assertIsNone(loaded.live_actor_frame(self.player)["holding_id"])
        self.assertEqual([(item["slot"], item["entity_id"]) for item in loaded.inventory_state(self.player)["items"]],
                         [(0, first), (1, held)])


if __name__ == "__main__":
    unittest.main()
