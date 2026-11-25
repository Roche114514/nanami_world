"""Tests for Text World Character Manager RPCs and frontend view contracts."""

from __future__ import annotations

import io
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from _shinsekai import WORLD_CODE

from plugins.nanami_world import PLUGIN_ID, PLUGIN_VERSION, sprite_pack, town_builder, world_page
from plugins.nanami_world.engine import WorldEngine
from plugins.nanami_world.external_bridge import WorldBridge
from plugins.nanami_world.heartbeat import WorldHeartbeat
from plugins.nanami_world.settings import WorldSettings
from sdk.register import PluginCapabilityRegistry


class CharacterManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="nanami-char-mgr-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "world"
        self.persona = self.root / "empty_persona"
        self.persona.mkdir()
        host_config = self.root / "data" / "config" / "characters.yaml"
        host_config.parent.mkdir(parents=True)
        host_config.write_text(
            "- name: 七海千秋\n  setting: 游戏玩家\n"
            "- name: 狛枝凪斗\n  setting: 幸运儿\n"
            "- name: 日向创\n  setting: 预备学科\n",
            encoding="utf-8",
        )
        self.settings = WorldSettings(persona_data_dir=str(self.persona))
        self.engine = WorldEngine(code_dir=WORLD_CODE, data_root=self.data, project_root=self.root)
        self.heartbeat = WorldHeartbeat(self.engine, lambda: self.settings, project_root=self.root)

        registry = PluginCapabilityRegistry()
        registry.set_settings_ui_plugin_context(PLUGIN_ID, PLUGIN_VERSION)
        world_page.register_pages(
            registry,
            code_dir=WORLD_CODE,
            engine=self.engine,
            heartbeat=self.heartbeat,
            settings_store=SimpleNamespace(get=lambda: self.settings),
        )
        canvas = next(p for p in registry.frontend_config_contributions if p.page_id == "world_canvas")
        self.actions = {a.id: a.run for a in canvas.actions}

    def test_cross_world_relations_rpc_and_appearance_outside_world(self) -> None:
        nanami = self.engine.import_actor(name="七海千秋", host_character="七海千秋", x=5, y=5)
        komaeda = self.engine.import_actor(name="狛枝凪斗", host_character="狛枝凪斗", x=6, y=5)
        town = self.engine.create_default_town(move_actor_id="")
        town_id = town["town_space_id"]
        self.assertEqual(town_id, town_builder.DEFAULT_TOWN_ID)

        # Ensure active space stays on room_default before/after querying town_sakura relations
        self.engine.switch_space("room_default")
        self.engine.place_actor(komaeda["actor_id"], f"{town_id}__cafe", 4, 4)

        before_active_sid = self.engine.snapshot_for_ui()["active_space_id"]
        before_active_aid = self.engine.snapshot_for_ui()["active_actor_id"]
        self.assertEqual(before_active_sid, "room_default")

        # Save directional + symmetric relation inside town_sakura
        save_res = self.actions["save_world_relation"]({
            "space_id": town_id,
            "actor_id": nanami["actor_id"],
            "subject_character": "七海千秋",
            "target_key": "char:狛枝凪斗",
            "known": True,
            "rel_stage": "friend",
            "attitude_stance": "warm",
            "call_name": "狛枝同学",
            "relation_tag": "同镇伙伴",
            "affective_valence": 0.65,
            "trust": 0.72,
            "social_willingness": 0.80,
            "shared_memories": "在樱町咖啡馆一起聊过游戏。",
            "sync_reverse": True,
        })
        self.assertTrue(save_res["ok"], save_res)
        self.assertEqual(save_res["realm_space_id"], town_id)

        # Query town_sakura for 七海千秋 -> 狛枝凪斗 without switching active_space_id
        q_town_n = self.actions["get_actor_relations"]({
            "space_id": town_id,
            "actor_id": nanami["actor_id"],
            "subject_character": "七海千秋",
        })
        self.assertTrue(q_town_n["ok"], q_town_n)
        self.assertEqual(q_town_n["realm_space_id"], town_id)
        self.assertIn("狛枝凪斗", q_town_n["in_realm_names"])
        edge_nk = q_town_n["relations"]["char:狛枝凪斗"]
        self.assertTrue(edge_nk["known"])
        self.assertEqual(edge_nk["rel_stage"], "friend")
        self.assertEqual(edge_nk["call_name"], "狛枝同学")
        self.assertAlmostEqual(edge_nk["affective_valence"], 0.65, places=2)

        # Reverse direction synced mutual fields without overwriting target's own call_name
        q_town_k = self.actions["get_actor_relations"]({
            "space_id": f"{town_id}__cafe",
            "actor_id": komaeda["actor_id"],
            "subject_character": "狛枝凪斗",
        })
        self.assertTrue(q_town_k["ok"], q_town_k)
        self.assertEqual(q_town_k["realm_space_id"], town_id)
        edge_kn = q_town_k["relations"]["char:七海千秋"]
        self.assertTrue(edge_kn["known"])
        self.assertEqual(edge_kn["rel_stage"], "friend")
        self.assertEqual(edge_kn["call_name"], "")
        self.assertAlmostEqual(edge_kn["affective_valence"], 0.65, places=2)

        # Different world (room_default) remains completely isolated
        q_room_n = self.actions["get_actor_relations"]({
            "space_id": "room_default",
            "actor_id": nanami["actor_id"],
            "subject_character": "七海千秋",
        })
        self.assertTrue(q_room_n["ok"], q_room_n)
        edge_room_nk = q_room_n["relations"]["char:狛枝凪斗"]
        self.assertFalse(edge_room_nk["known"])
        self.assertEqual(edge_room_nk["rel_stage"], "stranger")

        # Update skin/appearance for Komaeda (who sits in town_sakura__cafe) while hub/room_default is active
        app_res = self.actions["set_actor_appearance"]({
            "actor_id": komaeda["actor_id"],
            "appearance": {
                **(komaeda.get("appearance") or {}),
                "hair_style": "spiky",
                "hair_color": "#e7e4dd",
                "outfit_style": "jacket",
            },
            "map_style": "portrait",
        })
        self.assertTrue(app_res["ok"], app_res)
        self.assertEqual(app_res["actor"]["map_style"], "portrait")
        self.assertEqual(app_res["actor"]["appearance"]["hair_style"], "spiky")

        # Active state remained unmutated across read queries
        snap_after = self.engine.snapshot_for_ui()
        self.assertEqual(snap_after["active_space_id"], before_active_sid)
        self.assertEqual(snap_after["active_actor_id"], before_active_aid)

    def test_default_relations_bucket_cached_skin_extraction_and_bridge(self) -> None:
        nanami = self.engine.import_actor(name="七海千秋", host_character="七海千秋", x=5, y=5)

        # Save a 16x24 pixel PNG into sprite_pack cache for 日向创
        img = Image.new("RGBA", (16, 24), (0, 0, 0, 0))
        for y in range(2, 22):
            for x in range(4, 12):
                img.putpixel((x, y), (40, 90, 180, 255) if y >= 10 else (235, 200, 175, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        sprite_pack.save_cached_sprite(self.data, "日向创", buf.getvalue(), source="manual")

        frames = sprite_pack.extract_cached_sprite_frames(self.data, "日向创")
        self.assertIsNotNone(frames)
        self.assertGreaterEqual(frames["w"], 12)
        self.assertGreaterEqual(len(frames["down"]), 16)

        # Save custom skin for 七海千秋 & verify sibling propagation across a new isolated realm
        custom_down = [
            "................" if y < 2 or y >= 22 else ("....aaaaaaaa...." if y < 10 else "....bbbbbbbb....")
            for y in range(24)
        ]
        self.actions["set_actor_appearance"]({
            "actor_id": nanami["actor_id"],
            "appearance": {
                **(nanami.get("appearance") or {}),
                "hair_color": "#f6c1d3",
                "custom_frames": {"w": 16, "h": 24, "palette": {"a": "#f6c1d3", "b": "#5d7c6f"}, "down": custom_down},
            },
            "map_style": "portrait",
        })

        # Save default initial relation in "__default__" without touching any sandbox world
        def_save = self.actions["save_world_relation"]({
            "space_id": "__default__",
            "actor_id": nanami["actor_id"],
            "subject_character": "七海千秋",
            "target_key": "char:日向创",
            "known": True,
            "rel_stage": "close",
            "attitude_stance": "warm",
            "call_name": "日向君",
            "relation_tag": "默契搭档",
            "affective_valence": 0.78,
            "trust": 0.85,
            "social_willingness": 0.88,
            "shared_memories": "在游戏厅聊到深夜的挚友。",
            "sync_reverse": True,
        })
        self.assertTrue(def_save["ok"], def_save)
        self.assertEqual(def_save["realm_space_id"], "__default__")

        bridge = WorldBridge(self.engine, heartbeat=self.heartbeat)
        iso_world = bridge.dispatch("create_world", {
            "mode": "room",
            "name": "测试隔离舞台",
            "isolated_persona": True,
            "bare_room": True,
            "switch_active": False,
        })["space_id"]
        up_n = bridge.dispatch("upsert_actor", {
            "name": "七海千秋",
            "host_character": "七海千秋",
            "actor_id": f"{iso_world}__n",
            "space_id": iso_world,
            "ephemeral_realm_id": iso_world,
        })["actor"]
        up_h = bridge.dispatch("upsert_actor", {
            "name": "日向创",
            "host_character": "日向创",
            "actor_id": f"{iso_world}__h",
            "space_id": iso_world,
            "ephemeral_realm_id": iso_world,
        })["actor"]
        self.assertEqual(up_n["map_style"], "portrait")
        self.assertEqual(up_n["appearance"]["hair_color"], "#f6c1d3")
        self.assertTrue(up_n["appearance"]["custom_frames"].get("down"))
        self.assertTrue(up_h["appearance"]["custom_frames"].get("down"))

        seeded = bridge.dispatch("seed_initial_relations", {
            "space_id": iso_world,
            "subjects": ["七海千秋", "日向创"],
            "source_space_id": "auto",
            "include_user": True,
        })
        self.assertTrue(seeded["ok"], seeded)
        rels_res = bridge.dispatch("get_actor_relations", {
            "space_id": iso_world,
            "subject_character": "七海千秋",
            "known_actors": ["日向创"],
        })
        edge = rels_res["relations"]["char:日向创"]
        self.assertTrue(edge["known"])
        self.assertEqual(edge["rel_stage"], "close")
        self.assertEqual(edge["call_name"], "日向君")
        self.assertEqual(edge["relation_tag"], "默契搭档")

        # Verify persistent character_appearances survives engine reload
        reloaded = WorldEngine(code_dir=WORLD_CODE, data_root=self.data, project_root=self.root)
        self.assertTrue(reloaded.has_saved_character_appearance("七海千秋"))
        self.assertTrue(reloaded.has_saved_character_appearance("日向创"))

    def test_world_and_story_html_ui_invariants_and_no_native_dialogs(self) -> None:
        html_path = WORLD_CODE / "web" / "world.html"
        html = html_path.read_text(encoding="utf-8")
        for token in (
            'id="btn-hub-char-mgr"',
            'id="char-manager"',
            'id="cm-pane-appearance"',
            'id="cm-pane-relations"',
            'id="cm-world-pills"',
            'id="wm-char-mgr"',
            '"__default__"',
            "默认初始关系",
            "get_actor_relations",
            "sync_reverse",
            "openCharManager",
        ):
            self.assertIn(token, html, f"Expected UI token missing in world.html: {token}")

        html_docs: list[tuple[str, str]] = [("world.html", html)]
        story_html_path = WORLD_CODE.parent / "nanami_story" / "web" / "story.html"
        if story_html_path.is_file():
            story_html = story_html_path.read_text(encoding="utf-8")
            for token in (
                'id="cfg-relation-source"',
                'id="story-char-mgr-link"',
                "relation_source",
                "story_fresh",
                "__default__",
            ):
                self.assertIn(token, story_html, f"Expected UI token missing in story.html: {token}")
            html_docs.append(("story.html", story_html))

        for label, doc in html_docs:
            script_match = re.search(r"<script>(.*)</script>", doc, re.DOTALL)
            self.assertIsNotNone(script_match, label)
            script_body = script_match.group(1)
            forbidden = re.findall(r"(?:window\.)?\b(?:alert|confirm|prompt)\s*\(", script_body)
            self.assertEqual(forbidden, [], f"Forbidden native browser dialogs found in {label}: {forbidden}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
