"""Unit tests for nanami_world story-agnostic external bridge primitives."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _shinsekai import WORLD_CODE

from plugins.nanami_world import external_bridge, persona_bridge, projector, text_compiler, world_page
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.external_bridge import (
    BRIDGE_PROTOCOL_VERSION,
    WorldBridge,
    get_world_bridge,
    register_world_bridge,
    unregister_world_bridge,
)
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from plugins.nanami_world.world_chat import WorldChatService


class _DummyRegister:
    def __init__(self) -> None:
        self.actions: dict[str, object] = {}
        self.pages: list[object] = []
        self.config_pages: list[object] = []

    def register_frontend_page(self, page) -> None:
        self.pages.append(page)

    def register_frontend_config_page(self, contrib) -> None:
        self.config_pages.append(contrib)
        for act in contrib.actions:
            self.actions[act.id] = act.run


class ExternalBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = WorldSettings(heartbeat_use_llm=False)
        clock = patch("time.time", return_value=datetime(2026, 8, 1, 14, 0).timestamp())
        clock.start()
        self.addCleanup(clock.stop)

        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.root / "world", project_root=self.root)
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=self.root)
        self.addCleanup(self.heartbeat.stop)
        self.chat = WorldChatService(self.engine, project_root=self.root, settings_provider=lambda: self.settings)
        self.heartbeat._chat_service_provider = lambda: self.chat
        self.bridge = WorldBridge(self.engine, world_chat=self.chat, heartbeat=self.heartbeat)

    def test_synthesized_entities_are_realm_local_collectible_and_persisted(self) -> None:
        realm = self.bridge.dispatch("create_world", {
            "mode": "room", "name": "像素工坊", "isolated_persona": True, "switch_active": False,
        })["space_id"]
        player = self.bridge.dispatch("ensure_player", {"space_id": realm})["actor_id"]
        brief = "一小缕深褐色碎丝贴在木纹上。"
        detail = "细丝在一端弯折，另一端沾着未干的群青色颜料。" * 5
        fixed = self.bridge.dispatch("synthesize_entity", {
            "space_id": realm, "prompt_text": "深褐色发丝，末端沾蓝色油彩",
            "custom_name": "细丝", "brief": brief, "detail": detail, "collectible": False, "use_llm": False,
        })["entity"]
        portable = self.bridge.dispatch("synthesize_entity", {
            "space_id": realm, "prompt_text": "红色撕裂纸片和票根",
            "custom_name": "票根", "brief": "地上有红色纸片。", "detail": "能读出半行编号。",
            "collectible": True, "use_llm": False,
        })["entity"]
        self.assertEqual(fixed["brief"], brief)
        self.assertEqual(fixed["detail"], detail)
        self.assertEqual(fixed["ephemeral_realm_id"], realm)
        self.assertNotIn("portable", fixed["traits"])
        self.assertNotEqual(fixed["sprite"]["frames"], portable["sprite"]["frames"])
        self.assertEqual((fixed["sprite"]["w"], fixed["sprite"]["h"]), (16, 16))
        snap = self.engine.snapshot_for_ui(realm)
        self.assertEqual(next(e for e in snap["entities"] if e["entity_id"] == fixed["entity_id"])["detail"], "")
        self.assertFalse(any(bp.get("ephemeral_realm_id") for bp in snap["blueprints"]))
        self.engine.place_actor(player, realm, fixed["x"], fixed["y"])
        self.assertIn("拿不起来", self.engine.execute_act(player, fixed["entity_id"], "take"))
        self.assertIn(detail, self.engine.execute_act(player, fixed["entity_id"], "look"))
        observer = self.engine.import_actor(name="旁观者", space_id=realm, is_native_npc=True,
                                            native_realm_id=realm)["actor_id"]
        unseen = projector.project_terminal_data(self.engine, observer, consume_diffs=False)
        self.assertNotIn(detail, str(unseen))
        self.engine.place_actor(player, realm, portable["x"], portable["y"])
        self.engine.execute_act(player, portable["entity_id"], "take")
        self.assertEqual(self.engine._world["entities"][portable["entity_id"]]["parent_id"], f"inventory:{player}")

        locker = self.bridge.dispatch("synthesize_entity", {
            "space_id": realm, "prompt_text": "蓝色铁盒", "traits_hint": {"container": True, "locked": True, "key_tag": "trial_key"},
            "custom_name": "铁盒", "use_llm": False,
        })["entity"]
        nested = self.bridge.dispatch("synthesize_entity", {
            "space_id": realm, "prompt_text": "白纸碎片", "container_entity_id": locker["entity_id"], "use_llm": False,
        })["entity"]
        self.assertEqual(nested["z"], -1)
        self.assertEqual(nested["parent_id"], f"entity:{locker['entity_id']}")
        self.assertNotIn(nested["entity_id"], {row["entity_id"] for row in self.engine.snapshot_for_ui(realm)["entities"]})
        bid = portable["blueprint_id"]
        self.assertIn(bid, self.engine.all_blueprints(realm))
        self.assertNotIn(bid, self.engine.all_blueprints("room_default"))
        self.assertFalse((self.engine.data_root / "blueprints.json").exists())

        reloaded = WorldEngine(code_dir=WORLD_CODE, data_root=self.engine.data_root, project_root=self.root)
        self.assertIn(bid, reloaded.all_blueprints(realm))
        self.assertEqual(reloaded._world["entities"][portable["entity_id"]]["parent_id"], f"inventory:{player}")
        reloaded.delete_space(realm)
        self.assertFalse(reloaded._world["ephemeral_blueprints"])
        self.assertNotIn(portable["entity_id"], reloaded._world["entities"])
        self.assertIn("room_default", reloaded._world["spaces"])

    def test_native_npc_lifecycle_and_actor_status(self) -> None:
        keeper = self.engine.import_actor(name="常驻房客")["actor_id"]
        created = self.bridge.dispatch(
            "create_world",
            {"mode": "room", "name": "悬崖别墅", "space_id": "cliff_villa", "width": 12, "height": 9},
        )
        self.assertEqual(created["space_id"], "cliff_villa")

        alpha = self.bridge.dispatch(
            "upsert_actor",
            {
                "name": "管家阿尔法",
                "actor_id": "npc_alpha",
                "space_id": "cliff_villa",
                "x": 3,
                "y": 4,
                "is_native_npc": True,
                "native_realm_id": "cliff_villa",
            },
        )["actor_id"]
        beta = self.bridge.dispatch(
            "upsert_actor",
            {
                "name": "宾客贝塔",
                "actor_id": "npc_beta",
                "space_id": "cliff_villa",
                "x": 6,
                "y": 4,
                "is_native_npc": True,
                "native_realm_id": "cliff_villa",
                "actor_status": "incapacitated",
                "status_note": "昏倒在地",
            },
        )["actor_id"]
        gamma = self.bridge.dispatch(
            "upsert_actor",
            {
                "name": "旅人伽马",
                "actor_id": "npc_gamma",
                "space_id": "cliff_villa",
                "x": 8,
                "y": 4,
                "is_native_npc": True,
                "native_realm_id": "cliff_villa",
                "actor_status": "missing",
                "status_note": "失去踪影",
            },
        )["actor_id"]

        snap = self.engine.snapshot_for_ui("cliff_villa")
        shown_ids = {a["actor_id"] for a in snap["actors"]}
        self.assertIn(alpha, shown_ids)
        self.assertIn(beta, shown_ids)
        self.assertNotIn(gamma, shown_ids)

        player_id = self.engine.ensure_player_avatar(space_id="cliff_villa")["actor_id"]
        self.engine.place_actor(player_id, "cliff_villa", 5, 4)
        inspect_res = self.engine.player_interact_front(player_id, space_id="cliff_villa", facing="right")
        self.assertEqual(inspect_res["kind"], "inspect_actor")
        self.assertEqual(inspect_res["target_actor_id"], beta)
        self.assertIn("昏倒在地", inspect_res["message"])

        inspected_events = self.engine.poll_events(kinds=["actor_inspected"])
        self.assertEqual(len(inspected_events), 1)
        self.assertEqual(inspected_events[0]["payload"]["target_actor_id"], beta)

        with self.assertRaises(ValueError):
            self.chat.chat(beta, "能听见吗？")
        social = self.chat.conduct_actor_social_exchange(alpha, beta, generated_only=True)
        self.assertEqual(social["turns"], [])
        self.assertIsNone(self.engine.cognitive_scene(beta, include_rooms=True))
        self.assertIsNone(self.engine.cognitive_scene(gamma, include_rooms=True))

        # Place an item carried by native NPC and ensure cascade delete removes native NPCs + carried entity
        carried = self.engine.place_from_blueprint("default_brass_key", "cliff_villa", 3, 4)["entity_id"]
        self.engine.move_entity_ui(carried, 3, 4, target_parent_id=f"inventory:{alpha}")
        deleted = self.bridge.dispatch("delete_world", {"space_id": "cliff_villa", "switch_to": "room_default"})
        self.assertTrue(deleted["ok"])
        remaining_actors = {a["actor_id"] for a in self.engine.list_actors()}
        self.assertIn(keeper, remaining_actors)
        self.assertNotIn(alpha, remaining_actors)
        self.assertNotIn(beta, remaining_actors)
        self.assertNotIn(gamma, remaining_actors)
        self.assertNotIn(carried, self.engine._world["entities"])

    def test_opaque_minds_redacts_ui_live_and_terminal(self) -> None:
        self.bridge.dispatch(
            "create_world",
            {"mode": "room", "name": "迷雾公馆", "space_id": "fog_manor", "opaque_minds": True},
        )
        npc_id = self.bridge.dispatch(
            "upsert_actor",
            {"name": "密室住客", "actor_id": "npc_secret", "space_id": "fog_manor", "is_native_npc": True},
        )["actor_id"]
        player_id = self.engine.ensure_player_avatar(space_id="fog_manor")["actor_id"]

        self.engine.update_actor_autonomy(
            npc_id,
            {
                "last_thought": "不能让人发现地下室钥匙",
                "current_goal": "偷拿保险箱里的信件",
                "current_drive": "curiosity",
                "cognition": {"active_intention": {"kind": "entity", "goal": "偷看秘信"}},
            },
            expected_realm="fog_manor",
        )
        self.bridge.dispatch(
            "set_private_brief",
            {"actor_id": npc_id, "brief": "暗中销毁遗嘱草稿", "cover_goal": "散步"},
        )

        snap = self.engine.snapshot_for_ui("fog_manor")
        self.assertTrue(snap["opaque_minds"])
        npc_snap = next(a for a in snap["actors"] if a["actor_id"] == npc_id)
        self.assertEqual(npc_snap["autonomy"]["last_thought"], "")
        self.assertEqual(npc_snap["autonomy"]["current_goal"], "")
        self.assertEqual(npc_snap["autonomy"]["current_drive"], "")
        self.assertEqual(npc_snap["autonomy"]["cognition"], {})

        dummy_reg = _DummyRegister()
        world_page.register_pages(
            dummy_reg,
            code_dir=WORLD_CODE,
            engine=self.engine,
            settings_store=type("S", (), {"get": lambda _self: self.settings})(),
            heartbeat=self.heartbeat,
            world_chat=self.chat,
            bridge=self.bridge,
        )

        self.engine.set_active_actor(npc_id)
        page_npc = dummy_reg.actions["get_world_snapshot"]({"space_id": "fog_manor"})
        self.assertEqual(page_npc["snapshot"]["active_cognition"], {})
        self.assertIn("暗箱视角", page_npc["terminal"]["text"])
        self.assertNotIn("地下室钥匙", page_npc["terminal"]["text"])
        self.assertNotIn("遗嘱草稿", str(page_npc))

        player_term = projector.project_terminal_data(self.engine, player_id, consume_diffs=False)
        self.assertNotIn("暗箱视角", player_term["text"])

        quiet_npc = self.bridge.dispatch(
            "upsert_actor",
            {"name": "静默见证", "space_id": "fog_manor", "is_native_npc": True, "autonomy_enabled": False},
        )
        self.assertFalse(quiet_npc["actor"]["autonomy"]["enabled"])

        # Live frame preserves speech bubble while redacting mind & reflection
        live_res = self.heartbeat.live.control(npc_id, "test_client", "start", 1)
        self.assertEqual(live_res["actor"]["autonomy"]["last_thought"], "不能让人发现地下室钥匙")
        with self.heartbeat.live._lock:
            la = self.heartbeat.live._actors[npc_id]
            la.speech_queue = [("今晚月色真美。", 0.0, 9999999999.0)]
            la.reflection_log = [{"status": "applied", "detail": "决定销毁证物"}]
        live_rpc = dummy_reg.actions["get_live_frame"]({"actor_id": npc_id, "client": "test_client", "after": -1})["live"]
        self.assertEqual(live_rpc["speech"], "今晚月色真美。")
        self.assertEqual(live_rpc["reflection"], [])
        self.assertEqual(live_rpc["actor"]["autonomy"]["last_thought"], "")
        self.assertEqual(live_rpc["actor"]["autonomy"]["current_goal"], "")
        self.assertEqual(live_rpc["actor"]["autonomy"]["cognition"], {})

        # Turning off opaque_minds reveals internal state cleanly (deep copy was not destructive)
        self.bridge.dispatch("set_world_flags", {"space_id": "fog_manor", "opaque_minds": False})
        unredacted = self.engine.snapshot_for_ui("fog_manor")
        npc_open = next(a for a in unredacted["actors"] if a["actor_id"] == npc_id)
        self.assertEqual(npc_open["autonomy"]["last_thought"], "不能让人发现地下室钥匙")

    def test_private_brief_isolation(self) -> None:
        self.bridge.dispatch("create_world", {"mode": "room", "name": "孤岛大厅", "space_id": "hall_iso"})
        alice = self.bridge.dispatch(
            "upsert_actor",
            {"name": "爱丽丝", "actor_id": "actor_alice", "space_id": "hall_iso", "x": 3, "y": 4, "is_native_npc": True},
        )["actor_id"]
        bob = self.bridge.dispatch(
            "upsert_actor",
            {"name": "鲍勃", "actor_id": "actor_bob", "space_id": "hall_iso", "x": 4, "y": 4, "is_native_npc": True},
        )["actor_id"]

        self.bridge.dispatch(
            "set_private_brief",
            {"actor_id": alice, "brief": "只有爱丽丝知道：凶器藏在壁炉灰烬里。", "cover_goal": "烤火"},
        )
        self.bridge.dispatch(
            "set_private_brief",
            {"actor_id": bob, "brief": "只有鲍勃知道：昨晚十二点大厅曾停电。", "cover_goal": "看表"},
        )

        scene_a = self.engine.cognitive_scene(alice, include_rooms=True)
        scene_b = self.engine.cognitive_scene(bob, include_rooms=True)
        self.assertIn("壁炉灰烬", scene_a["external_private_brief"])
        self.assertNotIn("停电", scene_a["external_private_brief"])
        self.assertIn("停电", scene_b["external_private_brief"])
        self.assertNotIn("壁炉灰烬", scene_b["external_private_brief"])

        snap_text = str(self.engine.snapshot_for_ui("hall_iso"))
        self.assertNotIn("壁炉灰烬", snap_text)
        self.assertNotIn("停电", snap_text)

        seen_prompts: list[tuple[str, str]] = []

        def fake_ask_llm(sys_p: str, usr_p: str, **_kwargs):
            seen_prompts.append((sys_p, usr_p))
            return {"world_actions": [], "emotion": "平静", "intent": "嗯，我明白。"}

        with patch.object(text_compiler, "_ask_llm_json", side_effect=fake_ask_llm):
            self.chat.chat(alice, "你在想什么？", space_id="hall_iso")

        self.assertTrue(any("壁炉灰烬" in sp and "停电" not in sp for sp, _ in seen_prompts))

        self.bridge.dispatch("clear_private_brief", {"actor_id": alice})
        self.assertEqual(self.engine.cognitive_scene(alice, include_rooms=True)["external_private_brief"], "")
        self.assertIn("停电", self.engine.cognitive_scene(bob, include_rooms=True)["external_private_brief"])

    def test_covert_directive_privacy_gate_completion_and_witness(self) -> None:
        # Subcase A: privacy deferral while Bob watches, completion once Bob leaves
        self.bridge.dispatch(
            "create_world",
            {"mode": "room", "name": "书房密室", "space_id": "study_a", "width": 10, "height": 8, "opaque_minds": True},
        )
        alice = self.bridge.dispatch(
            "upsert_actor",
            {"name": "爱丽丝", "actor_id": "cd_alice", "space_id": "study_a", "x": 4, "y": 4, "is_native_npc": True},
        )["actor_id"]
        bob = self.bridge.dispatch(
            "upsert_actor",
            {"name": "鲍勃", "actor_id": "cd_bob", "space_id": "study_a", "x": 2, "y": 4, "is_native_npc": True},
        )["actor_id"]
        chest = self.bridge.dispatch(
            "place_entity",
            {"blueprint_id": "default_wooden_chest", "space_id": "study_a", "x": 5, "y": 4, "locked": True, "key_tag": ""},
        )["entity_id"]

        self.bridge.dispatch(
            "set_covert_directive",
            {
                "actor_id": alice,
                "directive": {
                    "directive_id": "open_secret_chest",
                    "kind": "entity",
                    "target": chest,
                    "action": "unlock",
                    "require_privacy": True,
                    "cover_goal": "整理书桌",
                    "reason": "偷看木箱",
                },
            },
        )

        scene1 = self.engine.cognitive_scene(alice, include_rooms=True)
        self.heartbeat._cognitive.step(scene1, self.settings, use_llm=False, talk=lambda *_: [])
        deferred_events = self.engine.poll_events(kinds=["covert_act_deferred"])
        self.assertGreaterEqual(len(deferred_events), 1)
        self.assertIn(bob, deferred_events[-1]["payload"]["witness_ids"])
        chest_ent = next(e for e in self.engine.snapshot_for_ui("study_a")["entities"] if e["entity_id"] == chest)
        self.assertTrue(chest_ent["traits"]["container"]["is_locked"])

        # UI events must redact covert events
        ui_kinds = {ev["kind"] for ev in self.engine.snapshot_for_ui("study_a")["events"]}
        self.assertNotIn("covert_act_deferred", ui_kinds)
        self.assertNotIn("covert_directive_assigned", ui_kinds)

        # Bob moves to another room; Alice walks back to chest and completes the covert directive
        self.engine.place_actor(bob, "room_default", 4, 4)
        for _ in range(4):
            scene2 = self.engine.cognitive_scene(alice, include_rooms=True)
            self.heartbeat._cognitive.step(scene2, self.settings, use_llm=False, talk=lambda *_: [])
            if (self.engine.get_covert_directive(alice) or {}).get("status") == "completed":
                break

        chest_after = next(e for e in self.engine.snapshot_for_ui("study_a")["entities"] if e["entity_id"] == chest)
        self.assertFalse(chest_after["traits"]["container"]["is_locked"])
        completed = self.engine.poll_events(kinds=["covert_act_completed"])
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["payload"]["directive_id"], "open_secret_chest")

        # Subcase B: caught in the act while walking toward covert target when Bob moves/enters
        self.bridge.dispatch("create_world", {"mode": "room", "name": "走廊", "space_id": "hall_b", "width": 8, "height": 8})
        door = self.bridge.dispatch(
            "place_entity",
            {"blueprint_id": "default_door", "space_id": "hall_b", "x": 3, "y": 2, "portal_target_space_id": "study_a"},
        )["entity_id"]
        notebook = self.bridge.dispatch(
            "place_entity",
            {"blueprint_id": "default_notebook", "space_id": "study_a", "x": 8, "y": 4, "writable_by_ai": True},
        )["entity_id"]
        self.engine.place_actor(alice, "study_a", 2, 4)
        self.engine.place_actor(bob, "hall_b", 3, 3)

        self.bridge.dispatch(
            "set_covert_directive",
            {
                "actor_id": alice,
                "directive": {
                    "directive_id": "forge_note",
                    "kind": "entity",
                    "target": notebook,
                    "action": "write",
                    "note_text": "十一点整去海边",
                    "require_privacy": True,
                    "cover_goal": "四处逛逛",
                },
            },
        )
        scene3 = self.engine.cognitive_scene(alice, include_rooms=True)
        self.heartbeat._cognitive.step(scene3, self.settings, use_llm=False, talk=lambda *_: [])
        self.assertEqual(self.engine.get_covert_directive(alice)["status"], "in_progress")

        # Bob pushes portal door into study_a -> catches Alice in the act
        self.engine.enter_portal(door, actor_id=bob)
        cd_after = self.engine.get_covert_directive(alice)
        self.assertEqual(cd_after["status"], "witnessed")
        self.assertIn(bob, cd_after["witnessed_by"])
        witness_ev = self.engine.poll_events(kinds=["covert_act_witnessed"])
        self.assertGreaterEqual(len(witness_ev), 1)
        self.assertEqual(witness_ev[-1]["payload"]["caught_by"], bob)

    def test_world_bridge_dispatch_poll_events_and_overlay_rpc(self) -> None:
        register_world_bridge(self.bridge)
        self.addCleanup(unregister_world_bridge)
        self.assertIs(get_world_bridge(), self.bridge)

        caps = self.bridge.dispatch("get_capabilities")
        self.assertEqual(caps["protocol_version"], BRIDGE_PROTOCOL_VERSION)
        self.assertTrue(caps["features"]["opaque_minds"])

        town_res = self.bridge.dispatch(
            "create_world",
            {"mode": "town", "name": "推理港湾", "prompt_text": "海边推理小镇", "opaque_minds": True, "building_count": 4},
        )
        town_id = town_res["space_id"]
        self.assertTrue(self.engine.is_opaque_realm(town_id))

        chest_id = self.bridge.dispatch(
            "place_entity",
            {
                "blueprint_id": "default_wooden_chest",
                "space_id": town_id,
                "custom_name": "密码木箱",
                "locked": True,
                "key_tag": "Harbor_Key",
            },
        )["entity_id"]
        key_id = self.bridge.dispatch(
            "place_entity",
            {
                "blueprint_id": "default_brass_key",
                "space_id": town_id,
                "key_tag": "Harbor_Key",
                "container_entity_id": chest_id,
            },
        )["entity_id"]

        state_q = self.bridge.dispatch("query_world_state", {"space_id": town_id})
        self.assertTrue(state_q["opaque_minds"])
        chest_obj = next(e for e in state_q["entities"] if e["entity_id"] == chest_id)
        self.assertEqual(chest_obj["name"], "密码木箱")
        self.assertEqual(chest_obj["traits"]["container"]["key_tag"], "harbor_key")
        self.assertEqual(self.engine._world["entities"][key_id]["parent_id"], f"entity:{chest_id}")

        ev_batch1 = self.bridge.dispatch("poll_events", {"after_seq": 0, "limit": 5})
        self.assertEqual(len(ev_batch1["events"]), 5)
        last_seq = ev_batch1["next_seq"]
        received_actions: list[dict] = []
        self.bridge.register_overlay_handler(town_id, lambda ctx: received_actions.append(ctx) or {"message": "已切换调查线索"})

        self.bridge.dispatch(
            "set_overlay",
            {
                "space_id": town_id,
                "overlay": {
                    "title": "案发现场勘查",
                    "badge": "第一幕 · 线索(2)",
                    "summary": "旅店门口发现了奇怪的铜钥匙。",
                    "sections": [{"heading": "已知线索", "items": ["大厅木箱被反锁"]}],
                    "actions": [{"action_id": "inspect_clue", "label": "核对时间线", "style": "primary"}],
                },
            },
        )
        ev_batch2 = self.bridge.dispatch("poll_events", {
            "after_seq": last_seq, "space_id": town_id, "kinds": ["external_overlay_updated"],
        })
        self.assertTrue(all(ev["seq"] > last_seq for ev in ev_batch2["events"]))
        self.assertTrue(any(ev["kind"] == "external_overlay_updated" for ev in ev_batch2["events"]))

        dummy_reg = _DummyRegister()
        world_page.register_pages(
            dummy_reg,
            code_dir=WORLD_CODE,
            engine=self.engine,
            settings_store=type("S", (), {"get": lambda _self: self.settings})(),
            heartbeat=self.heartbeat,
            world_chat=self.chat,
            bridge=self.bridge,
        )
        rpc_res = dummy_reg.actions["external_overlay_action"](
            {"space_id": town_id, "action_id": "inspect_clue", "payload": {"clue_id": "c1"}},
        )
        self.assertTrue(rpc_res["ok"])
        self.assertTrue(rpc_res["handled"])
        self.assertEqual(len(received_actions), 1)
        self.assertEqual(received_actions[0]["action_id"], "inspect_clue")
        self.assertEqual(rpc_res["snapshot"]["external_overlay"]["title"], "案发现场勘查")

    def test_isolated_persona_sandbox_and_native_npc_persona_brief(self) -> None:
        base_nanami = self.root / "data" / "nanami"
        main_state = base_nanami / "state"
        main_state.mkdir(parents=True, exist_ok=True)
        (base_nanami / "pointers.yaml").write_text(
            "active_edit_id: nanami_chiaki\npointers:\n"
            "  - id: nanami_chiaki\n    enabled: true\n    character_name: 七海千秋\n"
            "    profile_dir: profiles/nanami_chiaki\n    state_root: \"\"\n    chat_key_prefix: shinsekai\n",
            encoding="utf-8",
        )
        chiaki_profile = base_nanami / "profiles" / "nanami_chiaki"
        chiaki_profile.mkdir(parents=True, exist_ok=True)
        (chiaki_profile / "soul.md").write_text("我是七海千秋，主档里的超高校级游戏玩家。", encoding="utf-8")
        (base_nanami / "corpus").mkdir(parents=True, exist_ok=True)
        (base_nanami / "corpus" / "acg_corpus.tsv").write_text("casual\t你好\t打游戏吗？主档口癖\n", encoding="utf-8")

        mind_seed = json.dumps({
            "shinsekai:main": {
                "energy": 0.19,
                "sleepPressure": 0.88,
                "moodV": -0.66,
                "agenda": "主档专属心境议程",
                "updatedAt": 9999999999.0,
                "attitude": {"disengaged": True},
            }
        }, ensure_ascii=False)
        rel_seed = json.dumps({
            "targets": {
                "user": {"known": True, "rel_stage": "friend", "call_name": "主档搭档", "trust": 0.95, "updated_at": 9999.0}
            }
        }, ensure_ascii=False)
        traits_seed = json.dumps({"traits": {"domainEfficacy": {"game": 0.9, "nonGame": 0.3}}}, ensure_ascii=False)
        (main_state / "mind.json").write_text(mind_seed, encoding="utf-8")
        (main_state / "relations.json").write_text(rel_seed, encoding="utf-8")
        (main_state / "traits.json").write_text(traits_seed, encoding="utf-8")

        created = self.bridge.dispatch(
            "create_world",
            {"mode": "room", "name": "平行孤岛", "space_id": "iso_story", "isolated_persona": True, "opaque_minds": True},
        )
        self.assertEqual(created["space_id"], "iso_story")
        self.assertTrue(self.engine.is_isolated_persona_realm("iso_story"))

        chiaki_id = self.bridge.dispatch(
            "upsert_actor",
            {"name": "七海千秋", "host_character": "七海千秋", "actor_id": "story_chiaki", "space_id": "iso_story", "x": 3, "y": 4},
        )["actor_id"]
        butler_id = self.bridge.dispatch(
            "upsert_actor",
            {
                "name": "管家霍华德",
                "actor_id": "npc_butler",
                "space_id": "iso_story",
                "x": 5,
                "y": 4,
                "is_native_npc": True,
                "native_realm_id": "iso_story",
                "persona_brief": "老派严厉的庄园管家，说话一板一眼，从不谈论游戏。",
            },
        )["actor_id"]

        iso_state = persona_bridge.read_persona_state(
            self.root, "七海千秋", space_id="iso_story", actor_id=chiaki_id,
            world_data_root=self.engine.data_root, isolated_persona=True,
        )
        self.assertIsNotNone(iso_state)
        self.assertFalse(iso_state["world_state"])
        self.assertNotEqual(iso_state["agenda"], "主档专属心境议程")
        self.assertAlmostEqual(iso_state["mood_v"], 0.15, places=2)
        self.assertFalse(iso_state["disengaged"])

        iso_rels = persona_bridge.read_actor_relations(
            self.root, "七海千秋", space_id="iso_story",
            world_data_root=self.engine.data_root, isolated_persona=True,
        )
        self.assertFalse(iso_rels["user"]["known"])
        self.assertEqual(iso_rels["user"]["call_name"], "")

        self.bridge.dispatch(
            "set_actor_relation",
            {"space_id": "iso_story", "subject_character": "七海千秋", "target_character": "管家霍华德", "known": True, "rel_stage": "acquainted"},
        )
        iso_rels_after = persona_bridge.read_actor_relations(
            self.root, "七海千秋", space_id="iso_story",
            world_data_root=self.engine.data_root, isolated_persona=True,
        )
        self.assertTrue(iso_rels_after["char:管家霍华德"]["known"])

        seen_prompts: list[tuple[str, str]] = []

        def fake_ask_llm(sys_p: str, usr_p: str, **_kwargs):
            seen_prompts.append((sys_p, usr_p))
            return {"world_actions": [], "emotion": "平静", "intent": "明白，我会留意。", "reply": "明白，我会留意。"}

        with patch.object(text_compiler, "_ask_llm_json", side_effect=fake_ask_llm):
            self.chat.chat(chiaki_id, "你现在感觉怎么样？", space_id="iso_story")
            self.chat.chat(butler_id, "请问晚宴什么时候开始？", space_id="iso_story")

        # Main profile files must remain byte-for-byte untouched
        self.assertEqual((main_state / "mind.json").read_text(encoding="utf-8"), mind_seed)
        self.assertEqual((main_state / "relations.json").read_text(encoding="utf-8"), rel_seed)

        # Native NPC prompt must include its own persona_brief and must NOT fall back to Nanami Chiaki's soul/corpus
        butler_prompts = [sp for sp, _ in seen_prompts if "「管家霍华德」的决策大脑" in sp]
        self.assertTrue(butler_prompts)
        self.assertTrue(any("老派严厉的庄园管家" in sp for sp in butler_prompts))
        self.assertFalse(any("超高校级游戏玩家" in sp or "主档口癖" in sp for sp in butler_prompts))

        # Sandbox folder exists and is cascade-deleted on delete_world
        iso_root = self.engine.data_root / "isolated_personas"
        self.assertTrue(iso_root.is_dir() and any(iso_root.iterdir()))
        self.bridge.dispatch("delete_world", {"space_id": "iso_story"})
        self.assertFalse(any(iso_root.iterdir()) if iso_root.exists() else False)

    def test_dispatch_npc_to_player_cross_space_and_observer_mode(self) -> None:
        self.bridge.dispatch(
            "create_world",
            {"mode": "room", "name": "正厅", "space_id": "hall_main", "width": 10, "height": 8, "isolated_persona": True},
        )
        self.bridge.dispatch(
            "create_world",
            {"mode": "room", "name": "书房", "space_id": "study_side", "parent_space_id": "hall_main", "width": 10, "height": 8, "isolated_persona": True},
        )
        self.bridge.dispatch(
            "place_entity",
            {"blueprint_id": "default_door", "space_id": "hall_main", "x": 5, "y": 1, "portal_target_space_id": "study_side"},
        )
        self.bridge.dispatch(
            "place_entity",
            {"blueprint_id": "default_door", "space_id": "study_side", "x": 5, "y": 1, "portal_target_space_id": "hall_main"},
        )
        messenger = self.bridge.dispatch(
            "upsert_actor",
            {
                "name": "报信侍女",
                "actor_id": "npc_maid",
                "space_id": "hall_main",
                "x": 2,
                "y": 4,
                "is_native_npc": True,
                "native_realm_id": "hall_main",
                "persona_brief": "谨慎但善良的女仆。",
            },
        )["actor_id"]

        # Observer mode (no player_avatar) must return dispatched=False gracefully
        no_player = self.bridge.dispatch(
            "dispatch_npc_to_player",
            {"actor_id": messenger, "reason": "有紧急消息"},
        )
        self.assertTrue(no_player["ok"])
        self.assertFalse(no_player["dispatched"])
        self.assertEqual(no_player["reason"], "no_player_avatar")

        # Now place player_avatar in connected study_side and attach private brief
        player_id = self.bridge.dispatch(
            "ensure_player",
            {"space_id": "study_side", "name": "侦探", "x": 4, "y": 4},
        )["actor_id"]
        self.bridge.dispatch(
            "set_private_brief",
            {"actor_id": messenger, "brief": "你刚刚亲眼看到管家把银钥匙扔进了壁炉。"},
        )
        sent = self.bridge.dispatch(
            "dispatch_npc_to_player",
            {"actor_id": messenger, "player_id": player_id, "reason": "快去告诉侦探关于壁炉的发现"},
        )
        self.assertTrue(sent["dispatched"])

        for _ in range(12):
            scene = self.engine.cognitive_scene(messenger, include_rooms=True)
            if scene is None:
                break
            self.heartbeat._cognitive.step(scene, self.settings, use_llm=False, talk=self.heartbeat._cognitive_talk)
            pending_greet = self.chat.pending_player_greeting("study_side")
            if pending_greet and pending_greet["actor_id"] == messenger:
                break

        maid_after = next(a for a in self.engine.list_actors() if a["actor_id"] == messenger)
        self.assertEqual(maid_after["space_id"], "study_side")
        self.assertLessEqual(max(abs(maid_after["x"] - 4), abs(maid_after["y"] - 4)), 1)
        pending_greet = self.chat.pending_player_greeting("study_side")
        self.assertIsNotNone(pending_greet)
        self.assertEqual(pending_greet["actor_id"], messenger)
        dispatched_evs = self.bridge.dispatch("poll_events", {"kinds": ["npc_dispatched_to_player", "dialogue_exchange"]})["events"]
        self.assertTrue(any(ev["kind"] == "npc_dispatched_to_player" for ev in dispatched_evs))
        self.assertTrue(any(ev["kind"] == "dialogue_exchange" and ev["payload"].get("mode") == "npc_greet_player" for ev in dispatched_evs))

    def test_dialogue_exchange_and_evidence_events_with_ui_redaction(self) -> None:
        self.bridge.dispatch(
            "create_world",
            {"mode": "room", "name": "证物室", "space_id": "evidence_room", "isolated_persona": True},
        )
        npc_a = self.bridge.dispatch(
            "upsert_actor",
            {"name": "证人甲", "actor_id": "wit_a", "space_id": "evidence_room", "x": 3, "y": 4, "is_native_npc": True},
        )["actor_id"]
        npc_b = self.bridge.dispatch(
            "upsert_actor",
            {"name": "证人乙", "actor_id": "wit_b", "space_id": "evidence_room", "x": 4, "y": 4, "is_native_npc": True},
        )["actor_id"]
        player_id = self.bridge.dispatch(
            "ensure_player",
            {"space_id": "evidence_room", "name": "调查员", "x": 6, "y": 4},
        )["actor_id"]
        note_id = self.bridge.dispatch(
            "place_entity",
            {"blueprint_id": "default_notebook", "space_id": "evidence_room", "x": 7, "y": 4, "custom_name": "残破日记"},
        )["entity_id"]
        self.engine.execute_write(npc_a, note_id, "十点整书房熄灯。")

        self.engine.execute_act(player_id, note_id, "look")
        self.engine.execute_act(player_id, note_id, "read")
        with patch.object(text_compiler, "_ask_llm_json", return_value={"world_actions": [], "emotion": "平静", "intent": "我昨晚在走廊。"}):
            self.chat.chat(npc_a, "昨晚你在哪里？", space_id="evidence_room")
        self.chat.conduct_actor_social_exchange(npc_a, npc_b, reason="核对时间")

        evs = self.bridge.dispatch("poll_events", {"space_id": "evidence_room"})["events"]
        kinds = [ev["kind"] for ev in evs]
        self.assertIn("entity_inspected", kinds)
        self.assertIn("entity_read", kinds)
        dlg_evs = [ev for ev in evs if ev["kind"] == "dialogue_exchange"]
        self.assertTrue(any(ev["payload"].get("mode") == "player_chat" for ev in dlg_evs))
        self.assertTrue(any(ev["payload"].get("mode") == "npc_social" for ev in dlg_evs))

        ui_kinds = {ev["kind"] for ev in self.engine.snapshot_for_ui("evidence_room")["events"]}
        self.assertNotIn("dialogue_exchange", ui_kinds)
        self.assertNotIn("npc_dispatched_to_player", ui_kinds)

        q = self.bridge.dispatch("query_world_state", {"space_id": "evidence_room"})
        self.assertTrue(q["isolated_persona"])
        self.assertTrue(any(e["entity_id"] == note_id for e in q["realm_entities"]))
        bps = self.bridge.dispatch("list_blueprints")["blueprints"]
        self.assertIn("default_wooden_chest", bps)


if __name__ == "__main__":
    unittest.main()
