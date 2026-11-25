"""Tests for LLM-orchestrated map generation, compound realms, dispersed placement, clock isolation, and nocturnal schedules."""

from __future__ import annotations

from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _shinsekai import WORLD_CODE

try:
    from plugins.nanami_story.adapters.persona_adapter import PersonaAdapter
    from plugins.nanami_story.adapters.world_adapter import WorldAdapter
    from plugins.nanami_story.fate_runtime import FateRuntime
    from plugins.nanami_story.settings import StorySettings
    _HAS_STORY = True
except ImportError:
    PersonaAdapter = None  # type: ignore[assignment]
    WorldAdapter = None  # type: ignore[assignment]
    FateRuntime = None  # type: ignore[assignment]
    StorySettings = None  # type: ignore[assignment]
    _HAS_STORY = False
from plugins.nanami_world import map_generator, schedule, schema, text_compiler
from plugins.nanami_world.cognitive_engine import CognitiveEngine
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.external_bridge import WorldBridge
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.town_builder import validate_town_layout


class MapGenerationAndScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.day_ts = datetime(2026, 8, 1, 14, 0).timestamp()
        self.clock = patch("time.time", return_value=self.day_ts)
        self.clock.start()
        self.addCleanup(self.clock.stop)

        self.world_settings = WorldSettings(heartbeat_use_llm=False, auto_compile_llm=False)
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.world_settings, project_root=self.root)
        self.addCleanup(self.heartbeat.stop)
        self.bridge = WorldBridge(self.engine, heartbeat=self.heartbeat)

    def _talk(self, *_args, **_kwargs):
        return []

    def test_map_generator_three_modes_offline_and_mock_llm(self) -> None:
        places = [
            {"location_id": "loc_hall", "name": "公馆前厅", "venue_role": "public"},
            {"location_id": "loc_study", "name": "秘密画室", "venue_role": "library"},
            {"location_id": "loc_tea", "name": "暖灯茶室", "venue_role": "cafe"},
            {"location_id": "loc_lodge", "name": "客房长廊", "venue_role": "home"},
        ]

        # 1. Single furnished architectural room suite (18x13) with beds >= resident_count
        room_res = map_generator.compile_map_plan(
            "带着阳台和画架的公馆套间",
            mode="room",
            resident_count=2,
            places=[{"location_id": "loc_suite", "name": "画师套间", "venue_role": "home"}],
            use_llm=False,
        )
        room_built = map_generator.build_architectural_room(
            self.engine, room_res["map_plan"], room_id="suite_room", switch_active=False,
        )
        room_sp = self.engine._world["spaces"][room_built["space_id"]]
        self.assertGreaterEqual(room_sp["width"], 18)
        self.assertGreaterEqual(room_sp["height"], 13)
        self.assertTrue(room_sp.get("zones"))
        beds_room = [
            e for e in self.engine._world["entities"].values()
            if e["space_id"] == room_built["space_id"] and "restable" in (e.get("traits") or {})
            and e.get("blueprint_id") == "default_bed"
        ]
        self.assertGreaterEqual(len(beds_room), 2)

        # 2. Compound multi-room indoor building complex via Mock LLM
        mock_plan = {
            "theme": "manor",
            "name": "钟楼洋馆",
            "initial_hour": 17.5,
            "hub": {
                "location_id": "loc_hall",
                "name": "洋馆中央厅",
                "role": "public",
                "partition_style": "pillar",
                "furniture_clusters": ["sofa_lounge", "desk_set", "storage_nook"],
            },
            "rooms": [
                {"location_id": "loc_study", "name": "秘密画室", "role": "library", "partition_style": "wing_split"},
                {"location_id": "loc_tea", "name": "暖灯茶室", "role": "cafe", "partition_style": "none"},
                {"location_id": "loc_lodge", "name": "客房长廊", "role": "home", "bed_count": 2, "partition_style": "alcove"},
            ],
        }
        with patch.object(text_compiler, "_ask_llm_json", return_value=mock_plan):
            comp_res = map_generator.compile_map_plan(
                "双层推理洋馆与公馆客房群",
                mode="compound",
                places=places,
                resident_count=5,
                use_llm=True,
            )
        self.assertTrue(comp_res["used_llm"])
        comp_built = map_generator.build_compound_world(
            self.engine, comp_res["map_plan"], hub_id="manor_hub", switch_active=False,
        )
        hub_sp = self.engine._world["spaces"][comp_built["space_id"]]
        self.assertEqual(hub_sp["width"], 22)
        self.assertEqual(hub_sp["height"], 14)
        self.assertEqual(hub_sp["hub_location_id"], "loc_hall")
        val_comp = comp_built["validation"]
        self.assertTrue(val_comp["ok"], val_comp["errors"])
        loc_map = {b.get("location_id"): b["interior_space_id"] for b in hub_sp["buildings"] if b.get("location_id")}
        self.assertEqual(set(loc_map.keys()), {"loc_study", "loc_tea", "loc_lodge"})
        for rsid in comp_built["room_space_ids"]:
            rsp = self.engine._world["spaces"][rsid]
            self.assertGreaterEqual(rsp["width"], 16)
            self.assertGreaterEqual(rsp["height"], 11)
        realm_spaces = {comp_built["space_id"], *comp_built["room_space_ids"]}
        total_comp_beds = sum(
            1 for e in self.engine._world["entities"].values()
            if e["space_id"] in realm_spaces and e.get("blueprint_id") == "default_bed"
        )
        self.assertGreaterEqual(total_comp_beds, 5)

        # 3. Enlarged town (48x36 default) + hub_location_id kept outdoor + beds >= resident_count
        town_plan = map_generator.compile_map_plan(
            "雾夜学院小镇",
            mode="town",
            places=places,
            resident_count=5,
            use_llm=False,
        )["map_plan"]
        town_built = self.engine.generate_town(
            town_plan["town_spec"],
            town_id="town_large",
            initial_hour=town_plan["initial_hour"],
            hub_location_id=town_plan["hub_location_id"],
            switch_active=False,
        )
        tid = town_built["town_space_id"]
        tsp = self.engine._world["spaces"][tid]
        self.assertEqual((tsp["width"], tsp["height"]), (48, 36))
        self.assertEqual(tsp["wall_rows"], 0)
        self.assertEqual(tsp["hub_location_id"], "loc_hall")
        self.assertTrue(town_built["validation"]["ok"], town_built["validation"])
        town_realm_sids = {tid, *town_built["building_space_ids"]}
        total_town_beds = sum(
            1 for e in self.engine._world["entities"].values()
            if e["space_id"] in town_realm_sids and e.get("blueprint_id") == "default_bed"
        )
        self.assertGreaterEqual(total_town_beds, 5)

    def test_town_plan_finishes_when_residents_exceed_bed_capacity(self) -> None:
        plan = map_generator.sanitize_map_plan({}, mode="town", building_count=8, resident_count=40)
        homes = [room for room in plan["rooms"] if room["role"] == "home"]
        self.assertGreaterEqual(len(homes), 2)
        self.assertTrue(all(room["bed_count"] == 4 for room in homes))

    def test_dispersed_placement_and_actor_spawn_avoid_center_and_doors(self) -> None:
        created = self.bridge.dispatch("create_world", {
            "mode": "compound",
            "name": "分散测试馆",
            "prompt_text": "宽敞的双层公馆客厅与走廊",
            "resident_count": 4,
            "use_llm": False,
            "switch_active": False,
        })
        hid = created["space_id"]
        hub = self.engine._world["spaces"][hid]
        cx, cy = hub["width"] // 2, hub["height"] // 2
        doors = [
            (e["x"], e["y"])
            for e in self.engine._world["entities"].values()
            if e["space_id"] == hid and "portal" in (e.get("traits") or {})
        ]
        self.assertGreaterEqual(len(doors), 3)

        strategies = ["spread", "corner", "perimeter", "surface", "corner", "spread"]
        placed_coords: list[tuple[int, int]] = []
        for idx, strat in enumerate(strategies):
            res = self.bridge.dispatch("synthesize_entity", {
                "space_id": hid,
                "prompt_text": f"物证样本{idx + 1}，金属小饰品",
                "custom_name": f"线索物{idx + 1}",
                "collectible": strat in ("corner", "spread"),
                "placement_strategy": strat,
                "traits_hint": {"inscribable": True} if strat == "surface" else {},
                "use_llm": False,
            })
            ent = res["entity"]
            pt = (int(ent["x"]), int(ent["y"]))
            for dx, dy in doors:
                self.assertGreater(max(abs(pt[0] - dx), abs(pt[1] - dy)), 1, f"prop too close to door {(dx, dy)}")
            if int(ent["z"]) == 0:
                self.assertNotIn(pt, placed_coords, "floor props overlapped on same tile")
                placed_coords.append(pt)

        far_from_center = [pt for pt in placed_coords if abs(pt[0] - cx) + abs(pt[1] - cy) > 2]
        self.assertGreaterEqual(len(far_from_center), max(2, len(placed_coords) - 1))

        # Auto-imported actors also spread out instead of stacking at map center
        actor_pts = []
        for idx in range(4):
            ares = self.bridge.dispatch("upsert_actor", {
                "name": f"调查员{idx + 1}",
                "space_id": hid,
                "is_native_npc": True,
                "native_realm_id": hid,
                "ephemeral_realm_id": hid,
            })
            act = ares["actor"]
            actor_pts.append((int(act["x"]), int(act["y"])))
        self.assertEqual(len(set(actor_pts)), 4)

    def test_per_realm_initial_hour_isolation(self) -> None:
        night_ts = datetime(2026, 8, 1, 23, 30).timestamp()
        self.clock.stop()
        night_clock = patch("time.time", return_value=night_ts)
        night_clock.start()
        self.addCleanup(night_clock.stop)

        eng = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "clock_world", project_root=self.root)
        bridge = WorldBridge(eng)
        comp = bridge.dispatch("create_world", {
            "mode": "compound",
            "name": "午后洋馆",
            "prompt_text": "安静的林间洋馆",
            "resident_count": 3,
            "initial_hour": 15.0,
            "use_llm": False,
            "switch_active": False,
        })
        town = bridge.dispatch("create_world", {
            "mode": "town",
            "name": "午后小镇",
            "prompt_text": "春樱和风小镇",
            "resident_count": 3,
            "initial_hour": 15.0,
            "use_llm": False,
            "switch_active": False,
        })

        comp_light = eng.get_lighting(comp["space_id"])
        town_light = eng.get_lighting(town["space_id"])
        default_light = eng.get_lighting("room_default")

        self.assertEqual(comp_light["period"], "day")
        self.assertGreaterEqual(float(comp_light["hour_float"]), 14.5)
        self.assertLessEqual(float(comp_light["hour_float"]), 15.5)
        self.assertEqual(town_light["period"], "day")
        self.assertEqual(default_light["period"], "night")

    def test_schedule_a_night_rest_nocturnal_purpose_and_anti_freeze(self) -> None:
        night_ts = datetime(2026, 8, 1, 23, 30).timestamp()
        self.clock.stop()
        night_clock = patch("time.time", return_value=night_ts)
        night_clock.start()
        self.addCleanup(night_clock.stop)

        eng = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "night_world", project_root=self.root)
        hb = WorldHeartbeat(eng, lambda: self.world_settings, project_root=self.root)
        self.addCleanup(hb.stop)
        bridge = WorldBridge(eng, heartbeat=hb)

        comp = bridge.dispatch("create_world", {
            "mode": "compound",
            "name": "夜色宿馆",
            "prompt_text": "带有两间大客房与书房的推理洋馆",
            "resident_count": 4,
            "initial_hour": 23.5,
            "opaque_minds": True,
            "isolated_persona": True,
            "use_llm": False,
            "switch_active": False,
        })
        hid = comp["space_id"]
        cog = CognitiveEngine(eng, project_root=self.root)

        actor_ids: list[str] = []
        for idx, name in enumerate(("住客甲", "住客乙", "住客丙", "暗夜行动者")):
            res = bridge.dispatch("upsert_actor", {
                "name": name,
                "space_id": hid,
                "is_native_npc": True,
                "native_realm_id": hid,
                "ephemeral_realm_id": hid,
            })
            aid = res["actor_id"]
            actor_ids.append(aid)
            bridge.dispatch("set_private_brief", {
                "actor_id": aid,
                "space_id": hid,
                "brief": "在洋馆内休息并留意异常。" if idx < 3 else "趁深夜众人入睡后暗中行动。",
                "cover_goal": "散步" if idx < 3 else "核对走廊灯火",
                "night_active": idx == 3,
            })

        snap = eng.snapshot_for_ui(hid)
        home_by_id = {a["actor_id"]: a.get("resolved_home_space_id") for a in snap["actors"]}
        for aid in actor_ids:
            self.assertTrue(home_by_id[aid], f"actor {aid} missing home_space_id")
            self.assertIn(home_by_id[aid], comp["room_space_ids"])

        # Ordinary resident in deep night with isolated_persona + external_private_brief must NEVER freeze
        norm_aid = actor_ids[0]
        home_sp = home_by_id[norm_aid]
        door = next(b for b in comp["buildings"] if b["interior_space_id"] == home_sp)
        eng.update_actor_autonomy(norm_aid, {
            "cognition": {
                "portals": {
                    f"{hid}:{home_sp}": {
                        "space": hid, "target": home_sp, "name": door["name"],
                        "x": door["door_x"], "y": door["door_y"], "source": "signboard", "confidence": 0.9, "samples": [0.0],
                    }
                }
            }
        })
        norm_step = cog.step(eng.cognitive_scene(norm_aid), self.world_settings, use_llm=False, talk=self._talk)
        self.assertTrue(norm_step["goal"], norm_step)
        norm_cognition = (eng._world["actors"][norm_aid]["autonomy"].get("cognition") or {})
        norm_intent = norm_cognition.get("active_intention") or {}
        self.assertTrue(norm_step["actions"] or norm_intent.get("target") == home_sp, (norm_step, norm_intent))

        # Bare room with zero beds at 23:30 + isolated_persona + private_brief also must NOT freeze
        bare = bridge.dispatch("create_world", {
            "mode": "room",
            "name": "空室测试",
            "bare_room": True,
            "isolated_persona": True,
            "use_llm": False,
            "switch_active": False,
        })
        bare_aid = bridge.dispatch("upsert_actor", {
            "name": "空室测试员",
            "space_id": bare["space_id"],
            "is_native_npc": True,
            "native_realm_id": bare["space_id"],
            "ephemeral_realm_id": bare["space_id"],
        })["actor_id"]
        bridge.dispatch("set_private_brief", {"actor_id": bare_aid, "space_id": bare["space_id"], "brief": "等待调查。"})
        eng.place_from_blueprint("default_desk", bare["space_id"], 2, 2)
        eng.place_from_blueprint("default_notebook", bare["space_id"], 2, 2, note_text="桌面留条")
        bare_step = cog.step(eng.cognitive_scene(bare_aid), self.world_settings, use_llm=False, talk=self._talk)
        self.assertTrue(bare_step["goal"], bare_step)

        # Nocturnal actor overrides night_rest and wakes from bed when covert_directive / night_active
        noct_aid = actor_ids[3]
        noct_scene = eng.cognitive_scene(noct_aid)
        self.assertTrue(noct_scene["nocturnal_active"])
        sched = schedule.build_actor_schedule(noct_scene["actor"], None, None, noct_scene["realm_buildings"], home_space_id=noct_scene["home_space_id"])
        ctx_noct = schedule.active_schedule_context(sched, 23.5, circadian_strength=0.8, effortful_control=0.7, nocturnal_active=True)
        self.assertEqual(ctx_noct["effective_scale"], 0.0)

        # Put nocturnal actor to sleep on a bed with healthy energy, then issue covert directive -> must wake & act
        bed_eid = next(
            e["entity_id"] for e in eng._world["entities"].values()
            if e["space_id"] == home_by_id[noct_aid] and e.get("blueprint_id") == "default_bed"
        )
        bed_ent = eng._world["entities"][bed_eid]
        eng.place_actor(noct_aid, home_by_id[noct_aid], bed_ent["x"], bed_ent["y"])
        eng.cognitive_act(noct_aid, home_by_id[noct_aid], bed_eid, "rest")
        self.assertEqual(eng.live_actor_frame(noct_aid)["posture"], "resting")
        eng.set_actor_vitals(noct_aid, {"energy": 0.85, "sleep_pressure": 0.15})

        target_box = eng.place_ephemeral_entity(hid, {
            "name": "秘密笔记本",
            "category": "object",
            "brief": "摊开的笔记",
            "detail": "记录了关键时间",
            "placement": "floor",
            "solid": False,
            "footprint": [1, 1],
            "traits": {"inscribable": {"entries": [], "writable_by_ai": True, "max_entries": 6}},
        })
        eng.set_covert_directive(noct_aid, {
            "directive_id": "cd_night_mission",
            "kind": "entity",
            "target": target_box["entity_id"],
            "action": "write",
            "note_text": "深夜篡改痕迹",
            "require_privacy": False,
            "cover_goal": "翻看笔记",
        }, space_id=hid)

        res = cog.step(eng.cognitive_scene(noct_aid), self.world_settings, use_llm=False, talk=self._talk)
        self.assertNotEqual(eng.live_actor_frame(noct_aid)["posture"], "resting", f"nocturnal actor stayed asleep: {res}")

    @unittest.skipUnless(_HAS_STORY, "nanami_story plugin not installed")
    def test_nanami_story_compound_and_town_no_premature_climax(self) -> None:
        night_ts = datetime(2026, 8, 1, 23, 40).timestamp()
        self.clock.stop()
        night_clock = patch("time.time", return_value=night_ts)
        night_clock.start()
        self.addCleanup(night_clock.stop)

        eng = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "story_world", project_root=self.root)
        hb = WorldHeartbeat(eng, lambda: self.world_settings, project_root=self.root)
        self.addCleanup(hb.stop)
        bridge = WorldBridge(eng, heartbeat=hb)
        runtime = FateRuntime(
            WorldAdapter(bridge=bridge),
            PersonaAdapter(self.root),
            self.root / "story_data",
            settings_provider=lambda: StorySettings(enabled=True, use_llm_compiler=False),
        )
        self.addCleanup(runtime.close)

        for w_mode in ("compound", "town"):
            with self.subTest(world_mode=w_mode):
                session = runtime.start_standalone_stage({
                    "title": f"{w_mode}测试舞台",
                    "world_mode": w_mode,
                    "experience_mode": "observer",
                    "native_npc_count": 3,
                    "building_count": 4,
                    "switch_active": False,
                    "use_llm": False,
                })
                self.addCleanup(runtime.detach_session, session["session_id"], delete_world=True)

                realm = session["realm_space_id"]
                ws = bridge.dispatch("query_world_state", {"space_id": realm})
                realm_sp = next(s for s in ws["spaces"] if s["space_id"] == realm)
                if w_mode == "compound":
                    self.assertEqual(realm_sp["space_kind"], "room")
                    self.assertGreaterEqual(len(realm_sp.get("buildings") or []), 3)
                else:
                    self.assertEqual(realm_sp["space_kind"], "town")
                    self.assertEqual((realm_sp["width"], realm_sp["height"]), (48, 36))

                # Story starts in afternoon daylight (isolated from 23:40 host wall clock)
                light = eng.get_lighting(realm)
                self.assertEqual(light["period"], "day")

                # Fate Weaver opening prop is inside an interior room/floor, NEVER dumped on center plaza
                deployed_ids = set(session["threads"][0]["nodes"][0]["deployed_entity_ids"])
                prop_ents = [e for e in ws["realm_entities"] if e["entity_id"] in deployed_ids]
                self.assertTrue(prop_ents)
                self.assertTrue(all(e["space_id"] != realm for e in prop_ents), f"prop leaked to center plaza: {prop_ents}")

                # Residents start distributed across building spaces, not piled onto outdoor center
                realm_sids = {s["space_id"] for s in ws["spaces"] if s.get("realm_space_id") == realm}
                non_player_actors = [a for a in ws["actors"] if not a.get("is_player") and a["space_id"] in realm_sids]
                self.assertEqual(len(non_player_actors), 3)
                self.assertTrue(all(a["space_id"] != realm for a in non_player_actors))
                self.assertGreaterEqual(len({a["space_id"] for a in non_player_actors}), 2)

                # Startup tick must keep Fate Weaver thread active, NOT prematurely resolve
                ticked = runtime.tick_session(session["session_id"], use_llm=False)
                self.assertEqual(ticked["status"], "active")
                self.assertEqual(ticked["threads"][0]["status"], "active")

    def test_holodeck_interior_distinct_themes_and_multitile_furniture(self) -> None:
        prompts = [
            "秘密炼金实验室",
            "古典钢琴音乐沙龙",
            "冬雪温泉茶室",
            "复古钟楼齿轮机房",
        ]
        theme_signatures: list[frozenset[str]] = []
        rug_layouts: set[frozenset[tuple[str, str]]] = set()
        for idx, ptxt in enumerate(prompts):
            res = map_generator.compile_map_plan(ptxt, mode="room", width=16, height=12, use_llm=False)
            built = map_generator.build_architectural_room(
                self.engine, res["map_plan"], room_id=f"holo_room_{idx}", switch_active=False,
            )
            sp = self.engine._world["spaces"][built["space_id"]]
            ents = [e for e in self.engine._world["entities"].values() if e["space_id"] == built["space_id"]]
            ephem_multitile = [
                e for e in ents
                if e.get("ephemeral_realm_id") and max(e.get("footprint") or [1, 1]) >= 2
            ]
            self.assertTrue(ephem_multitile, f"{ptxt} should generate >=1 ephemeral multi-tile furniture")
            theme_signatures.append(frozenset(e["name"] for e in ephem_multitile))
            tiles = sp.get("tiles") or {}
            self.assertTrue(tiles, f"{ptxt} should paint tiles onto space['tiles']")
            rug_layouts.add(frozenset(
                (coord, tid) for coord, tid in tiles.items()
                if tid in ("tatami", "carpet_blue", "carpet_pink", "stone_tile", "dark_wood")
            ))

        self.assertEqual(len(set(theme_signatures)), 4, f"Expected 4 distinct furniture sets, got {theme_signatures}")
        self.assertGreaterEqual(len(rug_layouts), 2)

        # Single-batch compile_scene_graphs_batch integration with compile_map_plan(..., use_llm=True)
        base_mock = {
            "theme": "manor",
            "name": "钟楼机房",
            "hub": {"location_id": "loc_clock", "name": "钟楼机房", "role": "public"},
            "rooms": [],
        }
        batch_mock = {
            "loc_clock": {
                "partition_style": "corner_nook",
                "rug_style": "runner_h",
                "custom_items": [
                    {
                        "ref": "master_console",
                        "name": "主钟校准控制台",
                        "brief": "闪着黄铜微光的精密齿轮台",
                        "motif": "gear_console",
                        "footprint": [2, 1],
                        "relation": "centerpiece",
                    }
                ],
            }
        }
        with patch.object(text_compiler, "_ask_llm_json", side_effect=[base_mock, batch_mock]):
            comp = map_generator.compile_map_plan(
                "赛博钟楼密室",
                mode="room",
                places=[{"location_id": "loc_clock", "name": "钟楼机房", "venue_role": "public"}],
                use_llm=True,
            )
        self.assertTrue(comp["used_llm"])
        built_llm = map_generator.build_architectural_room(
            self.engine, comp["map_plan"], room_id="holo_batch_llm", switch_active=False,
        )
        placed_console = next(
            (e for e in self.engine._world["entities"].values()
             if e["space_id"] == built_llm["space_id"] and e["name"] == "主钟校准控制台"),
            None,
        )
        self.assertIsNotNone(placed_console)
        self.assertEqual(placed_console["footprint"], [2, 1])

    def test_multihop_compound_graph_navigation_and_story_mapping(self) -> None:
        mock_plan = {
            "theme": "manor",
            "name": "多层推理洋馆",
            "initial_hour": 15.0,
            "hub": {"location_id": "loc_hall", "name": "公馆主厅", "role": "public"},
            "rooms": [
                {"location_id": "loc_study", "name": "档案书房", "role": "library", "connect_to": ["hub"]},
                {"location_id": "loc_secret", "name": "隐藏密室", "role": "library", "connect_to": ["档案书房"]},
                {"location_id": "loc_tea", "name": "暖灯茶歇室", "role": "cafe", "connect_to": ["hub"]},
                {"location_id": "loc_lodge", "name": "东翼宿屋", "role": "home", "bed_count": 2, "connect_to": ["hub"]},
            ],
        }
        places = [
            {"location_id": "loc_hall", "name": "公馆主厅", "venue_role": "public"},
            {"location_id": "loc_study", "name": "档案书房", "venue_role": "library"},
            {"location_id": "loc_secret", "name": "隐藏密室", "venue_role": "library"},
            {"location_id": "loc_tea", "name": "暖灯茶歇室", "venue_role": "cafe"},
            {"location_id": "loc_lodge", "name": "东翼宿屋", "venue_role": "home"},
        ]
        plan_res = map_generator.sanitize_map_plan(
            mock_plan, mode="compound", places=places, resident_count=2,
        )
        built = map_generator.build_compound_world(self.engine, plan_res, hub_id="multihop_hub", switch_active=True)
        hub_id = built["space_id"]
        val = built["validation"]
        self.assertTrue(val["ok"], val["errors"])
        self.assertGreaterEqual(val["hops_max"], 2)

        all_bldgs = self.engine.list_realm_buildings(hub_id)
        study_b = next(b for b in all_bldgs if b.get("location_id") == "loc_study")
        secret_b = next(b for b in all_bldgs if b.get("location_id") == "loc_secret")
        study_sid = study_b["interior_space_id"]
        secret_sid = secret_b["interior_space_id"]

        with self.engine._lock:
            realm_portals = self.engine._realm_portals_locked(hub_id)
        self.assertNotIn(f"{hub_id}:{secret_sid}", realm_portals)
        self.assertIn(f"{hub_id}:{study_sid}", realm_portals)
        self.assertIn(f"{study_sid}:{secret_sid}", realm_portals)

        aid = self.engine.import_actor(name="侦探助手")["actor_id"]
        with self.engine._lock:
            sx, sy = self.engine._find_open_floor_cell_locked(self.engine._world["spaces"][hub_id])
        self.engine.place_actor(aid, hub_id, sx, sy)

        self.assertTrue(
            self.engine.inject_language_modulation(
                aid, {"commit_place_id": secret_sid, "commit_drive": "explore", "agenda": "前往隐藏密室"}, preempt=True,
            )
        )
        seq = [hub_id]
        for _ in range(50):
            self.heartbeat.step(actor_id=aid, advance_minutes=5, use_llm=False)
            cur = self.engine._world["actors"][aid]["space_id"]
            if cur != seq[-1]:
                seq.append(cur)
            if cur == secret_sid:
                break
        self.assertEqual(self.engine._world["actors"][aid]["space_id"], secret_sid, f"Failed to reach 2-hop secret room; seq={seq}")
        self.assertEqual(seq, [hub_id, study_sid, secret_sid], f"Expected clean 2-hop traversal without bouncing; got {seq}")

        self.assertTrue(
            self.engine.inject_language_modulation(
                aid, {"commit_place_id": hub_id, "commit_drive": "social", "agenda": "返回主厅"}, preempt=True,
            )
        )
        ret_seq = [secret_sid]
        for _ in range(50):
            self.heartbeat.step(actor_id=aid, advance_minutes=5, use_llm=False)
            cur = self.engine._world["actors"][aid]["space_id"]
            if cur != ret_seq[-1]:
                ret_seq.append(cur)
            if cur == hub_id:
                break
        self.assertEqual(self.engine._world["actors"][aid]["space_id"], hub_id, f"Failed to return to hub; ret_seq={ret_seq}")
        self.assertEqual(ret_seq, [secret_sid, study_sid, hub_id])

        # Verify WorldBridge realm_buildings + FateRuntime handles 2-hop compound room cleanly
        ws = self.bridge.dispatch("query_world_state", {"space_id": hub_id})
        self.assertTrue(any(b.get("interior_space_id") == secret_sid for b in ws.get("realm_buildings") or []))
        if _HAS_STORY:
            runtime = FateRuntime(
                WorldAdapter(bridge=self.bridge),
                PersonaAdapter(self.root),
                self.root / "story_map_test",
                settings_provider=lambda: StorySettings(enabled=True, use_llm_compiler=False),
            )
            self.addCleanup(runtime.close)
            sess = runtime.attach_world(hub_id, auto_pluck=True, use_llm=False)
            self.addCleanup(runtime.detach_session, sess["session_id"], delete_world=False)
            self.assertNotEqual(sess["threads"][0]["nodes"][0]["target_space_id"], hub_id)

    def test_non_crossroads_town_skeletons_and_variable_footprints(self) -> None:
        styles = ("ring_plaza", "river_bridges", "coastal_pier", "winding_avenue", "courtyard_manor")
        signatures: set[frozenset[tuple[str, str]]] = set()
        for style in styles:
            with self.subTest(layout_style=style):
                spec = text_compiler._sanitize_town_spec(
                    {"layout_style": style, "name": f"{style}町"},
                    width=48,
                    height=36,
                    building_count=6,
                    prompt_text=f"特色风景{style}像素小镇",
                )
                self.assertEqual(spec["layout_style"], style)
                gen = self.engine.generate_town(spec, town_id=f"town_{style}", switch_active=False)
                self.assertEqual(gen["layout_style"], style)
                self.assertTrue(gen["validation"]["ok"], gen["validation"]["errors"])
                self.assertEqual(gen["validation"]["reachable_doors"], 6)

                sp = self.engine._world["spaces"][gen["town_space_id"]]
                dims = {(int(b["w"]), int(b["h"])) for b in sp.get("buildings") or ()}
                self.assertGreaterEqual(len(dims), 2, f"{style} should place varied building footprints, got {dims}")
                self.assertTrue(any(int(b["y"]) >= 3 for b in sp.get("buildings") or ()), f"{style} should place street-front buildings away from top border")

                sig = frozenset(
                    (k, tid) for k, tid in (sp.get("tiles") or {}).items()
                    if tid in (spec["road_tile"], "water", "sand", "wood_floor")
                )
                signatures.add(sig)
        self.assertEqual(len(signatures), 5, "All 5 non-crossroads layouts should produce distinct spatial skeletons")

        # Verify LLM "crossroads" + same ground/plaza tile is overridden for non-crossroads prompts
        sanitized_neon = text_compiler._sanitize_town_spec(
            {
                "layout_style": "crossroads",
                "theme": "neon_market",
                "ground_tile": "stone_tile",
                "plaza_tile": "stone_tile",
                "road_tile": "stone_tile",
            },
            width=40,
            height=30,
            building_count=6,
            prompt_text="深夜商业区，有便利店和咖啡馆",
        )
        self.assertNotEqual(sanitized_neon["layout_style"], "crossroads")
        self.assertNotEqual(sanitized_neon["plaza_tile"], sanitized_neon["ground_tile"])
        self.assertNotEqual(sanitized_neon["road_tile"], sanitized_neon["ground_tile"])
        explicit_cross = text_compiler._sanitize_town_spec(
            {"layout_style": "crossroads"},
            width=40,
            height=30,
            building_count=6,
            prompt_text="经典十字路口小镇",
        )
        self.assertEqual(explicit_cross["layout_style"], "crossroads")

    def test_25d_elevation_cliffs_stairs_and_outdoor_parcels(self) -> None:
        spec = text_compiler._sanitize_town_spec(
            {"layout_style": "winding_avenue", "theme": "sakura", "name": "高台樱坂町", "seed": 20261124},
            width=48,
            height=36,
            building_count=6,
            prompt_text="依山而建的错落樱花坂道小镇",
        )
        self.assertEqual(spec["seed"], 20261124)
        gen = self.engine.generate_town(spec, town_id="town_25d", switch_active=True)
        self.assertTrue(gen["validation"]["ok"], gen["validation"]["errors"])

        tid = gen["town_space_id"]
        tsp = self.engine._world["spaces"][tid]
        elevs = tsp.get("elevations") or {}
        stairs = tsp.get("stairs") or {}
        self.assertTrue(elevs, "Generated town must have 2.5D elevated cells")
        self.assertTrue(stairs, "Generated town must carve stairs across elevation cliffs")
        self.assertIn(1, set(elevs.values()))
        self.assertIn(2, set(elevs.values()))

        # Find a non-stair cliff edge and prove direct step is blocked while BFS routes via stairs
        tw, th = int(tsp["width"]), int(tsp["height"])
        cliff_pair: tuple[tuple[int, int], tuple[int, int]] | None = None
        for y in range(2, th - 3):
            for x in range(2, tw - 3):
                if (
                    not self.engine.is_wall_cell(tsp, x, y)
                    and not self.engine.is_wall_cell(tsp, x, y + 1)
                    and self.engine.get_cell_tile(tsp, x, y).get("walkable", True)
                    and self.engine.get_cell_tile(tsp, x, y + 1).get("walkable", True)
                    and self.engine.get_cell_elevation(tsp, x, y) != self.engine.get_cell_elevation(tsp, x, y + 1)
                    and not self.engine.can_step_elevation(tsp, x, y, x, y + 1)
                ):
                    with self.engine._lock:
                        if (
                            self.engine._is_floor_walkable_locked(tsp, x, y)
                            and self.engine._is_floor_walkable_locked(tsp, x, y + 1)
                        ):
                            cliff_pair = ((x, y), (x, y + 1))
                            break
            if cliff_pair:
                break
        self.assertIsNotNone(cliff_pair, "Should find at least one impassable cliff boundary")
        assert cliff_pair is not None
        (cx0, cy0), (cx1, cy1) = cliff_pair

        player = self.engine.ensure_player_avatar(space_id=tid, name="漫游测试员")
        pid = player["actor_id"]
        self.engine.place_actor(pid, tid, cx0, cy0)
        move_blocked = self.engine.step_player_move(pid, 0, 1, space_id=tid, include_prompt=False)
        self.assertFalse(move_blocked["moved"], "Player WASD step must NOT walk across an unstaired cliff")

        with self.engine._lock:
            detour_path = self.engine._bfs_locked(tid, (cx0, cy0), {(cx1, cy1)}, ignore_actor_id=pid)
        self.assertIsNotNone(detour_path, "BFS must still connect upper and lower terraces via stairs")
        assert detour_path is not None
        self.assertGreater(len(detour_path), 2)

        # Multi-tile placement straddling a cliff or blocking a stair must be rejected
        stair_xy = next(iter(stairs.keys())).split(",")
        sx, sy = int(stair_xy[0]), int(stair_xy[1])
        with self.assertRaises(ValueError):
            self.engine.place_from_blueprint("default_box", tid, sx, sy, delivery_mode="silent")

        # Outdoor parcel subdivision: trees, fences/walls, and distributed street lamps
        town_ents = [e for e in self.engine._world["entities"].values() if e["space_id"] == tid and int(e.get("z", 0)) >= 0]
        trees = [e for e in town_ents if "tree" in (e.get("tags") or ())]
        fences = [e for e in town_ents if "fence" in (e.get("tags") or ())]
        lights = [
            e for e in town_ents
            if e.get("blueprint_id") == "default_street_lamp" or "light" in (e.get("tags") or ())
        ]
        self.assertGreaterEqual(len(trees), 2, "Town should populate outdoor trees in pocket parks")
        self.assertGreaterEqual(len(fences), 3, "Town should subdivide building courtyards with fences/low walls")
        self.assertGreaterEqual(len(lights), 4, "Town should distribute street lights along roads")

        # Two unseeded generations must produce genuinely varied layouts
        unseeded_a = self.engine.generate_town(
            text_compiler._sanitize_town_spec({"name": "随机町A"}, width=44, height=34, building_count=5, prompt_text="山间小镇"),
            town_id="town_rand_a",
            switch_active=False,
        )
        unseeded_b = self.engine.generate_town(
            text_compiler._sanitize_town_spec({"name": "随机町B"}, width=44, height=34, building_count=5, prompt_text="山间小镇"),
            town_id="town_rand_b",
            switch_active=False,
        )
        sp_a = self.engine._world["spaces"][unseeded_a["town_space_id"]]
        sp_b = self.engine._world["spaces"][unseeded_b["town_space_id"]]
        bldg_coords_a = [(b["x"], b["y"], b["w"], b["h"]) for b in sp_a["buildings"]]
        bldg_coords_b = [(b["x"], b["y"], b["w"], b["h"]) for b in sp_b["buildings"]]
        self.assertTrue(
            sp_a.get("elevations") != sp_b.get("elevations") or bldg_coords_a != bldg_coords_b,
            "Unseeded town generations should vary terrain relief and/or building lots",
        )

    def test_activity_layouts_keep_work_positions_and_a_player_route(self) -> None:
        npcs = [self.engine.import_actor(name=n)["actor_id"] for n in ("甲", "乙")]
        for seed in (3, 14, 29, 48):
            plan = map_generator.sanitize_map_plan(
                {"rooms": [{"name": "街角咖啡馆", "role": "cafe"},
                           {"name": "河岸书店", "role": "library"},
                           {"name": "双人客房", "role": "home"}]},
                mode="compound", prompt_text="沿河旅馆", resident_count=2,
            )
            plan["seed"] = seed
            built = map_generator.build_compound_world(self.engine, plan, hub_id=f"activity_{seed}", switch_active=False)
            self.assertTrue(built["validation"]["ok"], built["validation"]["errors"])
            checked = set()
            for sid in built["room_space_ids"]:
                space = self.engine._world["spaces"][sid]
                name = space["name"]
                if name not in ("街角咖啡馆", "河岸书店", "双人客房"):
                    continue
                checked.add(name)
                with self.subTest(seed=seed, room=name):
                    w, h = space["width"], space["height"]
                    entities = [e for e in self.engine._world["entities"].values() if e["space_id"] == sid]
                    by_name = {e["name"]: e for e in entities}
                    door = next(e for e in entities if "portal" in e["traits"] and e["y"] == h - 1)
                    start = (door["x"], h - 2)
                    if name == "街角咖啡馆":
                        self.assertGreater(w, h)
                        self.assertNotEqual(start[0], w // 2)
                        bar, machine = by_name["原木点单吧台"], by_name["意式咖啡机"]
                        self.assertIn((machine["x"], machine["y"]), self.engine._entity_cells(bar))
                        self.assertEqual(machine["z"], 1)
                        sofa, desk = by_name["靠窗卡座沙发"], by_name["双人橡木圆方桌"]
                        self.assertGreater(abs(sofa["x"] - bar["x"]), 3)
                        self.assertLessEqual(abs(sofa["x"] - desk["x"]), 2)
                        self.assertEqual(space["tiles"][f"{sofa['x']},0"], "glass_wall")
                        stations = [(machine["x"], machine["y"] - 1), (machine["x"], machine["y"] + 1)]
                    elif name == "河岸书店":
                        self.assertLess(w, h)
                        shelf, other = by_name["编目高书架"], by_name["分类选书架"]
                        self.assertEqual(shelf["x"], other["x"])
                        self.assertGreaterEqual(other["y"] - shelf["y"], 2)
                        self.assertGreater(by_name["新书展台"]["y"], other["y"])
                        self.assertLess(by_name["窗边阅读座"]["y"], shelf["y"])
                        stations = [(e["x"], e["y"] + 1) for e in (shelf, other)]
                    else:
                        beds = [e for e in entities if e["blueprint_id"] == "default_bed"]
                        self.assertEqual(len(beds), 2)
                        self.assertGreater(abs(beds[0]["x"] - beds[1]["x"]), 2)
                        self.assertTrue(self.engine.is_wall_cell(space, start[0], h - 4))
                        stations = [(e["x"], e["y"] + e["footprint"][1]) for e in beds]
                    player = self.engine.ensure_player_avatar(space_id=sid)["actor_id"]
                    self.engine.place_actor(player, sid, *start)
                    for npc, stand in zip(npcs, stations):
                        self.assertTrue(self.engine._bfs_locked(sid, start, {stand}, ignore_actor_id=player))
                        self.engine.place_actor(npc, sid, *stand)
                    path = self.engine._bfs_locked(sid, start, {(w // 2, 1)}, ignore_actor_id=player)
                    self.assertTrue(path)
                    for (x, y), (nx, ny) in zip(path, path[1:]):
                        step = self.engine.step_player_move(player, nx - x, ny - y, include_prompt=False)
                        self.assertTrue(step["moved"])
            self.assertEqual(len(checked), 3)

    def test_standalone_activity_defaults_and_explicit_size(self) -> None:
        for prompt, dims in (("街角咖啡馆", (20, 11)), ("河岸书店", (13, 16)), ("双人客房", (18, 13))):
            plan = map_generator.compile_map_plan(prompt, mode="room", use_llm=False)["map_plan"]
            self.assertEqual((plan["width"], plan["height"]), dims)
            built = map_generator.build_architectural_room(self.engine, plan, switch_active=False)
            sid = built["space_id"]
            beds = [e for e in self.engine._world["entities"].values()
                    if e["space_id"] == sid and e["blueprint_id"] == "default_bed"]
            self.assertEqual(len(beds), 2 if prompt == "双人客房" else 0)
        plan = map_generator.compile_map_plan("河岸书店", mode="room", width=17, height=12, use_llm=False)["map_plan"]
        self.assertEqual((plan["width"], plan["height"]), (17, 12))
        rooms = [{"name": "河岸书店", "role": "library", "interior_width": 17, "interior_height": 14},
                 {"name": "双人客房", "role": "home"}, {"name": "街角咖啡馆", "role": "cafe"}]
        for mode in ("town", "compound"):
            plan = map_generator.sanitize_map_plan({"rooms": rooms}, mode=mode)
            bookshop = next(r for r in plan["rooms"] if r["name"] == "河岸书店")
            self.assertEqual((bookshop["interior_width"], bookshop["interior_height"]), (17, 14))

    def test_snow_onsen_venue_furniture_and_clear_fireplace_lounge(self) -> None:
        cases = {
            "松风书斋": ("library", {"编目高书架", "地方志书架", "文集书架", "窗边阅读座", "抄读写字台"}),
            "热茶拉面屋": ("cafe", {"拉面出餐台", "豚骨拉面碗", "备餐案台", "窗边食桌", "双人食桌"}),
            "汤宿温泉旅馆": ("home", {"桧木温汤池", "浴衣收纳藤柜", "松竹折屏", "茶席坐垫", "汤后歇脚茶席"}),
            "暖炉区": ("public", {"石砌暖炉", "炉边长座", "炉边矮桌", "茶水台"}),
        }
        rooms = [{"name": name, "role": role} for name, (role, _) in cases.items()]
        for seed in (5, 24):
            plan = map_generator.sanitize_map_plan(
                {"name": "细雪温泉町", "theme": "snow_onsen", "rooms": rooms},
                mode="town", prompt_text="细雪温泉町", building_count=4,
            )
            plan["town_spec"]["seed"] = seed
            built = self.engine.generate_town(plan["town_spec"], town_id=f"snow_interiors_{seed}", switch_active=False)
            self.assertTrue(built["validation"]["ok"], built["validation"]["errors"])
            self.assertEqual({b["name"] for b in built["buildings"]}, cases.keys())
            for building in built["buildings"]:
                name = building["name"]
                with self.subTest(seed=seed, room=name):
                    sid = building["interior_space_id"]
                    space = self.engine._world["spaces"][sid]
                    ents = {e["name"]: e for e in self.engine._world["entities"].values() if e["space_id"] == sid}
                    self.assertTrue(cases[name][1] <= ents.keys(), ents.keys())
                    self.assertNotIn("星海游戏海报", ents)
                    if name == "热茶拉面屋":
                        bowl = ents["豚骨拉面碗"]
                        self.assertEqual(bowl["z"], 1)
                        self.assertIn((bowl["x"], bowl["y"]), self.engine._entity_cells(ents["拉面出餐台"]))
                    elif name == "暖炉区":
                        self.assertFalse(any(
                            self.engine.is_wall_cell(space, x, y)
                            for y in range(1, space["height"] - 1) for x in range(space["width"])
                        ))

    def test_wall_hang_requires_clear_floor_directly_in_front(self) -> None:
        from plugins.nanami_world import interior_designer

        sid = self.engine.create_space(name="带切角的厅", width=10, height=8, wall_rows=1, switch_active=False)["space_id"]
        self.engine.paint_tiles(sid, [
            {"x": x, "y": 1, "tile_id": "wood_wall"}
            for x in range(10) if x not in (3, 4, 5)
        ])
        self.engine.place_from_blueprint("default_bookshelf", sid, 3, 1, delivery_mode="silent")
        result = interior_designer.solve_and_place_scene_graph(
            self.engine, sid, {},
            {"items": [{"ref": "poster", "blueprint_id": "default_window_poster", "relation": "wall_hang"}]},
            seed=5,
        )
        self.assertEqual(result["placed_count"], 1)
        poster = self.engine._world["entities"][result["entities"][0]]
        self.assertEqual((poster["x"], poster["y"]), (5, 0))

    def test_indoor_role_priority_landmark_dedup_and_irregular_rooms(self) -> None:
        # 1. Onsen town: at most 1 venue gets hot_spring_tub; bookstore/shop/cafe stay role-accurate
        onsen_plan = map_generator.compile_map_plan(
            "冬雪汤宿小镇",
            mode="town",
            places=[
                {"location_id": "loc_sq", "name": "汤烟广场", "venue_role": "public"},
                {"location_id": "loc_bath", "name": "白雾温泉本馆", "venue_role": "home"},
                {"location_id": "loc_book", "name": "山麓古书店", "venue_role": "library"},
                {"location_id": "loc_shop", "name": "坂下杂货铺", "venue_role": "shop"},
                {"location_id": "loc_cafe", "name": "暖炉咖啡屋", "venue_role": "cafe"},
                {"location_id": "loc_home2", "name": "木立宿屋", "venue_role": "home"},
            ],
            resident_count=4,
            use_llm=False,
        )["map_plan"]
        onsen_built = self.engine.generate_town(
            onsen_plan["town_spec"],
            town_id="town_onsen_dedup",
            hub_location_id=onsen_plan["hub_location_id"],
            switch_active=False,
        )
        self.assertTrue(onsen_built["validation"]["ok"], onsen_built["validation"]["errors"])
        tub_spaces = [
            sid
            for sid in onsen_built["building_space_ids"]
            if any(
                "汤池" in str(e.get("name") or "")
                for e in self.engine._world["entities"].values()
                if e["space_id"] == sid
            )
        ]
        self.assertLessEqual(len(tub_spaces), 1, f"Hot spring tub must appear in <=1 building, got {tub_spaces}")
        book_bldg = next(b for b in onsen_built["buildings"] if "书店" in b["name"])
        book_ents = [e["name"] for e in self.engine._world["entities"].values() if e["space_id"] == book_bldg["interior_space_id"]]
        self.assertFalse(any("汤池" in n for n in book_ents), f"Bookstore leaked hot spring tub: {book_ents}")
        cafe = next(b for b in onsen_built["buildings"] if "咖啡" in b["name"])
        sid = cafe["interior_space_id"]
        space = self.engine._world["spaces"][sid]
        entities = self.engine._world["entities"].values()
        entry = next(e["traits"]["portal"] for e in entities if e["traits"].get("portal", {}).get("target_space_id") == sid)
        exit_door = next(e for e in entities if e["space_id"] == sid and "portal" in e["traits"])
        self.assertNotEqual(exit_door["x"], space["width"] // 2)
        self.assertEqual((entry["target_x"], entry["target_y"]), (exit_door["x"], exit_door["y"] - 1))

        # 2. Starry / midnight_handheld town: ordinary buildings must NOT mass-spawn altar_pedestal (星象观测石盘)
        star_plan = map_generator.compile_map_plan(
            "霓虹星夜小镇",
            mode="town",
            palette_id="midnight_handheld",
            places=[
                {"location_id": "s_sq", "name": "星夜广场", "venue_role": "public"},
                {"location_id": "s_cafe", "name": "星光咖啡吧", "venue_role": "cafe"},
                {"location_id": "s_shop", "name": "星街便利店", "venue_role": "shop"},
                {"location_id": "s_arc", "name": "深夜电玩馆", "venue_role": "arcade"},
                {"location_id": "s_home", "name": "星影公寓", "venue_role": "home"},
            ],
            resident_count=3,
            use_llm=False,
        )["map_plan"]
        star_built = self.engine.generate_town(
            star_plan["town_spec"],
            town_id="town_star_dedup",
            hub_location_id=star_plan["hub_location_id"],
            switch_active=False,
        )
        self.assertTrue(star_built["validation"]["ok"], star_built["validation"]["errors"])
        altar_spaces = [
            sid
            for sid in star_built["building_space_ids"]
            if any(
                "星象观测石盘" in str(e.get("name") or "")
                for e in self.engine._world["entities"].values()
                if e["space_id"] == sid
            )
        ]
        self.assertEqual(len(altar_spaces), 0, f"Ordinary buildings in starry town should not spawn observatory altars: {altar_spaces}")

        # 3. Irregular room footprints & split-level indoor mezzanine
        from plugins.nanami_world import interior_designer
        for shape in ("l_shape", "bay_recess", "foyer_hall", "split_terrace"):
            with self.subTest(room_shape=shape):
                sp = self.engine.create_space(
                    name=f"房型_{shape}",
                    width=16,
                    height=12,
                    wall_rows=1,
                    space_kind="room",
                    switch_active=False,
                )
                sid = sp["space_id"]
                rspec = {
                    "space_id": sid,
                    "name": f"测试厅_{shape}",
                    "role": "cafe",
                    "room_shape": shape,
                    "w": 16,
                    "h": 12,
                    "floor_tile": "wood_floor",
                    "rug_tile": "carpet_blue",
                    "wall_tile": "wood_wall",
                }
                sg = interior_designer.design_room_scene_graph(rspec, seed=42)
                self.assertEqual(sg["room_shape"], shape)
                info = interior_designer.solve_and_place_scene_graph(
                    self.engine, sid, rspec, sg, door_cells=[(8, 11)], seed=42,
                )
                self.assertGreaterEqual(info["placed_count"], 4)
                live_sp = self.engine._world["spaces"][sid]
                if shape == "split_terrace":
                    self.assertTrue(live_sp.get("elevations"), "split_terrace room must generate indoor elevated mezzanine")
                    self.assertTrue(live_sp.get("stairs"), "split_terrace room must place indoor stairs")
                elif shape in ("l_shape", "bay_recess"):
                    wall_cnt = sum(
                        1 for y in range(1, 12) for x in range(16)
                        if self.engine.is_wall_cell(live_sp, x, y)
                    )
                    self.assertGreaterEqual(wall_cnt, 8, f"{shape} should carve structural wall blocks")

    def test_multiroom_floorplans_multifloor_buildings_and_interior_doors(self) -> None:
        self.assertEqual(len(schema.TILE_CATALOG), 16)
        self.assertEqual(len(self.engine.all_blueprints()), 28)

        comp = map_generator.compile_map_plan(
            "山脚工坊小镇，包含杂货铺、旧书馆、游艺厅、工坊与住民宿舍",
            mode="town",
            width=40,
            height=30,
            places=[
                {"location_id": "loc_m_shop", "name": "铃兰杂货铺", "venue_role": "shop"},
                {"location_id": "loc_m_lib", "name": "镇立档案馆", "venue_role": "library"},
                {"location_id": "loc_m_arc", "name": "复古游艺厅", "venue_role": "arcade"},
                {"location_id": "loc_m_home", "name": "木叶合宿公寓", "venue_role": "home"},
                {"location_id": "loc_m_wk", "name": "木石手作工坊", "venue_role": "public"},
                {"location_id": "loc_m_caf", "name": "角落甜品铺", "venue_role": "cafe"},
            ],
            resident_count=6,
            use_llm=False,
        )
        town_built = self.engine.generate_town(
            comp["map_plan"]["town_spec"],
            town_id="town_multiroom_test",
            hub_location_id=comp["map_plan"]["hub_location_id"],
            switch_active=False,
        )
        tid = town_built["town_space_id"]
        self.assertTrue(town_built["validation"]["ok"], town_built["validation"])

        town_snap = self.engine.snapshot_for_ui(tid)
        self.assertEqual(len(town_snap["child_spaces"]), len(town_snap["space"]["buildings"]))

        multiroom_spaces = []
        multifloor_groups: dict[str, list[dict]] = {}
        for sid in town_built["building_space_ids"]:
            sp = self.engine._world["spaces"][sid]
            grp = str(sp.get("building_group_id") or "")
            if grp:
                multifloor_groups.setdefault(grp, []).append(sp)
            enclosed_zones = [z for z in (sp.get("zones") or ()) if z.get("enclosed")]
            doors = [
                e for e in self.engine._world["entities"].values()
                if e.get("space_id") == sid and "interior_door" in (e.get("tags") or ())
            ]
            w, h = int(sp["width"]), int(sp["height"])
            int_walls = sum(
                1 for y in range(1, h - 1) for x in range(w)
                if self.engine.is_wall_cell(sp, x, y)
            )
            has_glass = any((sp.get("tiles") or {}).get(f"{x},0") == "glass_wall" for x in range(w))
            if len(enclosed_zones) >= 2 and doors and int_walls >= 4 and (int(sp.get("floor_index", 1)) < 0 or has_glass):
                multiroom_spaces.append((sp, doors))

        self.assertGreaterEqual(len(multiroom_spaces), 3, "Expected multi-room partition floors with glass_wall & closed interior doors")
        upper_groups = [sps for sps in multifloor_groups.values() if any(int(s.get("floor_index", 1)) == 2 for s in sps)]
        self.assertGreaterEqual(len(upper_groups), 1, "Expected >= 1 building with 2F floor")

        sample_1f = next(s for s in upper_groups[0] if int(s.get("floor_index", 1)) == 1)
        snap_1f = self.engine.snapshot_for_ui(sample_1f["space_id"])
        self.assertGreaterEqual(len(snap_1f.get("building_floors") or []), 2)
        indices = [int(f["floor_index"]) for f in snap_1f["building_floors"]]
        self.assertEqual(indices, sorted(indices, reverse=True))
        sample_2f = next(s for s in upper_groups[0] if int(s.get("floor_index", 1)) == 2)
        stair_1_to_2 = [
            e for e in self.engine._world["entities"].values()
            if e.get("space_id") == sample_1f["space_id"]
            and ((e.get("traits") or {}).get("portal") or {}).get("target_space_id") == sample_2f["space_id"]
        ]
        stair_2_to_1 = [
            e for e in self.engine._world["entities"].values()
            if e.get("space_id") == sample_2f["space_id"]
            and ((e.get("traits") or {}).get("portal") or {}).get("target_space_id") == sample_1f["space_id"]
        ]
        self.assertTrue(stair_1_to_2 and stair_2_to_1, "1F and 2F must have bidirectional stair portals")

        # Verify closed interior_door blocks player movement until opened, while NPC auto-opens and traverses it.
        target_sp, target_doors = multiroom_spaces[0]
        target_sid = target_sp["space_id"]
        test_door = target_doors[0]
        door_eid = test_door["entity_id"]
        self.assertTrue(test_door["solid"])
        self.assertEqual(test_door["traits"]["portal"]["target_space_id"], "")
        self.assertFalse(test_door["traits"]["portal"]["is_open"])
        self.assertIn("open", (test_door.get("sprite") or {}).get("frames") or {})

        dx, dy = int(test_door["x"]), int(test_door["y"])
        solids_here = {
            (int(e["x"]) + fx, int(e["y"]) + fy)
            for e in self.engine._world["entities"].values()
            if e.get("space_id") == target_sid and e.get("solid")
            for fx in range(int((e.get("footprint") or [1, 1])[0]))
            for fy in range(int((e.get("footprint") or [1, 1])[1]))
        }
        side_pair = None
        for ox, oy in ((0, 1), (1, 0)):
            p1 = (dx + ox, dy + oy)
            p2 = (dx - ox, dy - oy)
            if (
                not self.engine.is_wall_cell(target_sp, *p1)
                and not self.engine.is_wall_cell(target_sp, *p2)
                and p1 not in solids_here
                and p2 not in solids_here
            ):
                side_pair = (p1, p2)
                break
        self.assertIsNotNone(side_pair, f"Interior door {(dx, dy)} should separate two open walkable cells")
        entry_pt, room_pt = side_pair

        player_id = self.engine.ensure_player_avatar(space_id=target_sid)["actor_id"]
        self.engine.place_actor(player_id, target_sid, *entry_pt)
        blocked_move = self.engine.step_player_move(player_id, dx - entry_pt[0], dy - entry_pt[1], include_prompt=False)
        self.assertFalse(blocked_move["moved"], "Player step must be blocked by closed interior_door")

        self.engine.execute_act(player_id, door_eid, "open")
        opened_door = self.engine._world["entities"][door_eid]
        self.assertTrue(opened_door["traits"]["portal"]["is_open"])
        self.assertFalse(opened_door["solid"])
        pass_move = self.engine.step_player_move(player_id, dx - entry_pt[0], dy - entry_pt[1], include_prompt=False)
        self.assertTrue(pass_move["moved"], "Player step must succeed after opening interior_door")

        # Standing inside the doorway must refuse closing instead of teleporting the actor.
        self.assertNotIn("close", dict(self.engine.legal_verbs(opened_door, self.engine._world["actors"][player_id])))
        self.assertEqual((self.engine._world["actors"][player_id]["x"], self.engine._world["actors"][player_id]["y"]), (dx, dy))
        refused_self = self.engine.execute_act(player_id, door_eid, "close")
        self.assertIn("门框里", refused_self)
        self.assertTrue(self.engine._world["entities"][door_eid]["traits"]["portal"]["is_open"])
        self.assertFalse(self.engine._world["entities"][door_eid]["solid"])
        self.assertEqual((self.engine._world["actors"][player_id]["x"], self.engine._world["actors"][player_id]["y"]), (dx, dy))

        # Step through to the inner room; when another actor stands in the doorway, closing is also refused.
        self.engine.step_player_move(player_id, room_pt[0] - dx, room_pt[1] - dy, include_prompt=False)
        blocker_npc = self.engine.import_actor(name="门前访客", space_id=target_sid, x=dx, y=dy)["actor_id"]
        self.assertNotIn("close", dict(self.engine.legal_verbs(opened_door, self.engine._world["actors"][player_id])))
        refused_other = self.engine.execute_act(player_id, door_eid, "close")
        self.assertIn("门前访客", refused_other)
        self.assertTrue(self.engine._world["entities"][door_eid]["traits"]["portal"]["is_open"])
        self.assertEqual((self.engine._world["actors"][blocker_npc]["x"], self.engine._world["actors"][blocker_npc]["y"]), (dx, dy))

        # Clear the doorway and verify close succeeds cleanly once unobstructed.
        self.engine.place_actor(blocker_npc, tid, *self.engine._find_open_floor_cell_locked(self.engine._world["spaces"][tid]))
        self.assertIn("close", dict(self.engine.legal_verbs(opened_door, self.engine._world["actors"][player_id])))
        self.engine.execute_act(player_id, door_eid, "close")
        self.assertTrue(self.engine._world["entities"][door_eid]["solid"])
        self.engine.place_actor(player_id, target_sid, *room_pt)

        npc_id = self.engine.import_actor(name="住户小汐", space_id=target_sid, x=entry_pt[0], y=entry_pt[1])["actor_id"]
        # Pick another clear cell or move player out of the exact door neighbor first
        open_tx, open_ty = self.engine._find_open_floor_cell_locked(self.engine._world["spaces"][tid])
        self.engine.place_actor(player_id, tid, open_tx, open_ty)
        npc_route = self.engine.plan_actor_route(npc_id, target_sid, room_pt[0], room_pt[1], reach=0, stride=1, cardinal=True)
        self.assertGreaterEqual(len(npc_route), 2, "NPC route planner must traverse unlocked closed interior_doors")
        moved_path = self.engine.move_actor_toward(npc_id, target_sid, room_pt[0], room_pt[1], reach=0, stride=3, cardinal=True)
        self.assertGreaterEqual(len(moved_path), 2)
        self.assertTrue(self.engine._world["entities"][door_eid]["traits"]["portal"]["is_open"])
        self.assertFalse(self.engine._world["entities"][door_eid]["solid"])

        # Schedule home resolution should prefer bedded floor (e.g. 2F bedroom floor)
        with self.engine._lock:
            realm_bldgs = self.engine._realm_buildings_locked(tid)
        resolved_home = schedule.default_home_space_id("住户小汐", "住户小汐", realm_bldgs, actor_id=npc_id)
        self.assertTrue(resolved_home)
        home_meta = next(b for b in realm_bldgs if b.get("interior_space_id") == resolved_home)
        self.assertGreater(int(home_meta.get("bed_count") or 0), 0)

        self.assertEqual(len(schema.TILE_CATALOG), 16)
        self.assertEqual(len(self.engine.all_blueprints()), 28)


if __name__ == "__main__":
    unittest.main(verbosity=2)
