"""轻天气系统（weather.py）：按 realm 维护 0-Token 确定性天气状态机与具身调制。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .schema import clamp_float

VALID_WEATHERS: tuple[str, ...] = ("clear", "cloudy", "rain", "sakura", "snow")
VALID_WEATHER_MODES: frozenset[str] = frozenset({"auto", *VALID_WEATHERS})
DEFAULT_ALLOWED_WEATHERS: tuple[str, ...] = ("clear", "cloudy", "rain", "sakura")

WEATHER_META: dict[str, dict[str, Any]] = {
    "clear": {
        "label": "晴朗",
        "icon": "☀️",
        "outdoor_desc": "晴空万里，微风和煦",
        "indoor_desc": "窗外天色晴朗",
        "visibility_delta": 0,
        "outdoor_thermal_rate": 1.0,
        "day_tint": "",
    },
    "cloudy": {
        "label": "多云",
        "icon": "⛅",
        "outdoor_desc": "云层轻遮，天色微阴",
        "indoor_desc": "窗外云影微阴",
        "visibility_delta": 0,
        "outdoor_thermal_rate": 1.08,
        "day_tint": "rgba(165, 178, 198, 0.10)",
    },
    "rain": {
        "label": "细雨",
        "icon": "🌧️",
        "outdoor_desc": "户外正下着细雨",
        "indoor_desc": "窗外正下着细雨",
        "visibility_delta": -3,
        "outdoor_thermal_rate": 1.25,
        "day_tint": "rgba(68, 92, 132, 0.18)",
    },
    "sakura": {
        "label": "飘樱",
        "icon": "🌸",
        "outdoor_desc": "微风里飘着零星樱花瓣",
        "indoor_desc": "窗外飘过几瓣樱花",
        "visibility_delta": 0,
        "outdoor_thermal_rate": 1.0,
        "day_tint": "rgba(255, 210, 226, 0.08)",
    },
    "snow": {
        "label": "轻雪",
        "icon": "❄️",
        "outdoor_desc": "空中飘着细腻的雪花",
        "indoor_desc": "窗外正飘着细雪",
        "visibility_delta": -2,
        "outdoor_thermal_rate": 1.35,
        "day_tint": "rgba(205, 224, 248, 0.14)",
    },
}

_TRANSITIONS: dict[str, tuple[tuple[str, float], ...]] = {
    "clear": (("clear", 0.50), ("cloudy", 0.24), ("sakura", 0.14), ("rain", 0.08), ("snow", 0.04)),
    "cloudy": (("clear", 0.44), ("cloudy", 0.22), ("rain", 0.22), ("sakura", 0.07), ("snow", 0.05)),
    "rain": (("cloudy", 0.42), ("clear", 0.36), ("rain", 0.18), ("sakura", 0.04)),
    "sakura": (("clear", 0.52), ("sakura", 0.24), ("cloudy", 0.18), ("rain", 0.06)),
    "snow": (("cloudy", 0.40), ("clear", 0.35), ("snow", 0.20), ("sakura", 0.05)),
}


def _default_intensity(code: str) -> float:
    if code == "clear":
        return 0.0
    if code in ("rain", "snow"):
        return 0.6
    return 0.5


def _clean_allowed(raw: Any) -> list[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return list(DEFAULT_ALLOWED_WEATHERS)
    cleaned: list[str] = []
    for item in raw:
        code = str(item or "").strip().lower()
        if code in WEATHER_META and code not in cleaned:
            cleaned.append(code)
    return cleaned or list(DEFAULT_ALLOWED_WEATHERS)


def default_weather_state(
    sim_minutes: float = 0.0,
    *,
    mode: str = "auto",
    current: str = "clear",
    intensity: float | None = None,
    allowed: Sequence[str] | None = None,
) -> dict[str, Any]:
    """创建初始天气状态（默认晴朗，首次自动切变在 240 模拟分钟后）。"""
    clean_mode = str(mode or "auto").strip().lower()
    if clean_mode not in VALID_WEATHER_MODES:
        clean_mode = "auto"
    clean_current = str(current or "clear").strip().lower()
    if clean_mode != "auto":
        clean_current = clean_mode
    elif clean_current not in WEATHER_META:
        clean_current = "clear"

    if clean_current == "clear":
        eff_intensity = 0.0
    elif intensity is None or float(intensity) <= 0.0:
        eff_intensity = _default_intensity(clean_current)
    else:
        eff_intensity = clamp_float(intensity, 0.15, 1.0, _default_intensity(clean_current))

    now_min = max(0.0, float(sim_minutes or 0.0))
    return {
        "mode": clean_mode,
        "current": clean_current,
        "intensity": round(eff_intensity, 3),
        "allowed": _clean_allowed(allowed),
        "changed_sim_min": round(now_min, 3),
        "next_change_sim_min": round(now_min + 240.0, 3),
        "transition_seq": 0,
    }


def normalize_weather_state(raw: Mapping[str, Any] | None, *, sim_minutes: float = 0.0) -> dict[str, Any]:
    """规范化持久化天气字典，兼容空值与手动锁定模式。"""
    if not isinstance(raw, Mapping):
        return default_weather_state(sim_minutes)
    now_min = max(0.0, float(sim_minutes or 0.0))
    mode = str(raw.get("mode") or "auto").strip().lower()
    if mode not in VALID_WEATHER_MODES:
        mode = "auto"
    current = str(raw.get("current") or "clear").strip().lower()
    if mode != "auto":
        current = mode
    elif current not in WEATHER_META:
        current = "clear"

    if current == "clear":
        intensity = 0.0
    else:
        raw_int = raw.get("intensity")
        parsed_int = clamp_float(raw_int, 0.0, 1.0, _default_intensity(current))
        intensity = _default_intensity(current) if parsed_int <= 0.0 else max(0.15, parsed_int)

    try:
        seq = max(0, int(raw.get("transition_seq") or 0))
    except (TypeError, ValueError):
        seq = 0
    changed_min = clamp_float(raw.get("changed_sim_min"), 0.0, 1e12, now_min)
    next_min = clamp_float(raw.get("next_change_sim_min"), 0.0, 1e12, now_min + 240.0)
    if next_min <= 0.0 and mode == "auto":
        next_min = now_min + 240.0

    return {
        "mode": mode,
        "current": current,
        "intensity": round(intensity, 3),
        "allowed": _clean_allowed(raw.get("allowed")),
        "changed_sim_min": round(changed_min, 3),
        "next_change_sim_min": round(next_min, 3),
        "transition_seq": seq,
    }


def _seed_floats(seed_text: str) -> tuple[float, float, float]:
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    u1 = int.from_bytes(digest[0:4], "big") / 0x1_0000_0000
    u2 = int.from_bytes(digest[4:8], "big") / 0x1_0000_0000
    u3 = int.from_bytes(digest[8:12], "big") / 0x1_0000_0000
    return u1, u2, u3


def advance_weather(
    state: Mapping[str, Any] | None,
    sim_minutes: float,
    *,
    realm_seed: str = "",
) -> tuple[dict[str, Any], bool]:
    """按模拟分钟确定性推进马尔可夫天气状态机；返回 `(new_state, code_changed)`。"""
    st = normalize_weather_state(state, sim_minutes=sim_minutes)
    now_min = max(0.0, float(sim_minutes or 0.0))
    if st["mode"] != "auto" or now_min < float(st["next_change_sim_min"]):
        return st, False

    orig_code = st["current"]
    allowed = list(st["allowed"])
    allowed_set = set(allowed)
    cur = orig_code
    seq = int(st["transition_seq"])
    changed_min = float(st["changed_sim_min"])
    due_min = float(st["next_change_sim_min"])
    intensity = float(st["intensity"])

    step_idx = 0
    while now_min >= due_min and step_idx < 12:
        step_idx += 1
        u_choice, u_dur, u_int = _seed_floats(f"{realm_seed}:{int(round(due_min))}:{seq}:{step_idx}")
        pool = [(code, w) for code, w in _TRANSITIONS.get(cur, _TRANSITIONS["clear"]) if code in allowed_set]
        if not pool:
            pool = [(allowed[0], 1.0)]
        total_w = sum(w for _c, w in pool)
        pick = u_choice * total_w
        acc = 0.0
        nxt = pool[-1][0]
        for code, weight in pool:
            acc += weight
            if pick <= acc:
                nxt = code
                break
        if nxt != cur:
            cur = nxt
            seq += 1
            changed_min = due_min
            intensity = 0.0 if cur == "clear" else round(0.40 + u_int * 0.40, 3)
        elif cur != "clear":
            intensity = round(0.40 + u_int * 0.40, 3)
        due_min += round(180.0 + u_dur * 180.0, 3)

    if due_min <= now_min:
        due_min = now_min + 240.0

    updated = {
        **st,
        "current": cur,
        "intensity": round(intensity, 3),
        "changed_sim_min": round(changed_min, 3),
        "next_change_sim_min": round(due_min, 3),
        "transition_seq": seq,
    }
    return updated, (cur != orig_code)


def weather_view(state: Mapping[str, Any] | None, *, is_outdoor: bool) -> dict[str, Any]:
    """生成给前端、感知帧与投影器消费的天气视图。"""
    st = normalize_weather_state(state)
    code = st["current"]
    meta = WEATHER_META.get(code, WEATHER_META["clear"])
    return {
        "mode": st["mode"],
        "current": code,
        "label": meta["label"],
        "icon": meta["icon"],
        "intensity": st["intensity"],
        "is_outdoor": bool(is_outdoor),
        "is_precip": code in ("rain", "snow"),
        "description": meta["outdoor_desc"] if is_outdoor else meta["indoor_desc"],
        "visibility_delta": int(meta["visibility_delta"]),
        "outdoor_thermal_rate": float(meta["outdoor_thermal_rate"]),
        "day_tint": meta["day_tint"],
        "transition_seq": int(st["transition_seq"]),
        "allowed": list(st["allowed"]),
        "next_change_sim_min": st["next_change_sim_min"],
    }


def weather_persona_event_spec(
    code: str,
    intensity: float,
    *,
    is_outdoor: bool,
    prev_code: str = "",
) -> dict[str, Any] | None:
    """生成与 `mind.mjs --world-event` 兼容的天气心理事件载荷。"""
    clean = str(code or "clear").strip().lower()
    prev = str(prev_code or "").strip().lower()
    _ = intensity
    if clean == "rain":
        if is_outdoor:
            return {
                "kind": "weather_rain_exposed",
                "need": "safety",
                "epa": {"e": -0.25, "p": -0.20, "a": 0.15},
                "label": "户外下起了细雨",
            }
        return {
            "kind": "weather_rain_cozy",
            "need": "safety",
            "epa": {"e": 0.25, "p": 0.10, "a": -0.25},
            "label": "听见窗外下起了细雨，屋里很安稳",
        }
    if clean == "snow":
        if is_outdoor:
            return {
                "kind": "weather_snow_exposed",
                "need": "safety",
                "epa": {"e": -0.18, "p": -0.15, "a": 0.12},
                "label": "户外飘起了细雪",
            }
        return {
            "kind": "weather_snow_cozy",
            "need": "safety",
            "epa": {"e": 0.28, "p": 0.10, "a": -0.22},
            "label": "看见窗外飘起细雪，屋里很安稳",
        }
    if clean == "sakura" and is_outdoor:
        return {
            "kind": "weather_sakura",
            "need": "competence",
            "epa": {"e": 0.32, "p": 0.15, "a": 0.08},
            "label": "微风里飘起了樱花瓣",
        }
    if clean == "clear" and is_outdoor and prev in ("rain", "snow", "cloudy"):
        return {
            "kind": "weather_cleared",
            "need": "safety",
            "epa": {"e": 0.28, "p": 0.18, "a": 0.10},
            "label": "天空放晴了",
        }
    return None


def weather_cognitive_modifiers(
    weather_data: Mapping[str, Any] | None,
    behavior_profile: Mapping[str, Any] | None,
    *,
    is_outdoor: bool,
) -> dict[str, float]:
    """根据当前天气与人格画像返回软认知调制系数。"""
    w = weather_data if isinstance(weather_data, Mapping) else {}
    bp = behavior_profile if isinstance(behavior_profile, Mapping) else {}
    code = str(w.get("current") or "clear").strip().lower()
    intensity = clamp_float(w.get("intensity"), 0.0, 1.0, 0.0)
    if code == "clear" or intensity <= 0.0:
        return {
            "outdoor_explore_delta": 0.0,
            "shelter_visit_bonus": 0.0,
            "exit_rain_penalty": 0.0,
            "warm_drink_bonus": 0.0,
            "cozy_indoor_bonus": 0.0,
            "outdoor_walk_speed_mult": 1.0,
        }

    safety_need = clamp_float(bp.get("safety_need"), 0.0, 1.0, 0.45)
    social_avoid = clamp_float(bp.get("social_avoidance"), 0.0, 1.0, 0.50)
    arousal = clamp_float(bp.get("arousal_baseline"), 0.0, 1.0, 0.35)

    if code in ("rain", "snow"):
        precip_scale = intensity * (1.0 if code == "rain" else 0.90)
        return {
            "outdoor_explore_delta": round(-precip_scale * (0.45 + 0.95 * safety_need), 4) if is_outdoor else 0.0,
            "shelter_visit_bonus": round(precip_scale * (0.55 + 0.85 * safety_need), 4) if is_outdoor else 0.0,
            "exit_rain_penalty": round(-precip_scale * (0.55 + 0.90 * safety_need), 4) if not is_outdoor else 0.0,
            "warm_drink_bonus": round(0.35 * precip_scale, 4),
            "cozy_indoor_bonus": round(0.25 * precip_scale * (1.0 - arousal), 4) if not is_outdoor else 0.0,
            "outdoor_walk_speed_mult": round(1.0 + 0.18 * precip_scale * safety_need, 4) if is_outdoor else 1.0,
        }

    if code == "sakura":
        sakura_bonus = round(0.18 * intensity * (0.5 + 0.5 * (1.0 - social_avoid)), 4) if is_outdoor else 0.0
        return {
            "outdoor_explore_delta": sakura_bonus,
            "shelter_visit_bonus": 0.0,
            "exit_rain_penalty": 0.0,
            "warm_drink_bonus": 0.0,
            "cozy_indoor_bonus": 0.0,
            "outdoor_walk_speed_mult": 1.0,
        }

    return {
        "outdoor_explore_delta": 0.0,
        "shelter_visit_bonus": 0.0,
        "exit_rain_penalty": 0.0,
        "warm_drink_bonus": round(0.12 * intensity, 4),
        "cozy_indoor_bonus": 0.0,
        "outdoor_walk_speed_mult": 1.0,
    }
