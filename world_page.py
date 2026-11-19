"""Shinsekai 前端页、设置页与聊天工具条按钮注册（React/Tauri iframe 合规）。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from . import persona_bridge, projector, text_compiler

logger = logging.getLogger(__name__)

CANVAS_PAGE_ID = "world_canvas"
SETTINGS_PAGE_ID = "world_settings"


def _err(msg: str) -> dict[str, Any]:
    return {"ok": False, "error": str(msg or "操作失败")}


def _ok_with_snap(
    engine: Any,
    extra: dict[str, Any] | None = None,
    *,
    space_id: str = "",
    world_chat: Any = None,
    custom_persona_dir: str = "",
) -> dict[str, Any]:
    snap = engine.snapshot_for_ui(space_id)
    eff_sid = str(snap.get("active_space_id") or space_id or "room_default").strip() or "room_default"
    aid = snap.get("active_actor_id") or ""
    if aid:
        act = next((a for a in (snap.get("actors") or []) if a.get("actor_id") == aid), None)
        if act:
            host_char = str(act.get("host_character") or act.get("name") or "").strip()
            all_names = [
                str(a.get("host_character") or a.get("name") or "").strip()
                for a in (snap.get("actors") or [])
                if a.get("actor_id") != aid
            ]
            for hc in engine.list_host_characters():
                cname = str(hc.get("name") or "").strip()
                if cname and cname not in all_names:
                    all_names.append(cname)
            snap["actor_relations"] = persona_bridge.read_actor_relations(
                engine.project_root,
                host_char,
                space_id=eff_sid,
                world_data_root=engine.data_root,
                custom_persona_dir=custom_persona_dir,
                known_actors=all_names,
            )
        else:
            snap["actor_relations"] = {}
    else:
        snap["actor_relations"] = {}

    terminal = projector.project_terminal_data(engine, aid, consume_diffs=False) if aid else {"text": "（尚未导入角色）", "anchors": []}
    chat_history = world_chat.get_history(aid, space_id=eff_sid) if (world_chat is not None and aid) else []
    out: dict[str, Any] = {
        "ok": True,
        "snapshot": snap,
        "terminal": terminal,
        "chat_history": chat_history,
    }
    if extra:
        out.update(extra)
    return out


def register_pages(
    register: Any,
    *,
    code_dir: Path,
    engine: Any,
    settings_store: Any,
    heartbeat: Any,
    world_chat: Any = None,
    submit_user_input: Callable[[str], bool] | None = None,
) -> None:
    """向宿主注册像素文字世界页面、动作 RPC、设置页与聊天工具条按钮。"""
    from sdk.types import (
        FrontendChatUIContribution,
        FrontendConfigAction,
        FrontendConfigContribution,
        FrontendPageContribution,
    )

    def _safe_run(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        def _runner(values: Mapping[str, Any]) -> dict[str, Any]:
            try:
                return fn(dict(values or {}))
            except ValueError as exc:
                return _err(str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.exception("nanami_world: 前端动作执行异常")
                return _err(f"{type(exc).__name__}: {exc}")

        return _runner

    def _snap_ok(extra: dict[str, Any] | None = None, *, space_id: str = "") -> dict[str, Any]:
        cfg = settings_store.get()
        return _ok_with_snap(
            engine,
            extra,
            space_id=space_id,
            world_chat=world_chat,
            custom_persona_dir=str(getattr(cfg, "persona_data_dir", "") or ""),
        )

    def _ensure_default_host_actor() -> None:
        if engine.list_actors():
            return
        hosts = engine.list_host_characters()
        if not hosts:
            return
        chosen = next((h for h in hosts if h.get("name") == "七海千秋"), None)
        if chosen is None:
            chosen = next((h for h in hosts if h.get("pack_sprites")), None) or hosts[0]
        cname = str(chosen.get("name") or "").strip()
        if not cname:
            return
        try:
            engine.import_actor(name=cname, host_character=cname)
        except Exception:  # noqa: BLE001
            logger.debug("nanami_world: 自动导入默认宿主角色失败", exc_info=True)

    # ── 画布页 RPC Actions ──────────────────────────────────────────────
    def _act_get_snapshot(v: dict[str, Any]) -> dict[str, Any]:
        _ensure_default_host_actor()
        return _snap_ok(space_id=str(v.get("space_id") or ""))

    def _act_place_from_bp(v: dict[str, Any]) -> dict[str, Any]:
        bp_id = str(v.get("blueprint_id") or "")
        sid = str(v.get("space_id") or "")
        x = int(v.get("x") or 0)
        y = int(v.get("y") or 1)
        mode = str(v.get("delivery_mode") or "silent")
        note = str(v.get("note_text") or "")
        portal_tgt = str(v.get("portal_target_space_id") or "")
        ent = engine.place_from_blueprint(
            bp_id,
            sid,
            x,
            y,
            delivery_mode=mode,
            note_text=note,
            portal_target_space_id=portal_tgt,
        )
        submitted = False
        chat_result: dict[str, Any] | None = None
        if mode == "give":
            msg = f"（我把「{ent['name']}」递给了你：{ent['brief']}）"
            if note.strip():
                msg = f"（我把「{ent['name']}」递给了你，上面写着「{note.strip()[:60]}」）"
            if submit_user_input is not None:
                submitted = bool(submit_user_input(msg))
            aid = str(v.get("actor_id") or "") or engine.resolve_actor_id()
            if world_chat is not None and aid:
                try:
                    chat_result = world_chat.chat(aid, msg)
                except Exception:  # noqa: BLE001
                    logger.debug("nanami_world: give 物品触发世界对话失败", exc_info=True)
        heartbeat.step(advance_minutes=0.0)
        extra: dict[str, Any] = {"entity": ent, "submitted_chat": submitted}
        if chat_result is not None:
            extra["chat_result"] = chat_result
        return _snap_ok(extra, space_id=ent["space_id"])

    def _act_move_entity(v: dict[str, Any]) -> dict[str, Any]:
        ent = engine.move_entity_ui(
            str(v.get("entity_id") or ""),
            int(v.get("x") or 0),
            int(v.get("y") or 1),
            str(v.get("target_parent_id") or ""),
        )
        return _snap_ok({"entity": ent}, space_id=ent["space_id"])

    def _act_remove_entity(v: dict[str, Any]) -> dict[str, Any]:
        res = engine.remove_entity(str(v.get("entity_id") or ""))
        return _snap_ok(res)

    def _act_update_entity_spec(v: dict[str, Any]) -> dict[str, Any]:
        ent = engine.update_entity_spec(
            str(v.get("entity_id") or ""),
            name=v.get("name"),
            brief=v.get("brief"),
            detail=v.get("detail"),
            traits=v.get("traits") if isinstance(v.get("traits"), Mapping) else None,
            portal_target_space_id=v.get("portal_target_space_id"),
        )
        return _snap_ok({"entity": ent}, space_id=ent["space_id"])

    def _act_entity_ui_interact(v: dict[str, Any]) -> dict[str, Any]:
        eid = str(v.get("entity_id") or "")
        action = str(v.get("action") or "").strip()
        aid = str(v.get("actor_id") or "") or engine.resolve_actor_id()
        if action == "write_user_note":
            if not aid:
                raise ValueError("请先导入一个角色，或先落格后再操作。")
            feedback = engine.execute_write(aid, eid, str(v.get("text") or ""), author="你")
        else:
            if not aid:
                raise ValueError("请先导入一个角色，才能在画布里演示交互动作。")
            feedback = engine.execute_act(aid, eid, action, str(v.get("target_id") or ""))
        return _snap_ok({"feedback": feedback})

    def _act_compile_text_bp(v: dict[str, Any]) -> dict[str, Any]:
        cfg = settings_store.get()
        pw = int(v.get("width") or 16)
        ph = int(v.get("height") or 16)
        res = text_compiler.compile_item_from_text(
            str(v.get("prompt_text") or ""),
            preferred_size=(pw, ph),
            use_llm=bool(v.get("use_llm", cfg.auto_compile_llm)),
        )
        return {"ok": True, **res}

    def _act_save_blueprint(v: dict[str, Any]) -> dict[str, Any]:
        raw_bp = v.get("blueprint")
        if not isinstance(raw_bp, Mapping):
            raise ValueError("blueprint 必须是对象。")
        bp = engine.save_blueprint(raw_bp)
        return _snap_ok({"blueprint": bp})

    def _act_delete_blueprint(v: dict[str, Any]) -> dict[str, Any]:
        deleted = engine.delete_blueprint(str(v.get("blueprint_id") or ""))
        return _snap_ok({"deleted": deleted})

    def _act_upload_sprite(v: dict[str, Any]) -> dict[str, Any]:
        rel = engine.save_uploaded_sprite(str(v.get("image_data") or ""))
        preview = engine._sprite_with_data_url({"image_path": rel})
        return {"ok": True, "image_path": rel, "image_data_url": preview.get("image_data_url", "")}

    def _act_import_actor(v: dict[str, Any]) -> dict[str, Any]:
        cfg = settings_store.get()
        host_char = str(v.get("host_character") or "").strip()
        name = str(v.get("name") or host_char).strip() or "七海千秋"
        lookup_name = host_char or name
        hosts = engine.list_host_characters()
        matched_host = next((row for row in hosts if row.get("name") == lookup_name), None)
        has_pack = bool(matched_host and matched_host.get("pack_sprites"))

        raw_mode = str(v.get("sprite_mode") or "").strip()
        sprite_mode = raw_mode or ("host_pack" if has_pack else "pixel_grid")
        sprite_prompt = str(v.get("sprite_prompt") or "").strip()
        sprite_image_data = str(v.get("sprite_image_data") or "").strip()
        sprite_payload: dict[str, Any] | None = None

        if sprite_mode == "host_pack":
            sprite_payload = None
        elif sprite_mode == "image" and sprite_image_data:
            rel = engine.save_uploaded_sprite(sprite_image_data)
            sprite_payload = {"mode": "image", "w": 16, "h": 24, "image_path": rel}
        elif sprite_mode == "token":
            sprite_payload = {"mode": "token", "w": 16, "h": 24, "accent_color": str(v.get("accent_color") or "#e8a2b8")}
        else:
            desc = sprite_prompt
            if not desc and matched_host:
                desc = str(matched_host.get("brief") or str(matched_host.get("setting") or "")[:120])
            sprite_payload = text_compiler.compile_actor_sprite_from_text(
                name,
                desc,
                use_llm=bool(v.get("use_llm", cfg.auto_compile_llm)) and bool(sprite_prompt or host_char),
            )

        actor = engine.import_actor(
            name=name,
            host_character=lookup_name,
            actor_id=str(v.get("actor_id") or ""),
            space_id=str(v.get("space_id") or ""),
            x=v.get("x"),
            y=v.get("y"),
            sprite=sprite_payload,
        )
        heartbeat.step(advance_minutes=0.0)
        return _snap_ok({"actor": actor}, space_id=actor["space_id"])

    def _act_place_actor(v: dict[str, Any]) -> dict[str, Any]:
        actor = engine.place_actor(
            str(v.get("actor_id") or ""),
            str(v.get("space_id") or ""),
            int(v.get("x") or 0),
            int(v.get("y") or 1),
        )
        return _snap_ok({"actor": actor}, space_id=actor["space_id"])

    def _act_set_active_actor(v: dict[str, Any]) -> dict[str, Any]:
        actor = engine.set_active_actor(str(v.get("actor_id") or ""))
        return _snap_ok({"actor": actor}, space_id=actor["space_id"])

    def _act_remove_actor(v: dict[str, Any]) -> dict[str, Any]:
        res = engine.remove_actor(str(v.get("actor_id") or ""))
        return _snap_ok({"removed": res})

    def _act_trigger_social_talk(v: dict[str, Any]) -> dict[str, Any]:
        if world_chat is None:
            raise ValueError("世界对话服务未就绪。")
        req_sid = str(v.get("space_id") or "").strip()
        aid = str(v.get("initiator_id") or v.get("actor_id") or "").strip() or engine.resolve_actor_id(space_id=req_sid)
        tid = str(v.get("target_actor_id") or "").strip()
        if not aid or not tid:
            raise ValueError("需要指定参与社交的两位角色。")
        res = world_chat.conduct_actor_social_exchange(aid, tid, reason=str(v.get("reason") or ""))
        return _snap_ok({"social_exchange": res}, space_id=req_sid)

    def _act_set_world_actor_mood(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("space_id") or "").strip()
        aid = str(v.get("actor_id") or "").strip() or engine.resolve_actor_id(space_id=sid)
        if not aid:
            raise ValueError("当前没有活动角色。")
        if not sid:
            act = next((a for a in engine.list_actors() if a.get("actor_id") == aid), None)
            sid = str((act or {}).get("space_id") or "room_default")

        vit_patch: dict[str, Any] = {}
        for key in ("energy", "sleep_pressure", "mood_v"):
            if key in v and v.get(key) not in (None, ""):
                vit_patch[key] = float(v[key])
        if "agenda" in v and v.get("agenda") is not None:
            vit_patch["agenda"] = str(v.get("agenda") or "")
        if "disengaged" in v and v.get("disengaged") is not None:
            vit_patch["disengaged"] = bool(v.get("disengaged"))

        aut_patch: dict[str, Any] = {}
        for key in ("hunger", "curiosity"):
            if key in v and v.get(key) not in (None, ""):
                aut_patch[key] = float(v[key])
        for key in ("current_goal", "current_drive", "last_thought"):
            if key in v and v.get(key) is not None:
                aut_patch[key] = str(v.get(key) or "")
        if "enabled" in v and v.get("enabled") is not None:
            aut_patch["enabled"] = bool(v.get("enabled"))

        updated = engine.set_actor_world_mood(
            aid,
            space_id=sid,
            vitals_patch=vit_patch or None,
            autonomy_patch=aut_patch or None,
        )
        return _snap_ok({"actor": updated}, space_id=sid)

    def _act_save_world_relation(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("space_id") or "").strip()
        if not sid:
            sid = str(engine.snapshot_for_ui().get("active_space_id") or "room_default")
        subj = str(v.get("subject_character") or "").strip()
        if not subj:
            aid = str(v.get("actor_id") or "").strip() or engine.resolve_actor_id(space_id=sid)
            act = next((a for a in engine.list_actors() if a.get("actor_id") == aid), None)
            if act:
                subj = str(act.get("host_character") or act.get("name") or "").strip()
        target_key = str(v.get("target_key") or "").strip()
        if not subj or not target_key:
            raise ValueError("缺少主体角色名称或目标对象标识（target_key）。")

        patch: dict[str, Any] = {}
        if "known" in v and v.get("known") is not None:
            patch["known"] = bool(v["known"])
        for key in ("rel_stage", "attitude_stance", "call_name", "relation_tag", "shared_memories"):
            if key in v and v.get(key) is not None:
                patch[key] = str(v.get(key) or "")
        for key in ("affective_valence", "trust", "warmth", "social_willingness", "repulsion"):
            if key in v and v.get(key) not in (None, ""):
                patch[key] = float(v[key])
        if "cool_off_turns" in v and v.get("cool_off_turns") not in (None, ""):
            patch["cool_off_turns"] = int(v["cool_off_turns"])

        edge = persona_bridge.save_world_actor_relation(
            engine.data_root,
            sid,
            subj,
            target_key,
            patch,
        )
        return _snap_ok({"relation": edge, "space_id": sid}, space_id=sid)

    def _act_create_space(v: dict[str, Any]) -> dict[str, Any]:
        cfg = settings_store.get()
        sp = engine.create_space(
            name=str(v.get("name") or "新房间"),
            width=int(v.get("width") or cfg.default_space_width),
            height=int(v.get("height") or cfg.default_space_height),
            ambient_phrase=str(v.get("ambient_phrase") or ""),
            palette_id=str(v.get("palette_id") or "warm_stardew"),
        )
        return _snap_ok({"space": sp}, space_id=sp["space_id"])

    def _act_switch_space(v: dict[str, Any]) -> dict[str, Any]:
        sp = engine.switch_space(str(v.get("space_id") or ""))
        return _snap_ok({"space": sp}, space_id=sp["space_id"])

    def _act_delete_space(v: dict[str, Any]) -> dict[str, Any]:
        res = engine.delete_space(str(v.get("space_id") or ""))
        return _snap_ok({"deleted": res}, space_id=str(res.get("active_space_id") or ""))

    def _act_save_palette(v: dict[str, Any]) -> dict[str, Any]:
        raw_pal = v.get("palette")
        if not isinstance(raw_pal, Mapping):
            raise ValueError("palette 必须是对象。")
        pal = engine.save_palette(raw_pal)
        apply_sid = str(v.get("apply_to_space_id") or "").strip()
        if apply_sid:
            engine.set_space_palette(apply_sid, pal["palette_id"])
        return _snap_ok({"palette": pal}, space_id=apply_sid)

    def _act_set_space_palette(v: dict[str, Any]) -> dict[str, Any]:
        sp = engine.set_space_palette(str(v.get("space_id") or ""), str(v.get("palette_id") or ""))
        return _snap_ok({"space": sp}, space_id=sp["space_id"])

    def _act_tick_world(v: dict[str, Any]) -> dict[str, Any]:
        adv = v.get("advance_minutes")
        res = heartbeat.step(
            advance_minutes=None if adv is None else float(adv),
            force_autonomy=bool(v.get("force_autonomy", True)),
            use_llm=v.get("use_llm"),
            actor_id=str(v.get("actor_id") or ""),
        )
        return _snap_ok({"tick_result": res})

    def _act_autonomous_step(v: dict[str, Any]) -> dict[str, Any]:
        _ensure_default_host_actor()
        adv = float(v.get("advance_minutes") if v.get("advance_minutes") is not None else 10.0)
        res = heartbeat.step(
            advance_minutes=adv,
            force_autonomy=True,
            use_llm=v.get("use_llm"),
            actor_id=str(v.get("actor_id") or "") or engine.resolve_actor_id(),
        )
        return _snap_ok({"tick_result": res})

    def _act_apply_starter(v: dict[str, Any]) -> dict[str, Any]:
        placed = engine.apply_starter_pack(str(v.get("space_id") or ""))
        return _snap_ok({"placed_count": len(placed)})

    def _act_clear_space(v: dict[str, Any]) -> dict[str, Any]:
        res = engine.clear_space(str(v.get("space_id") or ""))
        return _snap_ok(res)

    def _act_chat_in_world(v: dict[str, Any]) -> dict[str, Any]:
        _ensure_default_host_actor()
        req_sid = str(v.get("space_id") or "").strip()
        aid = str(v.get("actor_id") or "") or engine.resolve_actor_id(space_id=req_sid)
        if not aid:
            raise ValueError("当前房间内还没有角色，请先导入角色。")
        if world_chat is None:
            raise ValueError("世界对话服务尚未就绪。")
        text = str(v.get("text") or "").strip()
        if not text:
            raise ValueError("请输入想对她说的话。")
        res = world_chat.chat(aid, text, space_id=req_sid)
        return _snap_ok({"chat_result": res}, space_id=req_sid)

    def _act_clear_world_chat(v: dict[str, Any]) -> dict[str, Any]:
        req_sid = str(v.get("space_id") or "").strip()
        aid = str(v.get("actor_id") or "") or engine.resolve_actor_id(space_id=req_sid)
        if not aid:
            raise ValueError("当前没有活动角色。")
        if world_chat is not None:
            world_chat.clear_history(aid, space_id=req_sid)
        return _snap_ok({}, space_id=req_sid)

    def _act_set_actor_expression(v: dict[str, Any]) -> dict[str, Any]:
        aid = str(v.get("actor_id") or "") or engine.resolve_actor_id()
        if not aid:
            raise ValueError("当前没有活动角色。")
        mode = str(v.get("mode") or "manual").strip()
        raw_index = v.get("index")
        index = None if raw_index in (None, "") else int(raw_index)
        expr = engine.set_actor_expression(aid, index=index, mode=mode)
        return _snap_ok({"expression": expr})

    def _act_get_actor_expressions(v: dict[str, Any]) -> dict[str, Any]:
        aid = str(v.get("actor_id") or "") or engine.resolve_actor_id()
        if not aid:
            return {"ok": True, "actor_id": "", "gallery": [], "expression": {}, "active_sprite": {}}
        snap = engine.snapshot_for_ui()
        actor = next((a for a in snap.get("actors", []) if a.get("actor_id") == aid), None)
        gallery = engine.get_actor_expression_gallery(aid)
        return {
            "ok": True,
            "actor_id": aid,
            "gallery": gallery,
            "expression": (actor or {}).get("expression") or {},
            "active_sprite": (actor or {}).get("active_sprite") or {},
        }

    def _act_paint_tiles(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("space_id") or "")
        cells = v.get("cells") if isinstance(v.get("cells"), list) else None
        rect = v.get("rect") if isinstance(v.get("rect"), Mapping) else None
        res = engine.paint_tiles(
            sid,
            cells,
            rect=rect,
            auto_expand=bool(v.get("auto_expand", False)),
            demolish=bool(v.get("demolish", False)),
        )
        return _snap_ok({"painted": res}, space_id=res["space_id"])

    def _act_expand_space(v: dict[str, Any]) -> dict[str, Any]:
        sp = engine.expand_space(
            str(v.get("space_id") or ""),
            str(v.get("direction") or "east"),
            int(v.get("amount") or 4),
            fill_tile_id=str(v.get("fill_tile_id") or ""),
        )
        return _snap_ok({"space": sp}, space_id=sp["space_id"])

    def _act_generate_world_map(v: dict[str, Any]) -> dict[str, Any]:
        cfg = settings_store.get()
        prompt_text = str(v.get("prompt_text") or "").strip()
        w = int(v.get("width") or 16)
        h = int(v.get("height") or 12)
        use_llm = bool(v.get("use_llm", cfg.auto_compile_llm))
        mode = str(v.get("mode") or "new_space").strip()
        target_sid = str(v.get("space_id") or "")
        compiled = text_compiler.compile_map_from_text(
            prompt_text,
            width=w,
            height=h,
            use_llm=use_llm,
            available_blueprints=engine.all_blueprints(),
        )
        applied = engine.apply_generated_map(
            compiled["map_spec"],
            mode=mode,
            target_space_id=target_sid,
        )
        heartbeat.step(advance_minutes=0.0)
        return _snap_ok(
            {
                "generated": applied,
                "used_llm": compiled["used_llm"],
                "note": compiled["note"],
            },
            space_id=applied["space"]["space_id"],
        )

    action_specs: list[tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]]] = [
        ("get_world_snapshot", "刷新世界快照", _act_get_snapshot),
        ("place_from_blueprint", "放入物品", _act_place_from_bp),
        ("move_entity", "移动物品", _act_move_entity),
        ("remove_entity", "收走物品", _act_remove_entity),
        ("update_entity_spec", "更新物品规范", _act_update_entity_spec),
        ("entity_ui_interact", "物品交互", _act_entity_ui_interact),
        ("compile_text_blueprint", "文字编译物品", _act_compile_text_bp),
        ("save_blueprint", "保存素材蓝图", _act_save_blueprint),
        ("delete_blueprint", "删除自定义蓝图", _act_delete_blueprint),
        ("upload_sprite", "上传静态贴图", _act_upload_sprite),
        ("import_actor", "导入角色", _act_import_actor),
        ("place_actor", "移动角色落格", _act_place_actor),
        ("set_active_actor", "切换观察角色", _act_set_active_actor),
        ("remove_actor", "移除角色", _act_remove_actor),
        ("trigger_social_talk", "同屋角色交流", _act_trigger_social_talk),
        ("set_world_actor_mood", "调整本世界心情", _act_set_world_actor_mood),
        ("save_world_relation", "保存本世界社交指针", _act_save_world_relation),
        ("create_space", "新建空间", _act_create_space),
        ("switch_space", "切换空间", _act_switch_space),
        ("delete_space", "删除世界", _act_delete_space),
        ("save_palette", "保存调色板", _act_save_palette),
        ("set_space_palette", "切换空间调色板", _act_set_space_palette),
        ("tick_world", "推进世界时钟", _act_tick_world),
        ("autonomous_step", "触发角色自主行动", _act_autonomous_step),
        ("apply_starter_pack", "放入基础测试包", _act_apply_starter),
        ("clear_space", "清空当前空间", _act_clear_space),
        ("chat_in_world", "在世界中对话", _act_chat_in_world),
        ("clear_world_chat", "清空世界对话", _act_clear_world_chat),
        ("set_actor_expression", "切换角色表情立绘", _act_set_actor_expression),
        ("get_actor_expressions", "获取角色全部立绘", _act_get_actor_expressions),
        ("paint_tiles", "搭建或拆除地形方块", _act_paint_tiles),
        ("expand_space", "无限拓展地图边界", _act_expand_space),
        ("generate_world_map", "LLM 生成像素地图", _act_generate_world_map),
    ]

    actions = [
        FrontendConfigAction(
            id=aid,
            label=label,
            variant="ghost",
            order=float(idx + 1),
            run=_safe_run(fn),
        )
        for idx, (aid, label, fn) in enumerate(action_specs)
    ]

    def _load_canvas_summary() -> dict[str, Any]:
        snap = engine.snapshot_for_ui()
        return {
            "active_space_id": snap["active_space_id"],
            "active_actor_id": snap["active_actor_id"],
            "space_count": len(snap["spaces"]),
            "actor_count": len(snap["actors"]),
            "entity_count": len(snap["entities"]),
        }

    world_html = code_dir / "web" / "world.html"
    if world_html.is_file() and hasattr(register, "register_frontend_page"):
        register.register_frontend_page(
            FrontendPageContribution(
                page_id=CANVAS_PAGE_ID,
                title="文字世界",
                entry=str(world_html),
                kind="tools",
                description="",
                order=65.0,
            )
        )

    register.register_frontend_config_page(
        FrontendConfigContribution(
            page_id=CANVAS_PAGE_ID,
            title="文字世界",
            kind="tools",
            description="像素风自定义文字世界画布、以文生像素造物台与「她的视角」终端。",
            schema=[],
            load_values=_load_canvas_summary,
            save_values=lambda _v: None,
            actions=actions,
            order=65.0,
        )
    )

    # ── 标准设置页：world_settings ──────────────────────────────────────
    settings_schema = [
        {
            "id": "runtime",
            "title": "世界运行时开关",
            "fields": [
                {"key": "enabled", "type": "boolean", "label": "启用文字世界插件", "description": "关闭后不再向对话注入世界视野与工具（需重启或重载）"},
                {"key": "inject_world_prompt", "type": "boolean", "label": "对话中注入【世界现实】视野", "description": "每轮把当前房间坐标、可碰物品与变化白描送入角色上下文"},
                {"key": "inject_world_tools", "type": "boolean", "label": "开放世界交互工具（world_*）", "description": "允许角色通过受控工具拿取、放下、开关、使用物品或走动"},
                {"key": "auto_compile_llm", "type": "boolean", "label": "文字造物使用文本大模型", "description": "关闭时仅用纯规则关键词生成蓝图与像素框（零 Token）"},
            ],
        },
        {
            "id": "heartbeat",
            "title": "离线心跳与联动",
            "fields": [
                {"key": "heartbeat_enabled", "type": "boolean", "label": "开启后台心跳推进", "description": "按真实时间自动推进热饮降温、昼夜光影与低精力自主休息"},
                {"key": "heartbeat_interval_sec", "type": "integer", "label": "心跳间隔（秒）", "min": 30, "max": 3600, "step": 30},
                {"key": "heartbeat_use_llm", "type": "boolean", "label": "自主心跳使用大模型自由意志", "description": "开启后心跳步进优先让大模型决定她想做什么（关闭时用 0 Token 十类内心驱力规则）"},
                {"key": "allow_ai_diary", "type": "boolean", "label": "允许离线在笔记本自主写随手记", "description": "仅当房间内放有可写笔记本且触发关键状态变化时低频写一句"},
                {"key": "diary_cooldown_hours", "type": "number", "label": "自主写字冷却时长（小时）", "min": 1.0, "max": 72.0, "step": 0.5},
                {"key": "persona_data_dir", "type": "text", "label": "人格模型数据目录（可选覆盖）", "placeholder": "留空自动探测 data/nanami"},
            ],
        },
        {
            "id": "space_defaults",
            "title": "新建空间默认尺寸",
            "fields": [
                {"key": "default_space_width", "type": "integer", "label": "默认格网宽度（8–64）", "min": 8, "max": 64, "step": 1},
                {"key": "default_space_height", "type": "integer", "label": "默认格网高度（6–64）", "min": 6, "max": 64, "step": 1},
            ],
        },
    ]

    register.register_frontend_config_page(
        FrontendConfigContribution(
            page_id=SETTINGS_PAGE_ID,
            title="文字世界设置",
            kind="settings",
            description="配置文字世界视野注入、受控交互工具、离线心跳与人格插件文件桥路径。",
            schema=settings_schema,
            load_values=lambda: settings_store.get().to_dict(),
            save_values=lambda values: settings_store.update(values),
            order=65.0,
        )
    )

    # ── 聊天底部工具栏入口按钮（浮层打开）──────────────────────────────────
    if hasattr(register, "register_frontend_chat_ui"):
        try:
            register.register_frontend_chat_ui(
                FrontendChatUIContribution(
                    contribution_id="open_world",
                    slot="chat-toolbar",
                    title="文字世界",
                    description="打开像素风文字世界小屋与「她的视角」终端",
                    icon="sparkles",
                    presentation="button",
                    variant="ghost",
                    order=65.0,
                    action={"type": "open-plugin-page", "page_id": CANVAS_PAGE_ID, "mode": "overlay"},
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("nanami_world: 注册聊天工具条按钮失败", exc_info=True)
