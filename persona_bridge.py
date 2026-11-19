"""与 `nanami_persona` 的零耦合只读文件桥（双方绝不相互 import）。"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def _find_base_persona_dir(project_root: Path | None, custom_persona_dir: str = "") -> Path | None:
    candidates: list[Path] = []
    if custom_persona_dir:
        candidates.append(Path(custom_persona_dir).expanduser())
    if project_root:
        candidates.append(Path(project_root) / "data" / "nanami")
    else:
        candidates.append(Path("data/nanami"))
    return next((p for p in candidates if p.is_dir()), None)


def _resolve_persona_state_root(base_nanami_dir: Path, host_character: str, *, strict: bool = False) -> Path | None:
    """从 pointers.yaml 找到角色对应的 state_root（含默认隔离目录 profiles/<id>/runtime）。"""
    target = str(host_character or "").strip()
    pointers_file = base_nanami_dir / "pointers.yaml"
    if not pointers_file.is_file():
        if not target or target == "七海千秋":
            return base_nanami_dir
        return None if strict else base_nanami_dir
    try:
        text = pointers_file.read_text(encoding="utf-8")
    except OSError:
        return base_nanami_dir

    parsed: Any = None
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(text)
    except Exception:
        parsed = None

    entries: list[dict[str, str]] = []
    if isinstance(parsed, Mapping) and isinstance(parsed.get("pointers"), list):
        for item in parsed["pointers"]:
            if isinstance(item, Mapping):
                entries.append({
                    "id": str(item.get("id") or "").strip(),
                    "character_name": str(item.get("character_name") or "").strip(),
                    "state_root": str(item.get("state_root") or "").strip(),
                })
    else:
        cur: dict[str, str] | None = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("- "):
                if cur:
                    entries.append(cur)
                cur = {"id": "", "character_name": "", "state_root": ""}
                s = s[2:].strip()
            if cur is None:
                continue
            if s.startswith("id:"):
                cur["id"] = s.split(":", 1)[1].strip().strip("'\"")
            elif s.startswith("character_name:"):
                cur["character_name"] = s.split(":", 1)[1].strip().strip("'\"")
            elif s.startswith("state_root:"):
                cur["state_root"] = s.split(":", 1)[1].strip().strip("'\"")
        if cur:
            entries.append(cur)

    for row in entries:
        if row.get("character_name") != target:
            continue
        raw_sr = str(row.get("state_root") or "").strip()
        if raw_sr:
            cand = Path(raw_sr).expanduser()
            if not cand.is_absolute():
                cand = (base_nanami_dir / cand).resolve()
            return cand
        pid = str(row.get("id") or "").strip()
        if not pid or pid == "nanami_chiaki":
            return base_nanami_dir
        return (base_nanami_dir / "profiles" / pid / "runtime").resolve()

    if not target or target == "七海千秋":
        return base_nanami_dir
    return None if strict else base_nanami_dir


def read_persona_state(
    project_root: Path,
    host_character: str,
    *,
    space_id: str = "",
    actor_id: str = "",
    custom_persona_dir: str = "",
) -> dict[str, Any] | None:
    """只读读取 nanami_persona 的心理与特质快照（按 space_id 隔离，防止不同世界串味）。"""
    base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)
    if base_dir is None:
        return None

    state_root = _resolve_persona_state_root(base_dir, str(host_character or "").strip(), strict=True)
    if state_root is None or not state_root.is_dir():
        return None
    mind_raw = _read_json(state_root / "state" / "mind.json")
    if not mind_raw:
        return None

    eff_sid = str(space_id or "").strip()
    matched_entries: list[dict[str, Any]] = []
    if eff_sid:
        needle_exact = f"world:{eff_sid}:{actor_id}" if actor_id else ""
        needle_space = f"world:{eff_sid}:"
        for k, v in mind_raw.items():
            if not isinstance(v, dict):
                continue
            ks = str(k)
            if (needle_exact and needle_exact in ks) or (needle_space in ks):
                matched_entries.append(v)

    if matched_entries:
        entries = matched_entries
    elif eff_sid in ("", "room_default"):
        entries = [v for v in mind_raw.values() if isinstance(v, dict)]
    else:
        # 非默认世界且尚无该世界专属 mind.json 记录时，不继承主世界状态，维持该世界独立心情
        return None

    if not entries:
        return None
    latest = max(entries, key=lambda e: float(e.get("updatedAt") or 0))
    att = latest.get("attitude") if isinstance(latest.get("attitude"), dict) else {}

    traits_raw = _read_json(state_root / "state" / "traits.json") or {}
    traits_inner = traits_raw.get("traits") if isinstance(traits_raw.get("traits"), dict) else traits_raw
    dom_eff = traits_inner.get("domainEfficacy") if isinstance(traits_inner.get("domainEfficacy"), dict) else {}
    preferred_domains = [str(k) for k, v in dom_eff.items() if isinstance(v, (int, float)) and float(v) >= 0.65]
    if not preferred_domains:
        preferred_domains = ["game"]

    return {
        "energy": float(latest.get("energy", 0.72)),
        "sleep_pressure": float(latest.get("sleepPressure", 0.30)),
        "mood_v": float(latest.get("moodV", 0.15)),
        "agenda": str(latest.get("agenda") or ""),
        "disengaged": bool(att.get("disengaged", False)),
        "preferred_domains": preferred_domains,
        "updated_at": float(latest.get("updatedAt") or 0.0),
        "asleep": bool(latest.get("asleep", False)),
        "source": "nanami_persona",
    }


def write_world_bridge(engine: Any, actor_id: str, bridge_dir: Path, *, view_text: str = "") -> Path | None:
    """向 bridge/<actor_id>.json 原子写出只读世界摘要，供外部人格插件按需消费。"""
    snap = engine.snapshot_for_ui()
    actor = next((a for a in snap["actors"] if a["actor_id"] == actor_id), None)
    if actor is None:
        return None
    sid = actor["space_id"]
    if sid != snap["active_space_id"]:
        snap = engine.snapshot_for_ui(sid)
    space = snap["space"]
    lighting = snap["lighting"]
    recent_tags: list[str] = []
    for ent in snap["entities"]:
        for tag in ent.get("tags") or []:
            if tag not in recent_tags:
                recent_tags.append(tag)

    payload = {
        "writtenAt": int(time.time() * 1000),
        "actorId": actor_id,
        "actorName": actor["name"],
        "spaceId": sid,
        "spaceName": space["name"],
        "x": actor["x"],
        "y": actor["y"],
        "posture": actor["posture"],
        "restingOn": actor.get("resting_on_id"),
        "holdingId": actor.get("holding_id"),
        "autonomy": actor.get("autonomy") or {},
        "lighting": "dark" if lighting.get("is_dark") else "lit",
        "period": lighting.get("period"),
        "recentItemTags": recent_tags[:12],
        "recentSummary": "; ".join(engine.peek_diffs(sid)[-3:]),
        "viewText": view_text,
    }
    target = Path(bridge_dir) / f"{actor_id}.json"
    try:
        _atomic_write_json(target, payload)
        return target
    except OSError:
        logger.debug("nanami_world: 写出 bridge 文件失败: %s", target, exc_info=True)
        return None


# ── 态度与情感指针桥接（读取 nanami_persona state/relations.json + 世界社交账本）──
def _clamp_f(val: Any, lo: float, hi: float, default: float) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if num != num:
        return default
    return round(max(lo, min(hi, num)), 3)


def _default_rel(tkey: str, target_name: str) -> dict[str, Any]:
    is_user = tkey == "user"
    return {
        "target_key": tkey,
        "target_type": "user" if is_user else "character",
        "target_name": "用户" if is_user else target_name,
        "known": False,
        "rel_stage": "stranger",
        "attitude_stance": "neutral",
        "call_name": "",
        "relation_tag": "",
        "shared_memories": "",
        "attitude_score": 0.0,
        "affective_valence": 0.0,
        "trust": 0.55,
        "attraction": 0.35,
        "repulsion": 0.0,
        "warmth": 0.45,
        "social_willingness": 0.50,
        "exchange_balance": 0.0,
        "relational_debt": 0.0,
        "cool_off_turns": 0,
        "rounds": 0,
        "last_interaction": "",
        "manual_override": False,
        "updated_at": 0.0,
    }


def _coerce_rel(tkey: str, raw: Mapping[str, Any] | None, *, fallback_name: str = "") -> dict[str, Any]:
    default_name = "用户" if tkey == "user" else (fallback_name or (tkey.split(":", 1)[1] if ":" in tkey else tkey))
    out = _default_rel(tkey, default_name)
    if not isinstance(raw, Mapping):
        return out
    known = bool(raw.get("known", False))
    rel_stage = str(raw.get("rel_stage") or ("acquainted" if known else "stranger")).strip().lower()
    if tkey != "user":
        if not known:
            rel_stage = "stranger"
        elif rel_stage == "stranger":
            rel_stage = "acquainted"
    out.update({
        "target_name": str(raw.get("target_name") or default_name).strip()[:32],
        "known": known,
        "rel_stage": rel_stage,
        "attitude_stance": str(raw.get("attitude_stance") or "neutral").strip().lower(),
        "call_name": str(raw.get("call_name") or "").strip()[:24],
        "relation_tag": str(raw.get("relation_tag") or "").strip()[:40],
        "shared_memories": str(raw.get("shared_memories") or "").strip()[:400],
        "attitude_score": _clamp_f(raw.get("attitude_score"), -1.0, 1.0, 0.0),
        "affective_valence": _clamp_f(raw.get("affective_valence"), -1.0, 1.0, 0.0),
        "trust": _clamp_f(raw.get("trust"), 0.0, 1.0, 0.55),
        "attraction": _clamp_f(raw.get("attraction"), 0.0, 1.0, 0.35),
        "repulsion": _clamp_f(raw.get("repulsion"), 0.0, 1.0, 0.0),
        "warmth": _clamp_f(raw.get("warmth"), 0.0, 1.0, 0.45),
        "social_willingness": _clamp_f(raw.get("social_willingness"), 0.0, 1.0, 0.50),
        "exchange_balance": _clamp_f(raw.get("exchange_balance"), -1.0, 1.0, 0.0),
        "relational_debt": _clamp_f(raw.get("relational_debt"), 0.0, 1.0, 0.0),
        "cool_off_turns": max(0, int(raw.get("cool_off_turns") or 0)),
        "rounds": max(0, int(raw.get("rounds") or 0)),
        "last_interaction": str(raw.get("last_interaction") or "").strip()[:180],
        "manual_override": bool(raw.get("manual_override", False)),
        "updated_at": round(float(raw.get("updated_at") or 0.0), 3),
    })
    return out


def _world_relations_path(world_data_root: Path) -> Path:
    return Path(world_data_root) / "social_relations.json"


def _read_persona_relations_targets(state_root: Path) -> dict[str, dict[str, Any]]:
    raw = _read_json(Path(state_root) / "state" / "relations.json") or {}
    targets = raw.get("targets") if isinstance(raw.get("targets"), dict) else raw
    out: dict[str, dict[str, Any]] = {}
    if isinstance(targets, Mapping):
        for k, v in targets.items():
            if isinstance(v, Mapping):
                out[str(k)] = dict(v)
    return out


def _read_user_fallback_from_mind(state_root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    person_raw = _read_json(Path(state_root) / "state" / "person.json") or {}
    p_entries = [v for v in person_raw.values() if isinstance(v, Mapping)]
    if p_entries:
        latest_p = max(p_entries, key=lambda e: float(e.get("at") or 0.0))
        uname = str(latest_p.get("name") or "").strip()
        if uname:
            out["known"] = True
            out["call_name"] = uname
            out["relation_tag"] = "相熟的用户"
    mind_raw = _read_json(Path(state_root) / "state" / "mind.json") or {}
    m_entries = [v for v in mind_raw.values() if isinstance(v, Mapping)]
    if m_entries:
        latest_m = max(m_entries, key=lambda e: float(e.get("updatedAt") or 0.0))
        att = latest_m.get("attitude") if isinstance(latest_m.get("attitude"), Mapping) else {}
        soc = latest_m.get("sociology") if isinstance(latest_m.get("sociology"), Mapping) else {}
        out["trust"] = _clamp_f(att.get("respectTrust", latest_m.get("trust", 0.6)), 0.0, 1.0, 0.6)
        out["attitude_score"] = _clamp_f(att.get("attitudeScore", 0.0), -1.0, 1.0, 0.0)
        out["affective_valence"] = _clamp_f(att.get("affectiveValence", 0.0), -1.0, 1.0, 0.0)
        out["attraction"] = _clamp_f(soc.get("attraction", 0.4), 0.0, 1.0, 0.4)
        out["repulsion"] = _clamp_f(soc.get("repulsion", 0.0), 0.0, 1.0, 0.0)
        out["warmth"] = _clamp_f(latest_m.get("moodWarmth", 0.45), 0.0, 1.0, 0.45)
        out["cool_off_turns"] = max(0, int(soc.get("coolOffTurns") or 0))
        out["rounds"] = max(0, int(latest_m.get("rounds") or 0))
        if out.get("known") and out["attitude_score"] >= 0.35:
            out["rel_stage"] = "friend"
        elif out.get("known"):
            out["rel_stage"] = "acquainted"
        else:
            out["rel_stage"] = "stranger"
    return out


def _read_relations_doc(world_data_root: Path) -> dict[str, Any]:
    raw = _read_json(_world_relations_path(world_data_root)) or {}
    worlds = raw.get("worlds") if isinstance(raw.get("worlds"), Mapping) else None
    if isinstance(worlds, Mapping):
        clean_worlds: dict[str, dict[str, Any]] = {}
        for sid, bucket in worlds.items():
            if not isinstance(bucket, Mapping):
                continue
            edges = bucket.get("edges") if isinstance(bucket.get("edges"), Mapping) else {}
            clean_worlds[str(sid)] = {
                "edges": {
                    str(subj): {
                        str(tk): dict(val)
                        for tk, val in tgt_map.items()
                        if isinstance(val, Mapping)
                    }
                    for subj, tgt_map in edges.items()
                    if isinstance(tgt_map, Mapping)
                }
            }
        return {"version": 2, "worlds": clean_worlds}

    # 兼容 v1：旧版顶层 edges 归属默认房间 room_default，绝不污染其他世界
    legacy_edges = raw.get("edges") if isinstance(raw.get("edges"), Mapping) else {}
    if legacy_edges:
        return {
            "version": 2,
            "worlds": {
                "room_default": {
                    "edges": {
                        str(subj): {
                            str(tk): dict(val)
                            for tk, val in tgt_map.items()
                            if isinstance(val, Mapping)
                        }
                        for subj, tgt_map in legacy_edges.items()
                        if isinstance(tgt_map, Mapping)
                    }
                }
            },
        }
    return {"version": 2, "worlds": {}}


def _load_world_edges(world_data_root: Path, space_id: str) -> dict[str, dict[str, Any]]:
    sid = str(space_id or "room_default").strip() or "room_default"
    doc = _read_relations_doc(world_data_root)
    bucket = doc["worlds"].get(sid) or {}
    edges = bucket.get("edges")
    return dict(edges) if isinstance(edges, Mapping) else {}


def _save_world_edges(world_data_root: Path, space_id: str, space_edges: Mapping[str, Any]) -> None:
    sid = str(space_id or "room_default").strip() or "room_default"
    doc = _read_relations_doc(world_data_root)
    doc["worlds"][sid] = {"edges": {str(k): dict(v) for k, v in space_edges.items() if isinstance(v, Mapping)}}
    doc["updatedAt"] = round(time.time(), 3)
    try:
        _atomic_write_json(_world_relations_path(world_data_root), doc)
    except OSError:
        logger.debug("nanami_world: 写出 social_relations.json 失败", exc_info=True)


def save_world_actor_relation(
    world_data_root: Path,
    space_id: str,
    subject_character: str,
    target_key: str,
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """针对指定世界（space_id）保存一条角色社交指针，独立于其他世界。"""
    sid = str(space_id or "room_default").strip() or "room_default"
    subj = str(subject_character or "").strip()
    tkey = str(target_key or "").strip()
    if not subj or not tkey or (tkey != "user" and not tkey.startswith("char:")):
        raise ValueError(f"社交指针目标非法：{target_key}")
    if tkey == f"char:{subj}":
        raise ValueError("社交指针不能指向角色自身。")

    edges = _load_world_edges(world_data_root, sid)
    subj_map = dict(edges.get(subj) or {})
    cur = dict(subj_map.get(tkey) or {})
    cur.update(dict(patch or {}))
    cur["manual_override"] = True
    cur["updated_at"] = round(time.time(), 3)
    coerced = _coerce_rel(tkey, cur)
    subj_map[tkey] = coerced
    edges[subj] = subj_map
    _save_world_edges(world_data_root, sid, edges)
    return coerced


def delete_world_relations(world_data_root: Path, space_id: str) -> None:
    """删除世界时清理该世界对应的社交指针桶。"""
    sid = str(space_id or "").strip()
    if not sid or world_data_root is None:
        return
    wpath = _world_relations_path(world_data_root)
    if not wpath.is_file():
        return
    doc = _read_relations_doc(world_data_root)
    if sid in doc["worlds"]:
        doc["worlds"].pop(sid, None)
        doc["updatedAt"] = round(time.time(), 3)
        try:
            _atomic_write_json(wpath, doc)
        except OSError:
            logger.debug("nanami_world: 清理已删世界社交指针失败", exc_info=True)


def read_actor_relations(
    project_root: Path,
    host_character: str,
    *,
    space_id: str = "room_default",
    world_data_root: Path | None = None,
    custom_persona_dir: str = "",
    known_actors: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """按世界（space_id）读取角色的态度与情感指针；不同世界之间完全隔离。"""
    eff_sid = str(space_id or "room_default").strip() or "room_default"
    subj = str(host_character or "").strip()
    persona_targets: dict[str, dict[str, Any]] = {}
    user_live: dict[str, Any] = {}

    if eff_sid == "room_default":
        base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)
        if base_dir is not None:
            sr = _resolve_persona_state_root(base_dir, subj, strict=True)
            if sr is not None and sr.is_dir():
                persona_targets = _read_persona_relations_targets(sr)
                user_live = _read_user_fallback_from_mind(sr)

    world_targets: dict[str, dict[str, Any]] = {}
    if world_data_root is not None:
        edges = _load_world_edges(world_data_root, eff_sid)
        if isinstance(edges.get(subj), Mapping):
            world_targets = {str(k): dict(v) for k, v in edges[subj].items() if isinstance(v, Mapping)}

    candidate_keys: list[str] = ["user"]
    seen: set[str] = {"user"}

    def _add_key(tkey: str) -> None:
        if not tkey or tkey in seen:
            return
        if tkey.startswith("char:") and tkey.split(":", 1)[1].strip() == subj:
            return
        seen.add(tkey)
        candidate_keys.append(tkey)

    for cname in known_actors or ():
        clean = str(cname or "").strip()
        if clean and clean != subj:
            _add_key(f"char:{clean}")
    for k in list(persona_targets.keys()) + list(world_targets.keys()):
        if k == "user" or k.startswith("char:"):
            _add_key(k)

    result: dict[str, dict[str, Any]] = {}
    for tkey in candidate_keys:
        p_entry = persona_targets.get(tkey)
        w_entry = world_targets.get(tkey)
        if w_entry and p_entry:
            chosen = w_entry if float(w_entry.get("updated_at") or 0) >= float(p_entry.get("updated_at") or 0) else p_entry
        elif w_entry:
            chosen = w_entry
        elif p_entry:
            chosen = p_entry
        elif tkey == "user" and user_live:
            chosen = user_live
        else:
            chosen = None
        result[tkey] = _coerce_rel(tkey, chosen)
    return result


def _bump_directed_edge(
    cur: Mapping[str, Any],
    *,
    tkey: str,
    target_name: str,
    summary: str,
    first_meet: bool,
    valence_delta: float,
    trust_delta: float,
    warmth_delta: float,
    call_name: str = "",
) -> dict[str, Any]:
    now_ts = round(time.time(), 3)
    out = _coerce_rel(tkey, cur, fallback_name=target_name)
    was_known = bool(out.get("known"))
    if not was_known or first_meet:
        out["known"] = True
        if out.get("rel_stage") == "stranger":
            out["rel_stage"] = "acquainted"
        if not out.get("relation_tag"):
            out["relation_tag"] = "在世界里结识的同伴"
        if call_name and not out.get("call_name"):
            out["call_name"] = call_name[:24]
        if not out.get("shared_memories"):
            out["shared_memories"] = summary[:200]

    out["rounds"] = int(out.get("rounds") or 0) + 1
    out["affective_valence"] = _clamp_f(float(out.get("affective_valence") or 0.0) + valence_delta, -1.0, 1.0, 0.0)
    out["attitude_score"] = _clamp_f(float(out.get("attitude_score") or 0.0) + valence_delta * 0.8, -1.0, 1.0, 0.0)
    out["trust"] = _clamp_f(float(out.get("trust") or 0.55) + trust_delta, 0.0, 1.0, 0.55)
    out["warmth"] = _clamp_f(float(out.get("warmth") or 0.45) + warmth_delta, 0.0, 1.0, 0.45)
    out["attraction"] = _clamp_f(float(out.get("attraction") or 0.35) + valence_delta * 0.4, 0.0, 1.0, 0.35)
    out["social_willingness"] = _clamp_f(float(out.get("social_willingness") or 0.50) + (0.04 if valence_delta >= 0 else -0.05), 0.0, 1.0, 0.50)
    if out["affective_valence"] >= 0.30 and out.get("attitude_stance") in ("neutral", "shy"):
        out["attitude_stance"] = "warm"
    if out["rounds"] >= 4 and out["affective_valence"] >= 0.35 and out["trust"] >= 0.65 and out["rel_stage"] == "acquainted":
        out["rel_stage"] = "friend"
    out["last_interaction"] = str(summary or "")[:160]
    out["updated_at"] = now_ts
    return _coerce_rel(tkey, out, fallback_name=target_name)


def record_world_social_interaction(
    project_root: Path,
    world_data_root: Path,
    actor_a_name: str,
    actor_b_name: str,
    *,
    space_id: str = "room_default",
    summary: str,
    first_meet: bool = False,
    valence_delta: float = 0.06,
    trust_delta: float = 0.04,
    warmth_delta: float = 0.06,
    call_a_to_b: str = "",
    call_b_to_a: str = "",
    custom_persona_dir: str = "",
) -> dict[str, dict[str, Any]]:
    """按世界（space_id）双向记录两名角色的社交互动，各世界社交指针独立持久化互不覆盖。"""
    eff_sid = str(space_id or "room_default").strip() or "room_default"
    a_name = str(actor_a_name or "").strip()
    b_name = str(actor_b_name or "").strip()
    if not a_name or not b_name or a_name == b_name:
        return {}

    rel_a = read_actor_relations(
        project_root,
        a_name,
        space_id=eff_sid,
        world_data_root=world_data_root,
        custom_persona_dir=custom_persona_dir,
        known_actors=[b_name],
    )
    rel_b = read_actor_relations(
        project_root,
        b_name,
        space_id=eff_sid,
        world_data_root=world_data_root,
        custom_persona_dir=custom_persona_dir,
        known_actors=[a_name],
    )

    k_ab = f"char:{b_name}"
    k_ba = f"char:{a_name}"
    next_ab = _bump_directed_edge(
        rel_a.get(k_ab) or {},
        tkey=k_ab,
        target_name=b_name,
        summary=summary,
        first_meet=first_meet,
        valence_delta=valence_delta,
        trust_delta=trust_delta,
        warmth_delta=warmth_delta,
        call_name=call_a_to_b,
    )
    next_ba = _bump_directed_edge(
        rel_b.get(k_ba) or {},
        tkey=k_ba,
        target_name=a_name,
        summary=summary,
        first_meet=first_meet,
        valence_delta=valence_delta,
        trust_delta=trust_delta,
        warmth_delta=warmth_delta,
        call_name=call_b_to_a,
    )

    edges = _load_world_edges(world_data_root, eff_sid)
    a_map = dict(edges.get(a_name) or {})
    b_map = dict(edges.get(b_name) or {})
    a_map[k_ab] = next_ab
    b_map[k_ba] = next_ba
    edges[a_name] = a_map
    edges[b_name] = b_map
    _save_world_edges(world_data_root, eff_sid, edges)

    return {"a_to_b": next_ab, "b_to_a": next_ba}

