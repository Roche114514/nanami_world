"""M4-hunger tests: realistic decay rates, differentiated meals, morning restocking, and cognitive decoupling."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import projector
from plugins.nanami_world.cognitive_engine import CognitiveEngine
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService


class HungerSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.persona = self.root / "persona"
        self.persona.mkdir()
        self.settings = WorldSettings(heartbeat_use_llm=False, persona_data_dir=str(self.persona))
        self.clock_patch = patch("time.time", return_value=datetime(2026, 7, 1, 12, 0).timestamp())
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=self.root)
        self.addCleanup(self.heartbeat.stop)

    def test_hunger_rates_resting_slowdown_activity_cost_and_player_skip(self) -> None:
        room = self.engine.create_space(name="测速室", width=10, height=8, wall_rows=0)["space_id"]
        awake = self.engine.import_actor(name="清醒者", space_id=room, x=3, y=3)["actor_id"]
        sleeper = self.engine.import_actor(name="休眠者", space_id=room, x=5, y=3)["actor_id"]
        player = self.engine.ensure_player_avatar(space_id=room)["actor_id"]
        bed = self.engine.place_from_blueprint("default_bed", room, 5, 3)
        self.engine.place_from_blueprint("default_handheld", room, 3, 4)

        self.engine.update_actor_autonomy(awake, {"hunger": 0.20})
        self.engine.update_actor_autonomy(sleeper, {"hunger": 0.20})
        self.engine.update_actor_autonomy(player, {"hunger": 0.20})
        self.engine.execute_act(sleeper, bed["entity_id"], "rest")

        self.engine.tick_physics(advance_minutes=100.0)
        actors = {a["actor_id"]: a for a in self.engine.list_actors()}
        self.assertAlmostEqual(actors[awake]["autonomy"]["hunger"], 0.34, places=3)
        self.assertAlmostEqual(actors[sleeper]["autonomy"]["hunger"], 0.249, places=3)
        self.assertAlmostEqual(actors[player]["autonomy"]["hunger"], 0.20, places=3)

        handheld = next(e for e in self.engine.snapshot_for_ui(room)["entities"] if e["blueprint_id"] == "default_handheld")
        self.engine.execute_act(awake, handheld["entity_id"], "interact")
        awake_after = next(a for a in self.engine.list_actors() if a["actor_id"] == awake)
        self.assertAlmostEqual(awake_after["autonomy"]["hunger"], 0.35, places=3)

    def test_differentiated_hunger_delta_legacy_inference_and_npc_pack_cleanup(self) -> None:
        room = self.engine.create_space(name="餐厅", width=12, height=10, wall_rows=0)["space_id"]
        npc = self.engine.import_actor(name="食客", space_id=room, x=5, y=5)["actor_id"]
        player = self.engine.ensure_player_avatar(space_id=room)["actor_id"]
        self.engine.place_actor(player, room, 2, 2)

        ramen = self.engine.place_from_blueprint("default_ramen", room, 5, 4)
        cake = self.engine.place_from_blueprint("default_cake", room, 6, 4)
        snack = self.engine.place_from_blueprint("default_snack", room, 4, 4)
        drink = self.engine.place_from_blueprint("default_hot_drink", room, 5, 6)

        cases = [
            (ramen["entity_id"], 0.90, 0.35),
            (cake["entity_id"], 0.90, 0.52),
            (snack["entity_id"], 0.90, 0.62),
            (drink["entity_id"], 0.90, 0.72),
        ]
        for eid, start_h, expected_h in cases:
            self.engine.update_actor_autonomy(npc, {"hunger": start_h})
            self.engine.execute_act(npc, eid, "consume")
            cur_h = next(a for a in self.engine.list_actors() if a["actor_id"] == npc)["autonomy"]["hunger"]
            self.assertAlmostEqual(cur_h, expected_h, places=3, msg=eid)

        # Keep_empty vessel on the room table stays visible after second sip.
        self.engine.execute_act(npc, drink["entity_id"], "consume")
        room_cup = self.engine._world["entities"].get(drink["entity_id"])
        self.assertIsNotNone(room_cup)
        self.assertEqual(room_cup["traits"]["consumable"]["uses_left"], 0)

        # Legacy entity without explicit hunger_delta falls back cleanly by meal/drink semantics.
        legacy_meal = self.engine.place_from_blueprint("default_snack", room, 6, 6)
        self.engine.update_entity_spec(
            legacy_meal["entity_id"],
            name="特制牛肉拉面",
            traits={"consumable": {"max_uses": 2, "uses_left": 2, "consume_verb": "吃", "on_empty": "keep_empty"}},
        )
        # Ensure legacy_meal truly lacks hunger_delta in saved entity dict.
        self.engine._world["entities"][legacy_meal["entity_id"]]["effects"] = {"energy_delta": 0.10, "mood_v_delta": 0.08}
        self.engine.update_actor_autonomy(npc, {"hunger": 0.90})
        self.engine.execute_act(npc, legacy_meal["entity_id"], "consume")
        self.assertAlmostEqual(
            next(a for a in self.engine.list_actors() if a["actor_id"] == npc)["autonomy"]["hunger"],
            0.35,
            places=3,
        )

        # Keep_empty cup inside NPC inventory is auto-discarded on last sip; player's cup is kept.
        npc_cup = self.engine.place_from_blueprint("default_hot_drink", room, 4, 5)["entity_id"]
        self.engine.inventory_action(npc, "pickup", entity_id=npc_cup)
        for _ in range(2):
            self.engine.inventory_action(npc, "use", entity_id=npc_cup, verb="consume")
        self.assertNotIn(npc_cup, self.engine._world["entities"])

        self.engine.inventory_action(player, "claim", blueprint_id="default_hot_drink")
        p_cup = self.engine.inventory_state(player)["items"][0]["entity_id"]
        for _ in range(2):
            self.engine.inventory_action(player, "use", entity_id=p_cup, verb="consume")
        self.assertIn(p_cup, self.engine._world["entities"])
        self.assertEqual(self.engine._world["entities"][p_cup]["traits"]["consumable"]["uses_left"], 0)

    def test_daily_morning_shop_restock_and_bootstrap_recovery(self) -> None:
        self.clock_patch.stop()
        early_patch = patch("time.time", return_value=datetime(2026, 7, 1, 5, 50).timestamp())
        early_patch.start()
        self.addCleanup(early_patch.stop)

        early_engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world-restock", project_root=self.root)
        npc = early_engine.import_actor(name="七海千秋")["actor_id"]
        town = early_engine.create_default_town(move_actor_id=npc)["town_space_id"]
        cafe = f"{town}__cafe"

        # Deplete cafe coffee_maker & hot_drink and eat all of default_cake so it vanishes.
        cafe_snap = early_engine.snapshot_for_ui(cafe)
        cake = next(e for e in cafe_snap["entities"] if e["blueprint_id"] == "default_cake")
        coffee = next(e for e in cafe_snap["entities"] if e["blueprint_id"] == "default_coffee_maker")
        hot_drink = next(e for e in cafe_snap["entities"] if e["blueprint_id"] == "default_hot_drink")

        early_engine.place_actor(npc, cafe, coffee["x"], coffee["y"] + 1)
        for _ in range(3):
            early_engine.execute_act(npc, cake["entity_id"], "consume")
        self.assertNotIn(cake["entity_id"], early_engine._world["entities"])
        for ent_id, uses in ((coffee["entity_id"], 4), (hot_drink["entity_id"], 2)):
            for _ in range(uses):
                early_engine.execute_act(npc, ent_id, "consume")
        early_engine._world["entities"][coffee["entity_id"]]["traits"]["thermal"]["temp"] = "cold"

        early_engine.update_actor_autonomy(npc, {
            "cognition": {
                "places": {
                    cafe: {
                        "name": "微风咖啡馆",
                        "kind": "building",
                        "source": "direct",
                        "visits": 1,
                        "affordances": {"food": {"source": "direct", "confidence": 0.20, "samples": [0.0]}},
                    },
                },
            },
        })

        # 05:55 is still previous business day -> no restock yet.
        res_before = early_engine.tick_physics(advance_minutes=5.0)
        self.assertEqual(res_before["restocked"], [])
        self.assertEqual(early_engine._world["entities"][coffee["entity_id"]]["traits"]["consumable"]["uses_left"], 0)

        # Advance across 06:00 boundary -> morning restock triggers once!
        res_morning = early_engine.tick_physics(advance_minutes=10.0)
        self.assertIn("微风咖啡馆", res_morning["restocked"])
        coffee_after = early_engine._world["entities"][coffee["entity_id"]]
        drink_after = early_engine._world["entities"][hot_drink["entity_id"]]
        self.assertEqual(coffee_after["traits"]["consumable"]["uses_left"], 4)
        self.assertEqual(coffee_after["traits"]["thermal"]["temp"], "hot")
        self.assertEqual(drink_after["traits"]["consumable"]["uses_left"], 2)
        self.assertEqual(drink_after["brief"], early_engine._builtin_blueprints["default_hot_drink"]["brief"])
        self.assertTrue(
            any(
                e["space_id"] == cafe and e["blueprint_id"] == "default_cake"
                and e["traits"]["consumable"]["uses_left"] > 0
                for e in early_engine._world["entities"].values()
            )
        )
        cog = next(a for a in early_engine.list_actors() if a["actor_id"] == npc)["autonomy"]["cognition"]
        self.assertGreaterEqual(cog["places"][cafe]["affordances"]["food"]["confidence"], 0.65)

        # Second tick same afternoon does not restock again.
        res_again = early_engine.tick_physics(advance_minutes=60.0)
        self.assertEqual(res_again["restocked"], [])

        # Aged already-depleted save without shop_restock_day_by_realm bootstraps empty shops once.
        early_engine._world["shop_restock_day_by_realm"].pop(town, None)
        early_engine._world["entities"][coffee["entity_id"]]["traits"]["consumable"]["uses_left"] = 0
        early_engine._world["entities"][drink_after["entity_id"]]["traits"]["consumable"]["uses_left"] = 0
        for ent_id, ent in list(early_engine._world["entities"].items()):
            if ent.get("space_id") == cafe and "consumable" in (ent.get("traits") or {}):
                ent["traits"]["consumable"]["uses_left"] = 0
        res_boot = early_engine.tick_physics(advance_minutes=1.0)
        self.assertIn("微风咖啡馆", res_boot["restocked"])
        self.assertEqual(early_engine._world["entities"][coffee["entity_id"]]["traits"]["consumable"]["uses_left"], 4)

    def test_hunger_decoupling_pickup_chat_budget_and_projector(self) -> None:
        # Set daytime 12:00 and ensure town has no stray food so in_crisis decoupling is clean.
        npc = self.engine.import_actor(name="七海千秋", host_character="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=npc)["town_space_id"]
        arcade = f"{town}__arcade"
        for sid_x in (town, arcade):
            for ent in self.engine.snapshot_for_ui(sid_x)["entities"]:
                if "consumable" in (ent.get("traits") or {}):
                    traits = dict(ent["traits"])
                    traits["consumable"] = {**traits["consumable"], "uses_left": 0}
                    self.engine.update_entity_spec(ent["entity_id"], traits=traits)
        for item in self.engine.npc_pack(npc)["items"]:
            self.engine._world["entities"].pop(item["entity_id"], None)

        self.engine.place_actor(npc, town, 3, 14)
        player = self.engine.ensure_player_avatar(space_id=town)["actor_id"]
        self.engine.place_actor(player, town, 3, 15)

        now_min = float(self.engine.cognitive_scene(npc)["minutes"])
        self.engine.update_actor_autonomy(npc, {
            "hunger": 1.0,
            "cognition": {
                "modulation": {
                    "id": 99,
                    "realm": town,
                    "at": now_min,
                    "half_life": 12.0,
                    "strength": 1.0,
                    "source": "language",
                    "places": {arcade: 0.6},
                    "domains": {"game": 0.4},
                },
            },
        })
        cog_engine = CognitiveEngine(self.engine, self.root)
        scene = self.engine.cognitive_scene(npc)
        scene["engage"] = lambda *args: True
        decision = cog_engine.step(scene, self.settings, use_llm=False, talk=lambda *args: [])
        cog_after = next(a for a in self.engine.list_actors() if a["actor_id"] == npc)["autonomy"]["cognition"]
        self.assertNotEqual(cog_after.get("arbitration"), "生理稳态主导")
        self.assertEqual(cog_after.get("arbitration"), "社会脑/语言主导")

        # Hungry NPC with no food nearby still greets the player right in front of them.
        greet_room = self.engine.create_space(name="打招呼室", width=10, height=8, wall_rows=0)["space_id"]
        greeter = self.engine.import_actor(name="打招呼测试", space_id=greet_room, x=4, y=4)["actor_id"]
        greet_player = self.engine.ensure_player_avatar(space_id=greet_room)["actor_id"]
        self.engine.place_actor(greet_player, greet_room, 4, 5)
        for item in self.engine.npc_pack(greeter)["items"]:
            self.engine._world["entities"].pop(item["entity_id"], None)
        self.engine.update_actor_autonomy(greeter, {"hunger": 1.0})
        g_scene = self.engine.cognitive_scene(greeter, include_rooms=True)
        g_scene["engage"] = lambda *args: True
        g_decision = cog_engine.step(g_scene, self.settings, use_llm=False, talk=lambda *args: [])
        self.assertEqual(g_decision["goal"], "和眼前的玩家打招呼")

        # Hungry NPC eating available food still gets "生理稳态主导" arbitration label.
        eater = self.engine.import_actor(name="日向创", space_id=arcade, x=5, y=5)["actor_id"]
        self.engine.place_from_blueprint("default_snack", arcade, 5, 4)
        self.engine.update_actor_autonomy(eater, {"hunger": 0.90})
        eater_scene = self.engine.cognitive_scene(eater)
        eater_scene["engage"] = lambda *args: True
        eater_res = cog_engine.step(eater_scene, self.settings, use_llm=False, talk=lambda *args: [])
        eater_cog = next(a for a in self.engine.list_actors() if a["actor_id"] == eater)["autonomy"]["cognition"]
        self.assertEqual(eater_res["drive"], "food")
        self.assertEqual(eater_cog.get("arbitration"), "生理稳态主导")

        # Pack auto-pickup skips keep_empty tableware (hot_drink) and only pockets vanish snacks.
        picker_room = self.engine.create_space(name="拾取室", width=10, height=8, wall_rows=0)["space_id"]
        picker = self.engine.import_actor(name="拾取测试", space_id=picker_room, x=4, y=4)["actor_id"]
        self.engine.update_actor_autonomy(picker, {"hunger": 0.25})
        self.engine.npc_pack(picker)
        for item in self.engine.npc_pack(picker)["items"]:
            self.engine._world["entities"].pop(item["entity_id"], None)
        settled_tracks = {f"frontier|{picker_room}:{gx}:{gy}": [1.0, 0.0] for gx in range(3) for gy in range(3)}
        self.engine.update_actor_autonomy(picker, {"hunger": 0.25, "cognition": {"tracks": settled_tracks}})
        cup = self.engine.place_from_blueprint("default_hot_drink", picker_room, 4, 3)
        p_scene = self.engine.cognitive_scene(picker, include_rooms=True)
        p_scene["engage"] = lambda *args: True
        p_res1 = cog_engine.step(p_scene, self.settings, use_llm=False, talk=lambda *args: [])
        self.assertNotIn("带上", p_res1["goal"])
        self.assertEqual(self.engine.npc_pack(picker)["items"], [])

        self.engine.remove_entity(cup["entity_id"])
        self.engine.place_actor(picker, picker_room, 4, 4)
        self.engine.update_actor_autonomy(
            picker,
            {"hunger": 0.25, "cognition": {"active_intention": None, "tracks": settled_tracks}},
        )
        takeaway = self.engine.place_from_blueprint("default_snack", picker_room, 4, 3)
        p_scene2 = self.engine.cognitive_scene(picker, include_rooms=True)
        p_scene2["engage"] = lambda *args: True
        p_res2 = cog_engine.step(p_scene2, self.settings, use_llm=False, talk=lambda *args: [])
        self.assertIn("带上", p_res2["goal"])
        self.assertEqual([i["entity_id"] for i in self.engine.npc_pack(picker)["items"]], [takeaway["entity_id"]])

        # World chat budget at hunger=0.80 is NOT clamped to 2 turns, and single-chat projector shows hunger sensation.
        chat_room = self.engine.create_space(name="聊天室", width=10, height=8, wall_rows=0)["space_id"]
        ca = self.engine.import_actor(name="甲", space_id=chat_room, x=4, y=4)["actor_id"]
        cb = self.engine.import_actor(name="乙", space_id=chat_room, x=5, y=4)["actor_id"]
        self.engine.update_actor_autonomy(ca, {"hunger": 0.80})
        self.engine.update_actor_autonomy(cb, {"hunger": 0.80})
        chat_svc = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: WorldSettings(heartbeat_use_llm=True))
        prompts_seen: list[str] = []

        def chat_model(system, user, **kwargs):
            prompts_seen.append(system + "\n" + user)
            turn = len(prompts_seen)
            return {
                "intent_a": f"话题第{turn}句",
                "intent_b": f"回应第{turn}句",
                "end_chat": turn >= 3,
            }

        with patch("plugins.nanami_world.text_compiler._ask_llm_json", side_effect=chat_model), \
                patch.object(chat_svc, "_mouth_speak", side_effect=lambda *args, **kwargs: args[2]):
            chat_res = chat_svc.conduct_actor_social_exchange(ca, cb, generated_only=False)
        self.assertGreaterEqual(chat_res["turns_count"], 3)
        self.assertIn("有点饿", prompts_seen[0])
        self.assertNotIn("本轮达到状态上限 2 轮", prompts_seen[0])

        self.engine.update_actor_autonomy(ca, {"hunger": 0.30})
        self.assertNotIn("身体体感", projector.project_view(self.engine, ca))
        self.engine.update_actor_autonomy(ca, {"hunger": 0.70})
        self.assertIn("有点饿了", projector.project_view(self.engine, ca))
        self.engine.update_actor_autonomy(ca, {"hunger": 0.90})
        self.assertIn("肚子很饿，想找点吃的", projector.project_view(self.engine, ca))


if __name__ == "__main__":
    unittest.main(verbosity=2)
