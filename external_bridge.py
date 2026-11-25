"""nanami_world 外部宿主桥接层（External Bridge / Story-Agnostic World Director RPC）。

设计目标：
- 为外部控制层（例如独立的故事引擎插件 nanami_story）提供线程安全、协议稳定的世界原语调度接口。
- 严格保持“世界层零故事业务逻辑”：只暴露空间/NPC/物件/私密简报/隐蔽指令/暗箱视角/只读面板/增量事件流等通用原语。
- 通过进程内注册表支持跨插件解耦发现（无需互相修改源码或直接导入 nanami_persona）。
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from . import map_generator, persona_bridge, text_compiler
from .schema import normalize_pixel_grid

logger = logging.getLogger(__name__)

BRIDGE_PROTOCOL_VERSION = 1

SUPPORTED_OPS: tuple[str, ...] = (
    "get_capabilities",
    "create_world",
    "set_world_flags",
    "delete_world",
    "switch_world",
    "ensure_player",
    "upsert_actor",
    "remove_actor",
    "set_actor_status",
    "get_actor_relations",
    "set_actor_relation",
    "seed_initial_relations",
    "place_entity",
    "synthesize_entity",
    "synthesize_actor_sprite",
    "set_private_brief",
    "clear_private_brief",
    "set_covert_directive",
    "clear_covert_directive",
    "propose_actor_commitment",
    "dispatch_npc_to_player",
    "emit_ambient_cue",
    "set_world_environment",
    "resume_resting_group",
    "set_background_roster",
    "gather_actors",
    "set_dialogue_focus",
    "set_overlay",
    "poll_events",
    "query_world_state",
    "list_realms",
    "list_blueprints",
)


class WorldBridge:
    """面向外部宿主或上层导演引擎的通用线程安全桥接器。"""

    def __init__(
        self,
        engine: Any,
        *,
        world_chat: Any = None,
        heartbeat: Any = None,
        settings_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._engine = engine
        self._world_chat = world_chat
        self._heartbeat = heartbeat
        self._settings_provider = settings_provider
        self._handler_lock = threading.RLock()
        self._overlay_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    @property
    def engine(self) -> Any:
        return self._engine

    def register_overlay_handler(
        self,
        space_id: str,
        handler: Callable[[dict[str, Any]], Any],
    ) -> None:
        """为指定世界（或 '*' 通配）注册外部面板按钮动作回调。"""
        if not callable(handler):
            raise TypeError("handler 必须是可调用对象。")
        raw_sid = str(space_id or "*").strip() or "*"
        realm = raw_sid
        if raw_sid != "*" and hasattr(self._engine, "realm_space_id"):
            try:
                realm = self._engine.realm_space_id(raw_sid) or raw_sid
            except Exception:
                realm = raw_sid
        with self._handler_lock:
            self._overlay_handlers[realm] = handler

    def unregister_overlay_handler(self, space_id: str = "*") -> None:
        raw_sid = str(space_id or "*").strip() or "*"
        realm = raw_sid
        if raw_sid != "*" and hasattr(self._engine, "realm_space_id"):
            try:
                realm = self._engine.realm_space_id(raw_sid) or raw_sid
            except Exception:
                realm = raw_sid
        with self._handler_lock:
            self._overlay_handlers.pop(realm, None)
            self._overlay_handlers.pop(raw_sid, None)

    def handle_overlay_action(
        self,
        action_id: str,
        *,
        space_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """由前端 HUD 面板触发按钮动作，并转发给外部注册的回调处理器。"""
        clean_action = str(action_id or "").strip()
        if not clean_action:
            raise ValueError("action_id 不能为空。")
        sid = str(space_id or "").strip()
        if not sid:
            snap = self._engine.snapshot_for_ui()
            sid = str(snap.get("active_space_id") or "")
        realm = sid
        if sid and hasattr(self._engine, "realm_space_id"):
            try:
                realm = self._engine.realm_space_id(sid) or sid
            except Exception:
                realm = sid
        with self._handler_lock:
            handler = self._overlay_handlers.get(realm) or self._overlay_handlers.get(sid) or self._overlay_handlers.get("*")
        if handler is None:
            return {
                "ok": False,
                "handled": False,
                "error": "当前世界未接入外部面板动作处理器。",
            }
        result = handler({
            "action_id": clean_action,
            "space_id": sid,
            "realm_space_id": realm,
            "payload": dict(payload or {}),
        })
        if isinstance(result, Mapping):
            out = dict(result)
            out.setdefault("ok", True)
            out.setdefault("handled", True)
            return out
        return {"ok": True, "handled": True, "result": result}

    def dispatch(self, op: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """分派单条通用世界桥接指令。"""
        clean_op = str(op or "").strip()
        data = dict(args or {})

        if clean_op == "get_capabilities":
            return {
                "ok": True,
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "ops": list(SUPPORTED_OPS),
                "features": {
                    "opaque_minds": True,
                    "isolated_persona": True,
                    "native_npcs": True,
                    "actor_status": True,
                    "persona_brief": True,
                    "get_actor_relations": True,
                    "set_actor_relation": True,
                    "seed_initial_relations": True,
                    "private_brief": True,
                    "covert_directive": True,
                    "dispatch_npc_to_player": True,
                    "dialogue_exchange_events": True,
                    "dialogue_focus": self._heartbeat is not None,
                    "external_overlay": True,
                    "overlay_cards": True,
                    "ephemeral_blueprints": True,
                    "cursor_events": True,
                    "compound_world": True,
                    "map_generator": True,
                    "realm_initial_hour": True,
                    "placement_strategy": True,
                    "night_active": True,
                    "list_realms": True,
                    "propose_actor_commitment": True,
                    "emit_ambient_cue": True,
                    "set_world_environment": True,
                    "multi_floor_zones": True,
                },
            }

        if clean_op == "create_world":
            mode = str(data.get("mode") or "").strip().lower()
            opaque_minds = bool(data.get("opaque_minds", False))
            isolated_persona = bool(data.get("isolated_persona", False))
            fixed_sid = str(data.get("space_id") or data.get("fixed_space_id") or "").strip()
            name = str(data.get("name") or "").strip()
            town_spec = data.get("town_spec")
            prompt_text = str(data.get("prompt_text") or "").strip()
            places = data.get("places") if isinstance(data.get("places"), (list, tuple)) else ()
            resident_count = int(data.get("resident_count") or 0)
            raw_hour = data.get("initial_hour")
            initial_hour = float(raw_hour) if raw_hour is not None else None
            use_llm = self._use_compiler_llm(data)
            switch_active = bool(data.get("switch_active", True))

            if mode == "compound":
                plan_res = map_generator.compile_map_plan(
                    prompt_text or name or "建筑群",
                    mode="compound",
                    name=name,
                    width=int(data["width"]) if data.get("width") is not None else None,
                    height=int(data["height"]) if data.get("height") is not None else None,
                    places=places,
                    resident_count=resident_count,
                    initial_hour=initial_hour,
                    building_count=(
                        int(data["building_count"])
                        if data.get("building_count") is not None
                        else (int(data["room_count"]) if data.get("room_count") is not None else None)
                    ),
                    palette_id=str(data.get("palette_id") or ""),
                    use_llm=use_llm,
                )
                res = map_generator.build_compound_world(
                    self._engine,
                    plan_res["map_plan"],
                    hub_id=fixed_sid,
                    opaque_minds=opaque_minds,
                    isolated_persona=isolated_persona,
                    switch_active=switch_active,
                )
                eff_sid = str(res.get("space_id") or res.get("hub_space_id") or fixed_sid or "")
                return {"ok": True, "space_id": eff_sid, "used_llm": plan_res["used_llm"], **res}

            use_town = mode == "town" or isinstance(town_spec, Mapping) or (mode != "room" and bool(prompt_text))
            if use_town:
                used_llm_town = False
                if isinstance(town_spec, Mapping):
                    spec = dict(town_spec)
                    hub_loc = str(data.get("hub_location_id") or "")
                    eff_hour = initial_hour
                else:
                    plan_res = map_generator.compile_map_plan(
                        prompt_text or name or "小镇",
                        mode="town",
                        name=name,
                        width=int(data["width"]) if data.get("width") is not None else None,
                        height=int(data["height"]) if data.get("height") is not None else None,
                        places=places,
                        resident_count=resident_count,
                        initial_hour=initial_hour,
                        building_count=int(data["building_count"]) if data.get("building_count") is not None else 6,
                        palette_id=str(data.get("palette_id") or ""),
                        use_llm=use_llm,
                    )
                    used_llm_town = bool(plan_res["used_llm"])
                    map_plan = plan_res["map_plan"]
                    spec = dict(map_plan["town_spec"])
                    if name:
                        spec["name"] = name
                    hub_loc = str(map_plan.get("hub_location_id") or "")
                    eff_hour = initial_hour if initial_hour is not None else map_plan.get("initial_hour")
                res = self._engine.generate_town(
                    spec,
                    town_id=fixed_sid,
                    opaque_minds=opaque_minds,
                    isolated_persona=isolated_persona,
                    switch_active=switch_active,
                    initial_hour=eff_hour,
                    hub_location_id=hub_loc,
                )
                eff_sid = str(res.get("space_id") or res.get("town_space_id") or fixed_sid or "")
                return {"ok": True, "space_id": eff_sid, "used_llm": used_llm_town, **res}

            if not data.get("bare_room") and (bool(prompt_text) or bool(places) or resident_count > 0 or initial_hour is not None):
                plan_res = map_generator.compile_map_plan(
                    prompt_text or name or "室内空间",
                    mode="room",
                    name=name,
                    width=int(data["width"]) if data.get("width") is not None else None,
                    height=int(data["height"]) if data.get("height") is not None else None,
                    places=places,
                    resident_count=resident_count,
                    initial_hour=initial_hour,
                    palette_id=str(data.get("palette_id") or ""),
                    use_llm=use_llm,
                )
                res = map_generator.build_architectural_room(
                    self._engine,
                    plan_res["map_plan"],
                    room_id=fixed_sid,
                    parent_space_id=str(data.get("parent_space_id") or ""),
                    opaque_minds=opaque_minds,
                    isolated_persona=isolated_persona,
                    switch_active=switch_active,
                )
                eff_sid = str(res.get("space_id") or fixed_sid or "")
                return {"ok": True, "space_id": eff_sid, "used_llm": plan_res["used_llm"], **res}

            space = self._engine.create_space(
                name=name or "新世界",
                width=int(data.get("width") or 12),
                height=int(data.get("height") or 9),
                palette_id=str(data.get("palette_id") or data.get("theme_id") or "warm_wood"),
                ambient_phrase=str(data.get("ambient_phrase") or ""),
                parent_space_id=str(data.get("parent_space_id") or ""),
                fixed_space_id=fixed_sid,
                switch_active=switch_active,
                opaque_minds=opaque_minds,
                isolated_persona=isolated_persona,
            )
            return {"ok": True, "space_id": space["space_id"], "space": space}

        if clean_op == "set_world_flags":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                raise ValueError("set_world_flags 需要提供 space_id。")
            res: dict[str, Any] = {"ok": True, "space_id": self._engine.realm_space_id(sid)}
            if "opaque_minds" in data:
                res["opaque_minds"] = self._engine.set_realm_opaque_minds(sid, bool(data["opaque_minds"]))
            else:
                res["opaque_minds"] = self._engine.is_opaque_realm(sid)
            if "isolated_persona" in data:
                res["isolated_persona"] = self._engine.set_realm_isolated_persona(sid, bool(data["isolated_persona"]))
            else:
                res["isolated_persona"] = self._engine.is_isolated_persona_realm(sid)
            return res

        if clean_op == "delete_world":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                raise ValueError("delete_world 需要提供 space_id。")
            owned = data.get("owned_actor_ids") or ()
            if not isinstance(owned, (list, tuple)) or not all(isinstance(aid, str) for aid in owned):
                raise ValueError("角色 ID 列表无效。")
            res = self._engine.delete_space(sid, owned_actor_ids=tuple(owned))
            if self._heartbeat is not None:
                self._heartbeat.live.set_background_roster(sid, [])
            return {"ok": True, **res}

        if clean_op == "switch_world":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                raise ValueError("switch_world 需要提供 space_id。")
            sp = self._engine.switch_space(sid)
            return {"ok": True, "space_id": sp["space_id"], "space": sp}

        if clean_op == "ensure_player":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                snap = self._engine.snapshot_for_ui()
                sid = str(snap.get("active_space_id") or "")
            player = self._engine.ensure_player_avatar(
                space_id=sid,
                name=str(data.get("name") or "你"),
            )
            if data.get("x") is not None and data.get("y") is not None:
                player = self._engine.place_actor(player["actor_id"], sid, int(data["x"]), int(data["y"]))
            return {"ok": True, "actor_id": player["actor_id"], "actor": player}

        if clean_op == "upsert_actor":
            name = str(data.get("name") or "").strip()
            host_character = str(data.get("host_character") or "").strip()
            if not (name or host_character):
                raise ValueError("upsert_actor 需要提供 name 或 host_character。")
            is_native_npc = bool(data.get("is_native_npc", False))
            actor = self._engine.import_actor(
                name=name or host_character,
                host_character=host_character,
                actor_id=str(data.get("actor_id") or "").strip(),
                space_id=str(data.get("space_id") or "").strip(),
                x=int(data["x"]) if data.get("x") is not None else None,
                y=int(data["y"]) if data.get("y") is not None else None,
                map_style=str(data.get("map_style") or ""),
                is_native_npc=is_native_npc,
                native_realm_id=str(data.get("native_realm_id") or "").strip(),
                ephemeral_realm_id=str(data.get("ephemeral_realm_id") or "").strip(),
                actor_status=str(data.get("actor_status") or "active").strip(),
                status_note=str(data.get("status_note") or ""),
                persona_brief=str(data.get("persona_brief") or ""),
                home_space_id=str(data.get("home_space_id") or "").strip(),
                seed_familiar_realm=bool(data.get("seed_familiar_realm", False)),
            )
            if "autonomy_enabled" in data:
                actor["autonomy"] = self._engine.update_actor_autonomy(
                    actor["actor_id"],
                    {"enabled": bool(data["autonomy_enabled"])},
                )
            return {"ok": True, "actor_id": actor["actor_id"], "actor": actor}

        if clean_op == "remove_actor":
            aid = str(data.get("actor_id") or "").strip()
            if not aid:
                raise ValueError("remove_actor 需要提供 actor_id。")
            res = self._engine.remove_actor(aid)
            return {"ok": True, **res}

        if clean_op == "set_actor_status":
            aid = str(data.get("actor_id") or "").strip()
            status = str(data.get("status") or "").strip()
            if not aid or not status:
                raise ValueError("set_actor_status 需要提供 actor_id 与 status。")
            actor = self._engine.set_actor_status(
                aid,
                status,
                status_note=str(data.get("status_note") or ""),
            )
            return {"ok": True, "actor_id": actor["actor_id"], "actor": actor}

        if clean_op == "get_actor_relations":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                snap = self._engine.snapshot_for_ui()
                sid = str(snap.get("active_space_id") or "room_default")
            if sid == persona_bridge.DEFAULT_RELATIONS_REALM:
                realm_sid = persona_bridge.DEFAULT_RELATIONS_REALM
                iso = False
            else:
                realm_sid = self._engine.realm_space_id(sid)
                iso = self._engine.is_isolated_persona_realm(realm_sid)

            subj = str(data.get("subject_character") or data.get("subject") or "").strip()
            actors_all = self._engine.list_actors()
            if not subj:
                aid = str(data.get("actor_id") or "").strip()
                act = next((a for a in actors_all if a.get("actor_id") == aid), None)
                if act:
                    subj = str(act.get("host_character") or act.get("name") or "").strip()
            if not subj:
                raise ValueError("get_actor_relations 需要提供 subject_character。")

            known_names: list[str] = []
            seen_names: set[str] = {subj}
            raw_known = data.get("known_actors")
            if isinstance(raw_known, (list, tuple)):
                for item in raw_known:
                    cname = str(item or "").strip()
                    if cname and cname not in seen_names:
                        seen_names.add(cname)
                        known_names.append(cname)
            for a in actors_all:
                if a.get("is_player") or a.get("actor_status", "active") == "missing":
                    continue
                cname = str(a.get("host_character") or a.get("name") or "").strip()
                if not cname or cname in seen_names:
                    continue
                a_sid = str(a.get("space_id") or "")
                a_realm = self._engine.realm_space_id(a_sid) if a_sid else ""
                if realm_sid == persona_bridge.DEFAULT_RELATIONS_REALM:
                    if a_realm and not self._engine.is_isolated_persona_realm(a_realm):
                        seen_names.add(cname)
                        known_names.append(cname)
                elif a_realm == realm_sid:
                    seen_names.add(cname)
                    known_names.append(cname)

            settings = self._settings_provider() if self._settings_provider else None
            rels = persona_bridge.read_actor_relations(
                self._engine.project_root,
                subj,
                space_id=realm_sid,
                world_data_root=self._engine.data_root,
                custom_persona_dir=str(getattr(settings, "persona_data_dir", "") or ""),
                known_actors=known_names,
                isolated_persona=iso,
            )
            return {"ok": True, "space_id": realm_sid, "subject_character": subj, "relations": rels}

        if clean_op == "seed_initial_relations":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                raise ValueError("seed_initial_relations 需要提供 target space_id。")
            realm_sid = self._engine.realm_space_id(sid)
            raw_subjects = data.get("subjects")
            if not isinstance(raw_subjects, (list, tuple)):
                raise ValueError("seed_initial_relations 需要提供 subjects 列表。")
            subjects = [str(s or "").strip() for s in raw_subjects if str(s or "").strip()]
            source_sid = str(data.get("source_space_id") or "auto").strip() or "auto"
            include_user = bool(data.get("include_user", True))

            with self._engine._lock:
                spaces = self._engine._world.get("spaces") or {}
                sandbox_realms = [
                    sp_id
                    for sp_id in spaces
                    if self._engine._realm_space_id_locked(sp_id) == sp_id
                    and not self._engine._is_isolated_persona_realm_locked(sp_id)
                ]
                actor_homes: dict[str, str] = {}
                for a in (self._engine._world.get("actors") or {}).values():
                    if a.get("is_player") or a.get("is_native_npc") or a.get("ephemeral_realm_id"):
                        continue
                    char_key = str(a.get("host_character") or a.get("name") or "").strip()
                    a_sid = str(a.get("space_id") or "")
                    if not char_key or not a_sid:
                        continue
                    a_realm = self._engine._realm_space_id_locked(a_sid)
                    if not self._engine._is_isolated_persona_realm_locked(a_realm):
                        actor_homes.setdefault(char_key, a_realm)

            settings = self._settings_provider() if self._settings_provider else None
            resolved = persona_bridge.resolve_initial_world_relations(
                self._engine.project_root,
                self._engine.data_root,
                subjects,
                source_space_id=source_sid,
                actor_home_realms=actor_homes,
                sandbox_realm_ids=sandbox_realms,
                custom_persona_dir=str(getattr(settings, "persona_data_dir", "") or ""),
                include_user=include_user,
            )
            seeded_out: dict[str, dict[str, dict[str, Any]]] = {}
            for subj, tmap in resolved.items():
                for tkey, rel_row in tmap.items():
                    patch = {
                        "target_name": rel_row.get("target_name", ""),
                        "known": bool(rel_row.get("known", False)),
                        "rel_stage": str(rel_row.get("rel_stage") or "stranger"),
                        "attitude_stance": str(rel_row.get("attitude_stance") or "neutral"),
                        "call_name": str(rel_row.get("call_name") or ""),
                        "relation_tag": str(rel_row.get("relation_tag") or ""),
                        "shared_memories": str(rel_row.get("shared_memories") or ""),
                        "affective_valence": float(rel_row.get("affective_valence") or 0.0),
                        "trust": float(rel_row.get("trust") if rel_row.get("trust") is not None else 0.55),
                        "warmth": float(rel_row.get("warmth") if rel_row.get("warmth") is not None else 0.45),
                        "social_willingness": float(
                            rel_row.get("social_willingness")
                            if rel_row.get("social_willingness") is not None
                            else 0.50
                        ),
                        "repulsion": float(rel_row.get("repulsion") or 0.0),
                    }
                    saved = persona_bridge.save_world_actor_relation(
                        self._engine.data_root,
                        realm_sid,
                        subj,
                        tkey,
                        patch,
                    )
                    seeded_out.setdefault(subj, {})[tkey] = saved
            return {
                "ok": True,
                "space_id": realm_sid,
                "source_space_id": source_sid,
                "seeded": seeded_out,
            }

        if clean_op == "set_actor_relation":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                raise ValueError("set_actor_relation 需要提供 space_id。")
            realm_sid = self._engine.realm_space_id(sid)
            subj = str(data.get("subject_character") or data.get("subject") or "").strip()
            if not subj:
                raise ValueError("set_actor_relation 需要提供 subject_character。")
            tkey = str(data.get("target_key") or "").strip()
            if not tkey:
                tchar = str(data.get("target_character") or data.get("target") or "").strip()
                tkey = "user" if tchar in ("user", "player", "player_avatar") else (f"char:{tchar}" if tchar else "")
            if not tkey:
                raise ValueError("set_actor_relation 需要提供 target_key 或 target_character。")
            raw_patch = data.get("patch")
            if isinstance(raw_patch, Mapping):
                patch = dict(raw_patch)
            else:
                patch = {
                    k: v for k, v in data.items()
                    if k not in ("space_id", "subject_character", "subject", "target_key", "target_character", "target")
                }
            rel = persona_bridge.save_world_actor_relation(
                self._engine.data_root,
                realm_sid,
                subj,
                tkey,
                patch,
            )
            return {"ok": True, "space_id": realm_sid, "relation": rel}

        if clean_op == "place_entity":
            bp_id = str(data.get("blueprint_id") or "").strip()
            sid = str(data.get("space_id") or "").strip()
            if not bp_id or not sid:
                raise ValueError("place_entity 需要提供 blueprint_id 与 space_id。")
            if data.get("x") is None or data.get("y") is None:
                bp = self._engine.all_blueprints(sid).get(bp_id)
                if bp is None:
                    raise ValueError(f"素材蓝图不存在：{bp_id}")
                with self._engine._lock:
                    x, y = self._engine._find_placement_cell_locked(
                        sid,
                        bp,
                        strategy=str(data.get("placement_strategy") or "spread"),
                        zone_id=str(data.get("zone_id") or ""),
                    )
            else:
                x = int(data["x"])
                y = int(data["y"])
            placed = self._engine.place_from_blueprint(
                bp_id,
                sid,
                x,
                y,
                delivery_mode=str(data.get("delivery_mode") or "silent"),
                note_text=str(data.get("note_text") or ""),
                portal_target_space_id=str(data.get("portal_target_space_id") or ""),
            )
            needs_cfg = any(
                k in data
                for k in ("custom_name", "brief", "detail", "locked", "key_tag", "writable_by_ai", "container_entity_id")
            )
            if needs_cfg:
                placed = self._engine.configure_placed_entity(
                    placed["entity_id"],
                    custom_name=str(data["custom_name"]) if data.get("custom_name") is not None else None,
                    brief=str(data["brief"]) if data.get("brief") is not None else None,
                    detail=str(data["detail"]) if data.get("detail") is not None else None,
                    locked=bool(data["locked"]) if data.get("locked") is not None else None,
                    key_tag=str(data["key_tag"]) if data.get("key_tag") is not None else None,
                    writable_by_ai=bool(data["writable_by_ai"]) if data.get("writable_by_ai") is not None else None,
                    container_entity_id=str(data.get("container_entity_id") or ""),
                )
            return {"ok": True, "entity_id": placed["entity_id"], "entity": placed}

        if clean_op == "synthesize_entity":
            return self._synthesize_entity(data)

        if clean_op == "synthesize_actor_sprite":
            aid = str(data.get("actor_id") or "").strip()
            name, prompt = str(data.get("name") or aid), str(data.get("prompt_text") or "")
            try:
                compiled = text_compiler.compile_actor_appearance(name, prompt, use_llm=self._use_compiler_llm(data))
            except (ValueError, TypeError, OverflowError):
                compiled = text_compiler.compile_actor_appearance(name, prompt, use_llm=False)
            actor = self._engine.set_actor_appearance(aid, appearance=compiled["appearance"], map_style="doll")
            return {"ok": True, "actor_id": aid, "actor": actor}

        if clean_op == "set_private_brief":
            aid = str(data.get("actor_id") or "").strip()
            if not aid:
                raise ValueError("set_private_brief 需要提供 actor_id。")
            rec = self._engine.set_actor_private_brief(
                aid,
                str(data.get("brief") or ""),
                cover_goal=str(data.get("cover_goal") or ""),
                space_id=str(data.get("space_id") or ""),
                night_active=(bool(data["night_active"]) if data.get("night_active") is not None else None),
            )
            return {"ok": True, "actor_id": aid, "state": rec}

        if clean_op == "clear_private_brief":
            aid = str(data.get("actor_id") or "").strip()
            if not aid:
                raise ValueError("clear_private_brief 需要提供 actor_id。")
            cleared = self._engine.clear_actor_private_brief(aid, space_id=str(data.get("space_id") or ""))
            return {"ok": True, "actor_id": aid, "cleared": cleared}

        if clean_op == "set_covert_directive":
            aid = str(data.get("actor_id") or "").strip()
            if not aid:
                raise ValueError("set_covert_directive 需要提供 actor_id。")
            directive = data.get("directive") if isinstance(data.get("directive"), Mapping) else data
            cd = self._engine.set_covert_directive(aid, directive, space_id=str(data.get("space_id") or ""))
            return {"ok": True, "actor_id": aid, "directive": cd}

        if clean_op == "clear_covert_directive":
            aid = str(data.get("actor_id") or "").strip()
            if not aid:
                raise ValueError("clear_covert_directive 需要提供 actor_id。")
            cleared = self._engine.clear_covert_directive(
                aid,
                directive_id=str(data.get("directive_id") or ""),
                space_id=str(data.get("space_id") or ""),
            )
            return {"ok": True, "actor_id": aid, "cleared": cleared}

        if clean_op == "resume_resting_group":
            actor_ids = data.get("actor_ids")
            if not isinstance(actor_ids, list) or not actor_ids or not all(isinstance(aid, str) for aid in actor_ids):
                raise ValueError("需要提供参演角色 ID 列表。")
            return {"ok": True, **self._engine.resume_resting_group(str(data.get("space_id") or ""), actor_ids)}

        if clean_op in ("set_background_roster", "gather_actors"):
            sid = str(data.get("space_id") or "").strip()
            actor_ids = data.get("actor_ids")
            if not sid or not isinstance(actor_ids, list) or not all(isinstance(aid, str) for aid in actor_ids):
                raise ValueError("需要空间及角色 ID 列表。")
            if clean_op == "set_background_roster":
                if self._heartbeat is not None:
                    self._heartbeat.live.set_background_roster(self._engine.realm_space_id(sid), actor_ids)
                return {"ok": True}
            with self._engine._lock:
                world = self._engine._world
                space = world["spaces"].get(sid)
                if space is None:
                    raise ValueError(f"空间不存在：{sid}")
                realm = self._engine.realm_space_id(sid)
                moved = []
                for aid in actor_ids:
                    actor = world["actors"].get(aid)
                    if (not actor or actor.get("is_player") or actor.get("actor_status") != "active"
                            or actor["space_id"] == sid or self._engine.realm_space_id(actor["space_id"]) != realm):
                        continue
                    x, y = self._engine._find_open_floor_cell_locked(space, ignore_actor_id=aid)
                    if not self._engine._is_floor_walkable_locked(space, x, y, ignore_actor_id=aid):
                        continue
                    self._engine.place_actor(aid, sid, x, y)
                    moved.append(aid)
                return {"ok": True, "actor_ids": moved}

        if clean_op == "dispatch_npc_to_player":
            aid = str(data.get("actor_id") or "").strip()
            if not aid:
                raise ValueError("dispatch_npc_to_player 需要提供 actor_id。")
            return self._engine.dispatch_actor_to_player(
                aid,
                player_id=str(data.get("player_id") or "").strip(),
                reason=str(data.get("reason") or ""),
                space_id=str(data.get("space_id") or "").strip(),
                preempt=bool(data.get("preempt", True)),
            )

        if clean_op == "set_dialogue_focus":
            sid = str(data.get("space_id") or "").strip()
            scenes = data.get("scenes")
            if not sid or not isinstance(scenes, list) or len(scenes) > 3:
                raise ValueError("需要提供世界 ID 与当前关键对话列表。")
            for scene in scenes:
                actor_ids = scene.get("actor_ids") if isinstance(scene, Mapping) else None
                if (not isinstance(actor_ids, list) or len(actor_ids) != 2
                        or not all(isinstance(aid, str) and aid for aid in actor_ids)
                        or actor_ids[0] == actor_ids[1]
                        or not isinstance(scene.get("scene_id"), str) or not scene["scene_id"]
                        or not isinstance(scene.get("topic"), str) or not scene["topic"]
                        or not isinstance(scene.get("review_pending", False), bool)
                        or not isinstance(scene.get("revision"), int) or scene["revision"] < 0):
                    raise ValueError("关键对话须包含两名不同的角色、话题和当前轮次。")
            if self._heartbeat is not None:
                self._heartbeat.live.set_dialogue_focus(self._engine.realm_space_id(sid), scenes)
            return {"ok": True, "available": self._heartbeat is not None}

        if clean_op == "propose_actor_commitment":
            aid = str(data.get("actor_id") or "").strip()
            if not aid:
                raise ValueError("propose_actor_commitment 需要提供 actor_id。")
            commitment = data.get("commitment") if isinstance(data.get("commitment"), Mapping) else data
            return self._engine.propose_actor_commitment(
                aid,
                commitment,
                space_id=str(data.get("space_id") or "").strip(),
                preempt=bool(data.get("preempt", False)),
            )

        if clean_op == "emit_ambient_cue":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                raise ValueError("emit_ambient_cue 需要提供 space_id。")
            return self._engine.emit_ambient_cue(
                sid,
                str(data.get("text") or ""),
                zone_id=str(data.get("zone_id") or "").strip(),
                audible_adjacent_zones=bool(data.get("audible_adjacent_zones", True)),
                cue_kind=str(data.get("cue_kind") or "sound").strip(),
            )

        if clean_op == "set_world_environment":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                raise ValueError("set_world_environment 需要提供 space_id。")
            weather_res = None
            wmode = str(data.get("weather_mode") or "").strip().lower()
            if wmode:
                raw_int = data.get("intensity")
                weather_res = self._engine.set_weather(
                    sid,
                    wmode,
                    intensity=(float(raw_int) if raw_int is not None else None),
                )
            updated_entities: list[str] = []
            raw_updates = data.get("entity_updates")
            if isinstance(raw_updates, list) and raw_updates:
                realm_sid = self._engine.realm_space_id(sid)
                with self._engine._lock:
                    for row in raw_updates:
                        if not isinstance(row, Mapping):
                            continue
                        eid = str(row.get("entity_id") or "").strip()
                        ent = self._engine._world["entities"].get(eid)
                        if not ent or self._engine._realm_space_id_locked(str(ent.get("space_id") or "")) != realm_sid:
                            continue
                        cfg_kwargs: dict[str, Any] = {}
                        for k in ("brief", "detail", "key_tag"):
                            if row.get(k) is not None:
                                cfg_kwargs[k] = str(row[k])
                        if "locked" in row and row["locked"] is not None:
                            cfg_kwargs["locked"] = bool(row["locked"])
                        if cfg_kwargs:
                            self._engine.configure_placed_entity(eid, **cfg_kwargs)
                        if "is_open" in row and row["is_open"] is not None:
                            want_open = bool(row["is_open"])
                            traits = ent.get("traits") or {}
                            if "portal" in traits and "container" not in traits:
                                if not want_open and self._engine._door_occupant_refusal_locked(ent, ""):
                                    continue
                                traits["portal"]["is_open"] = want_open
                                ent["solid"] = not want_open
                                self._engine._append_event_locked(
                                    "entity_state_changed",
                                    space_id=str(ent["space_id"]),
                                    payload={"entity_id": eid, "is_open": want_open, "solid": ent["solid"]},
                                )
                            elif "container" in traits and isinstance(traits["container"], dict):
                                traits["container"]["is_open"] = want_open
                                self._engine._append_event_locked(
                                    "entity_state_changed",
                                    space_id=str(ent["space_id"]),
                                    payload={"entity_id": eid, "is_open": want_open},
                                )
                        updated_entities.append(eid)
                    if updated_entities:
                        self._engine._save_world_locked()
            return {"ok": True, "space_id": sid, "weather": weather_res, "updated_entities": updated_entities}

        if clean_op == "set_overlay":
            sid = str(data.get("space_id") or "").strip()
            if not sid:
                snap = self._engine.snapshot_for_ui()
                sid = str(snap.get("active_space_id") or "")
            overlay = data.get("overlay") if "overlay" in data else (None if data.get("clear") else data)
            ov = self._engine.set_external_overlay(sid, overlay)
            return {"ok": True, "space_id": self._engine.realm_space_id(sid), "overlay": ov}

        if clean_op == "poll_events":
            kinds = data.get("kinds")
            after_seq = int(data.get("after_seq") or 0)
            events = self._engine.poll_events(
                after_seq=after_seq,
                limit=int(data.get("limit") or 100),
                realm_id=str(data.get("realm_id") or data.get("space_id") or ""),
                kinds=kinds if isinstance(kinds, (list, tuple, set)) else None,
            )
            next_seq = max((int(ev.get("seq") or 0) for ev in events), default=after_seq)
            return {"ok": True, "events": events, "next_seq": next_seq}

        if clean_op == "query_world_state":
            sid = str(data.get("space_id") or "").strip()
            compact = data.get("compact") is True
            with self._engine._lock:
                world = self._engine._world
                if compact:
                    eff_sid = sid or world["active_space_id"]
                    if eff_sid not in world["spaces"]:
                        eff_sid = next(iter(world["spaces"]))
                    realm_sid = self._engine._realm_space_id_locked(eff_sid)
                    realm_space_ids = {sp_id for sp_id in world["spaces"] if self._engine._realm_space_id_locked(sp_id) == realm_sid}
                    snap = {
                        "active_actor_id": self._engine.resolve_actor_id(space_id=eff_sid),
                        "spaces": [
                            {
                                **{
                                    key: copy.deepcopy(sp[key])
                                    for key in (
                                        "space_id", "name", "parent_space_id", "hub_location_id",
                                        "space_kind", "buildings", "floor_index", "floor_label", "building_group_id",
                                    )
                                    if key in sp
                                },
                                "zones": [
                                    {
                                        "zone_id": str(z.get("zone_id") or ""),
                                        "name": str(z.get("name") or ""),
                                        "role": str(z.get("role") or ""),
                                        "x": int(z.get("x") or 0),
                                        "y": int(z.get("y") or 0),
                                        "w": int(z.get("w") or 0),
                                        "h": int(z.get("h") or 0),
                                        "enclosed": bool(z.get("enclosed", False)),
                                    }
                                    for z in (sp.get("zones") or ())
                                    if isinstance(z, Mapping)
                                ],
                                "realm_space_id": realm_sid,
                            }
                            for sp_id, sp in world["spaces"].items() if sp_id in realm_space_ids
                        ],
                    }
                    snap["space"] = next((sp for sp in snap["spaces"] if sp["space_id"] == eff_sid), {})
                    raw_actors = []
                    for actor in world["actors"].values():
                        if actor["space_id"] not in realm_space_ids:
                            continue
                        a_row = {
                            key: actor.get(key)
                            for key in (
                                "actor_id", "name", "host_character", "space_id", "x", "y",
                                "actor_status", "posture", "holding_id", "is_player", "is_native_npc",
                            )
                        }
                        sp_obj = world["spaces"].get(actor["space_id"]) or {}
                        z_hit = self._engine.get_zone_at(sp_obj, actor.get("x", 0), actor.get("y", 0)) if sp_obj else None
                        a_row["zone_id"] = str((z_hit or {}).get("zone_id") or "")
                        a_row["zone_name"] = str((z_hit or {}).get("name") or "")
                        raw_actors.append(a_row)
                else:
                    snap = self._engine.snapshot_for_ui(sid or None)
                    eff_sid = str(snap.get("active_space_id") or "")
                    realm_sid = self._engine.realm_space_id(eff_sid) if eff_sid else ""
                    raw_actors = self._engine.list_actors()
                    realm_space_ids = {sp["space_id"] for sp in snap.get("spaces", ()) if sp.get("realm_space_id") == realm_sid}
                sim_min = float(world.get("simulation_minutes") or 0.0)
                realm_entities = [
                    {
                        "entity_id": e["entity_id"],
                        "name": e["name"],
                        "blueprint_id": e.get("blueprint_id") or "",
                        "space_id": e.get("space_id") or "",
                        "parent_id": e.get("parent_id") or "",
                        "x": int(e.get("x") or 0),
                        "y": int(e.get("y") or 0),
                        "tags": list(e.get("tags") or ()),
                        "z": int(e.get("z") or 0),
                        "traits": copy.deepcopy(e.get("traits") or {}),
                        "brief": str(e.get("brief") or "")[:160],
                        "detail": str(e.get("detail") or "")[:240],
                        "state_tags": self._engine.entity_state_tags(e),
                        **({"sprite": copy.deepcopy(e["sprite"])} if not compact and e.get("ephemeral_realm_id") else {}),
                    }
                    for e in world["entities"].values()
                    if e.get("space_id") in realm_space_ids
                ]
                realm_buildings = self._engine._realm_buildings_locked(realm_sid) if realm_sid else []
                weather_view = self._engine._weather_view_locked(eff_sid) if eff_sid else {}
                lighting_full = self._engine.get_lighting(eff_sid) if eff_sid else {}
                lighting_summary = {
                    "period": str(lighting_full.get("period") or ""),
                    "is_dark": bool(lighting_full.get("is_dark", False)),
                }
            return {
                "ok": True,
                "active_space_id": eff_sid,
                "active_actor_id": snap.get("active_actor_id") or "",
                "realm_space_id": realm_sid,
                "opaque_minds": self._engine.is_opaque_realm(eff_sid) if eff_sid else False,
                "isolated_persona": self._engine.is_isolated_persona_realm(eff_sid) if eff_sid else False,
                "spaces": snap.get("spaces") or [],
                "space": snap.get("space") or {},
                "entities": snap.get("entities") or [],
                "realm_entities": realm_entities,
                "realm_buildings": realm_buildings,
                "actors": raw_actors,
                "host_characters": snap.get("host_characters") or [],
                "overlay": self._engine.get_external_overlay(eff_sid) if eff_sid else None,
                "weather": weather_view,
                "lighting": lighting_summary,
                "minutes": sim_min,
            }

        if clean_op == "list_realms":
            return {"ok": True, "realms": self._engine.list_realms_summary()}

        if clean_op == "list_blueprints":
            bps = self._engine.all_blueprints(str(data.get("space_id") or "") or None)
            out_bps: dict[str, dict[str, Any]] = {}
            for bp_id, bp in bps.items():
                out_bps[bp_id] = {
                    "blueprint_id": bp_id,
                    "name": str(bp.get("name") or bp_id),
                    "brief": str(bp.get("brief") or ""),
                    "tags": list(bp.get("tags") or ()),
                    "traits": list((bp.get("traits") or {}).keys()),
                }
            return {"ok": True, "blueprints": out_bps}

        raise ValueError(f"不支持的桥接操作：{clean_op}")

    def _use_compiler_llm(self, data: Mapping[str, Any]) -> bool:
        if data.get("use_llm") is not None:
            return data["use_llm"] is True
        settings = self._settings_provider() if self._settings_provider else None
        return bool(getattr(settings, "auto_compile_llm", False))

    def _synthesize_entity(self, data: Mapping[str, Any]) -> dict[str, Any]:
        space_id = str(data.get("space_id") or "").strip()
        prompt = str(data.get("prompt_text") or "").strip()
        if not space_id or not prompt:
            raise ValueError("synthesize_entity 需要提供 space_id 与 prompt_text。")
        hints = data.get("traits_hint")
        hints = hints if isinstance(hints, Mapping) else {}
        try:
            result = text_compiler.compile_item_from_text(
                prompt, preferred_size=(16, 16), use_llm=self._use_compiler_llm(data),
            )
        except (TypeError, ValueError, OverflowError):
            result = text_compiler.compile_item_from_text(prompt, preferred_size=(16, 16), use_llm=False)
        bp = result["blueprint"]
        sprite = bp["sprite"]
        frames = {key: normalize_pixel_grid(rows, 16, 16, auto_outline=False)
                  for key, rows in sprite.get("frames", {}).items()}
        if not any(set("".join(rows)) - {"."} for rows in frames.values()):
            sprite = text_compiler.compile_item_from_text(prompt, use_llm=False)["blueprint"]["sprite"]
            frames = sprite["frames"]
        bp["sprite"] = {"mode": "pixel_grid", "w": 16, "h": 16,
                        "frames": frames, "slot_colors": sprite.get("slot_colors")}
        bp["footprint"] = [1, 1]
        traits = bp["traits"]
        # 生成物的空间连接、拾取授权与锁定状态由调用方决定。
        traits.pop("portal", None)
        for key in ("container", "thermal", "inscribable"):
            if hints.get(key) is False:
                traits.pop(key, None)
            elif hints.get(key) is True or isinstance(hints.get(key), Mapping):
                traits[key] = {**traits.get(key, {}), **(hints[key] if isinstance(hints[key], Mapping) else {})}
        locked = data.get("locked", hints.get("locked"))
        key_tag = str(data.get("key_tag", hints.get("key_tag")) or "").strip().lower()[:32]
        if locked is True or "container" in traits:
            container = traits.setdefault("container", {})
            if locked is not None:
                container["is_locked"] = locked is True
                if locked:
                    container["is_open"] = False
            if key_tag:
                container["key_tag"] = key_tag
        elif key_tag:
            bp["tags"] = [*bp["tags"][:5], key_tag]
        if "inscribable" in traits and "writable_by_ai" in data:
            traits["inscribable"]["writable_by_ai"] = data["writable_by_ai"] is True
        if data.get("collectible") is True:
            traits["portable"] = {"weight": "light"}
            bp.update(solid=False, placement="any")
        else:
            traits.pop("portable", None)
        for source, target in (("custom_name", "name"), ("brief", "brief"), ("detail", "detail")):
            if data.get(source) is not None:
                bp[target] = str(data[source])
        placed = self._engine.place_ephemeral_entity(
            space_id, bp, x=int(data["x"]) if data.get("x") is not None else None,
            y=int(data["y"]) if data.get("y") is not None else None,
            container_entity_id=str(data.get("container_entity_id") or ""),
            placement_strategy=str(data.get("placement_strategy") or "spread"),
            zone_id=str(data.get("zone_id") or ""),
        )
        return {"ok": True, "entity_id": placed["entity_id"], "entity": placed, "used_llm": result["used_llm"]}


_REGISTRY_LOCK = threading.RLock()
_WORLD_BRIDGES: dict[str, WorldBridge] = {}


def register_world_bridge(bridge: WorldBridge, *, key: str = "default") -> WorldBridge:
    clean_key = str(key or "default").strip() or "default"
    with _REGISTRY_LOCK:
        _WORLD_BRIDGES[clean_key] = bridge
    return bridge


def unregister_world_bridge(key: str = "default") -> None:
    clean_key = str(key or "default").strip() or "default"
    with _REGISTRY_LOCK:
        _WORLD_BRIDGES.pop(clean_key, None)


def get_world_bridge(key: str = "default") -> WorldBridge | None:
    clean_key = str(key or "default").strip() or "default"
    with _REGISTRY_LOCK:
        return _WORLD_BRIDGES.get(clean_key)
