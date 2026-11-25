"""Shinsekai 前端页、设置页与聊天工具条按钮注册（React/Tauri iframe 合规）。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from . import danganronpa_catalog, persona_bridge, projector, schedule, sprite_pack, text_compiler
from .cognitive_engine import cognitive_summary

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
    realm_id = snap["realm_space_id"]
    isolated = snap["isolated_persona"]
    isolated_roots = {sp["space_id"] for sp in snap["spaces"] if sp.get("isolated_persona")}
    if isolated:
        spaces = [sp for sp in snap["spaces"] if sp["realm_space_id"] == realm_id]
    else:
        spaces = [sp for sp in snap["spaces"] if sp["realm_space_id"] not in isolated_roots]
    space_ids = {sp["space_id"] for sp in spaces}
    snap["spaces"] = spaces
    snap["actors"] = [actor for actor in snap["actors"] if actor["space_id"] in space_ids]
    aid = snap.get("active_actor_id") or ""
    if not any(actor["actor_id"] == aid for actor in snap["actors"]):
        aid = snap["active_actor_id"] = ""
    snap["active_cognition"] = {}
    snap["key_loans"] = engine.key_loans_for(aid) if aid else []
    snap["pending_key_loan"] = engine.pending_key_loan_response(aid) if aid else None
    snap["events"] = [
        event for event in snap.get("events", [])
        if event.get("kind") not in {
            "key_lent", "key_returned", "key_return_blocked",
            "key_extension_sent", "key_extension_received", "key_loan_response",
        }
        or aid in (event.get("payload") or {}).get("witness_ids", [])
    ]
    is_opaque = bool(snap.get("opaque_minds"))
    act = None
    if aid:
        act = next((a for a in (snap.get("actors") or []) if a.get("actor_id") == aid), None)
        if act:
            snap["active_cognition"] = {} if (is_opaque and not act.get("is_player")) else cognitive_summary(act)
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
            realm_sid = engine.realm_space_id(eff_sid) if hasattr(engine, "realm_space_id") else eff_sid
            isolated_persona = engine.is_isolated_persona_realm(realm_sid) if hasattr(engine, "is_isolated_persona_realm") else False
            snap["actor_relations"] = persona_bridge.read_actor_relations(
                engine.project_root,
                host_char,
                space_id=realm_sid,
                world_data_root=engine.data_root,
                custom_persona_dir=custom_persona_dir,
                known_actors=all_names,
                isolated_persona=isolated_persona,
            )
            try:
                vis_l0 = engine.visible_entities(aid)
                l0_now = persona_bridge.build_percept_frame(
                    engine.data_root,
                    engine.project_root,
                    snap.get("space") or {"space_id": eff_sid},
                    snap.get("lighting") or {},
                    act,
                    vis_l0,
                    actors_map={a["actor_id"]: a for a in (snap.get("actors") or [])},
                    custom_persona_dir=custom_persona_dir,
                    advance_exposure=0.0,
                    weather=snap.get("weather"),
                )
                snap["active_l0"] = {
                    "enabled": bool(l0_now.get("enabled")),
                    "familiarity": float(l0_now.get("familiarity") or 0.0),
                    "stage": str(l0_now.get("stage") or ""),
                    "stage_cn": str(l0_now.get("stage_cn") or ""),
                    "focused_names": list(l0_now.get("focused_names") or []),
                    "mental_map_count": int(l0_now.get("mental_map_count") or 0),
                    "bio_hour": float(l0_now.get("bio_hour") or 12.0),
                    "chronotype_offset": float(l0_now.get("chronotype_offset") or 0.0),
                    "circadian_strength": float(l0_now.get("circadian_strength") or 0.75),
                    "circadian_phase": str(l0_now.get("circadian_phase") or "daytime"),
                }
                if not act.get("is_player"):
                    realm_sp = next((s for s in (snap.get("spaces") or ()) if s.get("space_id") == realm_sid), {})
                    realm_buildings = realm_sp.get("buildings") or ()
                    home_sid = str(act.get("resolved_home_space_id") or engine.get_actor_home_space(aid, eff_sid))
                    sched = schedule.build_actor_schedule(
                        act,
                        l0_now.get("behavior_profile") or {},
                        l0_now,
                        realm_buildings,
                        home_space_id=home_sid,
                    )
                    s_ctx = schedule.active_schedule_context(
                        sched,
                        float(l0_now.get("bio_hour", 12.0)),
                        circadian_strength=float(sched.get("circadian_strength", 0.75)),
                        effortful_control=float((l0_now.get("behavior_profile") or {}).get("effortful_control", 0.70)),
                    )
                    home_candidates = [
                        {
                            "space_id": str(b.get("interior_space_id") or ""),
                            "name": str(b.get("name") or ""),
                            "category": str(b.get("category") or ""),
                            "icon": str(b.get("icon") or "🏠"),
                        }
                        for b in realm_buildings
                        if str(b.get("interior_space_id") or "").strip()
                    ]
                    snap["active_schedule"] = {
                        **sched,
                        "active_slot_id": str((s_ctx.get("slot") or {}).get("slot_id") or ""),
                        "bio_clock": str(s_ctx.get("bio_clock") or ""),
                        "home_candidates": home_candidates,
                    }
                else:
                    snap["active_schedule"] = {}
            except Exception:
                snap["active_l0"] = {}
                snap["active_schedule"] = {}
        else:
            snap["actor_relations"] = {}
            snap["active_l0"] = {}
            snap["active_schedule"] = {}
    else:
        snap["actor_relations"] = {}
        snap["active_l0"] = {}
        snap["active_schedule"] = {}

    if is_opaque and act and not act.get("is_player"):
        note = str(act.get("status_note") or "").strip()
        status_txt = f"\n状态：{note}" if note else ""
        terminal = {
            "text": f"【{act['name']}】内心想法与私人议程未公开（当前处于暗箱视角）。{status_txt}",
            "anchors": [],
        }
    else:
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
    bridge: Any = None,
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
        merged = dict(extra or {})
        merged.setdefault("pixellab_ready", bool(getattr(cfg, "pixellab_api_key", "").strip()))
        return _ok_with_snap(
            engine,
            merged,
            space_id=space_id,
            world_chat=world_chat,
            custom_persona_dir=str(getattr(cfg, "persona_data_dir", "") or ""),
        )

    _regen_lock = threading.Lock()
    _regen_jobs: dict[str, dict[str, Any]] = {}

    def _act_get_regen_job(v: dict[str, Any]) -> dict[str, Any]:
        jid = str(v.get("job_id") or "").strip()
        with _regen_lock:
            job = dict(_regen_jobs.get(jid) or {})
        if not job:
            return {"ok": False, "done": True, "error": "生图任务不存在或已过期"}
        return job

    def _spawn_bg_job(job_fn: Callable[[Callable[[dict[str, Any]], None]], dict[str, Any]], label: str) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        with _regen_lock:
            _regen_jobs[job_id] = {
                "ok": True,
                "pending": True,
                "done": False,
                "job_id": job_id,
                "label": label,
                "started_ts": time.time(),
            }

        def _update_progress(patch: dict[str, Any]) -> None:
            with _regen_lock:
                cur_j = _regen_jobs.get(job_id)
                if cur_j is not None:
                    cur_j.update(patch)

        def _worker() -> None:
            try:
                res_payload = job_fn(_update_progress)
                res_payload["pending"] = False
                res_payload["done"] = True
                res_payload["job_id"] = job_id
            except Exception as exc:  # noqa: BLE001
                logger.exception("nanami_world: 后台任务异常")
                res_payload = {"ok": False, "pending": False, "done": True, "job_id": job_id, "error": str(exc)}
            with _regen_lock:
                _regen_jobs[job_id] = res_payload
                if len(_regen_jobs) > 24:
                    oldest = next(iter(_regen_jobs))
                    if oldest != job_id:
                        _regen_jobs.pop(oldest, None)

        threading.Thread(target=_worker, name=f"nanami-gen-{job_id}", daemon=True).start()
        return {"ok": True, "pending": True, "done": False, "job_id": job_id, "label": label}

    def _ensure_default_host_actor() -> None:
        if any(not actor.get("is_player") and not engine.is_isolated_persona_realm(actor["space_id"])
               for actor in engine.list_actors()):
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
            desc = (str(chosen.get("brief") or "") + "。" + str(chosen.get("setting") or "")).strip("。")
            app = text_compiler.compile_actor_appearance(cname, desc, use_llm=False)["appearance"]
            snap = engine.snapshot_for_ui()
            sid = snap["active_space_id"]
            if snap["isolated_persona"]:
                sid = next((sp["space_id"] for sp in snap["spaces"]
                            if not engine.is_isolated_persona_realm(sp["space_id"])), "")
            if sid:
                engine.import_actor(name=cname, host_character=cname, appearance=app, space_id=sid)
        except Exception:  # noqa: BLE001
            logger.debug("nanami_world: 自动导入默认宿主角色失败", exc_info=True)

    # ── 画布页 RPC Actions ──────────────────────────────────────────────
    def _act_get_snapshot(v: dict[str, Any]) -> dict[str, Any]:
        if not v.get("strict_space"):
            _ensure_default_host_actor()
        sid = str(v.get("space_id") or "")
        if v.get("strict_space") and (not sid or not engine.is_isolated_persona_realm(sid)):
            raise ValueError("该故事舞台已不可用，请返回故事局列表。")
        if not sid and v.get("sandbox_only"):
            initial = engine.snapshot_for_ui()
            if initial["isolated_persona"]:
                sid = next((sp["space_id"] for sp in initial["spaces"]
                            if not engine.is_isolated_persona_realm(sp["space_id"])), "")
        out = _snap_ok(space_id=sid)
        if world_chat is not None and v.get("roaming"):
            out["player_greeting"] = world_chat.pending_player_greeting(sid)
        return out

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
        if not aid:
            raise ValueError("请先导入一个角色，再在画布上操作。")
        if action == "write_user_note":
            feedback = engine.execute_write(aid, eid, str(v.get("text") or ""), author="你")
        elif action == "invite_puzzle":
            feedback = heartbeat.invite_to_puzzle(aid, str(v.get("target_id") or ""), eid)
        else:
            feedback = engine.execute_act(aid, eid, action, str(v.get("target_id") or ""))
        heartbeat.deliver_persona_events()
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

        def _ref_sprite_path(host_row: Mapping[str, Any] | None) -> Path | None:
            if not host_row:
                return None
            sprites = host_row.get("pack_sprites") or []
            if not sprites:
                return None
            raw_p = str(sprites[0].get("path") or "").strip()
            if not raw_p:
                return None
            p = Path(raw_p)
            if not p.is_absolute():
                p = (engine.project_root / raw_p).resolve()
            return p if p.is_file() else None

        existing_ids = {a["actor_id"] for a in engine.list_actors()}
        req_actor_id = str(v.get("actor_id") or "")
        appearance_payload: dict[str, Any] | None = None
        if isinstance(v.get("appearance"), Mapping):
            appearance_payload = dict(v["appearance"])
        elif (
            (not req_actor_id or req_actor_id not in existing_ids)
            and (sprite_prompt or not hasattr(engine, "has_saved_character_appearance") or not engine.has_saved_character_appearance(lookup_name))
        ):
            app_desc = sprite_prompt
            if not app_desc and matched_host:
                app_desc = (str(matched_host.get("brief") or "") + "。" + str(matched_host.get("setting") or "")).strip("。")
            ref_img = _ref_sprite_path(matched_host)
            appearance_payload = text_compiler.compile_actor_appearance(
                lookup_name,
                app_desc,
                use_llm=bool(v.get("use_llm", cfg.auto_compile_llm)) and bool(app_desc or ref_img),
                use_web=bool(v.get("use_web", False)),
                reference_image_path=ref_img,
            )["appearance"]

        actor = engine.import_actor(
            name=name,
            host_character=lookup_name,
            actor_id=req_actor_id,
            space_id=str(v.get("space_id") or ""),
            x=v.get("x"),
            y=v.get("y"),
            sprite=sprite_payload,
            appearance=appearance_payload,
            map_style=str(v.get("map_style") or "").strip(),
        )
        imp_realm_sid = engine.realm_space_id(actor["space_id"]) if hasattr(engine, "realm_space_id") else actor["space_id"]
        imp_isolated = engine.is_isolated_persona_realm(imp_realm_sid) if hasattr(engine, "is_isolated_persona_realm") else False
        synced = persona_bridge.read_persona_state(
            engine.project_root,
            lookup_name,
            space_id=imp_realm_sid,
            actor_id=actor["actor_id"],
            custom_persona_dir=str(getattr(cfg, "persona_data_dir", "") or ""),
            world_data_root=engine.data_root,
            isolated_persona=imp_isolated,
        )
        if synced is not None:
            engine.set_actor_world_mood(
                actor["actor_id"],
                space_id=actor["space_id"],
                vitals_patch={
                    "mood_v": synced["mood_v"],
                    "agenda": synced["agenda"],
                    "disengaged": synced["disengaged"],
                    "preferred_domains": synced["preferred_domains"],
                    "source": "nanami_persona",
                },
            )
            actor = next((a for a in engine.list_actors() if a["actor_id"] == actor["actor_id"]), actor)
        heartbeat.step(advance_minutes=0.0)
        extra_res: dict[str, Any] = {"actor": actor}
        has_custom = bool(((actor.get("appearance") or {}).get("custom_frames") or {}).get("down"))
        if not has_custom:
            cached_px = sprite_pack.get_cached_sprite(engine.data_root, lookup_name)
            if cached_px is not None:
                extra_res["cached_pixel_data_url"] = cached_px["image_data_url"]
                extra_res["cached_pixel_title"] = cached_px["title"]
        return _snap_ok(extra_res, space_id=actor["space_id"])

    def _act_set_actor_appearance(v: dict[str, Any]) -> dict[str, Any]:
        aid = str(v.get("actor_id") or "").strip() or engine.resolve_actor_id(space_id=str(v.get("space_id") or ""))
        if not aid:
            raise ValueError("当前没有活动角色。")
        raw_app = v.get("appearance")
        actor = engine.set_actor_appearance(
            aid,
            appearance=dict(raw_app) if isinstance(raw_app, Mapping) else None,
            map_style=str(v.get("map_style") or "").strip(),
        )
        cache_img_url = str(v.get("cache_image_data_url") or "").strip()
        if cache_img_url:
            host_key = str(actor.get("host_character") or actor.get("name") or "").strip()
            if host_key:
                try:
                    sprite_pack.save_cached_sprite_data_url(
                        engine.data_root,
                        host_key,
                        cache_img_url,
                        source="manual",
                    )
                except Exception:
                    logger.debug("nanami_world: 自动沉淀手动上传像素图到缓存失败", exc_info=True)
        return _snap_ok({"actor": actor}, space_id=actor["space_id"])

    def _act_regen_actor_appearance(v: dict[str, Any]) -> dict[str, Any]:
        cfg = settings_store.get()
        aid = str(v.get("actor_id") or "").strip() or engine.resolve_actor_id(space_id=str(v.get("space_id") or ""))
        act = next((a for a in engine.list_actors() if a["actor_id"] == aid), None)
        if act is None:
            raise ValueError("当前没有活动角色。")
        host_name = str(act.get("host_character") or act.get("name") or "")
        matched = next((row for row in engine.list_host_characters() if row.get("name") == host_name), None)
        desc = str(v.get("description") or "").strip()
        if not desc and matched:
            desc = (str(matched.get("brief") or "") + "。" + str(matched.get("setting") or "")).strip("。")
        ref_img: Path | None = None
        if matched and matched.get("pack_sprites"):
            raw_p = str(matched["pack_sprites"][0].get("path") or "").strip()
            if raw_p:
                cand = Path(raw_p) if Path(raw_p).is_absolute() else (engine.project_root / raw_p).resolve()
                if cand.is_file():
                    ref_img = cand
        use_web = bool(v.get("use_web", True))
        use_llm = bool(v.get("use_llm", cfg.auto_compile_llm)) and bool(desc or use_web or ref_img)
        gen_mode = str(v.get("gen_mode") or "auto").strip().lower()
        force_refresh = bool(v.get("force_refresh", False))
        pixellab_key = str(v.get("pixellab_api_key") or getattr(cfg, "pixellab_api_key", "") or "").strip()

        def _spawn_async(job_fn: Callable[[], dict[str, Any]], label: str) -> dict[str, Any]:
            if not bool(v.get("async_job", True)):
                return job_fn()
            return _spawn_bg_job(lambda _upd: job_fn(), label)

        # 1. 优先尝试检索弹丸论破原版像素小人（先本地缓存，未命中再多路联网拉取）
        if gen_mode in ("auto", "official") and (use_web or gen_mode == "official"):
            target_label = host_name or act["name"]
            if not force_refresh:
                cached_off = sprite_pack.get_cached_sprite(engine.data_root, target_label)
                if cached_off is not None:
                    return {
                        "ok": True,
                        "used_official_pixel": True,
                        "official_image_data_url": cached_off["image_data_url"],
                        "official_title": cached_off["title"],
                        "english_name": cached_off["english_name"],
                        "from_cache": True,
                    }
            if gen_mode == "official":
                def _run_official() -> dict[str, Any]:
                    reason: dict[str, Any] = {}
                    off = text_compiler.search_official_pixel_sprite(
                        target_label,
                        "",
                        data_root=engine.data_root,
                        force_refresh=force_refresh,
                        reason_out=reason,
                    )
                    if off is None:
                        if reason.get("network_error"):
                            detail = "；".join(reason.get("errors") or [])[:220]
                            raise ValueError(
                                f"访问弹丸论破 Wiki 失败（{detail}），请检查网络或代理；"
                                "也可在「📦 像素图包」导入其他设备导出的 .zip 图包，或改用 PixelLab / 手动上传图片。"
                            )
                        raise ValueError(
                            f"未能在 Wiki 拉取到「{target_label}」的原版像素小人。"
                            "若当前设备无法访问 Fandom Wiki，可在「📦 像素图包」中导入其他设备导出的 .zip 图包，或改用 PixelLab / 本地上传图片。"
                        )
                    return {
                        "ok": True,
                        "used_official_pixel": True,
                        "official_image_data_url": off["image_data_url"],
                        "official_title": off["title"],
                        "english_name": off["english_name"],
                        "from_cache": False,
                    }

                return _spawn_async(_run_official, "检索弹丸原版像素小人中")
            if use_web:
                off = text_compiler.search_official_pixel_sprite(
                    target_label,
                    "",
                    data_root=engine.data_root,
                    force_refresh=force_refresh,
                )
                if off is not None:
                    return {
                        "ok": True,
                        "used_official_pixel": True,
                        "official_image_data_url": off["image_data_url"],
                        "official_title": off["title"],
                        "english_name": off["english_name"],
                        "from_cache": False,
                    }

        # 2. PixelLab 云端 4 向生成（作为备用 / 或显式选择 pixellab）
        if gen_mode == "pixellab" or (gen_mode == "auto" and pixellab_key and bool(v.get("use_pixellab", True))):
            if not pixellab_key:
                raise ValueError("尚未在「文字世界设置」中填写 PixelLab API Key。")

            def _run_pixellab() -> dict[str, Any]:
                pl_res = text_compiler.generate_pixellab_4dir_sprites(
                    host_name or act["name"],
                    desc,
                    api_key=pixellab_key,
                    use_web=use_web,
                    use_llm=use_llm,
                )
                return {
                    "ok": True,
                    "used_pixellab": True,
                    "pixellab_images": pl_res["images"],
                    "used_llm": pl_res["used_llm"],
                    "used_web": pl_res["used_web"],
                    "web_sources": pl_res["web_sources"],
                    "english_name": pl_res["english_name"],
                }

            return _spawn_async(_run_pixellab, "PixelLab 4向生图中")

        res = text_compiler.compile_actor_appearance(
            host_name or act["name"],
            desc,
            use_llm=use_llm,
            use_web=use_web,
            reference_image_path=ref_img,
        )
        return {
            "ok": True,
            "appearance": res["appearance"],
            "used_llm": res["used_llm"],
            "used_web": res.get("used_web", False),
            "web_sources": res.get("web_sources", []),
            "used_vision": res.get("used_vision", False),
            "has_custom_frames": res.get("has_custom_frames", False),
        }

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

    def _act_get_actor_relations(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("space_id") or "").strip()
        if not sid:
            sid = str(engine.snapshot_for_ui().get("active_space_id") or "room_default")
        is_default_bucket = sid == persona_bridge.DEFAULT_RELATIONS_REALM
        if is_default_bucket:
            realm_sid = persona_bridge.DEFAULT_RELATIONS_REALM
            isolated_persona = False
        else:
            realm_sid = engine.realm_space_id(sid) if hasattr(engine, "realm_space_id") else sid
            isolated_persona = engine.is_isolated_persona_realm(realm_sid) if hasattr(engine, "is_isolated_persona_realm") else False

        subj = str(v.get("subject_character") or "").strip()
        aid = str(v.get("actor_id") or "").strip()
        actors_all = engine.list_actors()
        if not subj:
            if not aid:
                aid = engine.resolve_actor_id(space_id="" if is_default_bucket else sid)
            act = next((a for a in actors_all if a.get("actor_id") == aid), None)
            if act:
                subj = str(act.get("host_character") or act.get("name") or "").strip()
        if not subj:
            raise ValueError("缺少主体角色名称或角色 ID。")

        in_realm_names: list[str] = []
        other_actor_names: list[str] = []
        seen_names: set[str] = {subj}
        for a in actors_all:
            if a.get("is_player") or a.get("actor_status", "active") == "missing":
                continue
            cname = str(a.get("host_character") or a.get("name") or "").strip()
            if not cname or cname in seen_names:
                continue
            a_sid = str(a.get("space_id") or "")
            a_realm = engine.realm_space_id(a_sid) if (a_sid and hasattr(engine, "realm_space_id")) else a_sid
            a_iso = engine.is_isolated_persona_realm(a_realm) if (a_realm and hasattr(engine, "is_isolated_persona_realm")) else False
            if not is_default_bucket and a_realm == realm_sid:
                seen_names.add(cname)
                in_realm_names.append(cname)
            elif not a_iso:
                seen_names.add(cname)
                other_actor_names.append(cname)

        host_names: list[str] = []
        for hc in engine.list_host_characters():
            cname = str(hc.get("name") or "").strip()
            if cname and cname not in seen_names:
                seen_names.add(cname)
                host_names.append(cname)

        cfg = settings_store.get()
        relations = persona_bridge.read_actor_relations(
            engine.project_root,
            subj,
            space_id=realm_sid,
            world_data_root=engine.data_root,
            custom_persona_dir=str(getattr(cfg, "persona_data_dir", "") or ""),
            known_actors=in_realm_names + other_actor_names + host_names,
            isolated_persona=isolated_persona,
        )
        return {
            "ok": True,
            "realm_space_id": realm_sid,
            "space_id": sid,
            "subject_character": subj,
            "in_realm_names": in_realm_names,
            "relations": relations,
        }

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

        is_default_bucket = sid == persona_bridge.DEFAULT_RELATIONS_REALM
        if is_default_bucket:
            realm_sid = persona_bridge.DEFAULT_RELATIONS_REALM
        else:
            realm_sid = engine.realm_space_id(sid) if hasattr(engine, "realm_space_id") else sid
        edge = persona_bridge.save_world_actor_relation(
            engine.data_root,
            realm_sid,
            subj,
            target_key,
            patch,
        )
        reverse_edge: dict[str, Any] | None = None
        if bool(v.get("sync_reverse")) and target_key.startswith("char:"):
            other_subj = target_key.split(":", 1)[1].strip()
            if other_subj and other_subj != subj:
                rev_patch = {
                    k: patch[k]
                    for k in (
                        "known",
                        "rel_stage",
                        "attitude_stance",
                        "relation_tag",
                        "shared_memories",
                        "affective_valence",
                        "trust",
                        "warmth",
                        "social_willingness",
                        "repulsion",
                    )
                    if k in patch
                }
                reverse_edge = persona_bridge.save_world_actor_relation(
                    engine.data_root,
                    realm_sid,
                    other_subj,
                    f"char:{subj}",
                    rev_patch,
                )
        extra_res: dict[str, Any] = {"relation": edge, "space_id": sid, "realm_space_id": realm_sid}
        if reverse_edge is not None:
            extra_res["reverse_relation"] = reverse_edge
        return _snap_ok(extra_res, space_id="" if is_default_bucket else sid)

    def _act_create_default_town(v: dict[str, Any]) -> dict[str, Any]:
        _ensure_default_host_actor()
        town_id = str(v.get("town_id") or "town_sakura").strip() or "town_sakura"
        replace = bool(v.get("replace", False))
        place_actor = bool(v.get("place_actor", True))
        req_aid = str(v.get("actor_id") or "").strip() or engine.resolve_actor_id()
        res = engine.create_default_town(
            town_id=town_id,
            replace=replace,
            move_actor_id=req_aid if place_actor else "",
        )
        if not res.get("created") and place_actor and req_aid:
            actors_now = engine.list_actors()
            has_in_realm = any(
                (engine.realm_space_id(str(a.get("space_id") or "")) == town_id)
                for a in actors_now
            )
            if not has_in_realm:
                try:
                    from . import town_builder
                    engine.place_actor(req_aid, town_id, *town_builder.DEFAULT_TOWN_SPAWN)
                except ValueError:
                    pass
        return _snap_ok({"town": res}, space_id=res["town_space_id"])

    def _act_enter_portal(v: dict[str, Any]) -> dict[str, Any]:
        eid = str(v.get("entity_id") or "").strip()
        aid = str(v.get("actor_id") or "").strip()
        res = engine.enter_portal(eid, actor_id=aid, observe_only=bool(v.get("observe_only")))
        heartbeat.deliver_persona_events()
        return _snap_ok(
            {"portal_result": res, "feedback": res.get("feedback", "")},
            space_id=str(res.get("target_space_id") or ""),
        )

    def _act_move_actor(v: dict[str, Any]) -> dict[str, Any]:
        req_sid = str(v.get("space_id") or "").strip()
        aid = str(v.get("actor_id") or "").strip() or engine.resolve_actor_id(space_id=req_sid)
        if not aid:
            raise ValueError("当前世界里还没有角色。")
        act = next((a for a in engine.list_actors() if a.get("actor_id") == aid), None)
        if act is None:
            raise ValueError(f"角色不存在：{aid}")
        if req_sid and act.get("space_id") != req_sid:
            raise ValueError("当前查看的场景里没有选中的角色。")
        feedback = engine.execute_move(aid, int(v.get("x") or 0), int(v.get("y") or 0))
        return _snap_ok({"feedback": feedback}, space_id=req_sid or str(act.get("space_id") or ""))

    def _act_create_building_subworld(v: dict[str, Any]) -> dict[str, Any]:
        town_sid = str(v.get("town_space_id") or v.get("space_id") or "").strip()
        if not town_sid:
            town_sid = str(engine.snapshot_for_ui().get("active_space_id") or "")
        kwargs: dict[str, Any] = {
            "name": str(v.get("name") or "新建筑"),
            "zone_id": str(v.get("zone_id") or ""),
            "ext_x": int(v.get("ext_x") or 2),
            "ext_y": int(v.get("ext_y") or 2),
            "ext_w": int(v.get("ext_w") or 5),
            "ext_h": int(v.get("ext_h") or 4),
            "roof_color": str(v.get("roof_color") or "#a85646"),
            "wall_tile": str(v.get("wall_tile") or "brick_wall"),
            "category": str(v.get("category") or "house"),
            "icon": str(v.get("icon") or "🏠"),
            "interior_width": int(v.get("interior_width") or 12),
            "interior_height": int(v.get("interior_height") or 9),
            "interior_palette": str(v.get("interior_palette") or "warm_stardew"),
            "interior_floor_tile": str(v.get("interior_floor_tile") or "wood_floor"),
            "interior_wall_tile": str(v.get("interior_wall_tile") or "wood_wall"),
            "interior_ambient": str(v.get("interior_ambient") or ""),
        }
        if v.get("door_x") is not None:
            kwargs["door_x"] = int(v["door_x"])
        if v.get("door_y") is not None:
            kwargs["door_y"] = int(v["door_y"])
        if v.get("building_id"):
            kwargs["building_id"] = str(v["building_id"])
        if v.get("interior_space_id"):
            kwargs["interior_space_id"] = str(v["interior_space_id"])
        res = engine.create_building_subworld(town_sid, **kwargs)
        return _snap_ok({"building": res}, space_id=town_sid)

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
        sid = str(v.get("space_id") or "")
        if engine.is_isolated_persona_realm(sid):
            raise ValueError("请在所属故事大厅删除该世界。")
        res = engine.delete_space(sid)
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
        sid = str(v.get("space_id") or "")
        out = _snap_ok({"tick_result": res}, space_id=sid)
        if world_chat is not None and v.get("roaming"):
            out["player_greeting"] = world_chat.pending_player_greeting(sid)
        return out

    def _act_live_control(v: dict[str, Any]) -> dict[str, Any]:
        aid, client = str(v.get("actor_id") or ""), str(v.get("client") or "")[:80]
        command = str(v.get("command") or "")
        speed = v.get("speed", 1)
        if not aid or not client or command not in ("start", "speed", "stop") or speed not in (0, 1, 2, 4):
            raise ValueError("实时生活控制参数无效。")
        return {"ok": True, "live": heartbeat.live.control(aid, client, command, int(speed))}

    def _act_get_live_frame(v: dict[str, Any]) -> dict[str, Any]:
        aid = str(v.get("actor_id") or "")
        peers = v.get("peers")
        live = heartbeat.live.frame(
            aid, str(v.get("client") or "")[:80], int(v.get("after", -1)),
            peers if isinstance(peers, dict) else None,
        )
        extra = {}
        if v.get("roaming"):
            extra["player_gifts"] = engine.gift_offers_for(engine.player_actor_id(str(v.get("space_id") or "")))
            if world_chat is not None:
                extra["player_greeting"] = world_chat.pending_player_greeting(str(v.get("space_id") or ""))
        if not live:
            return {"ok": True, "live": {}, **extra}
        sid = live["space_id"]
        req_sid = str(v.get("space_id") or "").strip()
        view_sid = req_sid if (req_sid and hasattr(engine, "has_space") and engine.has_space(req_sid)) else sid
        live = engine.redact_live_view_if_opaque(live, view_sid)
        cur_w = engine.get_weather(view_sid)
        cur_lt = engine.get_lighting(view_sid)
        night_skip = engine.latest_night_skip(view_sid) if hasattr(engine, "latest_night_skip") else None
        live["night_skip"] = night_skip
        version = "|".join([
            *(f"{row['actor_id']}:{row['world_revision']}" for row in [live, *live["peers"]]
              if row["space_id"] == view_sid),
            f"w:{cur_w.get('transition_seq', 0)}",
            f"p:{cur_lt.get('period', '')}:{'1' if cur_lt.get('is_dark') else '0'}",
            f"ns:{(night_skip or {}).get('seq', 0)}",
        ])
        live["world_version"] = version
        out = {"ok": True, "live": live, **extra}
        if view_sid == req_sid and version != v.get("world_version"):
            out["changes"] = engine.live_space_changes(view_sid)
        if world_chat is not None:
            history = world_chat.get_history_if_changed(aid, sid, str(v.get("history_after", "")))
            if history is not None:
                out["chat_history"] = history
        return out

    def _act_key_loan(v: dict[str, Any]) -> dict[str, Any]:
        aid = str(v.get("actor_id") or "")
        operation = str(v.get("operation") or "list")
        loan_id = str(v.get("loan_id") or "")
        space_id = str(v.get("space_id") or "")
        if operation == "list":
            return {"ok": True, "loans": engine.key_loans_for(aid), "pending": engine.pending_key_loan_response(aid)}
        if operation == "lend":
            engine.lend_key(aid, str(v.get("borrower_id") or ""), str(v.get("item_id") or ""),
                            minutes=int(v.get("minutes", 30)))
            feedback = "钥匙已交给借用人。"
        elif operation == "return":
            result = engine.return_key(aid, loan_id)
            feedback = {
                "returned": "钥匙已归还（逾期）。" if result.get("late") else "钥匙已按时归还。",
                "route_blocked": "回去的路被挡住了，钥匙还在你手里。",
                "different_space": "请到出借人所在的空间交接。",
                "key_not_held": "请先把要归还的钥匙拿在手上。",
                "receiver_hands_full": "出借人手上还有物品，暂时接不了。",
                "receiver_resting": "出借人正在休息，稍后再交接。",
            }[result["outcome"]]
        elif operation == "send_extension":
            engine.send_key_loan_extension(aid, loan_id, minutes=int(v.get("minutes", 60)))
            feedback = "延期请求已发出，等待投递。"
        elif operation == "deliver_extension":
            engine.deliver_key_loan_extension(aid, loan_id)
            feedback = "出借人已收到延期请求。"
        elif operation == "respond":
            response = heartbeat.respond_to_key_loan(aid)
            feedback = response["goal"] if response else "当前没有新的借物消息需要回应。"
        else:
            raise ValueError("未知的借物操作。")
        return _snap_ok({"feedback": feedback}, space_id=space_id)

    def _act_apply_starter(v: dict[str, Any]) -> dict[str, Any]:
        placed = engine.apply_starter_pack(str(v.get("space_id") or ""))
        return _snap_ok({"placed_count": len(placed)})

    def _act_clear_space(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("space_id") or engine.snapshot_for_ui()["active_space_id"])
        if engine.is_isolated_persona_realm(sid):
            raise ValueError("故事舞台由故事引擎管理。")
        res = engine.clear_space(sid)
        return _snap_ok(res)

    def _act_chat_in_world(v: dict[str, Any]) -> dict[str, Any]:
        req_sid = str(v.get("space_id") or "").strip()
        if not engine.is_isolated_persona_realm(req_sid):
            _ensure_default_host_actor()
        aid = str(v.get("actor_id") or "") or engine.resolve_actor_id(space_id=req_sid)
        if not aid:
            raise ValueError("当前房间内还没有角色，请先导入角色。")
        actor = engine.live_actor_frame(aid)
        overlay = engine.get_external_overlay((actor or {}).get("space_id") or req_sid)
        if overlay and overlay.get("view_only"):
            raise ValueError("当前舞台仅供观剧或复盘。")
        if world_chat is None:
            raise ValueError("世界对话服务尚未就绪。")
        text = str(v.get("text") or "").strip()
        if not text:
            raise ValueError("请输入想对她说的话。")
        if v.get("roaming") is True and _face_to_face_player(aid, str(v.get("player_id") or "")):
            heartbeat.live.hold_for_user_chat(aid, 20.0)
        res = world_chat.chat(aid, text, space_id=req_sid)
        if res.get("action_queued") or engine.pending_preempt_seq(aid) > 0:
            cog_now = (next((a for a in engine.list_actors() if a["actor_id"] == aid), None) or {}).get("autonomy", {}).get("cognition") or {}
            if res.get("action_queued") or cog_now.get("pending_modulation"):
                heartbeat.live.release_user_chat(aid)
        heartbeat.deliver_persona_events()
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

    def _act_generate_town(v: dict[str, Any]) -> dict[str, Any]:
        _ensure_default_host_actor()
        cfg = settings_store.get()
        prompt_text = str(v.get("prompt_text") or "").strip()
        w = int(v.get("width") or 40)
        h = int(v.get("height") or 30)
        b_cnt = int(v.get("building_count") or 6)
        use_llm = bool(v.get("use_llm", cfg.auto_compile_llm))
        place_actor = bool(v.get("place_actor", True))
        req_aid = str(v.get("actor_id") or "").strip() or engine.resolve_actor_id()
        adapter_prov = getattr(world_chat, "_adapter_provider", None) if world_chat else None
        compiled = text_compiler.compile_town_spec_from_text(
            prompt_text,
            width=w,
            height=h,
            building_count=b_cnt,
            use_llm=use_llm,
            adapter_provider=adapter_prov,
        )
        town = engine.generate_town(
            compiled["town_spec"],
            town_id=str(v.get("town_id") or "").strip(),
            move_actor_id=req_aid if place_actor else "",
        )
        return _snap_ok(
            {
                "town": town,
                "town_spec": compiled["town_spec"],
                "used_llm": compiled["used_llm"],
                "note": compiled["note"],
            },
            space_id=town["town_space_id"],
        )

    def _act_add_town_building(v: dict[str, Any]) -> dict[str, Any]:
        req_sid = str(v.get("town_space_id") or v.get("space_id") or "").strip()
        if not req_sid:
            req_sid = str(engine.snapshot_for_ui().get("active_space_id") or "")
        town_sid = engine.realm_space_id(req_sid) if hasattr(engine, "realm_space_id") else req_sid
        res = engine.add_town_building(
            town_sid,
            name=str(v.get("name") or "").strip() or "新建筑",
            role=str(v.get("role") or "public").strip(),
            zone_id=str(v.get("zone_id") or "").strip(),
            ext_w=int(v.get("ext_w") or 6),
            ext_h=int(v.get("ext_h") or 5),
            interior_ambient=str(v.get("interior_ambient") or "").strip(),
            note_text=str(v.get("note_text") or "").strip(),
        )
        return _snap_ok({"building": res, "validation": res.get("validation")}, space_id=town_sid)

    def _act_validate_town(v: dict[str, Any]) -> dict[str, Any]:
        req_sid = str(v.get("town_space_id") or v.get("space_id") or "").strip()
        if not req_sid:
            req_sid = str(engine.snapshot_for_ui().get("active_space_id") or "")
        town_sid = engine.realm_space_id(req_sid) if hasattr(engine, "realm_space_id") else req_sid
        validation = engine.validate_town(town_sid)
        return {"ok": True, "validation": validation}

    def _act_get_pixel_pack_status(_v: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **sprite_pack.list_cache_status(engine.data_root)}

    def _act_get_cached_pixel_sprite(v: dict[str, Any]) -> dict[str, Any]:
        probe = str(v.get("name") or v.get("key") or "").strip()
        if not probe:
            raise ValueError("缺少角色名称或键名。")
        sp = sprite_pack.get_cached_sprite(engine.data_root, probe)
        if sp is None:
            raise ValueError(f"角色「{probe}」尚未在本地缓存像素小人。")
        return {"ok": True, "sprite": sp}

    def _act_import_pixel_pack_zip(v: dict[str, Any]) -> dict[str, Any]:
        payload = str(v.get("zip_data_url") or v.get("zip_b64") or "").strip()
        res = sprite_pack.import_pack_zip_b64(engine.data_root, payload)
        return {"ok": True, **res}

    def _act_export_pixel_pack_zip(_v: dict[str, Any]) -> dict[str, Any]:
        res = sprite_pack.export_pack_zip_b64(engine.data_root)
        return {"ok": True, **res}

    def _act_batch_cache_official_pixels(v: dict[str, Any]) -> dict[str, Any]:
        series = str(v.get("series") or "all").strip()
        force_refresh = bool(v.get("force_refresh", False))
        rows = danganronpa_catalog.list_catalog(series)
        if not rows:
            raise ValueError(f"未找到系列「{series}」的角色列表。")

        def _worker(update_progress: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
            total = len(rows)
            ok_cnt = 0
            skip_cnt = 0
            failed_names: list[str] = []
            for idx, item in enumerate(rows, start=1):
                zh = item["zh"]
                en = item["en"]
                update_progress({
                    "label": f"预缓存 {idx}/{total}：{zh}（已就绪 {ok_cnt + skip_cnt}）",
                    "progress": f"{idx}/{total}",
                })
                if not force_refresh and sprite_pack.get_cached_sprite(engine.data_root, zh, en) is not None:
                    skip_cnt += 1
                    continue
                fetched = text_compiler.search_official_pixel_sprite(
                    zh,
                    en,
                    data_root=engine.data_root,
                    force_refresh=force_refresh,
                )
                if fetched is not None:
                    ok_cnt += 1
                else:
                    failed_names.append(zh)
            return {
                "ok": True,
                "series": series,
                "total": total,
                "succeeded": ok_cnt,
                "skipped": skip_cnt,
                "failed_names": failed_names,
                "status": sprite_pack.list_cache_status(engine.data_root),
            }

        return _spawn_bg_job(_worker, f"准备预缓存 {len(rows)} 位角色")

    def _act_player_spawn(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("space_id") or "").strip()
        name = str(v.get("name") or "你").strip()
        overlay = engine.get_external_overlay(sid)
        if overlay and overlay.get("view_only"):
            raise ValueError("当前舞台仅供观剧或复盘。")
        player = engine.ensure_player_avatar(space_id=sid, name=name)
        return _snap_ok({"player": player, "inventory": engine.inventory_state(player["actor_id"])},
                        space_id=str(player["space_id"]))

    def _act_player_move(v: dict[str, Any]) -> dict[str, Any]:
        pid = str(v.get("player_id") or "player_avatar").strip() or "player_avatar"
        dx = int(v.get("dx") or 0)
        dy = int(v.get("dy") or 0)
        res = engine.step_player_move(
            pid, dx, dy, space_id=str(v.get("space_id") or ""), include_prompt=bool(v.get("include_prompt", True)),
        )
        return {"ok": True, **res}

    def _act_player_interact(v: dict[str, Any]) -> dict[str, Any]:
        pid = str(v.get("player_id") or "player_avatar").strip() or "player_avatar"
        rx = v.get("x")
        ry = v.get("y")
        rfacing = str(v.get("facing") or "").strip()
        res = engine.player_interact_front(
            pid,
            x=int(rx) if rx is not None and str(rx) != "" else None,
            y=int(ry) if ry is not None and str(ry) != "" else None,
            facing=rfacing,
            space_id=str(v.get("space_id") or ""),
            target_kind=str(v.get("target_kind") or ""),
            target_id=str(v.get("target_id") or ""),
        )
        if res.get("kind") == "talk_actor" and _face_to_face_player(res["target_actor_id"], pid):
            heartbeat.live.hold_for_user_chat(res["target_actor_id"], 22.0)
        sid = str(res["space_id"])
        if res.get("kind") == "entered_space":
            return _snap_ok({"interaction": res}, space_id=sid)
        out = {"ok": True, "interaction": res, "player": engine.live_actor_frame(pid) or {}}
        if entity_ids := res.get("changed_entity_ids"):
            out["changes"] = engine.live_space_changes(sid, entity_ids=entity_ids)
        if res["kind"] == "container":
            out["inventory"] = engine.inventory_state(pid, res["entity_id"])
        elif res.get("action") == "take":
            out["inventory"] = engine.inventory_state(pid)
        return out

    def _act_player_inventory(v: dict[str, Any]) -> dict[str, Any]:
        pid = str(v.get("player_id") or engine.player_actor_id(str(v.get("space_id") or "")))
        operation = str(v.get("operation") or "list")
        eid = str(v.get("entity_id") or "")
        chest_id = str(v.get("chest_id") or "")
        offer_id = str(v.get("offer_id") or "")
        owner_id = str(v.get("owner_id") or "")
        out: dict[str, Any] = {"ok": True}
        # The read, validation, mutation and response use the same world state.
        with engine._lock:
            player = engine.live_actor_frame(pid)
            if not player or not player.get("is_player"):
                raise ValueError("请先进入主控漫游。")
            if v.get("space_id") and player["space_id"] != v["space_id"]:
                raise ValueError("当前场景已变化。")
            if operation == "gift":
                out["gift"] = engine.give_item(pid, str(v.get("recipient_id") or ""), eid,
                                              custom_persona_dir=settings_store.get().persona_data_dir)
                out["message"] = out["gift"]["message"]
            elif operation in ("accept_gift", "decline_gift"):
                out["gift"] = engine.respond_to_gift(pid, offer_id, accept=operation == "accept_gift")
                out["message"] = out["gift"]["message"]
            elif operation != "list":
                out["message"] = engine.inventory_action(
                    pid, operation, entity_id=eid, chest_id=chest_id, blueprint_id=str(v.get("blueprint_id") or ""),
                    verb=str(v.get("verb") or ""), slot=int(v.get("slot", -1)), owner_id=owner_id,
                    facing=str(v.get("facing") or ""),
                )
            out["player"] = engine.live_actor_frame(pid)
            out["inventory"] = engine.inventory_state(pid, "" if operation == "assign_chest" and owner_id not in ("", pid) else chest_id)
            dirty_id = chest_id if operation == "assign_chest" else eid
            if dirty_id and operation in ("place", "pickup", "withdraw", "deposit", "assign_chest", "use"):
                out["changes"] = engine.live_space_changes(player["space_id"], entity_ids=[dirty_id])
        return out

    def _face_to_face_player(aid: str, pid: str) -> bool:
        if not aid or not pid:
            return False
        with engine._lock:
            actor = engine.live_actor_frame(aid)
            player = engine.live_actor_frame(pid)
            if not actor or not player or actor.get("is_player") or not player.get("is_player"):
                return False
            sid = actor["space_id"]
            overlay = engine.get_external_overlay(sid) or {}
            return bool(
                player["space_id"] == sid and pid == engine.player_actor_id(sid)
                and player.get("actor_status", "active") == actor.get("actor_status", "active") == "active"
                and not overlay.get("view_only") and engine.chebyshev_distance(player, actor) <= 3
            )

    def _act_user_chat_presence(v: dict[str, Any]) -> dict[str, Any]:
        aid = str(v.get("actor_id") or "").strip()
        active = bool(v.get("active", True))
        if aid and not active:
            heartbeat.live.release_user_chat(aid)
        elif v.get("roaming") is True and _face_to_face_player(aid, str(v.get("player_id") or "")):
            heartbeat.live.hold_for_user_chat(aid, 20.0)
        return {"ok": True}

    def _act_open_player_greeting(v: dict[str, Any]) -> dict[str, Any]:
        if world_chat is None:
            return {"ok": True}
        sid = str(v.get("space_id") or "")
        if (engine.get_external_overlay(sid) or {}).get("view_only"):
            return {"ok": True}
        greeting = world_chat.open_player_greeting(str(v.get("greeting_id") or ""), sid)
        if greeting is None:
            return {"ok": True}
        aid = greeting["actor_id"]
        engine.set_active_actor(aid, expected_space_id=sid)
        heartbeat.live.hold_for_user_chat(aid, 20.0)
        return {"ok": True, "greeting": greeting}

    def _act_set_world_weather(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("space_id") or "")
        mode = str(v.get("mode") or "auto")
        w = engine.set_weather(sid, mode, intensity=v.get("intensity"))
        return _snap_ok({"weather": w}, space_id=sid)

    def _act_set_actor_home(v: dict[str, Any]) -> dict[str, Any]:
        sid = str(v.get("space_id") or "")
        aid = str(v.get("actor_id") or "").strip() or engine.resolve_actor_id(space_id=sid)
        if not aid:
            raise ValueError("当前没有可设置住处的角色。")
        home_sid = str(v.get("home_space_id") or "")
        resolved = engine.set_actor_home_space(aid, home_sid, realm_space_id=sid)
        return _snap_ok({"home_space_id": resolved}, space_id=sid)

    def _act_external_overlay_action(v: dict[str, Any]) -> dict[str, Any]:
        if bridge is None:
            return {"ok": False, "handled": False, "error": "当前未注册外部桥接器。"}
        sid = str(v.get("space_id") or "")
        action_id = str(v.get("action_id") or "")
        payload = v.get("payload") if isinstance(v.get("payload"), Mapping) else None
        res = bridge.handle_overlay_action(action_id, space_id=sid, payload=payload)
        return _snap_ok({"overlay_result": res, "handled": bool(res.get("handled", res.get("ok")))}, space_id=sid)

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
        ("get_actor_relations", "读取指定世界角色社交指针", _act_get_actor_relations),
        ("save_world_relation", "保存本世界社交指针", _act_save_world_relation),
        ("set_actor_appearance", "保存像素小人外观", _act_set_actor_appearance),
        ("regen_actor_appearance", "按设定生成小人外观", _act_regen_actor_appearance),
        ("get_regen_job", "查询生图任务状态", _act_get_regen_job),
        ("get_pixel_pack_status", "查询本地像素图包状态", _act_get_pixel_pack_status),
        ("get_cached_pixel_sprite", "读取已缓存像素小人", _act_get_cached_pixel_sprite),
        ("import_pixel_pack_zip", "导入像素图包(.zip)", _act_import_pixel_pack_zip),
        ("export_pixel_pack_zip", "导出像素图包(.zip)", _act_export_pixel_pack_zip),
        ("batch_cache_official_pixels", "按系列一键预缓存像素小人", _act_batch_cache_official_pixels),
        ("create_space", "新建空间", _act_create_space),
        ("create_default_town", "创建并进入默认示范小镇", _act_create_default_town),
        ("create_building_subworld", "建造可进入建筑子世界", _act_create_building_subworld),
        ("switch_space", "切换空间", _act_switch_space),
        ("delete_space", "删除世界", _act_delete_space),
        ("enter_portal", "推门进入连通空间", _act_enter_portal),
        ("move_actor", "引导角色走动到坐标", _act_move_actor),
        ("save_palette", "保存调色板", _act_save_palette),
        ("set_space_palette", "切换空间调色板", _act_set_space_palette),
        ("tick_world", "推进世界时钟", _act_tick_world),
        ("autonomous_step", "触发角色自主行动", _act_autonomous_step),
        ("live_control", "控制实时生活", _act_live_control),
        ("get_live_frame", "读取实时动作帧", _act_get_live_frame),
        ("key_loan", "借还钥匙与延期通知", _act_key_loan),
        ("apply_starter_pack", "放入基础测试包", _act_apply_starter),
        ("clear_space", "清空当前空间", _act_clear_space),
        ("chat_in_world", "在世界中对话", _act_chat_in_world),
        ("clear_world_chat", "清空世界对话", _act_clear_world_chat),
        ("set_actor_expression", "切换角色表情立绘", _act_set_actor_expression),
        ("get_actor_expressions", "获取角色全部立绘", _act_get_actor_expressions),
        ("paint_tiles", "搭建或拆除地形方块", _act_paint_tiles),
        ("expand_space", "无限拓展地图边界", _act_expand_space),
        ("generate_world_map", "LLM 生成像素地图", _act_generate_world_map),
        ("generate_town", "分层生成主题小镇", _act_generate_town),
        ("add_town_building", "在城镇内加盖可进入建筑", _act_add_town_building),
        ("validate_town", "校验城镇连通性与建筑布局", _act_validate_town),
        ("player_spawn", "生成或切换主控玩家小人", _act_player_spawn),
        ("player_move", "主控玩家单步移动", _act_player_move),
        ("player_interact", "主控玩家眼前交互", _act_player_interact),
        ("player_inventory", "主控背包、箱子与赠礼", _act_player_inventory),
        ("user_chat_presence", "同步玩家面对面交谈状态", _act_user_chat_presence),
        ("open_player_greeting", "接收角色的当面招呼", _act_open_player_greeting),
        ("set_world_weather", "切换世界天气", _act_set_world_weather),
        ("set_actor_home", "指派角色住处", _act_set_actor_home),
        ("external_overlay_action", "触发外部面板动作", _act_external_overlay_action),
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
            load_values=engine.canvas_summary,
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
                {"key": "heartbeat_use_llm", "type": "boolean", "label": "自主心跳使用大模型", "description": "自动生活中，关键经历会触发后台反思，清醒角色相遇时可生成对话。步行与日常动作由本地引擎执行。"},
                {"key": "episodic_memory_enabled", "type": "boolean", "label": "使用世界经历", "description": "角色在休息时整理亲历的探索、活动与交往，并在相关话题或解谜时回想"},
                {"key": "allow_ai_diary", "type": "boolean", "label": "允许自主写随手记", "description": "城镇角色有新经历时，可在可写的笔记本或读过的布告栏留一句话。"},
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
        {
            "id": "pixellab_sprite",
            "title": "像素小人云端生成配置（PixelLab）",
            "fields": [
                {
                    "key": "pixellab_api_key",
                    "type": "text",
                    "label": "PixelLab API Key（云端原生 4 向像素小人备用）",
                    "description": "在 pixellab.ai 获取免费试用/正式 Key（格式 pixl_...），非弹丸论破角色可由此生成 4 向小人",
                    "placeholder": "pixl_...",
                },
            ],
        },
    ]

    settings_html = code_dir / "web" / "settings.html"
    if settings_html.is_file() and hasattr(register, "register_frontend_page"):
        register.register_frontend_page(
            FrontendPageContribution(
                page_id=SETTINGS_PAGE_ID,
                title="文字世界设置",
                entry=str(settings_html),
                kind="settings",
                description="配置文字世界视野注入、受控交互工具、离线心跳与人格插件文件桥路径。",
                order=65.0,
            )
        )
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
