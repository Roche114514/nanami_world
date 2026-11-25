"""NPC 昼夜生理作息表与住处归属（schedule.py）：按 L0 时型与 L1–L8 特质 0-Token 生成。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from . import venue_semantics
from .schema import clamp_float


def _fmt_hour(hour_val: float) -> str:
    total_min = int(round((float(hour_val) % 24.0) * 60.0)) % (24 * 60)
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def _in_window(hour_val: float, start_h: float, end_h: float) -> bool:
    h = float(hour_val) % 24.0
    if start_h < end_h:
        return start_h <= h < end_h
    return h >= start_h or h < end_h


def default_home_space_id(
    actor_name: str,
    host_character: str,
    realm_buildings: Sequence[Mapping[str, Any]] = (),
    *,
    explicit_home: str = "",
    valid_space_ids: set[str] | None = None,
    actor_id: str = "",
) -> str:
    """解析角色在当前城镇域内的默认住处（优先显式指派与名称匹配，否则跨宿舍均匀分配）。"""
    buildings = [b for b in (realm_buildings or ()) if isinstance(b, Mapping)]
    building_spaces = {
        str(b.get("interior_space_id") or "").strip()
        for b in buildings
        if str(b.get("interior_space_id") or "").strip()
    }
    clean_explicit = str(explicit_home or "").strip()
    if clean_explicit and (clean_explicit in building_spaces or (valid_space_ids and clean_explicit in valid_space_ids)):
        return clean_explicit

    homes = [
        b for b in buildings
        if venue_semantics.is_lodging_building(b)
        and str(b.get("interior_space_id") or "").strip()
    ]
    bedded_homes = [b for b in homes if int(b.get("bed_count") or 0) > 0]
    if bedded_homes:
        homes = bedded_homes
    if not homes:
        return ""

    tokens: list[str] = []
    for raw_name in (actor_name, host_character):
        clean = str(raw_name or "").strip()
        if len(clean) >= 2:
            tokens.append(clean)
            tokens.append(clean[:2])
    for b in homes:
        bname = str(b.get("name") or "")
        if any(tok in bname for tok in tokens):
            return str(b["interior_space_id"]).strip()

    # 多位角色在同一平行世界中有多间宿舍时，按稳定哈希均匀错峰分配，避免全员挤进同一间房
    seed = str(actor_id or actor_name or host_character or "").strip()
    if seed and len(homes) > 1:
        weighted: list[str] = []
        for b in homes:
            sid = str(b["interior_space_id"]).strip()
            beds = max(1, min(4, int(b.get("bed_count") or 1)))
            weighted.extend([sid] * beds)
        digest = hashlib.md5(seed.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "little") % len(weighted)
        return weighted[idx]

    for b in homes:
        bname = str(b.get("name") or "")
        if any(kw in bname for kw in ("宿舍", "客房", "旅馆", "民宿", "汤宿", "公寓")):
            return str(b["interior_space_id"]).strip()

    return str(homes[0]["interior_space_id"]).strip()


def _pick_building_place(
    realm_buildings: Sequence[Mapping[str, Any]],
    categories: tuple[str, ...],
    keywords: tuple[str, ...] = (),
) -> tuple[str, str]:
    buildings = [b for b in (realm_buildings or ()) if isinstance(b, Mapping) and b.get("interior_space_id")]
    for cat in categories:
        for b in buildings:
            if venue_semantics.building_matches_schedule_category(b, cat):
                if not keywords or any(kw in str(b.get("name") or "") for kw in keywords):
                    return str(b["interior_space_id"]).strip(), str(b.get("name") or "").strip()
    for cat in categories:
        for b in buildings:
            if venue_semantics.building_matches_schedule_category(b, cat):
                return str(b["interior_space_id"]).strip(), str(b.get("name") or "").strip()
    for b in buildings:
        bname = str(b.get("name") or "")
        if any(kw in bname for kw in keywords):
            return str(b["interior_space_id"]).strip(), bname
    return "", ""


def chronotype_label(offset: float) -> str:
    val = float(offset or 0.0)
    if val >= 0.75:
        return f"+{val:.1f}h 夜猫子"
    if val <= -0.75:
        return f"{val:.1f}h 早鸟"
    return "标准时型"


def build_actor_schedule(
    actor: Mapping[str, Any],
    behavior_profile: Mapping[str, Any] | None,
    l0_cfg: Mapping[str, Any] | None,
    realm_buildings: Sequence[Mapping[str, Any]] = (),
    *,
    home_space_id: str = "",
) -> dict[str, Any]:
    """根据 L0 时型与 L1–L8 人格画像为角色生成当日 6 段生理作息表。"""
    bp = behavior_profile if isinstance(behavior_profile, Mapping) else {}
    l0 = l0_cfg if isinstance(l0_cfg, Mapping) else {}
    vit = actor.get("vitals") if isinstance(actor.get("vitals"), Mapping) else {}
    pref_domains = {str(d).strip() for d in (vit.get("preferred_domains") or ()) if str(d).strip()}

    enabled = bool(l0.get("enabled", True))
    chrono = clamp_float(l0.get("chronotype_offset"), -3.0, 3.0, 0.0) if enabled else 0.0
    c_str = clamp_float(l0.get("circadian_strength"), 0.0, 1.0, 0.75) if enabled else 0.35

    dom_game = clamp_float(bp.get("domain_efficacy_game"), 0.0, 1.0, 0.5)
    dom_nongame = clamp_float(bp.get("domain_efficacy_non_game"), 0.0, 1.0, 0.5)
    soc_init = clamp_float(bp.get("social_initiative"), 0.0, 1.0, 0.4)
    soc_avoid = clamp_float(bp.get("social_avoidance"), 0.0, 1.0, 0.5)
    rel_need = clamp_float(bp.get("relatedness_need"), 0.0, 1.0, 0.6)
    safety = clamp_float(bp.get("safety_need"), 0.0, 1.0, 0.5)
    comp = clamp_float(bp.get("competence_need"), 0.0, 1.0, 0.6)

    game_score = dom_game + 0.15 * comp + (0.20 if "game" in pref_domains else 0.0)
    reading_score = dom_nongame + 0.25 * safety + 0.15 * soc_avoid + (0.20 if "reading" in pref_domains else 0.0)
    social_score = 0.55 * soc_init + 0.45 * rel_need - 0.35 * soc_avoid + (0.20 if "social" in pref_domains else 0.0)

    if game_score >= reading_score and game_score >= social_score:
        top_axis = "game"
    elif social_score >= reading_score:
        top_axis = "social"
    else:
        top_axis = "reading"

    resolved_home = default_home_space_id(
        str(actor.get("name") or ""),
        str(actor.get("host_character") or ""),
        realm_buildings,
        explicit_home=home_space_id,
        actor_id=str(actor.get("actor_id") or ""),
    )
    home_name = next(
        (str(b.get("name") or "") for b in realm_buildings if str(b.get("interior_space_id") or "") == resolved_home),
        "",
    )
    arcade_id, arcade_name = _pick_building_place(realm_buildings, ("arcade",), ("电玩", "游戏"))
    library_id, library_name = _pick_building_place(realm_buildings, ("library", "public"), ("图书", "书"))
    cafe_id, cafe_name = _pick_building_place(realm_buildings, ("cafe", "shop"), ("咖啡", "茶"))
    shop_id, shop_name = _pick_building_place(realm_buildings, ("shop", "cafe"), ("杂货", "工坊", "咖啡"))

    def _slot(
        slot_id: str,
        start_bio: float,
        end_bio: float,
        label: str,
        domains: list[str],
        categories: list[str],
        place_id: str,
        place_name: str,
        weight: float,
    ) -> dict[str, Any]:
        return {
            "slot_id": slot_id,
            "start_bio": start_bio,
            "end_bio": end_bio,
            "display_start": _fmt_hour(start_bio + chrono),
            "display_end": _fmt_hour(end_bio + chrono),
            "label": label,
            "preferred_domains": domains,
            "preferred_categories": categories,
            "preferred_place_id": place_id,
            "preferred_place_name": place_name,
            "weight": round(weight, 3),
        }

    morning_place_id, morning_place_name = (
        (resolved_home, home_name)
        if (resolved_home and (safety >= 0.60 or soc_avoid >= 0.60))
        else (cafe_id or resolved_home, cafe_name or home_name)
    )

    if top_axis == "game":
        late_morn = _slot(
            "late_morning", 10.0, 12.5, "上午暖手休闲",
            ["game"], ["arcade", "home"], arcade_id, arcade_name, 0.65,
        )
        afternoon = _slot(
            "afternoon_focus", 14.5, 18.5, "午后电玩时光",
            ["game", "hobby"], ["arcade"], arcade_id, arcade_name, 0.78,
        )
    elif top_axis == "social":
        late_morn = _slot(
            "late_morning", 10.0, 12.5, "上午串门逛街",
            ["social"], ["cafe", "shop"], cafe_id, cafe_name, 0.65,
        )
        afternoon = _slot(
            "afternoon_focus", 14.5, 18.5, "午后茶歇社交",
            ["social", "food"], ["cafe", "shop"], cafe_id, cafe_name, 0.78,
        )
    else:
        late_morn = _slot(
            "late_morning", 10.0, 12.5, "上午静读漫步",
            ["reading"], ["library", "public"], library_id, library_name, 0.65,
        )
        afternoon = _slot(
            "afternoon_focus", 14.5, 18.5, "午后阅览沉浸",
            ["reading", "hobby"], ["library", "public"], library_id, library_name, 0.78,
        )

    if (reading_score >= game_score and reading_score >= social_score) or safety >= 0.70:
        evening = _slot(
            "evening_wind_down", 18.5, 23.5, "傍晚静阅时光",
            ["reading", "comfort", "rest"], ["library", "public", "home"], library_id or resolved_home, library_name or home_name, 0.62,
        )
    elif social_score >= game_score:
        evening = _slot(
            "evening_wind_down", 18.5, 23.5, "傍晚街区社交",
            ["social", "food"], ["cafe", "public"], cafe_id, cafe_name, 0.62,
        )
    else:
        evening = _slot(
            "evening_wind_down", 18.5, 23.5, "晚间放松娱乐",
            ["game", "comfort"], ["arcade", "home"], arcade_id, arcade_name, 0.62,
        )

    slots = [
        _slot(
            "morning_wake", 7.0, 10.0, "晨间醒神",
            ["food", "comfort"], ["home", "house", "cafe"], morning_place_id, morning_place_name, 0.60,
        ),
        late_morn,
        _slot(
            "midday_meal", 12.5, 14.5, "午间餐点",
            ["food", "social"] if soc_avoid < 0.65 else ["food"],
            ["cafe", "shop"], cafe_id or shop_id, cafe_name or shop_name, 0.72,
        ),
        afternoon,
        evening,
        _slot(
            "night_rest", 23.5, 7.0, "夜间归宿休息",
            ["rest", "comfort"], ["home", "house"], resolved_home, home_name, 0.92,
        ),
    ]

    return {
        "axis": top_axis,
        "chronotype_offset": round(chrono, 2),
        "chronotype_label": chronotype_label(chrono),
        "circadian_strength": round(c_str, 3),
        "home_space_id": resolved_home,
        "home_name": home_name,
        "slots": slots,
    }


def active_schedule_context(
    schedule: Mapping[str, Any] | None,
    bio_hour: float,
    *,
    circadian_strength: float,
    effortful_control: float,
    arbitration: str = "稳态/习惯",
    has_active_modulation: bool = False,
    in_crisis: bool = False,
    nocturnal_active: bool = False,
    clock_hour: float | None = None,
) -> dict[str, Any]:
    """根据当前生理时钟与 Daw 仲裁状态，计算作息槽与温和软先验缩放系数。"""
    sched = schedule if isinstance(schedule, Mapping) else {}
    slots = [s for s in (sched.get("slots") or ()) if isinstance(s, Mapping)]
    b_hour = float(bio_hour) % 24.0
    in_night_curfew = clock_hour is not None and _in_window(float(clock_hour) % 24.0, 23.5, 7.0)
    night_slot = next((s for s in slots if str(s.get("slot_id") or "") == "night_rest"), None)
    if in_night_curfew and night_slot is not None and not nocturnal_active:
        slot = night_slot
    else:
        slot = next(
            (s for s in slots if _in_window(b_hour, float(s.get("start_bio", 0.0)), float(s.get("end_bio", 24.0)))),
            slots[-1] if slots else {},
        )
    if not slot:
        return {"slot": {}, "effective_scale": 0.0, "adherence": 0.0, "bio_clock": _fmt_hour(b_hour), "prompt_line": ""}

    c_str = clamp_float(circadian_strength, 0.0, 1.0, 0.75)
    eff = clamp_float(effortful_control, 0.0, 1.0, 0.70)
    adherence = c_str * (0.55 + 0.45 * eff)
    slot_w = clamp_float(slot.get("weight"), 0.0, 1.0, 0.65)
    slot_id = str(slot.get("slot_id") or "")
    home_sid = str(sched.get("home_space_id") or "").strip()

    if slot_id == "night_rest" and not in_crisis and (nocturnal_active or not home_sid):
        effective_scale = 0.0
    elif in_night_curfew and slot_id == "night_rest" and not nocturnal_active and home_sid and not in_crisis:
        effective_scale = max(adherence * slot_w, 0.92)
    elif in_crisis:
        effective_scale = adherence * 0.25 if slot_id == "night_rest" else 0.0
    elif arbitration == "社会脑/语言主导" or has_active_modulation:
        effective_scale = adherence * slot_w * 0.45
    else:
        effective_scale = adherence * slot_w

    place_hint = f"/{slot['preferred_place_name']}" if slot.get("preferred_place_name") else ""
    if slot_id == "night_rest" and nocturnal_active:
        prompt_line = f"生理时间 {_fmt_hour(b_hour)} · 夜间暗中行动（暂缓归宿）"
    else:
        prompt_line = f"生理时间 {_fmt_hour(b_hour)} · {slot.get('label', '')}（倾向{place_hint.lstrip('/') or '顺应节律'}）"
    return {
        "slot": dict(slot),
        "effective_scale": round(effective_scale, 4),
        "adherence": round(adherence, 4),
        "bio_clock": _fmt_hour(b_hour),
        "home_space_id": home_sid,
        "home_name": str(sched.get("home_name") or ""),
        "prompt_line": prompt_line,
    }


def routine_candidate_bonus(
    ctx: Mapping[str, Any] | None,
    *,
    kind: str,
    target: str,
    drive: str,
    target_category: str = "",
    target_role: str = "",
    is_home_space: bool = False,
    is_inside_home: bool = False,
) -> float:
    """在认知地图既知候选项上叠加温和作息软先验（封顶 ±0.85，绝不压过危机或承诺）。"""
    if kind == "explore" or not isinstance(ctx, Mapping):
        return 0.0
    scale = float(ctx.get("effective_scale") or 0.0)
    slot = ctx.get("slot")
    if scale <= 0.0 or not isinstance(slot, Mapping):
        return 0.0

    bonus = 0.0
    pref_domains = slot.get("preferred_domains") or ()
    pref_cats = slot.get("preferred_categories") or ()
    pref_place = str(slot.get("preferred_place_id") or "").strip()
    slot_id = str(slot.get("slot_id") or "")
    has_home = bool(str(ctx.get("home_space_id") or "").strip())

    if slot_id == "night_rest" and has_home and kind == "entity" and drive == "rest" and not is_inside_home:
        bonus -= 0.45 * scale
    elif drive and drive in pref_domains:
        bonus += 0.48 * scale

    if kind == "visit":
        if (pref_place and target == pref_place) or (slot_id == "night_rest" and is_home_space):
            bonus += 0.52 * scale
        elif (target_category and target_category in pref_cats) or (target_role and target_role in pref_cats):
            bonus += 0.34 * scale
    elif kind == "exit" and slot_id == "night_rest" and is_inside_home:
        bonus -= 0.65 * scale

    return round(max(-0.85, min(0.85, bonus)), 4)
