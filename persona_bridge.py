"""与 `nanami_persona` 的零耦合只读文件桥（双方绝不相互 import）。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_MIND_WRITE_LOCK = threading.Lock()
_SANDBOX_LOCK = threading.RLock()
_STATIC_SANDBOX_STATE_FILES: tuple[str, ...] = (
    "traits.json",
    "social_traits.json",
    "world_perception.json",
    "scenes.json",
    "her-phrasings.json",
    "psyche-model-cn.json",
)


def _safe_slug(raw: Any, default: str = "item") -> str:
    text = str(raw or "").strip()
    if not text:
        return default
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", text).strip("_-")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    prefix = cleaned[:36] if cleaned else default
    return f"{prefix}_{digest}"


def ensure_isolated_persona_sandbox(
    project_root: Path,
    world_data_root: Path,
    space_id: str,
    host_character: str,
    *,
    custom_persona_dir: str = "",
) -> Path | None:
    """为平行世界创建完全隔离的人格沙盒目录（仅拷贝静态性格种子，绝不拷贝主档心境/关系/记忆）。"""
    if world_data_root is None:
        return None
    target_char = str(host_character or "").strip()
    base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)
    if base_dir is None:
        return None
    src_root, _ = _resolve_persona_pointer(base_dir, target_char, strict=True)
    if src_root is None or not src_root.is_dir():
        return None

    realm_slug = _safe_slug(space_id or "room_default", "realm")
    char_slug = _safe_slug(target_char or "actor", "char")
    sandbox_root = Path(world_data_root) / "isolated_personas" / realm_slug / char_slug
    meta_path = sandbox_root / ".sandbox_meta.json"
    state_dir = sandbox_root / "state"

    with _SANDBOX_LOCK:
        state_dir.mkdir(parents=True, exist_ok=True)
        if not meta_path.is_file():
            src_state = src_root / "state"
            for fname in _STATIC_SANDBOX_STATE_FILES:
                src_file = src_state / fname
                dst_file = state_dir / fname
                if src_file.is_file() and not dst_file.exists():
                    try:
                        shutil.copy2(src_file, dst_file)
                    except OSError:
                        logger.debug("nanami_world: 拷贝沙盒静态文件失败: %s", src_file, exc_info=True)
            try:
                _atomic_write_json(
                    meta_path,
                    {
                        "space_id": str(space_id or ""),
                        "host_character": target_char,
                        "initialized_at": round(time.time(), 3),
                    },
                )
            except OSError:
                logger.debug("nanami_world: 写入沙盒元数据失败: %s", meta_path, exc_info=True)
    return sandbox_root


def delete_isolated_persona_sandbox(world_data_root: Path | None, space_id: str) -> None:
    """删除平行世界时级联清理该世界下全部隔离人格沙盒目录。"""
    sid = str(space_id or "").strip()
    if not sid or world_data_root is None:
        return
    target_dir = Path(world_data_root) / "isolated_personas" / _safe_slug(sid, "realm")
    with _SANDBOX_LOCK:
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)


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


def _resolve_persona_pointer(base_nanami_dir: Path, host_character: str, *, strict: bool = False) -> tuple[Path | None, str]:
    """从 pointers.yaml 找到角色对应的 (state_root, chat_key_prefix)，优先匹配已启用且存在状态文件的指针。"""
    target = str(host_character or "").strip()
    pointers_file = base_nanami_dir / "pointers.yaml"
    if not pointers_file.is_file():
        if not target or target == "七海千秋":
            return base_nanami_dir, "shinsekai"
        return (None if strict else base_nanami_dir), ""
    try:
        text = pointers_file.read_text(encoding="utf-8")
    except OSError:
        return base_nanami_dir, ""

    parsed: Any = None
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(text)
    except Exception:
        parsed = None

    entries: list[dict[str, Any]] = []
    if isinstance(parsed, Mapping) and isinstance(parsed.get("pointers"), list):
        for item in parsed["pointers"]:
            if isinstance(item, Mapping):
                entries.append({
                    "id": str(item.get("id") or "").strip(),
                    "enabled": bool(item.get("enabled", True)),
                    "character_name": str(item.get("character_name") or "").strip(),
                    "state_root": str(item.get("state_root") or "").strip(),
                    "chat_key_prefix": str(item.get("chat_key_prefix") or "").strip(),
                })
    else:
        cur: dict[str, Any] | None = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("- "):
                if cur:
                    entries.append(cur)
                cur = {"id": "", "enabled": True, "character_name": "", "state_root": "", "chat_key_prefix": ""}
                s = s[2:].strip()
            if cur is None:
                continue
            if s.startswith("id:"):
                cur["id"] = s.split(":", 1)[1].strip().strip("'\"")
            elif s.startswith("enabled:"):
                cur["enabled"] = s.split(":", 1)[1].strip().lower() not in ("false", "0", "no")
            elif s.startswith("character_name:"):
                cur["character_name"] = s.split(":", 1)[1].strip().strip("'\"")
            elif s.startswith("state_root:"):
                cur["state_root"] = s.split(":", 1)[1].strip().strip("'\"")
            elif s.startswith("chat_key_prefix:"):
                cur["chat_key_prefix"] = s.split(":", 1)[1].strip().strip("'\"")
        if cur:
            entries.append(cur)

    def _entry_root(row: Mapping[str, Any]) -> Path:
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

    matches = [row for row in entries if row.get("character_name") == target or row.get("id") == target]
    if not matches and target:
        matches = [
            row for row in entries
            if row.get("character_name") and (target in row["character_name"] or row["character_name"] in target)
        ]
    if matches:
        def _rank(row: Mapping[str, Any]) -> tuple[int, int, int]:
            sr = _entry_root(row)
            has_rel = 1 if (sr / "state" / "relations.json").is_file() else 0
            has_mind = 1 if (sr / "state" / "mind.json").is_file() else 0
            return (1 if row.get("enabled", True) else 0, has_rel, has_mind)

        best = max(matches, key=_rank)
        return _entry_root(best), str(best.get("chat_key_prefix") or "")

    if not target or target == "七海千秋":
        return base_nanami_dir, "shinsekai"
    return (None if strict else base_nanami_dir), ""


def _resolve_persona_state_root(base_nanami_dir: Path, host_character: str, *, strict: bool = False) -> Path | None:
    """从 pointers.yaml 找到角色对应的 state_root（含默认隔离目录 profiles/<id>/runtime）。"""
    sr, _prefix = _resolve_persona_pointer(base_nanami_dir, host_character, strict=strict)
    return sr


def read_persona_state(
    project_root: Path,
    host_character: str,
    *,
    space_id: str = "",
    actor_id: str = "",
    custom_persona_dir: str = "",
    world_data_root: Path | None = None,
    isolated_persona: bool = False,
) -> dict[str, Any] | None:
    """只读读取 nanami_persona 的心理与特质快照（按 space_id 隔离，防止不同世界串味）。"""
    eff_sid = str(space_id or "").strip()
    clean_char = str(host_character or "").strip()
    if isolated_persona:
        if world_data_root is None:
            return None
        sandbox_root = ensure_isolated_persona_sandbox(
            project_root,
            world_data_root,
            eff_sid or "room_default",
            clean_char,
            custom_persona_dir=custom_persona_dir,
        )
        if sandbox_root is None or not sandbox_root.is_dir():
            return None
        state_root, chat_prefix = sandbox_root, ""
    else:
        base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)
        if base_dir is None:
            return None
        state_root, chat_prefix = _resolve_persona_pointer(base_dir, clean_char, strict=True)
        if state_root is None or not state_root.is_dir():
            return None

    mind_raw = _read_json(state_root / "state" / "mind.json") or {}

    matched_entries: list[dict[str, Any]] = []
    if eff_sid:
        needle_exact = f"world:{eff_sid}:{actor_id}" if actor_id else ""
        needle_space = f"world:{eff_sid}:"
        for k, v in mind_raw.items():
            if not isinstance(v, dict):
                continue
            ks = str(k)
            if (needle_exact and ks == needle_exact) or (not needle_exact and ks.startswith(needle_space)):
                matched_entries.append(v)

    if matched_entries:
        entries = matched_entries
    elif isolated_persona:
        # 平行世界严禁回退读取主档或其它历史记录
        entries = []
    else:
        # 回退到该角色在人格模型中的主状态记录（排除其他世界的 world: 桶，并按 chat_key_prefix 过滤）
        entries = [
            v for k, v in mind_raw.items()
            if isinstance(v, dict)
            and "world:" not in str(k)
            and (not chat_prefix or str(k).startswith(chat_prefix))
        ] or [
            v for k, v in mind_raw.items()
            if isinstance(v, dict) and "world:" not in str(k)
        ]

    latest = max(entries, key=lambda e: float(e.get("updatedAt") or 0)) if entries else {}
    att = latest.get("attitude") if isinstance(latest.get("attitude"), dict) else {}

    traits_raw = _read_json(state_root / "state" / "traits.json") or {}
    if not latest and not traits_raw and not isolated_persona:
        return None
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
        "world_state": bool(matched_entries),
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
        "weather": snap.get("weather") or {},
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


DEFAULT_RELATIONS_REALM = "__default__"


def _is_meaningful_rel(entry: Mapping[str, Any] | None) -> bool:
    if not isinstance(entry, Mapping):
        return False
    return bool(
        entry.get("known")
        or entry.get("manual_override")
        or int(entry.get("rounds") or 0) > 0
        or str(entry.get("call_name") or "").strip()
        or str(entry.get("relation_tag") or "").strip()
        or str(entry.get("shared_memories") or "").strip()
    )


def _read_persona_relations_targets(state_root: Path) -> dict[str, dict[str, Any]]:
    raw = _read_json(Path(state_root) / "state" / "relations.json") or {}
    targets = raw.get("targets") if isinstance(raw.get("targets"), dict) else raw
    out: dict[str, dict[str, Any]] = {}
    if isinstance(targets, Mapping):
        for k, v in targets.items():
            if isinstance(v, Mapping):
                out[str(k)] = dict(v)
    return out


def _read_user_fallback_from_mind(state_root: Path, *, chat_key_prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    person_raw = _read_json(Path(state_root) / "state" / "person.json") or {}
    p_entries = [
        v for k, v in person_raw.items()
        if isinstance(v, Mapping) and (not chat_key_prefix or str(k).startswith(chat_key_prefix))
    ] or [v for v in person_raw.values() if isinstance(v, Mapping)]
    if p_entries:
        latest_p = max(p_entries, key=lambda e: float(e.get("at") or 0.0))
        uname = str(latest_p.get("name") or "").strip()
        if uname:
            out["known"] = True
            out["call_name"] = uname
            out["relation_tag"] = "相熟的用户"
    mind_raw = _read_json(Path(state_root) / "state" / "mind.json") or {}
    m_entries = [
        v for k, v in mind_raw.items()
        if isinstance(v, Mapping) and (not chat_key_prefix or str(k).startswith(chat_key_prefix))
    ] or [v for v in mind_raw.values() if isinstance(v, Mapping)]
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
    isolated_persona: bool = False,
) -> dict[str, dict[str, Any]]:
    """按世界（space_id）读取角色的态度与情感指针；不同世界之间完全隔离。"""
    eff_sid = str(space_id or "room_default").strip() or "room_default"
    subj = str(host_character or "").strip()
    persona_targets: dict[str, dict[str, Any]] = {}
    user_live: dict[str, Any] = {}
    default_targets: dict[str, dict[str, Any]] = {}

    if not isolated_persona:
        # 非平行世界可继承「默认初始关系」与人格模型中的基础关系指针作为初值，各世界内的后续互动仍写入各自 space_id 桶独立演化
        base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)
        if base_dir is not None:
            sr, chat_prefix = _resolve_persona_pointer(base_dir, subj, strict=True)
            if sr is not None and sr.is_dir():
                persona_targets = _read_persona_relations_targets(sr)
                user_live = _read_user_fallback_from_mind(sr, chat_key_prefix=chat_prefix)
        if world_data_root is not None and eff_sid != DEFAULT_RELATIONS_REALM:
            def_edges = _load_world_edges(world_data_root, DEFAULT_RELATIONS_REALM)
            if isinstance(def_edges.get(subj), Mapping):
                default_targets = {str(k): dict(v) for k, v in def_edges[subj].items() if isinstance(v, Mapping)}

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
    for k in list(persona_targets.keys()) + list(default_targets.keys()) + list(world_targets.keys()):
        if k == "user" or k.startswith("char:"):
            _add_key(k)

    result: dict[str, dict[str, Any]] = {}
    for tkey in candidate_keys:
        p_entry = persona_targets.get(tkey)
        d_entry = default_targets.get(tkey)
        w_entry = world_targets.get(tkey)
        if w_entry and d_entry:
            chosen = w_entry if float(w_entry.get("updated_at") or 0) >= float(d_entry.get("updated_at") or 0) else d_entry
        elif w_entry and p_entry:
            chosen = w_entry if float(w_entry.get("updated_at") or 0) >= float(p_entry.get("updated_at") or 0) else p_entry
        elif w_entry:
            chosen = w_entry
        elif d_entry:
            chosen = d_entry
        elif p_entry:
            chosen = p_entry
        elif tkey == "user" and user_live:
            chosen = user_live
        else:
            chosen = None
        result[tkey] = _coerce_rel(tkey, chosen)
    return result


def resolve_initial_world_relations(
    project_root: Path,
    world_data_root: Path | None,
    subjects: Sequence[str],
    *,
    source_space_id: str = "auto",
    actor_home_realms: Mapping[str, str] | None = None,
    sandbox_realm_ids: Sequence[str] | None = None,
    custom_persona_dir: str = "",
    include_user: bool = True,
) -> dict[str, dict[str, dict[str, Any]]]:
    """为新世界或独立场景解析各角色的初始情感指针（仅复制初值，绝不回写源世界）。"""
    src = str(source_space_id or "auto").strip()
    if src == "story_fresh" or world_data_root is None:
        return {}

    clean_subjects = list(dict.fromkeys(str(s or "").strip() for s in subjects if str(s or "").strip()))
    if not clean_subjects:
        return {}

    doc = _read_relations_doc(world_data_root)
    worlds = doc.get("worlds") if isinstance(doc.get("worlds"), Mapping) else {}
    sandbox_list = list(
        dict.fromkeys(
            str(s or "").strip()
            for s in (sandbox_realm_ids or ())
            if str(s or "").strip() and str(s or "").strip() != DEFAULT_RELATIONS_REALM
        )
    )
    base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)

    out: dict[str, dict[str, dict[str, Any]]] = {}
    for subj in clean_subjects:
        persona_targets: dict[str, dict[str, Any]] = {}
        user_live: dict[str, Any] = {}
        if base_dir is not None:
            sr, chat_prefix = _resolve_persona_pointer(base_dir, subj, strict=True)
            if sr is not None and sr.is_dir():
                persona_targets = _read_persona_relations_targets(sr)
                user_live = _read_user_fallback_from_mind(sr, chat_key_prefix=chat_prefix)

        def _edge_in(realm_id: str, tkey: str) -> dict[str, Any] | None:
            if not realm_id:
                return None
            bucket = worlds.get(realm_id)
            if not isinstance(bucket, Mapping):
                return None
            edges = bucket.get("edges")
            if not isinstance(edges, Mapping):
                return None
            smap = edges.get(subj)
            if not isinstance(smap, Mapping):
                return None
            row = smap.get(tkey)
            return dict(row) if isinstance(row, Mapping) else None

        target_keys: list[str] = (["user"] if include_user else []) + [
            f"char:{other}" for other in clean_subjects if other != subj
        ]
        resolved_subj: dict[str, dict[str, Any]] = {}
        for tkey in target_keys:
            p_cand = persona_targets.get(tkey) or (user_live if tkey == "user" else None)
            chosen: dict[str, Any] | None = None

            if src == DEFAULT_RELATIONS_REALM:
                chosen = _edge_in(DEFAULT_RELATIONS_REALM, tkey)
            elif src and src != "auto":
                chosen = _edge_in(src, tkey) or _edge_in(DEFAULT_RELATIONS_REALM, tkey)
                if chosen is None and _is_meaningful_rel(p_cand):
                    chosen = dict(p_cand)
            else:
                chosen = _edge_in(DEFAULT_RELATIONS_REALM, tkey)
                if chosen is None:
                    home_r = str((actor_home_realms or {}).get(subj) or "").strip()
                    if home_r and home_r in sandbox_list:
                        chosen = _edge_in(home_r, tkey)
                if chosen is None:
                    cands = [
                        edge
                        for r_id in sandbox_list
                        if (edge := _edge_in(r_id, tkey)) is not None
                    ]
                    if cands:
                        chosen = max(
                            cands,
                            key=lambda e: (
                                1 if e.get("manual_override") else 0,
                                float(e.get("updated_at") or 0.0),
                            ),
                        )
                if chosen is None and _is_meaningful_rel(p_cand):
                    chosen = dict(p_cand)

            if chosen is not None:
                resolved_subj[tkey] = _coerce_rel(tkey, chosen)

        if resolved_subj:
            out[subj] = resolved_subj
    return out


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
    isolated_persona: bool = False,
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
        isolated_persona=isolated_persona,
    )
    rel_b = read_actor_relations(
        project_root,
        b_name,
        space_id=eff_sid,
        world_data_root=world_data_root,
        custom_persona_dir=custom_persona_dir,
        known_actors=[a_name],
        isolated_persona=isolated_persona,
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


def evaluate_gift(
    profile: Mapping[str, Any], relation: Mapping[str, Any], recipient: Mapping[str, Any],
    item: Mapping[str, Any],
) -> tuple[bool, float]:
    """评估收礼意愿与物品喜好，使用 L0 人格特征和当前关系指针。"""
    traits = item["traits"]
    activity = traits.get("activity") or {}
    food = traits.get("consumable")
    domain = activity.get("domain")
    if food is not None:
        preference = 0.35 + 0.55 * recipient["autonomy"]["hunger"] if food["uses_left"] else 0.10
    elif domain:
        efficacy = profile["domain_efficacy_game"] if domain == "game" else profile["domain_efficacy_non_game"]
        preference = 0.27 + 0.48 * efficacy
        if domain in recipient["vitals"]["preferred_domains"]:
            preference += 0.25
        if activity.get("battery") == 0:
            preference *= 0.6
    else:
        preference = 0.40
    stance = relation.get("attitude_stance")
    trust = float(relation.get("trust", 0.55))
    willingness = preference + 0.25 * trust + 0.2 * float(profile["norm_courtesy"])
    if stance == "guarded":
        willingness -= 0.22
    if activity and not relation.get("known"):
        willingness -= 0.30
    accepted = stance != "cold" and not relation.get("cool_off_turns") and willingness >= 0.48
    return accepted, round(min(preference, 1.0), 2)


def record_world_gift(
    world_data_root: Path, space_id: str, subject: str, target_key: str, target_name: str,
    relation: Mapping[str, Any], summary: str, impact: float,
) -> None:
    if target_key == f"char:{subject}":
        return
    updated = _bump_directed_edge(
        relation, tkey=target_key, target_name=target_name, summary=summary, first_meet=False,
        valence_delta=impact, trust_delta=impact * 0.35, warmth_delta=impact * 0.75,
    )
    edges = _load_world_edges(world_data_root, space_id)
    edges[subject] = {**(edges.get(subject) or {}), target_key: updated}
    _save_world_edges(world_data_root, space_id, edges)


# ── L0 环境感知与适应层桥接（Itti-Koch / Groves-Thompson / Tolman-ACT-R / Borbély）──
_DEFAULT_L0_CFG: dict[str, Any] = {
    "enabled": True,
    "sensory_radius": 5.0,
    "attentional_breadth": 4,
    "novelty_weight": 0.55,
    "ior_decay": 0.60,
    "initial_caution": 0.65,
    "habituation_rate": 0.25,
    "social_buffering": 0.60,
    "memory_decay": 0.45,
    "chronotype_offset": 0.0,
    "circadian_strength": 0.75,
}


def read_world_perception_config(
    project_root: Path,
    host_character: str,
    *,
    custom_persona_dir: str = "",
) -> dict[str, Any]:
    """只读读取 nanami_persona 的 state/world_perception.json。"""
    out = dict(_DEFAULT_L0_CFG)
    base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)
    if base_dir is None:
        return out
    sr, _pfx = _resolve_persona_pointer(base_dir, str(host_character or "").strip(), strict=True)
    if sr is None or not sr.is_dir():
        return out
    raw = _read_json(sr / "state" / "world_perception.json")
    if not isinstance(raw, Mapping):
        return out
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    for k, dv in _DEFAULT_L0_CFG.items():
        if k != "enabled" and k in raw:
            try:
                out[k] = int(round(float(raw[k]))) if k == "attentional_breadth" else float(raw[k])
            except (TypeError, ValueError):
                out[k] = dv
    return out


def update_and_project_world_perception(
    world_data_root: Path,
    space_id: str,
    actor: Mapping[str, Any],
    entities: Sequence[Mapping[str, Any]],
    cfg: Mapping[str, Any],
    *,
    peer_known: bool = False,
    look_eid: str = "",
    touched_eid: str = "",
    advance_exposure: float = 0.0,
    behavior_profile: Mapping[str, Any] | None = None,
    weather_vis_scale: float = 1.0,
) -> dict[str, Any]:
    """计算 L0 主观注意力焦点 Top-K、更新 Groves-Thompson 熟悉度与 Tolman/ACT-R 认知地图。"""
    if not bool(cfg.get("enabled", True)):
        return {
            "enabled": False,
            "familiarity": 1.0,
            "stage": "settled",
            "focused_entities": list(entities),
            "mental_map_count": len(entities),
            "chronotype_offset": 0.0,
            "circadian_strength": 0.0,
            "initial_caution": 0.0,
        }

    sid = str(space_id or "room_default").strip() or "room_default"
    aid = str(actor.get("actor_id") or "").strip() or "actor"
    p_path = Path(world_data_root) / "perception_state.json"
    doc = _read_json(p_path) or {}
    worlds = doc.get("worlds") if isinstance(doc.get("worlds"), dict) else {}
    sp_bucket = worlds.get(sid) if isinstance(worlds.get(sid), dict) else {}
    cur = dict(sp_bucket.get(aid) or {})

    caution = float(cfg.get("initial_caution", 0.65))
    hab_rate = max(0.05, float(cfg.get("habituation_rate", 0.25)))
    soc_buf = float(cfg.get("social_buffering", 0.60))
    if "familiarity" not in cur:
        fam = 0.35 if sid == "room_default" else max(0.05, round(0.32 - 0.27 * caution, 3))
    else:
        fam = max(0.0, min(1.0, float(cur.get("familiarity") or 0.0)))

    delta_exp = max(0.0, float(advance_exposure))
    if look_eid:
        delta_exp += 0.35
    if touched_eid:
        delta_exp += 0.25
    if peer_known and delta_exp > 0:
        delta_exp *= 1.0 + 0.8 * soc_buf
    if delta_exp > 0:
        fam = 1.0 - (1.0 - fam) * math.exp(-hab_rate * delta_exp)
    fam = round(max(0.0, min(1.0, fam)), 3)

    stage = (
        "unfamiliar" if fam < 0.25
        else ("exploring" if fam < 0.55 else ("familiar" if fam < 0.85 else "settled"))
    )

    mmap = dict(cur.get("mental_map") or {}) if isinstance(cur.get("mental_map"), dict) else {}
    now_ts = round(time.time(), 2)
    vit = actor.get("vitals") or {}
    aut = actor.get("autonomy") or {}
    sleep_p = float(vit.get("sleep_pressure", 0.30))
    hunger = float(aut.get("hunger", 0.25))
    preferred = set(vit.get("preferred_domains") or ["game"])
    recent = list(aut.get("recent_targets") or [])

    base_r = max(1.5, float(cfg.get("sensory_radius", 5.0)))
    eff_r = base_r * (0.72 if sleep_p > 0.70 else 1.0) * max(0.5, min(1.2, float(weather_vis_scale)))
    nov_w = float(cfg.get("novelty_weight", 0.55))
    ior_w = float(cfg.get("ior_decay", 0.60))
    top_k = max(2, min(8, int(cfg.get("attentional_breadth", 4))))

    ax, ay = int(actor.get("x") or 0), int(actor.get("y") or 0)
    scored: list[tuple[float, dict[str, Any]]] = []
    for ent in entities:
        eid = str(ent.get("entity_id") or "")
        ex, ey = int(ent.get("x") or 0), int(ent.get("y") or 0)
        dist = max(abs(ax - ex), abs(ay - ey))
        traits = ent.get("traits") or {}

        # Itti-Koch + Easterbrook 显著度评分（叠加人格特质/域效能自上而下偏置）
        bp = behavior_profile if isinstance(behavior_profile, Mapping) else {}
        s_dist = math.exp(-dist / max(1.5, eff_r))
        s_topdown = 0.0
        if "consumable" in traits and int(traits["consumable"].get("uses_left") or 0) > 0:
            s_topdown += 0.45 * hunger
        if "restable" in traits and sleep_p >= 0.55:
            s_topdown += 0.45 * sleep_p
        if "activity" in traits:
            dom = str(traits["activity"].get("domain") or "")
            if dom in preferred:
                s_topdown += 0.40
            if dom == "game":
                s_topdown += 0.30 * float(bp.get("domain_efficacy_game", 0.5))
        if "switchable" in traits and traits["switchable"].get("provides_light") and not traits["switchable"].get("is_on"):
            s_topdown += 0.35 * float(bp.get("safety_priority", 0.5))
        if "container" in traits and not traits["container"].get("is_open"):
            s_topdown += 0.25 * float(bp.get("exploration_drive", 0.5))
        is_new = eid not in mmap
        s_nov = nov_w * (0.6 if is_new else 0.0)
        s_ior = ior_w * (0.75 if eid in recent[:2] else 0.0)
        total = s_dist + s_topdown + s_nov - s_ior
        scored.append((total, dict(ent)))

        if dist <= eff_r or eid in (look_eid, touched_eid):
            prev_m = mmap.get(eid) if isinstance(mmap.get(eid), dict) else {}
            mmap[eid] = {
                "name": str(ent.get("name") or eid),
                "exposures": int(prev_m.get("exposures") or 0) + (1 if (advance_exposure > 0 or eid in (look_eid, touched_eid) or is_new) else 0),
                "last_seen": now_ts,
            }

    scored.sort(key=lambda pair: pair[0], reverse=True)
    focused = [e for _s, e in scored[:top_k]]

    if advance_exposure > 0 or look_eid or touched_eid or "familiarity" not in cur:
        sp_bucket[aid] = {
            "host_character": str(actor.get("host_character") or actor.get("name") or ""),
            "actor_name": str(actor.get("name") or ""),
            "familiarity": fam,
            "stage": stage,
            "mental_map": mmap,
            "updated_at": now_ts,
        }
        worlds[sid] = sp_bucket
        doc["worlds"] = worlds
        doc["updatedAt"] = now_ts
        try:
            _atomic_write_json(p_path, doc)
        except OSError:
            pass

    return {
        "enabled": True,
        "familiarity": fam,
        "stage": stage,
        "focused_entities": focused,
        "mental_map_count": len(mmap),
        "chronotype_offset": float(cfg.get("chronotype_offset", 0.0)),
        "circadian_strength": float(cfg.get("circadian_strength", 0.75)),
        "initial_caution": caution,
    }


_STAGE_LABELS: dict[str, str] = {
    "unfamiliar": "初入陌生期（刚到此处，还在谨慎打量环境）",
    "exploring": "试探适应期（逐步熟悉中，开始尝试感兴趣的物件）",
    "familiar": "熟悉放松期（对屋里比较习惯，按日常习惯行动）",
    "settled": "归属稳态期（安全基地，非常自在）",
}


def _sensory_item_desc(ent: Mapping[str, Any], actors_map: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """把客观实体转换为无系统遥测（无 ID / 无进度局数）的纯粹感官白描。"""
    name = str(ent.get("name") or "物品").strip()
    traits = ent.get("traits") or {}
    cues: list[str] = []
    if "restable" in traits:
        occ = traits["restable"].get("occupied_by")
        if occ and isinstance(actors_map, Mapping):
            occ_a = actors_map.get(str(occ))
            occ_nm = str(occ_a.get("name") or "").strip() if isinstance(occ_a, Mapping) else ""
            cues.append(f"{occ_nm}正在上面睡觉" if occ_nm else "有人正在上面休息")
        elif occ:
            cues.append("有人在上面休息")
        else:
            cues.append("空着可歇息")
    if "consumable" in traits:
        left = int(traits["consumable"].get("uses_left") or 0)
        verb = str(traits["consumable"].get("consume_verb") or "吃").strip()
        if left > 0:
            temp = str((traits.get("thermal") or {}).get("temp") or "").strip()
            t_cn = "温热" if temp == "hot" else ("凉了" if temp == "cold" else "")
            cues.append(f"{t_cn + '、' if t_cn else ''}还能{verb}")
        else:
            cues.append("已经空了")
    if "activity" in traits:
        dom = str(traits["activity"].get("domain") or "").strip()
        cues.append({"game": "可以玩", "music": "可放音乐", "reading": "可翻阅"}.get(dom, "可互动"))
    if "switchable" in traits:
        cues.append("亮着" if traits["switchable"].get("is_on") else "关着")
    if "container" in traits:
        ct = traits["container"]
        cues.append("上了锁" if ct.get("is_locked") else ("开着" if ct.get("is_open") else "合着"))
    if "portal" in traits:
        cues.append("门开着" if traits["portal"].get("is_open") else "门关着")
    return f"{name}（{'，'.join(cues)}）" if cues else name


def read_persona_behavior_profile(
    project_root: Path,
    host_character: str,
    *,
    custom_persona_dir: str = "",
) -> dict[str, Any]:
    """只读读取 traits.json 与 social_traits.json，映射为基于 L1–L8/S1–S3 真实字段的行为权重。"""
    defaults: dict[str, float] = {
        "effortfulControl": 0.80,
        "arousalBaseline": 0.35,
        "domainEfficacy.game": 0.85,
        "domainEfficacy.nonGame": 0.30,
        "dynamics.hysteresisInertia": 0.65,
        "evaluativeSensitivity": 0.70,
        "needProfile.safety": 0.50,
        "needProfile.relatedness": 0.70,
        "needProfile.competence": 0.70,
        "socialEfficacy": 0.30,
        "surgency": 0.25,
        "attachment.avoidance": 0.55,
        "attachment.anxiety": 0.25,
        "social.affiliation": 0.60,
        "social.achievement": 0.65,
        "social.prosocial": 0.75,
        "social.normCompliance": 0.75,
        "attraction.friendThreshold": 0.42,
    }
    base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)
    merged = dict(defaults)
    state_root: Path | None = None
    if base_dir is not None:
        state_root, _ = _resolve_persona_pointer(base_dir, str(host_character or "").strip(), strict=True)
    if state_root is not None and state_root.is_dir():
        for fname in ("traits.json", "social_traits.json"):
            raw = _read_json(state_root / "state" / fname)
            if not raw:
                continue
            wrapper = "traits" if fname == "traits.json" else "socialTraits"
            src = raw.get(wrapper, raw.get("traits", raw))
            if not isinstance(src, Mapping):
                continue
            for k in defaults:
                if k in src:
                    value = src[k]
                else:
                    parent, _, child = k.partition(".")
                    branch = src.get(parent)
                    value = branch.get(child) if child and isinstance(branch, Mapping) else None
                if value is not None:
                    merged[k] = _clamp_f(value, 0.0, 1.0, merged[k])

    safety_priority = _clamp_f(
        0.50 * merged["needProfile.safety"] + 0.30 * merged["effortfulControl"] + 0.20 * (1.0 - merged["arousalBaseline"]),
        0.0, 1.0, 0.55,
    )
    social_initiative = _clamp_f(
        0.35 * merged["surgency"]
        + 0.30 * merged["socialEfficacy"]
        + 0.25 * merged["social.affiliation"]
        + 0.10 * (1.0 - merged["attachment.avoidance"]),
        0.0, 1.0, 0.38,
    )
    social_avoidance = _clamp_f(
        0.55 * merged["attachment.avoidance"]
        + 0.25 * (1.0 - merged["surgency"])
        + 0.20 * (1.0 - merged["socialEfficacy"]),
        0.0, 1.0, 0.63,
    )
    exploration_drive = _clamp_f(
        0.45 * merged["needProfile.competence"]
        + 0.35 * merged["social.achievement"]
        + 0.20 * (1.0 - merged["needProfile.safety"]),
        0.0, 1.0, 0.64,
    )
    norm_courtesy = _clamp_f(
        0.60 * merged["social.normCompliance"] + 0.40 * merged["social.prosocial"],
        0.0, 1.0, 0.75,
    )
    return {
        "safety_priority": round(safety_priority, 3),
        "social_initiative": round(social_initiative, 3),
        "social_avoidance": round(social_avoidance, 3),
        "exploration_drive": round(exploration_drive, 3),
        "norm_courtesy": round(norm_courtesy, 3),
        "domain_efficacy_game": round(merged["domainEfficacy.game"], 3),
        "domain_efficacy_non_game": round(merged["domainEfficacy.nonGame"], 3),
        "effortful_control": round(merged["effortfulControl"], 3),
        "arousal_baseline": round(merged["arousalBaseline"], 3),
        "hysteresis_inertia": round(merged["dynamics.hysteresisInertia"], 3),
        "safety_need": round(merged["needProfile.safety"], 3),
        "competence_need": round(merged["needProfile.competence"], 3),
        "norm_compliance": round(merged["social.normCompliance"], 3),
        "relatedness_need": round(merged["needProfile.relatedness"], 3),
        "friend_threshold": round(merged["attraction.friendThreshold"], 3),
    }


def emit_world_event_to_persona(
    project_root: Path,
    host_character: str,
    *,
    space_id: str,
    actor_id: str,
    event_payload: Mapping[str, Any],
    custom_persona_dir: str = "",
    thought: bool = False,
    world_data_root: Path | None = None,
    isolated_persona: bool = False,
) -> dict[str, Any] | None:
    """经 mind.mjs 记录行动反馈或阶段性反思。"""
    sid = str(space_id or "room_default").strip() or "room_default"
    if isolated_persona:
        if world_data_root is None:
            return {"status": "unbound"}
        state_root = ensure_isolated_persona_sandbox(
            project_root,
            world_data_root,
            sid,
            str(host_character or "").strip(),
            custom_persona_dir=custom_persona_dir,
        )
        if state_root is None:
            return {"status": "unbound"}
    else:
        base_dir = _find_base_persona_dir(project_root, custom_persona_dir=custom_persona_dir)
        if base_dir is None:
            return {"status": "unbound"}
        state_root, _ = _resolve_persona_pointer(base_dir, str(host_character or "").strip(), strict=True)
        if state_root is None:
            return {"status": "unbound"}
    if not state_root.is_dir():
        return None
    mind_mjs = next(
        (
            p for p in (
                Path(project_root) / "plugins" / "nanami_persona" / "toolkit" / "mind.mjs",
                Path("plugins/nanami_persona/toolkit/mind.mjs"),
            )
            if p.is_file()
        ),
        None,
    )
    node = shutil.which("node")
    if mind_mjs is None or not node:
        return None
    aid = str(actor_id or "actor").strip() or "actor"
    command = [node, str(mind_mjs), "--root", str(state_root), "--chat", f"world:{sid}:{aid}"]

    def call(args: list[str]) -> dict[str, Any] | None:
        with _MIND_WRITE_LOCK:
            res = subprocess.run(
                [*command, *args], capture_output=True, text=True, timeout=3.0,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        parsed = json.loads(res.stdout.strip().splitlines()[-1])
        return parsed if isinstance(parsed, dict) else None

    try:
        payload = json.dumps(dict(event_payload), ensure_ascii=False)
        if not thought:
            return call(["--world-event", payload])
        with tempfile.TemporaryDirectory(prefix="nanami-thought-") as directory:
            path = Path(directory) / "thought.json"
            path.write_text(payload, encoding="utf-8")
            return call(["--think", "--think-file", str(path)])
    except Exception:
        logger.debug("nanami_world: mind.mjs 调用失败", exc_info=True)
        return None


def _resolve_hour_float(lighting: Mapping[str, Any]) -> float:
    if "hour_float" in lighting:
        try:
            return float(lighting["hour_float"])
        except (TypeError, ValueError):
            pass
    if "hour" in lighting:
        try:
            return float(lighting["hour"]) + float(lighting.get("minute") or 0.0) / 60.0
        except (TypeError, ValueError):
            pass
    clock_txt = str(lighting.get("clock") or "").strip()
    if ":" in clock_txt:
        try:
            hh, mm = clock_txt.split(":", 1)
            return int(hh) + int(mm) / 60.0
        except (TypeError, ValueError):
            pass
    return 12.0


def build_percept_frame(
    world_data_root: Path,
    project_root: Path,
    space_meta: Mapping[str, Any],
    lighting: Mapping[str, Any],
    actor: Mapping[str, Any],
    entities: Sequence[Mapping[str, Any]],
    *,
    actors_map: Mapping[str, Mapping[str, Any]] | None = None,
    visible_peer_ids: set[str] | None = None,
    custom_persona_dir: str = "",
    peer_known: bool = False,
    look_eid: str = "",
    touched_eid: str = "",
    advance_exposure: float = 0.0,
    weather: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """统一输入层（Input）：客观世界刺激 → L0 主观感知帧（PerceptFrame，供思考层 Brain 消费）。"""
    sid = str(space_meta.get("space_id") or actor.get("space_id") or "room_default").strip() or "room_default"
    sp_name = str(space_meta.get("name") or sid).strip()
    sp_kind = str(space_meta.get("space_kind") or "room")
    host_char = str(actor.get("host_character") or actor.get("name") or "").strip()
    cfg = read_world_perception_config(project_root, host_char, custom_persona_dir=custom_persona_dir)
    behavior_profile = read_persona_behavior_profile(project_root, host_char, custom_persona_dir=custom_persona_dir)
    w_dict = dict(weather) if isinstance(weather, Mapping) else {}
    w_code = str(w_dict.get("current") or "clear").strip().lower()
    vis_scale = 0.85 if (sp_kind == "town" and w_code in ("rain", "snow")) else 1.0
    l0 = update_and_project_world_perception(
        world_data_root,
        sid,
        actor,
        entities,
        cfg,
        peer_known=peer_known,
        look_eid=look_eid,
        touched_eid=touched_eid,
        advance_exposure=advance_exposure,
        behavior_profile=behavior_profile,
        weather_vis_scale=vis_scale,
    )

    hour = _resolve_hour_float(lighting)
    chrono = float(l0.get("chronotype_offset") or 0.0) if l0.get("enabled") else 0.0
    bio_hour = (hour - chrono) % 24.0
    circ_phase = "deep_night" if (bio_hour >= 23.0 or bio_hour < 6.0) else ("evening" if bio_hour >= 21.0 else "daytime")
    period_str = str(lighting.get("period") or "day")
    period_cn = {"dawn": "清晨", "day": "白天", "dusk": "傍晚", "night": "夜晚"}.get(period_str, "白天")
    if sp_kind == "town":
        light_desc = (
            ("夜色已深，街灯微亮" if lighting.get("lamp_positions") else "夜色有些暗")
            if period_str == "night"
            else "户外光线充足"
        )
    else:
        light_desc = "屋里暗着" if lighting.get("is_dark") else "室内光线明亮"
    w_desc = str(w_dict.get("description") or "").strip()
    w_seg = f"｜天气：{w_desc}" if w_desc else ""

    aid = str(actor.get("actor_id") or "")
    ax, ay = int(actor.get("x") or 0), int(actor.get("y") or 0)
    is_large = sp_kind == "town" or max(int(space_meta.get("width") or 0), int(space_meta.get("height") or 0)) >= 24
    focused_peers: list[str] = []
    if isinstance(actors_map, Mapping):
        peer_rows: list[tuple[int, str]] = []
        for pid, peer in actors_map.items():
            if pid == aid or not isinstance(peer, Mapping):
                continue
            if peer.get("space_id") and str(peer.get("space_id")) != sid:
                continue
            if visible_peer_ids is not None and pid not in visible_peer_ids:
                continue
            pdist = max(abs(ax - int(peer.get("x") or 0)), abs(ay - int(peer.get("y") or 0)))
            if visible_peer_ids is None and is_large and pdist > 12:
                continue
            pname = str(peer.get("name") or pid).strip()
            dist_txt = "就在眼前" if pdist <= 1 else ("几步外" if pdist <= 5 else "不远处")
            post_txt = "正在休息" if peer.get("posture") == "resting" else "站着"
            peer_rows.append((pdist, f"{pname}（{dist_txt}，{post_txt}）"))
        peer_rows.sort(key=lambda item: item[0])
        focused_peers = [text for _d, text in peer_rows[:6]]

    focused = l0.get("focused_entities") or []
    focused_names = [str(e.get("name") or "").strip() for e in focused if str(e.get("name") or "").strip()]
    focused_sensory = [_sensory_item_desc(e, actors_map=actors_map) for e in focused]
    stage = str(l0.get("stage") or "settled")
    stage_cn = _STAGE_LABELS.get(stage, stage)

    minute_i = int(lighting.get("minute", round((hour % 1.0) * 60.0))) % 60
    peer_line = f"\n- 眼前人物（社会注意焦点）：{'；'.join(focused_peers)}" if focused_peers else ""
    if l0.get("enabled"):
        prompt_block = (
            f"【{actor.get('name') or host_char} · L0 主观感知输入】\n"
            f"- 身处空间：「{sp_name}」｜客观时段：{period_cn}({int(hour):02d}:{minute_i:02d})｜{light_desc}{w_seg}\n"
            f"- 适应阶段：{stage_cn}（熟悉度={l0['familiarity']:.2f}，已记住物件={l0['mental_map_count']}件）"
            f"{peer_line}\n"
            f"- 此刻物体焦点（按显著度过滤）：{'；'.join(focused_sensory) if focused_sensory else '周围空荡荡的'}"
        )
    else:
        all_sensory = [_sensory_item_desc(e, actors_map=actors_map) for e in entities[:12]]
        prompt_block = (
            f"【{actor.get('name') or host_char} · 环境输入（L0 已关闭）】\n"
            f"- 身处空间：「{sp_name}」｜客观时段：{period_cn}({int(hour):02d}:{minute_i:02d})｜{light_desc}{w_seg}"
            f"{peer_line}\n"
            f"- 周围物件：{'；'.join(all_sensory) if all_sensory else '周围空荡荡的'}"
        )

    return {
        **l0,
        "novelty_weight": _clamp_f(cfg.get("novelty_weight"), 0.0, 1.0, 0.55),
        "memory_decay": _clamp_f(cfg.get("memory_decay"), 0.10, 0.90, 0.45),
        "habituation_rate": _clamp_f(cfg.get("habituation_rate"), 0.05, 1.0, 0.25),
        "space_name": sp_name,
        "stage_cn": stage_cn,
        "hour_float": round(hour, 3),
        "bio_hour": round(bio_hour, 2),
        "circadian_phase": circ_phase,
        "weather": w_dict,
        "focused_names": focused_names,
        "focused_peers": focused_peers,
        "focused_sensory": focused_sensory,
        "behavior_profile": behavior_profile,
        "prompt_block": prompt_block,
    }

