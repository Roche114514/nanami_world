"""语言视野投影器（projector.py）：把确定性世界实体实时编译成角色上下文内的白描语言。"""

from __future__ import annotations

import time
from typing import Any

from . import persona_bridge
from .engine import WorldEngine, compute_lighting
from .schema import TILE_CATALOG

_LOOK_WINDOW_SEC = 180.0


def _direction_text(ax: int, ay: int, tx: int, ty: int) -> str:
    dx = tx - ax
    dy = ty - ay
    if dx == 0 and dy == 0:
        return "脚下"
    ns = "北" if dy < 0 else ("南" if dy > 0 else "")
    ew = "西" if dx < 0 else ("东" if dx > 0 else "")
    if ns and ew:
        return f"{ew}{ns}"
    return f"正{ew or ns}"


def _coord_label(ent: dict[str, Any]) -> str:
    x0 = int(ent.get("x") or 0)
    y0 = int(ent.get("y") or 0)
    fw, fh = ent.get("footprint") or [1, 1]
    x_txt = f"{x0}..{x0 + fw - 1}" if fw > 1 else f"{x0}"
    y_txt = f"{y0}..{y0 + fh - 1}" if fh > 1 else f"{y0}"
    return f"({x_txt}, {y_txt})"


def _format_entity_line(
    engine: WorldEngine,
    ent: dict[str, Any],
    actor: dict[str, Any],
    *,
    dist: int,
    show_detail: bool,
    children_lines: list[str] | None = None,
) -> str:
    eid = ent["entity_id"]
    brief = engine.entity_display_brief(ent)
    tags = engine.entity_state_tags(ent)
    verbs = [label for _canon, label in engine.legal_verbs(ent, actor)]
    coord = _coord_label(ent)
    pos_part = coord if dist <= 1 else f"{coord}｜{_direction_text(int(actor['x']), int(actor['y']), int(ent['x']), int(ent['y']))} {dist} 格"
    brief_part = f"｜{brief}" if brief and brief != ent["name"] else ""
    state_part = f"｜{'，'.join(tags)}" if tags else ""
    detail_part = f"｜细看：{ent['detail']}" if show_detail else ""
    verb_part = f"｜可用：{'、'.join(verbs)}" if verbs else ""
    line = f"  - [{eid}] {ent['name']} {pos_part}{brief_part}{state_part}{detail_part}{verb_part}"
    if children_lines:
        return line + "\n" + "\n".join(children_lines)
    return line


def project_terminal_data(
    engine: WorldEngine,
    actor_id: str,
    *,
    consume_diffs: bool = False,
) -> dict[str, Any]:
    """编译角色此刻眼中的纯文字世界与格网反查索引。"""
    snap = engine.snapshot_for_ui()
    actor = next((a for a in snap["actors"] if a["actor_id"] == actor_id), None)
    if actor is None:
        return {"text": "（世界里还没有导入角色。）", "anchors": []}

    sid = actor["space_id"]
    if sid != snap["active_space_id"]:
        snap = engine.snapshot_for_ui(sid)
        actor = next((a for a in snap["actors"] if a["actor_id"] == actor_id), actor)

    space = snap["space"]
    lighting = compute_lighting(space, {e["entity_id"]: e for e in snap["entities"]}, time.time(), float(snap.get("clock_offset_minutes") or 0.0))
    period_cn = {"dawn": "清晨", "day": "白天", "dusk": "傍晚", "night": "夜晚"}.get(lighting["period"], "白天")
    light_cn = "屋里暗着（没开灯）" if lighting["is_dark"] else ("台灯亮着" if lighting["has_lamp_on"] else "室内明亮")

    vis = engine.visible_entities(actor_id)
    now = time.time()
    looked_map = snap.get("looked_entities") or {}

    held_ent = None
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    top_level: list[dict[str, Any]] = []
    for ent in vis:
        pid = str(ent.get("parent_id") or "")
        if pid == f"actor:{actor_id}":
            held_ent = ent
        elif pid.startswith("entity:"):
            children_by_parent.setdefault(pid.split(":", 1)[1], []).append(ent)
        else:
            top_level.append(ent)

    near_lines: list[str] = []
    far_lines: list[str] = []
    anchors: list[dict[str, Any]] = []

    for ent in top_level:
        eid = ent["entity_id"]
        dist = engine.chebyshev_distance(actor, ent)
        show_detail = (now - float(looked_map.get(eid) or 0)) <= _LOOK_WINDOW_SEC
        child_lines: list[str] = []
        for ch in children_by_parent.get(eid, []):
            cid = ch["entity_id"]
            c_show = (now - float(looked_map.get(cid) or 0)) <= _LOOK_WINDOW_SEC
            c_verbs = [lbl for _c, lbl in engine.legal_verbs(ch, actor)]
            c_tags = engine.entity_state_tags(ch)
            c_brief = engine.entity_display_brief(ch)
            c_state = f"｜{'，'.join(c_tags)}" if c_tags else ""
            c_det = f"｜细看：{ch['detail']}" if c_show else ""
            c_v = f"｜可用：{'、'.join(c_verbs)}" if c_verbs else ""
            child_lines.append(f"      - [{cid}] {ch['name']} ({ch['x']}, {ch['y']})｜{c_brief}{c_state}{c_det}{c_v}")
            anchors.append({"entity_id": cid, "name": ch["name"], "x": ch["x"], "y": ch["y"]})

        line = _format_entity_line(engine, ent, actor, dist=dist, show_detail=show_detail, children_lines=child_lines)
        anchors.append({"entity_id": eid, "name": ent["name"], "x": ent["x"], "y": ent["y"]})
        if dist <= 1:
            near_lines.append(line)
        else:
            far_lines.append(line)

    diffs = engine.consume_diffs(sid) if consume_diffs else engine.peek_diffs(sid)
    peer_actors = [
        a for a in snap["actors"]
        if a["space_id"] == sid and a["actor_id"] != actor_id
    ]
    actor_host = str(actor.get("host_character") or actor.get("name") or "").strip()
    peer_names = [str(a.get("host_character") or a.get("name") or "").strip() for a in peer_actors]
    relations_map = (
        persona_bridge.read_actor_relations(
            engine.project_root,
            actor_host,
            space_id=sid,
            world_data_root=engine.data_root,
            known_actors=peer_names,
        )
        if peer_actors
        else {}
    )
    other_actor_lines: list[str] = []
    for pa in peer_actors:
        pname = str(pa.get("name") or "").strip()
        phost = str(pa.get("host_character") or pname).strip()
        p_dist = max(abs(int(actor["x"]) - int(pa["x"])), abs(int(actor["y"]) - int(pa["y"])))
        p_dir = _direction_text(int(actor["x"]), int(actor["y"]), int(pa["x"]), int(pa["y"]))
        p_pos = "就在身边" if p_dist <= 1 else f"{p_dir} {p_dist} 格"
        p_posture = "休息中" if pa.get("posture") == "resting" else "站着"
        rel = relations_map.get(f"char:{phost}") or relations_map.get(f"char:{pname}") or {}
        if not rel.get("known"):
            rel_txt = "关系：尚不认识（初次见面可先打招呼认识）"
        else:
            stage_cn = {
                "stranger": "不认识",
                "acquainted": "刚认识",
                "friend": "同伴",
                "close": "挚友",
                "rival": "对手",
                "guarded": "戒备",
                "hostile": "排斥",
            }.get(str(rel.get("rel_stage") or "acquainted"), "相识")
            call_t = f"，称呼：{rel['call_name']}" if rel.get("call_name") else ""
            tag_t = f"·{rel['relation_tag']}" if rel.get("relation_tag") else ""
            mem_t = f"｜经历：{str(rel['shared_memories'])[:42]}" if rel.get("shared_memories") else ""
            rel_txt = f"关系：{stage_cn}{tag_t}{call_t}｜好感 {float(rel.get('affective_valence') or 0):+.2f}{mem_t}"
        other_actor_lines.append(f"  - [{pa['actor_id']}] {pname} ({pa['x']}, {pa['y']})｜{p_pos}｜{p_posture}｜{rel_txt}")

    underfoot = engine.get_cell_tile(space, int(actor["x"]), int(actor["y"]))
    raw_tiles = space.get("tiles") or {}
    terrain_names: list[str] = []
    if isinstance(raw_tiles, dict) and raw_tiles:
        seen_tids: set[str] = set()
        for tid in raw_tiles.values():
            if tid in TILE_CATALOG and tid not in seen_tids:
                seen_tids.add(tid)
                terrain_names.append(str(TILE_CATALOG[tid]["name"]))

    posture_cn = "正躺着休息" if actor.get("posture") == "resting" else "站着"
    lines: list[str] = [
        "【世界现实】（以下是你此刻所在文字世界的客观事实，严禁编造或使用清单之外的物品；这不是任务清单，不必每句都刻意报幕）",
        f"- 空间：{space['name']}（{space['width']}×{space['height']}，{lighting['clock']} {period_cn}，{light_cn}）｜你在 ({actor['x']}, {actor['y']}) {posture_cn}［脚下：{underfoot['name']}］",
        f"- 环境底色：{space['ambient_phrase']}",
    ]
    if terrain_names:
        lines.append("- 地形与建材：" + "、".join(terrain_names[:10]))
    aut = actor.get("autonomy") if isinstance(actor.get("autonomy"), dict) else {}
    if aut.get("current_goal") or aut.get("last_action_summary"):
        aut_parts = [f"当前想「{aut.get('current_goal') or '四处打量'}」"]
        if aut.get("last_action_summary"):
            aut_parts.append(f"刚做了：{aut['last_action_summary']}")
        if aut.get("last_thought"):
            aut_parts.append(f"心声：「{aut['last_thought']}」")
        lines.append("- 自主状态与心愿：" + "｜".join(aut_parts))
    if held_ent:
        h_verbs = [lbl for _c, lbl in engine.legal_verbs(held_ent, actor)]
        h_tags = engine.entity_state_tags(held_ent)
        h_state = f"｜{'，'.join(h_tags)}" if h_tags else ""
        lines.append(f"- 手里拿着：[{held_ent['entity_id']}] {held_ent['name']}{h_state}｜可用：{'、'.join(h_verbs)}")
    else:
        lines.append("- 手里：空")

    if other_actor_lines:
        lines.append("- 同屋的其他角色：")
        lines.extend(other_actor_lines)

    if diffs:
        lines.append("- 刚发生的变化：" + "；".join(diffs[-4:]))

    if near_lines:
        lines.append("- 身边可碰（1 格内）：")
        lines.extend(near_lines)
    if far_lines:
        lines.append("- 远处可见：")
        lines.extend(far_lines)
    if not near_lines and not far_lines and not held_ent:
        lines.append("- 视野内：这里空荡荡的，还没有放进任何物品。")

    return {"text": "\n".join(lines), "anchors": anchors}


def project_view(engine: WorldEngine, actor_id: str, *, consume_diffs: bool = False) -> str:
    return str(project_terminal_data(engine, actor_id, consume_diffs=consume_diffs).get("text") or "")
