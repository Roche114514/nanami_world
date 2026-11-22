"""M4a tests: light weather state machine, persona coupling, circadian clock fix, and NPC schedules."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from _shinsekai import PROJECT, WORLD_CODE

from plugins.nanami_world import persona_bridge, projector, schedule, weather
from plugins.nanami_world.engine import WorldEngine, compute_lighting
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_page import CANVAS_PAGE_ID, register_pages


class M4aWeatherAndScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.persona = self.root / "persona"
        self.persona.mkdir()
        self.settings = WorldSettings(heartbeat_use_llm=False, persona_data_dir=str(self.persona))
        self.clock_patch = patch("time.time", return_value=datetime(2026, 7, 1, 14, 30).timestamp())
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=self.root)
        self.addCleanup(self.heartbeat.stop)

    def _write_persona(self, name: str, traits: dict, perception: dict) -> None:
        pointers = self.persona / "pointers.yaml"
        existing = pointers.read_text(encoding="utf-8") if pointers.is_file() else "pointers:\n"
        pointers.write_text(
            existing + f"- id: {name}\n  character_name: {name}\n  state_root: {name}\n",
            encoding="utf-8",
        )
        st_dir = self.persona / name / "state"
        st_dir.mkdir(parents=True, exist_ok=True)
        (st_dir / "traits.json").write_text(json.dumps({"traits": traits}), encoding="utf-8")
        (st_dir / "world_perception.json").write_text(json.dumps(perception), encoding="utf-8")

    def test_compute_lighting_clock_fix_and_chronotype_bio_hour(self) -> None:
        self._write_persona(
            "early_bird",
            {"effortfulControl": 0.9, "needProfile": {"safety": 0.8}},
            {"chronotype_offset": -3.0, "circadian_strength": 0.85},
        )
        self._write_persona(
            "night_owl",
            {"domainEfficacy": {"game": 0.95}, "needProfile": {"competence": 0.85}},
            {"chronotype_offset": 2.0, "circadian_strength": 0.85},
        )
        room = self.engine.snapshot_for_ui("room_default")["space"]
        # Pinned clock is 14:30 -> shift +390 min to 21:00
        lighting = compute_lighting(room, {}, datetime(2026, 7, 1, 14, 30).timestamp(), 390.0)
        self.assertEqual(lighting["hour"], 21)
        self.assertEqual(lighting["minute"], 0)
        self.assertAlmostEqual(lighting["hour_float"], 21.0, places=2)
        self.assertIn("sim_day", lighting)

        early = self.engine.import_actor(name="早起同学", host_character="early_bird")
        owl = self.engine.import_actor(name="夜猫同学", host_character="night_owl")
        l0_early = persona_bridge.build_percept_frame(
            self.engine.data_root, self.root, room, lighting, early, [], custom_persona_dir=str(self.persona),
        )
        l0_owl = persona_bridge.build_percept_frame(
            self.engine.data_root, self.root, room, lighting, owl, [], custom_persona_dir=str(self.persona),
        )
        # 21:00 - (-3.0) = 0:00 (deep_night); 21:00 - (+2.0) = 19:00 (daytime)
        self.assertEqual(l0_early["bio_hour"], 0.0)
        self.assertEqual(l0_early["circadian_phase"], "deep_night")
        self.assertEqual(l0_owl["bio_hour"], 19.0)
        self.assertEqual(l0_owl["circadian_phase"], "daytime")

    def test_weather_determinism_realm_sharing_sight_and_thermal_cooling(self) -> None:
        nanami = self.engine.import_actor(name="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=nanami)["town_space_id"]
        cafe = f"{town}__cafe"

        # Initial weather starts clear and stays deterministic over advance_weather steps
        self.assertEqual(self.engine.get_weather(town)["current"], "clear")
        w0 = weather.default_weather_state(0.0)
        w1_a, _ = weather.advance_weather(w0, 400.0, realm_seed="seed-demo")
        w1_b, _ = weather.advance_weather(w0, 400.0, realm_seed="seed-demo")
        self.assertEqual(w1_a, w1_b)

        # Setting rain on town shares with indoor cafe, but differentiates outdoor vs indoor
        self.engine.set_weather(town, "rain")
        w_out = self.engine.get_weather(town)
        w_in = self.engine.get_weather(cafe)
        self.assertEqual(w_out["current"], "rain")
        self.assertEqual(w_in["current"], "rain")
        self.assertTrue(w_out["is_outdoor"])
        self.assertFalse(w_in["is_outdoor"])
        self.assertIn("户外", w_out["description"])
        self.assertIn("窗外", w_in["description"])

        # Outdoor rain shrinks spatial sight radius (12 -> 9); indoor remains unrestricted
        self.engine.place_actor(nanami, town, 19, 15)
        drink_near = self.engine.place_from_blueprint("default_hot_drink", town, 19, 24)  # dist=9 -> visible
        drink_edge = self.engine.place_from_blueprint("default_hot_drink", town, 19, 26)  # dist=11 -> hidden in rain
        vis_rain = {e["entity_id"] for e in self.engine.visible_entities(nanami)}
        self.assertIn(drink_near["entity_id"], vis_rain)
        self.assertNotIn(drink_edge["entity_id"], vis_rain)

        self.engine.set_weather(town, "clear")
        vis_clear = {e["entity_id"] for e in self.engine.visible_entities(nanami)}
        self.assertIn(drink_edge["entity_id"], vis_clear)

        # Outdoor rain accelerates hot drink cooling vs indoor hot drink (cool_minutes=45)
        drink_out = self.engine.place_from_blueprint("default_hot_drink", town, 18, 15)
        drink_in = self.engine.place_from_blueprint("default_hot_drink", cafe, 5, 5)
        self.engine.set_weather(town, "rain")
        # 18 min * 1.25 = 22.5 >= 45 * 0.45 (20.25) -> outdoor becomes warm, indoor stays hot
        self.engine.tick_physics(advance_minutes=18.0)
        snap_town = self.engine.snapshot_for_ui(town)
        snap_cafe = self.engine.snapshot_for_ui(cafe)
        temp_out = next(e for e in snap_town["entities"] if e["entity_id"] == drink_out["entity_id"])["traits"]["thermal"]["temp"]
        temp_in = next(e for e in snap_cafe["entities"] if e["entity_id"] == drink_in["entity_id"])["traits"]["thermal"]["temp"]
        self.assertEqual(temp_out, "warm")
        self.assertEqual(temp_in, "hot")

    def test_weather_persona_events_and_shelter_utility_without_omniscience(self) -> None:
        self._write_persona(
            "cautious_girl",
            {"effortfulControl": 0.85, "needProfile": {"safety": 0.92, "competence": 0.6}},
            {"chronotype_offset": 0.0, "circadian_strength": 0.75},
        )
        out_actor = self.engine.import_actor(name="小雨", host_character="cautious_girl")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=out_actor)["town_space_id"]
        cafe = f"{town}__cafe"
        library = f"{town}__library"
        in_actor = self.engine.import_actor(name="小静", host_character="cautious_girl", space_id=cafe, x=5, y=5)["actor_id"]

        self.engine.set_weather(town, "rain")
        pending = self.engine.pending_persona_events()
        weather_evs = [ev for ev in pending if ev["payload"].get("action") == "weather"]
        by_actor = {ev["actor_id"]: ev["payload"] for ev in weather_evs}
        self.assertEqual(by_actor[out_actor]["kind"], "weather_rain_exposed")
        self.assertEqual(by_actor[out_actor]["need"], "safety")
        self.assertLess(by_actor[out_actor]["epa"]["e"], 0.0)
        self.assertEqual(by_actor[in_actor]["kind"], "weather_rain_cozy")
        self.assertEqual(by_actor[in_actor]["need"], "safety")
        self.assertGreater(by_actor[in_actor]["epa"]["e"], 0.0)

        # Deliver directly through real mind.mjs --world-event bridge to verify L2/L5 compatibility
        if (PROJECT / "plugins" / "nanami_persona" / "toolkit" / "mind.mjs").is_file():
            ev_out = next(ev for ev in weather_evs if ev["actor_id"] == out_actor)
            ev_in = next(ev for ev in weather_evs if ev["actor_id"] == in_actor)
            res_out = persona_bridge.emit_world_event_to_persona(
                PROJECT, "cautious_girl", space_id=town, actor_id=out_actor,
                event_payload={**ev_out["payload"], "seq": ev_out["seq"], "at": round(ev_out["ts"] * 1000)},
                custom_persona_dir=str(self.persona),
            )
            res_in = persona_bridge.emit_world_event_to_persona(
                PROJECT, "cautious_girl", space_id=town, actor_id=in_actor,
                event_payload={**ev_in["payload"], "seq": ev_in["seq"], "at": round(ev_in["ts"] * 1000)},
                custom_persona_dir=str(self.persona),
            )
            self.assertEqual((res_out or {}).get("status"), "applied")
            self.assertEqual((res_in or {}).get("status"), "applied")
            self.assertLess(res_out["appraisal"]["deltaMood"], 0.0)
            self.assertGreater(res_in["appraisal"]["deltaMood"], 0.0)

        # Place out_actor away from the central notice board & library door; teach only cafe portal
        self.engine.place_actor(out_actor, town, 4, 15)
        cafe_door = next(
            e for e in self.engine.snapshot_for_ui(town)["entities"]
            if e["traits"].get("portal", {}).get("target_space_id") == cafe
        )
        self.engine.update_actor_autonomy(out_actor, {
            "cognition": {
                "places": {town: {"name": "樱町", "kind": "town", "visits": 1}, cafe: {"name": "微风咖啡馆", "kind": "building", "visits": 1}},
                "portals": {f"{town}:{cafe}": {"space": town, "target": cafe, "name": "微风咖啡馆", "x": cafe_door["x"], "y": cafe_door["y"], "confidence": 1.0, "samples": [1.0]}},
                "zones": {}, "tracks": {}, "blocked": {}, "read": {}, "peers": {},
            }
        })

        def _visit_util(weather_mode: str) -> tuple[float, dict]:
            self.engine.set_weather(town, weather_mode)
            scene = self.engine.cognitive_scene(out_actor)
            cog = self.heartbeat._cognitive
            cog.step(scene, self.settings, use_llm=False, talk=lambda *args: [])
            act_now = next(a for a in self.engine.list_actors() if a["actor_id"] == out_actor)
            cognition = act_now["autonomy"]["cognition"]
            self.assertNotIn(library, cognition["places"])
            sc = self.engine.cognitive_scene(out_actor)
            percept = persona_bridge.build_percept_frame(
                self.engine.data_root, self.root, sc["space"], sc["lighting"], sc["actor"], sc["visible"],
                custom_persona_dir=str(self.persona), weather=sc["weather"],
            )
            sc["percept"] = percept
            sc["schedule"] = schedule.build_actor_schedule(sc["actor"], percept["behavior_profile"], percept, sc["realm_buildings"], home_space_id=sc["home_space_id"])
            st = sc["actor"]["autonomy"]["cognition"]
            cog._update_drives(st, sc)
            cands = cog._candidates(st, sc, [], {}, False)
            cafe_u = next(c["utility"] for c in cands if c["kind"] == "visit" and c["target"] == cafe)
            return cafe_u, cognition

        clear_u, _ = _visit_util("clear")
        rain_u, cog_after = _visit_util("rain")
        self.assertGreater(rain_u, clear_u)
        self.assertNotIn(library, cog_after["places"])

    def test_schedule_generation_home_persistence_and_room_isolation(self) -> None:
        self._write_persona(
            "gamer_p",
            {"domainEfficacy": {"game": 0.98, "nonGame": 0.18}, "needProfile": {"competence": 0.9, "safety": 0.3}},
            {"chronotype_offset": 2.0, "circadian_strength": 0.85},
        )
        self._write_persona(
            "social_p",
            {"surgency": 0.95, "socialEfficacy": 0.92, "domainEfficacy": {"game": 0.2, "nonGame": 0.55}, "needProfile": {"relatedness": 0.95}},
            {"chronotype_offset": 0.0, "circadian_strength": 0.80},
        )
        nanami = self.engine.import_actor(name="七海千秋", host_character="gamer_p")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=nanami)["town_space_id"]
        hinata = self.engine.import_actor(name="日向创", host_character="social_p", space_id=town, x=18, y=15)["actor_id"]

        self.assertEqual(self.engine.get_actor_home_space(nanami, town), f"{town}__home")
        self.assertEqual(self.engine.get_actor_home_space(hinata, town), f"{town}__guest")

        # Manual home assignment persists across reload and stays isolated from room_default
        self.engine.set_actor_home_space(hinata, f"{town}__cafe", realm_space_id=town)
        reloaded = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)
        self.assertEqual(reloaded.get_actor_home_space(hinata, town), f"{town}__cafe")

        sc_n = reloaded.cognitive_scene(nanami)
        sc_h = reloaded.cognitive_scene(hinata)
        l0_n = persona_bridge.read_world_perception_config(self.root, "gamer_p", custom_persona_dir=str(self.persona))
        l0_h = persona_bridge.read_world_perception_config(self.root, "social_p", custom_persona_dir=str(self.persona))
        bp_n = persona_bridge.read_persona_behavior_profile(self.root, "gamer_p", custom_persona_dir=str(self.persona))
        bp_h = persona_bridge.read_persona_behavior_profile(self.root, "social_p", custom_persona_dir=str(self.persona))
        sched_n = schedule.build_actor_schedule(sc_n["actor"], bp_n, l0_n, sc_n["realm_buildings"], home_space_id=sc_n["home_space_id"])
        sched_h = schedule.build_actor_schedule(sc_h["actor"], bp_h, l0_h, sc_h["realm_buildings"], home_space_id=sc_h["home_space_id"])
        self.assertEqual(sched_n["axis"], "game")
        self.assertEqual(sched_h["axis"], "social")
        self.assertNotEqual(sched_n["slots"][3]["preferred_place_id"], sched_h["slots"][3]["preferred_place_id"])

        reloaded.place_actor(nanami, "room_default", 5, 5)
        self.assertNotIn("cognition", next(a for a in reloaded.list_actors() if a["actor_id"] == nanami)["autonomy"])

    def test_schedule_obeys_daw_arbitration_and_rpcs_and_projector(self) -> None:
        nanami = self.engine.import_actor(name="七海千秋")["actor_id"]
        town = self.engine.create_default_town(move_actor_id=nanami)["town_space_id"]
        cafe = f"{town}__cafe"
        home = f"{town}__home"

        # 1. Notice board remains priority over routine priors; unknown portals are not leaked
        self.heartbeat.step(actor_id=nanami, advance_minutes=5, force_autonomy=True, use_llm=False)
        cog = next(a for a in self.engine.list_actors() if a["actor_id"] == nanami)["autonomy"]["cognition"]
        self.assertEqual(cog["active_intention"]["drive"], "notice")

        # 2. Nighttime schedule bonus yields to crisis and pfc_committed language modulation
        now = datetime(2026, 7, 1, 14, 30)
        night = now.replace(hour=23, minute=30)
        self.engine._world["clock_offset_minutes"] = (night - now).total_seconds() / 60.0
        self.engine.inject_language_modulation(
            nanami,
            {
                "id": 99,
                "agenda": "去微风咖啡馆看看",
                "commit_place_id": cafe,
                "commit_drive": "food",
                "commit_reason": "约好去咖啡馆拿东西",
                "priority": "high",
            },
            preempt=True,
        )
        scene = self.engine.cognitive_scene(nanami)
        decision = self.heartbeat._cognitive.step(scene, self.settings, use_llm=False, talk=lambda *args: [])
        self.assertEqual(decision["drive"], "food")
        self.assertEqual(
            next(a for a in self.engine.list_actors() if a["actor_id"] == nanami)["autonomy"]["cognition"]["active_intention"]["target"],
            cafe,
        )

        # 2b. Physiological crisis overrides daytime schedule at 15:00
        self.engine._world["clock_offset_minutes"] = 30.0  # 14:30 + 30m = 15:00
        self.engine.place_actor(nanami, home, 2, 2)
        self.engine.set_actor_world_mood(
            nanami,
            space_id=home,
            vitals_patch={"energy": 0.10, "sleep_pressure": 0.92, "source": "builtin"},
            autonomy_patch={"cognition": {}},
        )
        crisis_scene = self.engine.cognitive_scene(nanami)
        crisis_dec = self.heartbeat._cognitive.step(crisis_scene, self.settings, use_llm=False, talk=lambda *args: [])
        self.assertEqual(crisis_dec["drive"], "rest")
        self.assertEqual(
            next(a for a in self.engine.list_actors() if a["actor_id"] == nanami)["autonomy"]["cognition"]["arbitration"],
            "生理稳态主导",
        )

        # 3. Projector includes weather & schedule context
        self.engine.set_weather(town, "sakura")
        view_txt = projector.project_view(self.engine, nanami)
        self.assertIn("樱花", view_txt)
        self.assertIn("节律与作息", view_txt)

        # 4. RPC end-to-end: set_world_weather & set_actor_home
        register = Mock()
        register_pages(
            register,
            code_dir=WORLD_CODE,
            engine=self.engine,
            settings_store=Mock(get=Mock(return_value=self.settings)),
            heartbeat=self.heartbeat,
        )
        config = next(
            call.args[0] for call in register.register_frontend_config_page.call_args_list
            if call.args[0].page_id == CANVAS_PAGE_ID
        )
        act_weather = next(a.run for a in config.actions if a.id == "set_world_weather")
        act_home = next(a.run for a in config.actions if a.id == "set_actor_home")

        w_res = act_weather({"space_id": town, "mode": "rain"})
        self.assertTrue(w_res["ok"])
        self.assertEqual(w_res["snapshot"]["weather"]["current"], "rain")
        self.assertEqual(w_res["snapshot"]["weather"]["mode"], "rain")

        h_res = act_home({"actor_id": nanami, "space_id": town, "home_space_id": cafe})
        self.assertTrue(h_res["ok"])
        self.assertEqual(h_res["home_space_id"], cafe)
        self.assertEqual(h_res["snapshot"]["active_schedule"]["home_space_id"], cafe)
        self.assertEqual(len(h_res["snapshot"]["active_schedule"]["slots"]), 6)

        # Clear custom home back to auto-resolved home
        h_auto = act_home({"actor_id": nanami, "space_id": town, "home_space_id": ""})
        self.assertEqual(h_auto["home_space_id"], home)


if __name__ == "__main__":
    unittest.main(verbosity=2)
