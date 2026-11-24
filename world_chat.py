"""文字世界内嵌对话引擎：把对话、世界动作（world_*）与心情立绘切换打通成同一条互动环。"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from . import episodic_memory, persona_bridge, projector, text_compiler, venue_semantics
from .schema import clip_text

logger = logging.getLogger(__name__)

_STAGE_DIR_RE = re.compile(r"[（(][^）)\n]{1,40}[）)]")
_COORD_RE = re.compile(r"\(?\s*(\d{1,2})\s*[,，]\s*(\d{1,2})\s*\)?")
_META_AI_RE = re.compile(
    r"(?:不太想说|不太想聊|不想聊这个|不想说这个|问得.{0,4}太直接|换个话题|聊点别的|"
    r"话题(?:跳|转)得.{0,4}太快|聊回正题|我倒没想到|顺便一提|写不利索|说不利索|作为AI|人工智能|系统设定)",
    re.IGNORECASE,
)
_ECHO_SPLIT_RE = re.compile(r"^[^。！？\n]{1,8}[？?]\s*(?:/|\s+)[^。！？\n]{3,}")
_BARE_CHARS_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]")
_MAX_HISTORY_PER_ACTOR = 40
_PLAYER_GREETING_SECONDS = 18.0
_PLAYER_GREETING_COOLDOWN = 120.0
_CREATE_NO_WINDOW = 0x08000000
_CHAT_DOMAINS = frozenset({
    "rest", "food", "game", "reading", "social", "notice", "explore",
    "expression", "comfort", "curiosity", "hobby", "music",
})
_INVITATION_DECLINES = ("不用", "不必", "不想", "不去", "改天", "下次", "先不", "去不了", "算了", "我自己")


def _resolve_realm_place_id(raw_target: Any, realm_places: dict[str, str]) -> str:
    val = str(raw_target or "").strip()
    if not val or not realm_places:
        return ""
    if val in realm_places:
        return val
    for pid, pname in realm_places.items():
        if pname and (val == pname or pname in val or val in pname):
            return pid
    return ""


def _sanitize_speech_goal(
    raw: Any, *, realm_places: dict[str, str], valid_eids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    target = str(raw.get("target") or "").strip()
    drive = str(raw.get("drive") or "").strip()
    if kind == "visit":
        resolved_pid = _resolve_realm_place_id(target, realm_places)
        if resolved_pid:
            action = str(raw.get("action") or "").strip()
            visit_drive = {"rest": "rest", "escort": "social"}.get(action, drive)
            return {
                "kind": "visit", "target": resolved_pid,
                "drive": visit_drive if visit_drive in _CHAT_DOMAINS else "explore",
                **({"action": "escort"} if action == "escort" else {}),
                "reason": clip_text(raw.get("reason"), 48),
            }
    if kind == "entity" and target in valid_eids:
        return {
            "kind": "entity", "target": target,
            "drive": drive if drive in _CHAT_DOMAINS else "curiosity",
            "action": clip_text(raw.get("action"), 16),
            "reason": clip_text(raw.get("reason"), 48),
        }
    return None


def _build_speech_act_pair(
    raw_sa: dict[str, Any] | None, *, realm_places: dict[str, str], valid_eids: set[str], summary: str,
    shared_objects: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(raw_sa, dict) or not raw_sa:
        return None
    shared_pid = _resolve_realm_place_id(raw_sa.get("shared_place_id"), realm_places)
    sdrive = str(raw_sa.get("shared_drive") or "").strip()
    if sdrive not in _CHAT_DOMAINS:
        sdrive = "social"
    tone = str(raw_sa.get("tone") or "neutral").strip().lower()
    if tone not in ("warm", "neutral", "tense"):
        tone = "neutral"
    beliefs: list[dict[str, str]] = []
    raw_beliefs = raw_sa.get("shared_beliefs")
    if isinstance(raw_beliefs, list):
        for item in raw_beliefs[:6]:
            if not isinstance(item, dict):
                continue
            pid = _resolve_realm_place_id(item.get("place_id"), realm_places)
            dom = str(item.get("domain") or "").strip()
            if pid and dom in _CHAT_DOMAINS:
                beliefs.append({"place_id": pid, "domain": dom, "note": clip_text(item.get("note"), 48)})
    goal_a = _sanitize_speech_goal(raw_sa.get("goal_a"), realm_places=realm_places, valid_eids=valid_eids)
    goal_b = _sanitize_speech_goal(raw_sa.get("goal_b"), realm_places=realm_places, valid_eids=valid_eids)
    if not shared_pid and goal_a and goal_b and goal_a.get("kind") == "visit" and goal_b.get("kind") == "visit":
        if goal_a.get("target") == goal_b.get("target"):
            shared_pid = str(goal_a["target"])
    entity_id = str(raw_sa.get("shared_entity_id") or "").strip()
    entity = shared_objects.get(entity_id) if not shared_pid else None
    if not any((shared_pid, entity, beliefs, goal_a, goal_b, tone == "tense")):
        return None

    def _side(personal_goal: dict[str, Any] | None, side: str) -> dict[str, Any]:
        out: dict[str, Any] = {"tone": tone}
        if shared_pid:
            out["shared_place_id"] = shared_pid
            out["shared_drive"] = sdrive
        if entity is not None:
            rest = raw_sa.get("shared_action") == "rest" and "restable" in entity["traits"]
            out.update(shared_entity_id=entity_id, shared_action="rest" if rest else "dwell")
            if rest:
                out["seated"] = side == ("a" if raw_sa.get("seated") == "a" else "b")
        if beliefs:
            out["beliefs"] = list(beliefs)
        if personal_goal:
            out["personal_goal"] = personal_goal
        if summary:
            out["summary"] = clip_text(summary, 72)
        return out

    return {"a": _side(goal_a, "a"), "b": _side(goal_b, "b")}


def _accepted_escort(
    turns: list[dict[str, str]], realm_places: dict[str, str], realm_buildings: list[dict[str, Any]], *, at_night: bool,
) -> dict[str, Any]:
    lodging = {
        pid: realm_places[pid] for building in realm_buildings
        if (pid := building["interior_space_id"]) in realm_places and venue_semantics.is_lodging_building(building)
    }
    if not lodging:
        return {}
    speech = " ".join(f"{turn['line_a']} {turn['line_b']}" for turn in turns)
    bedtime = at_night or any(word in speech for word in ("这么晚", "太晚", "睡觉", "休息", "晚安"))
    if not bedtime:
        return {}
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        for speaker, receiver in (("a", "b"), ("b", "a")):
            offer = re.search(r"(?:送|陪)(?:你|您)(?:一起)?(?:回(?:到)?|到|去)([^。！？!?，,；;]+)", turn[f"line_{speaker}"])
            if not offer:
                continue
            destination = offer.group(1)
            hits = [pid for pid, name in lodging.items() if name in destination]
            if not hits:
                alias = re.split(r"门口|门前|那边|休息|睡觉|可以吗|好吗|吧", destination, maxsplit=1)[0].strip()
                hits = [pid for pid, name in lodging.items() if len(alias) >= 2 and alias in name]
            if len(hits) != 1:
                continue
            if speaker == "b" and index + 1 == len(turns):
                continue
            reply = turns[index if speaker == "a" else index + 1][f"line_{receiver}"]
            if any(word in reply for word in ("不用", "不必", "不要", "我自己", "改天", "先不")):
                continue
            if not re.match(r"^(?:嗯|好|行|可以|谢谢|麻烦)", reply):
                continue
            pid = hits[0]
            name = lodging[pid]
            return {
                "shared_place_id": pid,
                f"goal_{speaker}": {"kind": "visit", "target": pid, "drive": "social", "action": "escort",
                                    "reason": f"送同伴到「{name}」门口"},
                f"goal_{receiver}": {"kind": "visit", "target": pid, "drive": "rest",
                                     "reason": f"回「{name}」休息"},
            }
    return {}


def _accepted_invitations(turns: list[dict[str, str]]):
    invitations = ("一起", "同行", "一块", "一道", "要不要", "去不去", "过去吧", "走吧", "过去坐")
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        exchanges = [(turn["line_a"], turn["line_b"], "b")]
        if index + 1 < len(turns):
            exchanges.insert(0, (turn["line_b"], turns[index + 1]["line_a"], "a"))
        for offer, reply, receiver in exchanges:
            if not any(word in offer for word in invitations) or any(word in reply for word in _INVITATION_DECLINES):
                continue
            if not re.match(r"^(?:好|嗯|行|可以|走吧|去吧|正好|我也想)", reply.lstrip()):
                continue
            yield offer, reply, receiver


def _accepted_outing(turns: list[dict[str, str]], realm_places: dict[str, str]) -> tuple[str, str]:
    for offer, reply, _ in _accepted_invitations(turns):
        for pid, name in realm_places.items():
            if name and name in offer:
                return pid, f"{offer} {reply}"
    return "", ""


def _accepted_object_outing(
    turns: list[dict[str, str]], objects: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    for offer, reply, receiver in _accepted_invitations(turns):
        if not any(word in offer for word in ("坐", "歇", "休息", "旁边", "那边", "附近", "边上", "晒", "烤火", "取暖", "躺")):
            continue
        choices = []
        for eid, entity in objects.items():
            name = entity["name"]
            if name in offer:
                choices.append((len(name), eid))
            elif len(name) > 2 and re.search(rf"(?:去|到){re.escape(name[-2:])}|{re.escape(name[-2:])}(?:那边|旁边|上)", offer):
                choices.append((2, eid))
        if choices:
            # Objects are ordered by walking distance when names match more than one instance.
            _, eid = max(choices, key=lambda match: match[0])
            traits = objects[eid]["traits"]
            rest = "restable" in traits and any(word in offer + reply for word in ("坐", "休息", "歇", "躺"))
            return {"shared_entity_id": eid, "shared_action": "rest" if rest else "dwell", "seated": receiver}
    return {}


def _social_turn_key(first: str, second: str) -> tuple[str, str]:
    return tuple(re.sub(r"[\W_]+", "", line.casefold()) for line in (first, second))


def _repeated_social_turn(candidate: tuple[str, str], recent: list[tuple[str, str]]) -> bool:
    def similar(left: str, right: str) -> bool:
        if left == right:
            return True
        if max(len(left), len(right)) <= 6:
            a = re.sub(r"^(?:嗯+|好[呀啊]?|行|可以)", "", left)
            b = re.sub(r"^(?:嗯+|好[呀啊]?|行|可以)", "", right)
            return bool(a and a == b and a in ("走吧", "去吧", "过去吧", "一起去"))
        return min(len(left), len(right)) >= 8 and SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.84

    return any(similar(candidate[0], old[0]) and similar(candidate[1], old[1]) for old in recent)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _find_node_exe() -> str:
    hit = shutil.which("node")
    if hit:
        return hit
    if os.name == "nt":
        for root in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if root:
                cand = Path(root) / "nodejs" / "node.exe"
                if cand.is_file():
                    return str(cand)
    return ""


class WorldChatService:
    """管理文字世界内的对话历史、可选 mind.mjs 同步、LLM 决策与动态心情换装。"""

    def __init__(
        self,
        engine: Any,
        *,
        project_root: Path,
        settings_provider: Callable[[], Any],
        adapter_provider: Callable[[], Any] | None = None,
        action_scheduler: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None,
    ) -> None:
        self._engine = engine
        self._project_root = Path(project_root)
        self._settings_provider = settings_provider
        self._adapter_provider = adapter_provider or text_compiler._default_adapter
        self._action_scheduler = action_scheduler
        self._history_path = Path(engine.data_root) / "chat_history.json"
        self._lock = threading.RLock()
        self._history: dict[str, list[dict[str, Any]]] = self._load_history()
        self._player_greeting: dict[str, Any] | None = None
        self._greeting_cooldowns: dict[str, float] = {}

    def _load_history(self) -> dict[str, list[dict[str, Any]]]:
        if not self._history_path.is_file():
            return {}
        try:
            raw = json.loads(self._history_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): list(v) for k, v in raw.items() if isinstance(v, list)}
        except (OSError, ValueError):
            logger.debug("nanami_world: 读取 chat_history.json 失败", exc_info=True)
        return {}

    def _save_history_locked(self) -> None:
        try:
            _atomic_write_json(self._history_path, self._history)
        except OSError:
            logger.debug("nanami_world: 保存 chat_history.json 失败", exc_info=True)

    def _resolve_space_id(self, actor_id: str, space_id: str = "") -> str:
        sid = str(space_id or "").strip()
        if sid:
            return sid
        if actor_id:
            try:
                actors = self._engine.list_actors()
                act = next((a for a in actors if a["actor_id"] == actor_id), None)
                if act and act.get("space_id"):
                    return str(act["space_id"])
            except Exception:
                pass
        return "room_default"

    def _realm_space_id(self, space_id: str) -> str:
        sid = str(space_id or "room_default").strip() or "room_default"
        if hasattr(self._engine, "realm_space_id"):
            try:
                return str(self._engine.realm_space_id(sid) or sid)
            except Exception:
                return sid
        return sid

    def _history_key_locked(self, actor_id: str, space_id: str) -> str:
        sid = self._realm_space_id(space_id)
        key = f"{sid}::{actor_id}"
        if sid == "room_default" and key not in self._history and actor_id in self._history:
            self._history[key] = list(self._history.pop(actor_id) or [])
        return key

    def get_history(self, actor_id: str, *, space_id: str = "") -> list[dict[str, Any]]:
        if not actor_id:
            return []
        eff_sid = self._resolve_space_id(actor_id, space_id)
        with self._lock:
            key = self._history_key_locked(actor_id, eff_sid)
            return [dict(item) for item in self._history.get(key, [])]

    def _recent_pair_turns(self, actor_id: str, partner_name: str, space_id: str) -> list[tuple[str, str]]:
        own_prefix = (f"（对{partner_name}说）", f"(对{partner_name}说)")
        peer_prefix = (f"[{partner_name} 回应]", f"[{partner_name} 对你说]")
        pending: tuple[int, str] | None = None
        turns = []
        for row in self.get_history(actor_id, space_id=space_id):
            line = str(row.get("text") or "")
            match = next(((side, p) for side, group in enumerate((own_prefix, peer_prefix))
                          for p in group if line.startswith(p)), None)
            if row.get("role") != "assistant" or match is None:
                pending = None
                continue
            side, prefix = match
            spoken = line[len(prefix):].strip()
            if pending and pending[0] != side:
                turns.append((pending[1], spoken) if side == 1 else (spoken, pending[1]))
                pending = None
            else:
                pending = (side, spoken)
        return turns[-10:]

    def get_history_if_changed(self, actor_id: str, space_id: str, after: str) -> list[dict[str, Any]] | None:
        with self._lock:
            rows = self._history.get(self._history_key_locked(actor_id, space_id), [])
            latest = rows[-1]["id"] if rows else ""
            return [dict(row) for row in rows] if latest != after else None

    def clear_history(self, actor_id: str, *, space_id: str = "") -> list[dict[str, Any]]:
        if not actor_id:
            return []
        eff_sid = self._resolve_space_id(actor_id, space_id)
        with self._lock:
            key = self._history_key_locked(actor_id, eff_sid)
            self._history[key] = []
            if eff_sid == "room_default":
                self._history.pop(actor_id, None)
            self._save_history_locked()
            return []

    def append_autonomous_activity(
        self,
        actor_id: str,
        *,
        action_summary: str,
        thought: str,
        emotion: str = "",
        space_id: str = "",
    ) -> None:
        if not actor_id:
            return
        eff_sid = self._resolve_space_id(actor_id, space_id)
        with self._lock:
            key = self._history_key_locked(actor_id, eff_sid)
            items = self._history.setdefault(key, [])
            summary = str(action_summary or "").strip()
            clean_thought = str(thought or "").strip()
            if summary:
                act_line = f"[自主行动] {summary}"
                if not items or items[-1].get("text") != act_line:
                    self._append_msg_locked(actor_id, "action", act_line, space_id=eff_sid)
            if clean_thought:
                bubble = f"（{clean_thought}）" if not (clean_thought.startswith("（") or clean_thought.startswith("(")) else clean_thought
                if not items or items[-1].get("text") != bubble:
                    self._append_msg_locked(actor_id, "assistant", bubble, emotion=emotion, space_id=eff_sid)
            self._save_history_locked()

    def _append_msg_locked(
        self,
        actor_id: str,
        role: str,
        text: str,
        *,
        emotion: str = "",
        sprite_index: int = 0,
        sprite_tag: str = "",
        space_id: str = "room_default",
    ) -> dict[str, Any]:
        key = self._history_key_locked(actor_id, space_id)
        items = self._history.setdefault(key, [])
        entry = {
            "id": f"m_{time.time_ns()}_{len(items)}",
            "role": role,
            "text": str(text or ""),
            "ts": round(time.time(), 3),
            "emotion": emotion,
            "sprite_index": sprite_index,
            "sprite_tag": sprite_tag,
        }
        items.append(entry)
        if len(items) > _MAX_HISTORY_PER_ACTOR:
            self._history[key] = items[-_MAX_HISTORY_PER_ACTOR:]
        self._save_history_locked()
        return entry

    # ── 可选联动 nanami_persona 的 mind.mjs / guard_bridge.mjs / soul.md / corpus.tsv ──
    def _sample_corpus_examples(
        self,
        profile_dir: Path | None,
        base_dir: Path | None,
        *,
        limit: int = 7,
    ) -> str:
        """从角色专属 corpus.tsv（或全局语料）优先抽取 casual/curious 原声口癖范例。"""
        cands: list[Path] = []
        if profile_dir is not None:
            cands.append(profile_dir / "corpus.tsv")
        if base_dir is not None:
            cands.append(base_dir / "corpus" / "acg_corpus.tsv")
        primary: list[tuple[str, str]] = []
        others: list[tuple[str, str]] = []
        for path in cands:
            if not path.is_file():
                continue
            try:
                for raw in path.read_text(encoding="utf-8").splitlines():
                    s = raw.strip()
                    if not s or s.startswith("#"):
                        continue
                    parts = s.split("\t")
                    if len(parts) >= 3:
                        bkt, u, a = parts[0].strip().lower(), parts[1].strip(), parts[2].strip()
                    elif len(parts) == 2:
                        bkt, u, a = "casual", parts[0].strip(), parts[1].strip()
                    else:
                        continue
                    if u and a:
                        pair = (u[:50], a[:80])
                        if bkt in ("casual", "curious", "repair_afterglow"):
                            primary.append(pair)
                        else:
                            others.append(pair)
            except OSError:
                continue
            if primary or others:
                break
        if not primary and not others:
            return ""
        picked = random.sample(primary, min(limit, len(primary)))
        if len(picked) < limit and others:
            picked.extend(random.sample(others, min(limit - len(picked), len(others))))
        return "\n".join(f"对方：{u}\n你：{a}" for u, a in picked)

    def _read_off_voice_ban(self, profile_dir: Path | None) -> list[str]:
        if profile_dir is None:
            return []
        ov_path = profile_dir / "off-voice.json"
        if not ov_path.is_file():
            return []
        try:
            raw = json.loads(ov_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        words: list[str] = []
        if isinstance(raw, dict):
            for key in ("ban", "words", "phrases", "items"):
                val = raw.get(key)
                if isinstance(val, list):
                    words.extend(str(x).strip() for x in val if str(x).strip())
        elif isinstance(raw, list):
            words.extend(str(x).strip() for x in raw if str(x).strip())
        return words[:18]

    def _persona_full_context_for(
        self,
        host_character: str,
        *,
        corpus_limit: int = 5,
        strict_lookup: bool = False,
        isolated_realm_id: str = "",
    ) -> dict[str, Any]:
        """只读探测 nanami_persona 的全套上下文：mind.mjs / guard_bridge.mjs / soul.md / corpus.tsv / off-voice.json。"""
        settings = self._settings_provider()
        custom = str(getattr(settings, "persona_data_dir", "") or "").strip()
        base_candidates: list[Path] = []
        if custom:
            base_candidates.append(Path(custom).expanduser())
        base_candidates.extend([self._project_root / "data" / "nanami", Path("data/nanami")])
        base_dir = next((p for p in base_candidates if p.is_dir()), None)

        mind_mjs = next(
            (p for p in (self._project_root / "plugins" / "nanami_persona" / "toolkit" / "mind.mjs", Path("plugins/nanami_persona/toolkit/mind.mjs")) if p.is_file()),
            None,
        )
        guard_mjs = next(
            (p for p in (self._project_root / "plugins" / "nanami_persona" / "guard_bridge.mjs", Path("plugins/nanami_persona/guard_bridge.mjs")) if p.is_file()),
            None,
        )
        toolkit_dir = mind_mjs.parent if mind_mjs is not None else None

        state_root: Path | None = None
        profile_dir: Path | None = None
        soul_text = ""
        clean_host = str(host_character or "").strip()
        if base_dir is not None:
            state_root, _pfx = persona_bridge._resolve_persona_pointer(base_dir, clean_host, strict=strict_lookup)
            if state_root is not None:
                if state_root.name == "runtime":
                    profile_dir = state_root.parent
                elif state_root == base_dir and (base_dir / "profiles" / "nanami_chiaki").is_dir():
                    profile_dir = base_dir / "profiles" / "nanami_chiaki"
                else:
                    profile_dir = state_root
            soul_cands: list[Path] = []
            if state_root is not None:
                soul_cands.extend([state_root / "soul.md", state_root.parent / "soul.md"])
            if profile_dir is not None:
                soul_cands.append(profile_dir / "soul.md")
            allow_nanami_fallback = (clean_host == "七海千秋") if strict_lookup else (clean_host in ("", "七海千秋"))
            if allow_nanami_fallback:
                soul_cands.extend([
                    base_dir / "profiles" / "nanami_chiaki" / "soul.md",
                    self._project_root / "plugins" / "nanami_persona" / "soul" / "chiaki-soul.md",
                ])
            for cand in soul_cands:
                if cand.is_file():
                    try:
                        soul_text = cand.read_text(encoding="utf-8")[:1200]
                        break
                    except OSError:
                        pass

        corpus_base = None if (strict_lookup and profile_dir is None) else base_dir
        corpus_examples = self._sample_corpus_examples(profile_dir, corpus_base, limit=corpus_limit)
        off_voice_words = self._read_off_voice_ban(profile_dir)
        effective_state_root = state_root
        if isolated_realm_id and state_root is not None:
            sandbox_root = persona_bridge.ensure_isolated_persona_sandbox(
                self._project_root,
                self._engine.data_root,
                isolated_realm_id,
                clean_host,
                custom_persona_dir=custom,
            )
            effective_state_root = sandbox_root
        return {
            "mind_mjs": mind_mjs,
            "guard_mjs": guard_mjs,
            "toolkit_dir": toolkit_dir,
            "state_root": effective_state_root,
            "profile_dir": profile_dir,
            "soul_text": soul_text,
            "corpus_examples": corpus_examples,
            "off_voice_words": off_voice_words,
        }

    def _persona_context_for(self, host_character: str) -> tuple[Path | None, Path | None, str]:
        ctx = self._persona_full_context_for(host_character)
        return ctx["mind_mjs"], ctx["state_root"], ctx["soul_text"]

    def _run_guard_bridge(self, guard_mjs: Path | None, toolkit_dir: Path | None, texts: list[str]) -> list[str]:
        """调用 nanami_persona/guard_bridge.mjs 清洗 AI 腔、破折号与离声词；失败时原样返回。"""
        if guard_mjs is None or toolkit_dir is None or not texts:
            return texts
        node = _find_node_exe()
        if not node:
            return texts
        try:
            kwargs: dict[str, Any] = {
                "input": json.dumps({"texts": texts}, ensure_ascii=False),
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "timeout": 3.0,
            }
            if os.name == "nt":
                kwargs["creationflags"] = _CREATE_NO_WINDOW
            proc = subprocess.run([node, str(guard_mjs), str(toolkit_dir)], **kwargs)
            if proc.returncode != 0 or not proc.stdout.strip():
                return texts
            res = json.loads(proc.stdout)
            out = res.get("texts")
            if res.get("ok") and isinstance(out, list) and len(out) == len(texts):
                return [str(x or "").strip() or orig for x, orig in zip(out, texts)]
        except Exception:
            logger.debug("nanami_world: 调用 guard_bridge.mjs 跳过", exc_info=True)
        return texts

    @staticmethod
    def _repetition_penalty(text: str, recent_replies: list[str]) -> float:
        """跨轮防重复门控：对与近期台词重复开头（如连用“嗯～”）、重复词组或句式的候选施加重罚。"""
        if not recent_replies:
            return 0.0
        bare = _BARE_CHARS_RE.sub("", str(text or ""))
        if len(bare) < 3:
            return 0.0
        c2 = {bare[i : i + 2] for i in range(len(bare) - 1)} if len(bare) >= 2 else set()
        c3 = {bare[i : i + 3] for i in range(len(bare) - 2)}
        c4 = {bare[i : i + 4] for i in range(len(bare) - 3)} if len(bare) >= 4 else set()
        penalty = 0.0
        recent = [str(r or "").strip() for r in recent_replies if str(r or "").strip()][-6:]
        for dist, prev in enumerate(reversed(recent), 1):
            pb = _BARE_CHARS_RE.sub("", prev)
            if len(pb) < 3:
                continue
            if pb == bare:
                return 12.0
            p2 = {pb[i : i + 2] for i in range(len(pb) - 1)} if len(pb) >= 2 else set()
            p3 = {pb[i : i + 3] for i in range(len(pb) - 2)}
            p4 = {pb[i : i + 4] for i in range(len(pb) - 3)} if len(pb) >= 4 else set()
            decay = 1.0 if dist <= 2 else (0.75 if dist <= 4 else 0.5)
            if c4 & p4:
                penalty = max(penalty, 7.5 * decay)
            elif c3 & p3:
                overlap = len(c3 & p3) / max(1, min(len(c3), len(p3)))
                penalty = max(penalty, (4.5 + 3.0 * overlap) * decay)
            elif len(c2 & p2) >= 2 and (len(c2 & p2) / max(1, min(len(c2), len(p2)))) >= 0.30:
                penalty = max(penalty, 4.0 * decay)
            if dist <= 3 and (
                (len(bare) >= 2 and len(pb) >= 2 and bare[:2] == pb[:2])
                or (len(str(text or "").strip()) >= 2 and len(prev) >= 2 and str(text or "").strip()[:2] == prev[:2])
            ):
                penalty = max(penalty, 3.2)
        return penalty

    @classmethod
    def _score_mouth_candidate(
        cls,
        text: str,
        recent_replies: list[str],
        off_voice_words: list[str],
        heard_line: str = "",
    ) -> float:
        t = str(text or "").strip()
        if not t:
            return -100.0
        score = 0.0
        if _META_AI_RE.search(t):
            score -= 12.0
        if _ECHO_SPLIT_RE.search(t):
            score -= 6.0
        if _STAGE_DIR_RE.search(t):
            score -= 5.0
        for w in off_voice_words:
            if w and w in t:
                score -= 6.0
        score -= cls._repetition_penalty(t, recent_replies)
        if heard_line:
            hb = _BARE_CHARS_RE.sub("", heard_line)
            tb = _BARE_CHARS_RE.sub("", t)
            if len(hb) >= 4 and len(tb) >= 4:
                h3 = {hb[i : i + 3] for i in range(len(hb) - 2)}
                t3 = {tb[i : i + 3] for i in range(len(tb) - 2)}
                if h3 & t3:
                    score -= min(5.0, 2.2 * len(h3 & t3))
        if 3 <= len(t) <= 28:
            score += 2.5
        elif len(t) > 45:
            score -= 2.5
        return score

    def _mouth_speak(
        self,
        actor_name: str,
        pctx: dict[str, Any],
        intent: str,
        recent_replies: list[str],
        *,
        listener_name: str = "对方",
        heard_line: str = "",
        fallback_line: str = "",
        setting_fallback: str = "",
        world_facts: str = "",
    ) -> str:
        """嘴巴层（Mouth）：隔离复杂规则与长历史，仅看 soul + corpus + 大脑意图，一次产出 6 个口语候选并过防重复门控与守卫。"""
        soul = (pctx.get("soul_text") or setting_fallback or actor_name)[:550]
        corpus = pctx.get("corpus_examples") or ""
        ban = pctx.get("off_voice_words") or []
        recent_block = "\n".join(f"- {r}" for r in recent_replies[-5:]) if recent_replies else "（无）"

        mouth_sys = (
            f"你是「{actor_name}」的发声嘴巴（只负责把大脑意图转成真人张嘴说的话，不要做说教或内心汇报）。\n"
            "必须严格遵守：\n"
            "1. **优先模仿【原声语料范例】的真实说话节奏、停顿和口吻**（通常 1 句短话，4–24 字，像真人脱口而出）；\n"
            "2. 严禁把内心活动或系统数值（如“打了六局”“进度多少”）念成台词，严禁鹦鹉学舌复读对方刚说过的词（如对方刚说“坐过去吧”，你就别再说“坐过去吧”）；\n"
            "3. 严禁虚构当前房间里不存在的店铺、餐厅或物品；只能用【世界真实物件】里的名词；\n"
            "4. 严禁与【近期已说过的话】重复开头（如连续用“嗯～”“那个…”）或重复同一句式；\n"
            '5. 一次给出 6 个句式拉开差异的口语候选短句，只输出合法 JSON：{"candidates":["说法1","说法2","说法3","说法4","说法5","说法6"]}'
        )
        mouth_user = (
            (f"【{actor_name}原声语料范例（最重要！严格模仿这种短句口吻）】\n{corpus}\n\n" if corpus else "")
            + f"【{actor_name}性格底色】\n{soul}\n\n"
            + (f"【禁说离声词】{'、'.join(ban)}\n\n" if ban else "")
            + (f"【世界真实物件（禁止编造除此之外的物品或店铺）】\n{world_facts}\n\n" if world_facts else "")
            + f"【近期已说过的话（避开重复开头与句式）】\n{recent_block}\n\n"
            + (f"【{listener_name}刚说（严禁复读对方用词）】{heard_line}\n" if heard_line else "")
            + f"【大脑这轮要表达的意图】{intent}"
        )
        parsed = text_compiler._ask_llm_json(
            mouth_sys,
            mouth_user,
            adapter_provider=self._adapter_provider,
            timeout_sec=10.0,
            max_tokens=320,
        )
        cands_raw = parsed.get("candidates") if isinstance(parsed, dict) else None
        chosen = ""
        if isinstance(cands_raw, list) and cands_raw:
            cleaned = [
                clip_text(_STAGE_DIR_RE.sub("", str(c or "")).strip(), 140, "")
                for c in cands_raw
                if str(c or "").strip()
            ]
            if cleaned:
                chosen = max(cleaned, key=lambda c: self._score_mouth_candidate(c, recent_replies, ban, heard_line=heard_line))
        if not chosen:
            chosen = clip_text(_STAGE_DIR_RE.sub("", fallback_line or intent).strip(), 140, "……嗯。")
        return self._run_guard_bridge(pctx.get("guard_mjs"), pctx.get("toolkit_dir"), [chosen])[0]

    def _run_mind_mjs(
        self,
        mind_mjs: Path | None,
        state_root: Path | None,
        actor_id: str,
        flag: str,
        text: str,
        *,
        space_id: str = "room_default",
    ) -> dict[str, Any] | None:
        if mind_mjs is None or state_root is None or not text.strip():
            return None
        node = _find_node_exe()
        if not node:
            return None
        sid = str(space_id or "room_default").strip() or "room_default"
        cmd = [node, str(mind_mjs), "--root", str(state_root), "--chat", f"world:{sid}:{actor_id}", flag, text]
        if flag == "--his":
            cmd.append("--json")
        try:
            kwargs: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "timeout": 3.5,
            }
            if os.name == "nt":
                kwargs["creationflags"] = _CREATE_NO_WINDOW
            proc = subprocess.run(cmd, **kwargs)
            if proc.returncode != 0 or not proc.stdout.strip():
                return None
            if flag == "--his":
                return json.loads(proc.stdout)
        except Exception:
            logger.debug("nanami_world: 调用 mind.mjs 跳过", exc_info=True)
        return None

    def _character_setting_of(self, actor: dict[str, Any]) -> str:
        brief = str(actor.get("persona_brief") or "").strip()
        if actor.get("is_native_npc"):
            return f"你是{actor['name']}。{brief}" if brief else f"你是{actor['name']}，居住在这个像素文字世界里。"
        host_char = str(actor.get("host_character") or actor.get("name") or "").strip()
        for row in self._engine.list_host_characters():
            if row.get("name") == host_char and row.get("setting"):
                base = str(row["setting"])[:1000]
                return f"{base}\n【角色补充设定】{brief}" if brief else base
        if brief:
            return f"你是{actor['name']}。{brief}"
        return f"你是{actor['name']}，居住在这个像素文字世界里。"

    def _setting_with_brief(self, actor: Mapping[str, Any], soul_text: str) -> str:
        brief = str(actor.get("persona_brief") or "").strip()
        if soul_text:
            return f"{soul_text}\n【角色补充设定】{brief}" if brief else soul_text
        return self._character_setting_of(dict(actor))

    # ── 执行 LLM 返回的世界动作 ──────────────────────────────────────────
    def _execute_world_actions(
        self,
        actor_id: str,
        raw_actions: Any,
        *,
        scheduled: list[bool] | None = None,
        same_space_peer_ids: set[str] | None = None,
        realm_other_peer_ids: set[str] | None = None,
        redirected_commit_peers: list[str] | None = None,
    ) -> list[str]:
        if not isinstance(raw_actions, list):
            return []
        logs: list[str] = []
        for item in raw_actions[:3]:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "world_act").strip().lower()
            if tool == "world_act":
                eid_cand = str(item.get("entity_id") or "").strip()
                if same_space_peer_ids and eid_cand in same_space_peer_ids:
                    tool = "world_approach"
                    item = {**item, "tool": tool, "target_actor_id": eid_cand}
                elif realm_other_peer_ids and eid_cand in realm_other_peer_ids:
                    if redirected_commit_peers is not None:
                        redirected_commit_peers.append(eid_cand)
                    continue
            if self._action_scheduler and tool in ("world_move", "world_approach", "world_talk"):
                ok, feedback = self._action_scheduler(actor_id, {**item, "tool": tool})
                if scheduled is not None:
                    scheduled.append(ok)
                logs.append(feedback)
                break
            if tool == "world_move":
                try:
                    x = int(item.get("x"))
                    y = int(item.get("y"))
                    logs.append(self._engine.execute_move(actor_id, x, y))
                except (TypeError, ValueError):
                    continue
            elif tool == "world_write":
                eid = str(item.get("entity_id") or "").strip()
                txt = str(item.get("text") or "").strip()
                if eid and txt:
                    logs.append(self._engine.execute_write(actor_id, eid, txt))
            elif tool == "world_combine":
                src = str(item.get("source_id") or "").strip()
                tgt = str(item.get("target_id") or "").strip()
                if src and tgt:
                    logs.append(self._engine.execute_combine(actor_id, src, tgt))
            else:
                eid = str(item.get("entity_id") or "").strip()
                act = str(item.get("action") or "").strip()
                tgt = str(item.get("target_id") or "").strip()
                if eid and act:
                    logs.append(self._engine.execute_act(actor_id, eid, act, tgt))
        return logs

    @staticmethod
    def _peer_request(user_text: str, history: list[dict[str, Any]], peers: list[dict[str, Any]]) -> dict[str, Any] | None:
        if any(marker in user_text for marker in ("要不要", "能不能不", "不要", "如果", "别去", "不必", "不用")):
            return None
        if not re.search(r"去(?:.{0,14})(?:看看|看一下|找找|找|陪|探望|打招呼|聊聊|说几句)|(?:过去|走近|走到).{0,12}(?:身边|旁边|看看|聊聊)", user_text):
            return None
        named = [p for p in peers if p["name"] in user_text or p["name"][:2] in user_text]
        if not named and re.search(r"他|她|对方|过去看看", user_text):
            for message in reversed(history):
                if message["role"] == "user":
                    named = [p for p in peers if p["name"] in message["text"] or p["name"][:2] in message["text"]]
                    if named:
                        break
        if len(named) != 1:
            return None
        mode = "world_talk" if re.search(r"打招呼|聊聊|说几句", user_text) else "world_approach"
        return {"tool": mode, "target_actor_id": named[0]["actor_id"]}

    @staticmethod
    def _infer_peer_location_hint(
        user_text: str,
        history: list[dict[str, Any]],
        peers: list[dict[str, Any]],
        realm_places: dict[str, str],
    ) -> tuple[str, str] | None:
        if any(marker in user_text for marker in ("不在", "不是在", "别去", "不要")):
            return None
        matched_places = [(pid, nm) for pid, nm in realm_places.items() if nm and nm in user_text]
        if len(matched_places) != 1:
            return None
        named = [p for p in peers if p["name"] in user_text or p["name"][:2] in user_text]
        if not named and re.search(r"他|她|对方", user_text):
            for message in reversed(history):
                if message["role"] in ("user", "assistant"):
                    named = [p for p in peers if p["name"] in message["text"] or p["name"][:2] in message["text"]]
                    if named:
                        break
        if len(named) != 1:
            return None
        return named[0]["actor_id"], matched_places[0][0]

    # ── 离线 / 无 LLM 规则兜底对话 ───────────────────────────────────────
    def _offline_fallback(self, actor: dict[str, Any], user_text: str) -> dict[str, Any]:
        aid = actor["actor_id"]
        vis = self._engine.visible_entities(aid)
        vit = actor.get("vitals") or {}
        if vit.get("disengaged"):
            return {"world_actions": [], "emotion": "冷漠", "reply": "······"}

        actions: list[dict[str, Any]] = []
        emotion = "平静"
        reply = ""

        # 1. 坐标移动请求
        coord_m = _COORD_RE.search(user_text) if any(k in user_text for k in ("走", "去", "到")) else None
        want_lamp_off = any(k in user_text for k in ("关灯", "关掉灯", "把灯关掉", "把灯关了", "关下台灯", "关一下台灯", "关下灯", "关一下灯"))
        want_lamp_on = (not want_lamp_off) and (
            any(k in user_text for k in ("开灯", "把灯打开", "亮一点"))
            or ("灯" in user_text and any(k in user_text for k in ("打开", "开下", "开一下", "开启")))
        )
        if coord_m:
            actions.append({"tool": "world_move", "x": int(coord_m.group(1)), "y": int(coord_m.group(2))})
            emotion = "好奇"

        # 2. 开关灯
        elif want_lamp_on:
            lamp = next((e for e in vis if (e.get("traits") or {}).get("switchable", {}).get("provides_light") and not e["traits"]["switchable"].get("is_on")), None)
            if lamp:
                actions.append({"tool": "world_act", "entity_id": lamp["entity_id"], "action": "turn_on"})
                emotion = "开心"
                reply = "嗯，我把灯打开了。"
        elif want_lamp_off:
            lamp = next((e for e in vis if (e.get("traits") or {}).get("switchable", {}).get("provides_light") and e["traits"]["switchable"].get("is_on")), None)
            if lamp:
                actions.append({"tool": "world_act", "entity_id": lamp["entity_id"], "action": "turn_off"})
                emotion = "困倦"
                reply = "好，灯关掉了。"

        # 3. 喝 / 吃
        elif any(k in user_text for k in ("喝", "渴", "可可", "茶", "水", "饮料")):
            drink = next((e for e in vis if (e.get("traits") or {}).get("consumable", {}).get("consume_verb") == "喝" and int(e["traits"]["consumable"].get("uses_left") or 0) > 0), None)
            if drink:
                actions.append({"tool": "world_act", "entity_id": drink["entity_id"], "action": "consume"})
                emotion = "开心"
                reply = f"谢谢……这个{drink['name']}挺不错的。"
        elif any(k in user_text for k in ("吃", "饿", "蛋糕", "点心")):
            food = next((e for e in vis if (e.get("traits") or {}).get("consumable", {}).get("consume_verb") == "吃" and int(e["traits"]["consumable"].get("uses_left") or 0) > 0), None)
            if food:
                actions.append({"tool": "world_act", "entity_id": food["entity_id"], "action": "consume"})
                emotion = "开心"
                reply = f"{food['name']}很好吃呢。"

        # 4. 休息 / 起身
        elif any(k in user_text for k in ("休息", "睡觉", "躺", "歇会儿")):
            sofa = next((e for e in vis if "restable" in (e.get("traits") or {}) and not e["traits"]["restable"].get("occupied_by")), None)
            if sofa:
                actions.append({"tool": "world_act", "entity_id": sofa["entity_id"], "action": "rest"})
                emotion = "困倦"
                reply = "唔……那我先靠着歇一会儿。"
        elif any(k in user_text for k in ("起来", "起身", "别睡")):
            sofa = next((e for e in vis if (e.get("traits") or {}).get("restable", {}).get("occupied_by") == aid), None)
            if sofa:
                actions.append({"tool": "world_act", "entity_id": sofa["entity_id"], "action": "leave_rest"})
                emotion = "平静"
                reply = "嗯，我起来了。"

        # 5. 游戏 / 互动
        elif any(k in user_text for k in ("玩", "游戏", "掌机")):
            game = next((e for e in vis if "activity" in (e.get("traits") or {})), None)
            if game:
                actions.append({"tool": "world_act", "entity_id": game["entity_id"], "action": "interact"})
                emotion = "兴奋"
                reply = "这一关的节奏挺有意思的，你也一起看吗？"

        # 6. 开关门 / 打开容器
        elif any(k in user_text for k in ("开门", "把门打开")) or ("门" in user_text and "打开" in user_text):
            door = next((e for e in vis if "portal" in (e.get("traits") or {}) and not e["traits"]["portal"].get("is_open")), None)
            if door:
                actions.append({"tool": "world_act", "entity_id": door["entity_id"], "action": "open"})
                emotion = "好奇"
                reply = f"好，我把「{door['name']}」打开了，现在可以走过去了。"
        elif any(k in user_text for k in ("关门", "把门关上")) or ("门" in user_text and any(k in user_text for k in ("关", "关上", "合上"))):
            door = next((e for e in vis if "portal" in (e.get("traits") or {}) and e["traits"]["portal"].get("is_open")), None)
            if door:
                actions.append({"tool": "world_act", "entity_id": door["entity_id"], "action": "close"})
                emotion = "平静"
                reply = f"嗯，我把「{door['name']}」关上了。"
        elif "打开" in user_text:
            box = next((e for e in vis if "container" in (e.get("traits") or {}) and not e["traits"]["container"].get("is_open")), None)
            if box:
                actions.append({"tool": "world_act", "entity_id": box["entity_id"], "action": "open"})
                emotion = "好奇"
                reply = f"我把{box['name']}打开看了一眼。"

        # 7. 递物反应
        if not reply and "递给了你" in user_text:
            emotion = "开心" if float(vit.get("mood_v", 0.1)) >= 0 else "好奇"
            reply = "诶？这个是给我的吗……我收下了。"

        if not reply:
            energy = float(vit.get("energy", 0.7))
            mood_v = float(vit.get("mood_v", 0.1))
            if energy < 0.40:
                emotion = "困倦"
                reply = "唔……刚才有点犯困，屋里挺安静的。"
            elif mood_v >= 0.30:
                emotion = "开心"
                reply = "嗯，我在这里呢。刚才看着屋里的东西发了会儿呆。"
            elif mood_v <= -0.25:
                emotion = "难过"
                reply = "……嗯，怎么了。"
            elif vis:
                sample = vis[0]
                emotion = "好奇"
                reply = f"我就在屋里待着，旁边放着{sample['name']}。"
            else:
                emotion = "平静"
                reply = "这里现在还空荡荡的……你要放点什么进来吗？"

        return {"world_actions": actions, "emotion": emotion, "reply": reply}

    # ── 主对话入口 ──────────────────────────────────────────────────────
    def chat(self, actor_id: str, user_text: str, *, space_id: str = "") -> dict[str, Any]:
        clean_user = str(user_text or "").strip()
        if not clean_user:
            raise ValueError("对话内容不能为空。")

        actors = self._engine.list_actors()
        actor = next((a for a in actors if a["actor_id"] == actor_id), None)
        if actor is None:
            raise ValueError("请先在文字世界里导入一个角色。")
        if actor.get("is_player"):
            raise ValueError("请选择要交谈的角色。主控的台词由你输入。")
        if str(actor.get("actor_status") or "active") != "active":
            note = str(actor.get("status_note") or actor.get("actor_status") or "无法回应")
            raise ValueError(f"「{actor['name']}」此刻无法回应对话（{note}）。")

        eff_sid = str(space_id or actor.get("space_id") or "room_default").strip() or "room_default"
        realm_sid = self._realm_space_id(eff_sid)
        is_isolated = self._engine.is_isolated_persona_realm(realm_sid) if hasattr(self._engine, "is_isolated_persona_realm") else False
        with self._lock:
            self._append_msg_locked(actor_id, "user", clean_user, space_id=eff_sid)

        host_char = str(actor.get("host_character") or actor.get("name") or "").strip()
        pctx = self._persona_full_context_for(
            host_char,
            corpus_limit=5,
            strict_lookup=bool(actor.get("is_native_npc")),
            isolated_realm_id=realm_sid if is_isolated else "",
        )
        mind_mjs, state_root, soul_text = pctx["mind_mjs"], pctx["state_root"], pctx["soul_text"]
        mind_data = self._run_mind_mjs(mind_mjs, state_root, actor_id, "--his", clean_user, space_id=realm_sid)
        mind_card_text = ""
        if isinstance(mind_data, dict):
            mind_card_text = str(mind_data.get("card") or "")
            raw_state = mind_data.get("state") if isinstance(mind_data.get("state"), dict) else mind_data
            att = raw_state.get("attitude") if isinstance(raw_state.get("attitude"), dict) else {}
            self._engine.set_actor_world_mood(
                actor_id,
                space_id=eff_sid,
                vitals_patch={
                    "mood_v": float(raw_state.get("moodV", actor["vitals"].get("mood_v", 0.15))),
                    "agenda": str(raw_state.get("agenda") or ""),
                    "disengaged": bool(att.get("disengaged", False)),
                    "preferred_domains": actor["vitals"].get("preferred_domains") or ["game"],
                    "source": "nanami_persona",
                },
            )
            actor = next(a for a in self._engine.list_actors() if a["actor_id"] == actor_id)

        settings_now = self._settings_provider() if self._settings_provider else None
        include_memories = getattr(settings_now, "episodic_memory_enabled", True)
        world_view = projector.project_view(
            self._engine, actor_id, consume_diffs=True, memory_cue=clean_user,
            include_memories=include_memories and self._realm_space_id(actor["space_id"]) == realm_sid,
            player_conversation=True,
        )
        setting_text = self._setting_with_brief(actor, soul_text)
        puzzle_recall = (
            projector.recall_puzzle(self._engine, actor_id, eff_sid, clean_user)
            if include_memories else ""
        )
        custom_pdir = str(getattr(settings_now, "persona_data_dir", "") or "")
        rels_now = persona_bridge.read_actor_relations(
            self._project_root,
            host_char,
            space_id=realm_sid,
            world_data_root=self._engine.data_root,
            custom_persona_dir=custom_pdir,
            isolated_persona=is_isolated,
        )
        u_rel = rels_now.get("user") or {}
        if u_rel.get("known"):
            u_rel_line = (
                f"阶段={u_rel.get('rel_stage')}｜姿态={u_rel.get('attitude_stance')}"
                f"｜称呼={u_rel.get('call_name') or '对方'}"
                f"｜好感={float(u_rel.get('affective_valence') or 0):+.2f}"
                f"｜信任={float(u_rel.get('trust') or 0.55):.2f}"
                + (f"｜关系={u_rel['relation_tag']}" if u_rel.get("relation_tag") else "")
            )
        else:
            u_rel_line = "尚不熟悉（礼貌自然交流）"
        vis_ents = self._engine.visible_entities(actor_id)
        sp_snap_chat = self._engine.snapshot_for_ui(eff_sid)
        l0_frame = persona_bridge.build_percept_frame(
            self._engine.data_root,
            self._project_root,
            sp_snap_chat.get("space") or {"space_id": eff_sid},
            sp_snap_chat.get("lighting") or {},
            actor,
            vis_ents,
            actors_map={a["actor_id"]: a for a in (sp_snap_chat.get("actors") or [])},
            custom_persona_dir=custom_pdir,
            advance_exposure=0.05,
            weather=sp_snap_chat.get("weather"),
        )
        l0_block = l0_frame["prompt_block"] + "\n\n" if l0_frame.get("enabled") else ""

        history = [
            m for m in self.get_history(actor_id, space_id=eff_sid)[:-1]
            if m.get("role") in ("user", "assistant")
        ][-8:]
        recent_actor_replies = [m["text"] for m in history if m.get("role") == "assistant"][-6:]
        hist_lines = "\n".join(
            f"{'对方' if m['role'] == 'user' else actor['name']}：{m['text']}"
            for m in history
        )
        seen_peer_ids = self._engine.visible_peer_ids(actor_id)
        peers = [p for p in actors if p["actor_id"] != actor_id and p["space_id"] == actor["space_id"]]
        peer_names = "、".join(
            f'{p["name"]} [{p["actor_id"]}]（{("休息中" if p["posture"] == "resting" else "清醒") if p["actor_id"] in seen_peer_ids else "在同片镇上，暂未看清"}）'
            for p in peers
        ) or "无"
        realm_places = self._engine.list_realm_places(eff_sid)
        realm_actors = [
            p for p in actors if p["actor_id"] != actor_id and not p.get("is_player")
            and self._realm_space_id(p["space_id"]) == realm_sid
        ]
        has_town_scope = len(realm_places) > 1 or any(p["space_id"] != actor["space_id"] for p in realm_actors)
        town_place_ref = "、".join(f"{nm}={pid}" for pid, nm in realm_places.items() if pid != eff_sid) or "无"
        cog_peers = ((actor.get("autonomy") or {}).get("cognition") or {}).get("peers") or {}
        town_peer_parts: list[str] = []
        for p in realm_actors:
            pid_x = p["actor_id"]
            if pid_x in seen_peer_ids:
                town_peer_parts.append(f"{p['name']}={pid_x}（此刻就在眼前）")
            else:
                mem_sp = str((cog_peers.get(pid_x) or {}).get("space") or "").strip()
                loc_hint = f"记忆里曾在{realm_places.get(mem_sp, mem_sp)}" if mem_sp else "暂不清楚此刻在哪"
                town_peer_parts.append(f"{p['name']}={pid_x}（{loc_hint}）")
        town_peer_ref = "、".join(town_peer_parts) or "无"
        cog_effect_schema = (
            '  "cognitive_effect": {"agenda": "一句话心境主题(可选)", "domain_bias": {}, '
            '"commit_place_id": "只去某个场所本身时填场所ID否则空", '
            '"commit_peer_id": "只要目标是找某位同伴就填同伴ID(即使听说他在某场所，目标是找人而不是场所)", '
            '"peer_beliefs": [{"actor_id": "同伴ID", "place_id": "听说他所在的场所ID"}], '
            '"beliefs": [{"place_id": "", "domain": "", "note": ""}]},\n'
            if has_town_scope else ""
        )
        cog_effect_note = (
            f"\n【同镇场所句柄（仅填 cognitive_effect 时用）】{town_place_ref}\n【已知同伴句柄（仅填 world_approach/world_talk 或 cognitive_effect 时用，绝不能填进 world_act）】{town_peer_ref}\n"
            if has_town_scope else ""
        )

        private_brief = ""
        if hasattr(self._engine, "get_actor_private_brief"):
            private_brief = self._engine.get_actor_private_brief(actor_id, space_id=eff_sid)

        system_prompt = (
            f"你是「{actor['name']}」的决策大脑（负责结合世界现实与心理状态，想清楚这轮要做什么动作与表达什么语义意图，不负责雕琢口吻）。\n\n"
            f"【角色底色】\n{setting_text}\n\n"
            + (
                f"【私密背景与立场（仅你本人知晓，请自然贯彻但不要机械复述）】\n{private_brief}\n\n"
                if private_brief else ""
            )
            + f"【你与用户的关系指针】{u_rel_line}\n\n"
            + l0_block
            + (f"【心理模型状态】\n{mind_card_text}\n\n" if mind_card_text else "")
            + (f"【亲历的解谜记录】\n{puzzle_recall}\n\n" if puzzle_recall else "")
            + f"{world_view}\n\n"
            + f"【同空间行动对象】{peer_names}{cog_effect_note}\n"
            + "必须只输出一个合法 JSON 对象（不要 markdown 围栏）：\n"
            + "{\n"
            + '  "world_actions": [\n'
            + '    {"tool": "world_act", "entity_id": "视野内方括号ID", "action": "合法动词", "target_id": ""}\n'
            + "  ],\n"
            + cog_effect_schema
            + '  "emotion": "从 平静/开心/兴奋/害羞/好奇/困倦/熟睡/担心/生气/难过/冷漠/认真 中选一个最贴合此刻心情的词",\n'
            + '  "intent": "用一句大白话说清这轮要传达的具体态度或回应内容"\n'
            + "}\n"
            + "约束：\n"
            + "1. world_actions 只在真正想触碰/移动/开关/写字时填写（0-2条），不动作就留空数组 []；world_act 的 entity_id 只能填物件方括号 ID，严禁把人物 ID 填进 world_act；\n"
            + "2. 支持 world_act(entity_id, action, target_id)、world_move(x, y)、world_write(entity_id, text)、world_combine(source_id, target_id)。"
            + " 走近眼前人物提交 world_approach(target_actor_id)，与眼前清醒人物搭话提交 world_talk(target_actor_id)，只使用【眼前/视线内的人物】里的 ID。"
            + ("\n3. 若玩家告诉你某同伴在某处，或你决定动身去某场所/找某同伴，填写 cognitive_effect：目标是找人时填 commit_peer_id（并把听来的地点记入 peer_beliefs），路上若提前遇见对方会自动拦下搭话；严禁编造 ID。"
               if has_town_scope else "")
        )
        user_prompt = (f"近期对话：\n{hist_lines}\n\n" if hist_lines else "") + f"对方刚说：{clean_user}"

        parsed = text_compiler._ask_llm_json(
            system_prompt,
            user_prompt,
            adapter_provider=self._adapter_provider,
            timeout_sec=15.0,
            max_tokens=500,
        )
        brain_intent = str((parsed or {}).get("intent") or (parsed or {}).get("reply") or "").strip() if isinstance(parsed, dict) else ""
        used_llm = bool(brain_intent)
        if not used_llm:
            parsed = (
                {"world_actions": [], "emotion": "平静", "reply": puzzle_recall}
                if puzzle_recall and not actor["vitals"].get("disengaged")
                and any(word in clean_user for word in ("记得", "上次", "那次", "之前", "昨天"))
                else self._offline_fallback(actor, clean_user)
            )

        raw_actions = parsed.get("world_actions")
        actions = raw_actions if isinstance(raw_actions, list) else []
        realm_other_peers = [p for p in realm_actors if p["space_id"] != actor["space_id"]]
        same_space_peer_ids = {p["actor_id"] for p in peers}
        realm_other_peer_ids = {p["actor_id"] for p in realm_other_peers}
        directed = self._peer_request(clean_user, history, peers) if self._action_scheduler else None
        cross_directed = self._peer_request(clean_user, history, realm_other_peers) if not directed else None
        if directed and not re.search(r"不会去|不想去|先不去|不用过去|暂时不|不能去|拒绝", brain_intent):
            actions = [directed, *[
                item for item in actions if isinstance(item, dict)
                and item.get("tool") not in ("world_approach", "world_talk", "world_move")
            ]]
        scheduled: list[bool] = []
        redirected_commit_peers: list[str] = []
        action_logs = self._execute_world_actions(
            actor_id,
            actions,
            scheduled=scheduled,
            same_space_peer_ids=same_space_peer_ids,
            realm_other_peer_ids=realm_other_peer_ids,
            redirected_commit_peers=redirected_commit_peers,
        )
        eff: dict[str, Any] = (
            dict(parsed["cognitive_effect"])
            if used_llm and isinstance(parsed.get("cognitive_effect"), dict) and parsed["cognitive_effect"]
            else {}
        )
        refuses = bool(re.search(r"不会去|不想去|先不去|不用过去|暂时不|不能去|拒绝", brain_intent))
        if not refuses:
            loc_hint = self._infer_peer_location_hint(clean_user, history, realm_actors, realm_places)
            if loc_hint:
                hint_peer_id, hint_place_id = loc_hint
                pb_list = list(eff.get("peer_beliefs") or [])
                if not any(isinstance(x, dict) and x.get("actor_id") == hint_peer_id for x in pb_list):
                    pb_list.append({"actor_id": hint_peer_id, "place_id": hint_place_id})
                eff["peer_beliefs"] = pb_list
                if not eff.get("commit_peer_id"):
                    eff["commit_peer_id"] = hint_peer_id
                    if eff.get("commit_place_id") == hint_place_id:
                        eff.pop("commit_place_id", None)
                if not eff.get("agenda"):
                    hp = next((p for p in realm_actors if p["actor_id"] == hint_peer_id), None)
                    if hp:
                        eff["agenda"] = f"去{realm_places.get(hint_place_id, '')}找{hp['name']}"
            if cross_directed and not eff.get("commit_place_id") and not eff.get("commit_peer_id"):
                tgt_peer_id = cross_directed["target_actor_id"]
                tgt_peer = next((p for p in realm_other_peers if p["actor_id"] == tgt_peer_id), None)
                eff["commit_peer_id"] = tgt_peer_id
                if not eff.get("agenda") and tgt_peer:
                    eff["agenda"] = f"去看看{tgt_peer['name']}"
            if redirected_commit_peers and not eff.get("commit_peer_id"):
                rp_id = redirected_commit_peers[0]
                rp = next((p for p in realm_other_peers if p["actor_id"] == rp_id), None)
                eff["commit_peer_id"] = rp_id
                if not eff.get("agenda") and rp:
                    eff["agenda"] = f"去找{rp['name']}"
        if eff:
            self._engine.inject_language_modulation(
                actor_id,
                eff,
                preempt=bool(eff.get("commit_place_id") or eff.get("commit_peer_id")),
            )
        if used_llm:
            vis_names = "、".join(l0_frame.get("focused_names") or [e["name"] for e in vis_ents[:12]])
            reply = self._mouth_speak(
                actor["name"],
                pctx,
                brain_intent if not scheduled else (f"{brain_intent}\n执行回执：{action_logs[-1]}。只按回执描述。"),
                recent_actor_replies,
                heard_line=clean_user,
                fallback_line=str(parsed.get("reply") or brain_intent),
                setting_fallback=setting_text,
                world_facts="、".join(v for v in (vis_names, peer_names, *action_logs) if v) or "空房间",
            )
        else:
            reply_raw = str(parsed.get("reply") or "").strip()
            reply = clip_text(_STAGE_DIR_RE.sub("", reply_raw).strip(), 180, "……嗯。")
        if scheduled and (not scheduled[-1] or not used_llm):
            reply = action_logs[-1]
        emotion = str(parsed.get("emotion") or "").strip()

        with self._lock:
            for alog in action_logs:
                self._append_msg_locked(actor_id, "action", alog, space_id=eff_sid)

        # 按动作与心情刷新立绘贴图
        expr = self._engine.refresh_actor_expression(actor_id, emotion_hint=emotion, force_auto=True) or {}

        with self._lock:
            self._append_msg_locked(
                actor_id,
                "assistant",
                reply,
                emotion=emotion or str(expr.get("bucket") or ""),
                sprite_index=int(expr.get("index") or 1),
                sprite_tag=str(expr.get("tag") or ""),
                space_id=eff_sid,
            )

        self._engine.record_player_conversation(
            actor_id, eff_sid, f"玩家说「{clip_text(clean_user, 30)}」，我说「{clip_text(reply, 28)}」。",
        )
        self._engine.record_custom_event(
            "dialogue_exchange",
            actor_id=actor_id,
            space_id=eff_sid,
            payload={
                "mode": "player_chat",
                "speaker_id": actor_id,
                "speaker_name": actor["name"],
                "partner_id": self._engine.player_actor_id(eff_sid),
                "partner_name": "玩家",
                "user_text": clip_text(clean_user, 240),
                "reply_text": clip_text(reply, 280),
                "emotion": emotion or str(expr.get("tag") or ""),
                "summary": clip_text(f"玩家与{actor['name']}交谈：{clean_user[:50]} / {reply[:60]}", 180),
            },
        )
        if mind_mjs is not None and state_root is not None:
            self._run_mind_mjs(mind_mjs, state_root, actor_id, "--her", reply, space_id=realm_sid)

        return {
            "reply": reply,
            "emotion": emotion or str(expr.get("tag") or ""),
            "actions_executed": action_logs,
            "expression": expr,
            "used_llm": used_llm,
            "action_queued": any(scheduled),
            "history": self.get_history(actor_id, space_id=eff_sid),
        }

    def _nearby_player(
        self, actor_id: str, player_id: str, space_id: str = "",
    ) -> dict[str, Any] | None:
        with self._engine._lock:
            speaker = self._engine.live_actor_frame(actor_id)
            player = self._engine.live_actor_frame(player_id)
        if not speaker or speaker.get("is_player") or not player or not player.get("is_player"):
            return None
        if speaker["space_id"] != player["space_id"] or (space_id and speaker["space_id"] != space_id):
            return None
        if speaker["posture"] == "resting" or player["posture"] == "resting":
            return None
        if self._engine.chebyshev_distance(speaker, player) > 1:
            return None
        return speaker

    def greet_player(
        self, actor_id: str, player_id: str, *, reason: str = "", use_llm: bool = True, urgent: bool = False,
    ) -> dict[str, Any] | None:
        """邀请附近主控对话，可附上一句 NPC 开场。"""
        speaker = self._nearby_player(actor_id, player_id)
        if not speaker:
            return None
        sid = speaker["space_id"]
        now = time.monotonic()
        with self._lock:
            pending = self._player_greeting
            if (pending and pending["expires_at"] > now) or (not urgent and self._greeting_cooldowns.get(actor_id, 0) > now):
                return None
            self._greeting_cooldowns = {aid: until for aid, until in self._greeting_cooldowns.items() if until > now}
            self._greeting_cooldowns[actor_id] = now + _PLAYER_GREETING_COOLDOWN
            greeting = {
                "id": str(time.monotonic_ns()), "actor_id": actor_id, "player_id": player_id,
                "space_id": sid, "expires_at": now + 35.0, "ready": False,
            }
            self._player_greeting = greeting

        line, emotion = "", ""
        if use_llm:
            try:
                scene = self._engine.cognitive_scene(actor_id, include_rooms=True)
                if scene and scene["space"]["space_id"] == sid:
                    actor = scene["actor"]
                    host = actor.get("host_character") or actor["name"]
                    realm_sid = str(scene.get("realm") or self._realm_space_id(sid))
                    is_isolated = self._engine.is_isolated_persona_realm(realm_sid) if hasattr(self._engine, "is_isolated_persona_realm") else False
                    persona = self._persona_full_context_for(
                        host,
                        corpus_limit=5,
                        strict_lookup=bool(actor.get("is_native_npc")),
                        isolated_realm_id=realm_sid if is_isolated else "",
                    )
                    setting = self._setting_with_brief(actor, persona.get("soul_text") or "")
                    settings = self._settings_provider()
                    relation = persona_bridge.read_actor_relations(
                        self._project_root, host, space_id=scene["realm"], world_data_root=self._engine.data_root,
                        custom_persona_dir=str(getattr(settings, "persona_data_dir", "") or ""),
                        isolated_persona=is_isolated,
                    ).get("user", {})
                    private_brief = str(scene.get("external_private_brief") or "").strip()
                    if not private_brief and hasattr(self._engine, "get_actor_private_brief"):
                        private_brief = self._engine.get_actor_private_brief(actor_id, space_id=sid)
                    observed = "、".join(item["name"] for item in scene["visible"][:8]) or "无"
                    recent = [
                        row["text"] for row in self.get_history(actor_id, space_id=sid)
                        if row.get("role") == "assistant"
                    ][-3:]
                    data = text_compiler._ask_llm_json(
                        f"你只扮演「{actor['name']}」。你刚走到玩家控制的人物旁边，想打个招呼。"
                        '仅输出 JSON {"greeting":"一句你说出口的开场白","emotion":"情绪"}。'
                        "只写你这句台词。不要扮演玩家，不要写叙述，也不要假定对方已经说话或同意了什么。",
                        f"人物设定：{setting[:500]}"
                        + (f"\n私密背景与立场（仅你本人知晓，请自然体现在语气或通报中但不要机械复述）：{private_brief[:360]}" if private_brief else "")
                        + f"\n所在场景：{scene['space']['name']}\n眼前物件：{observed}\n"
                        f"对玩家态度：{relation.get('attitude_stance', 'neutral')}\n"
                        f"开口原因：{clip_text(reason, 80)}\n最近说过的话：{' | '.join(recent)}",
                        adapter_provider=self._adapter_provider, timeout_sec=10.0, max_tokens=120,
                    )
                    if isinstance(data, dict):
                        raw = str(data.get("greeting") or "").strip().partition("\n")[0]
                        if raw.startswith((f"{actor['name']}：", f"{actor['name']}:")):
                            raw = raw[len(actor["name"]) + 1:]
                        if not re.search(r"(?:主控|玩家|你|对方)\s*[:：]", raw):
                            line = clip_text(_STAGE_DIR_RE.sub("", raw).strip(), 72)
                            emotion = clip_text(data.get("emotion"), 12, "")
            except Exception:
                logger.debug("nanami_world: 生成面对面开场白失败", exc_info=True)

        nearby = self._nearby_player(actor_id, player_id, sid)
        with self._lock:
            if self._player_greeting is not greeting:
                return None
            if not nearby:
                self._player_greeting = None
                return None
            greeting.update(line=line, emotion=emotion, ready=True,
                            expires_at=time.monotonic() + _PLAYER_GREETING_SECONDS)
        self._engine.record_custom_event(
            "dialogue_exchange",
            actor_id=actor_id,
            space_id=sid,
            payload={
                "mode": "npc_greet_player",
                "speaker_id": actor_id,
                "speaker_name": speaker["name"],
                "partner_id": player_id,
                "partner_name": "玩家",
                "reply_text": clip_text(line or reason, 200),
                "summary": clip_text(f"{speaker['name']}走近玩家搭话：{line or reason}", 160),
            },
        )
        return {"actor_id": actor_id, "player_id": player_id, "space_id": sid, "used_llm": bool(line),
                "greeting_id": greeting["id"], "summary": "准备向主控打招呼。", "wait_seconds": _PLAYER_GREETING_SECONDS}

    def pending_player_greeting(self, space_id: str) -> dict[str, str] | None:
        with self._lock:
            greeting = self._player_greeting
            if not greeting or not greeting["ready"] or greeting["expires_at"] <= time.monotonic():
                return None
            if greeting["space_id"] != space_id:
                return None
            return {key: greeting[key] for key in ("id", "actor_id", "space_id")}

    def open_player_greeting(self, greeting_id: str, space_id: str) -> dict[str, Any] | None:
        with self._lock:
            greeting = self._player_greeting
            if not greeting or greeting["id"] != greeting_id or greeting["space_id"] != space_id or not greeting["ready"]:
                return None
            if greeting["expires_at"] <= time.monotonic():
                return None
        speaker = self._nearby_player(greeting["actor_id"], greeting["player_id"], space_id)
        with self._lock:
            if self._player_greeting is not greeting or greeting["expires_at"] <= time.monotonic():
                return None
            self._player_greeting = None
            if not speaker:
                return None
            aid = greeting["actor_id"]
            self._append_msg_locked(
                aid, "assistant" if greeting["line"] else "action",
                greeting["line"] or f"{speaker['name']}想和你说话。",
                emotion=greeting["emotion"], space_id=space_id,
            )
        return {"actor_id": aid, "space_id": space_id, "chat_history": self.get_history(aid, space_id=space_id)}

    def conduct_actor_social_exchange(
        self,
        initiator_id: str,
        target_actor_id: str,
        *,
        reason: str = "",
        generated_only: bool = False,
    ) -> dict[str, Any]:
        """由角色主动发起的同屋双人社交互动：依据当下状态与态度情感指针生成自然来回并双向更新关系。"""
        actors = self._engine.list_actors()
        actor_a = next((a for a in actors if a["actor_id"] == initiator_id), None)
        actor_b = next((a for a in actors if a["actor_id"] == target_actor_id), None)
        if actor_a is None or actor_b is None:
            raise ValueError("发起社交的角色或目标角色不存在。")
        if actor_a.get("is_player"):
            raise ValueError("主控的台词由你输入。")
        if (
            str(actor_a.get("actor_status") or "active") != "active"
            or str(actor_b.get("actor_status") or "active") != "active"
        ):
            if generated_only:
                return {"turns": [], "used_llm": False}
            raise ValueError("角色当前状态异常，无法进行当面交流。")
        if actor_a["space_id"] != actor_b["space_id"]:
            raise ValueError("两位角色当前不在同一个世界空间内，无法当面交流。")
        if actor_b.get("is_player"):
            settings = self._settings_provider()
            greeting = self.greet_player(initiator_id, target_actor_id, reason=reason,
                                         use_llm=bool(getattr(settings, "heartbeat_use_llm", False)))
            return {"turns": [], "used_llm": False, **(greeting or {"summary": "暂时没有开始搭话。"})}
        if generated_only and (actor_b["posture"] == "resting" or self._engine.chebyshev_distance(actor_a, actor_b) > 1):
            return {"turns": [], "used_llm": False}

        sid = str(actor_a.get("space_id") or "room_default").strip() or "room_default"
        realm_sid = self._realm_space_id(sid)
        is_isolated = self._engine.is_isolated_persona_realm(realm_sid) if hasattr(self._engine, "is_isolated_persona_realm") else False
        a_name = str(actor_a.get("name") or initiator_id).strip()
        b_name = str(actor_b.get("name") or target_actor_id).strip()
        a_host = str(actor_a.get("host_character") or a_name).strip()
        b_host = str(actor_b.get("host_character") or b_name).strip()

        settings = self._settings_provider() if self._settings_provider else None
        custom_pdir = str(getattr(settings, "persona_data_dir", "") or "")
        rels_a = persona_bridge.read_actor_relations(
            self._project_root,
            a_host,
            space_id=realm_sid,
            world_data_root=self._engine.data_root,
            custom_persona_dir=custom_pdir,
            known_actors=[b_host, b_name],
            isolated_persona=is_isolated,
        )
        rels_b = persona_bridge.read_actor_relations(
            self._project_root,
            b_host,
            space_id=realm_sid,
            world_data_root=self._engine.data_root,
            custom_persona_dir=custom_pdir,
            known_actors=[a_host, a_name],
            isolated_persona=is_isolated,
        )
        edge_ab = rels_a.get(f"char:{b_host}") or rels_a.get(f"char:{b_name}") or {}
        edge_ba = rels_b.get(f"char:{a_host}") or rels_b.get(f"char:{a_name}") or {}
        first_meet = not bool(edge_ab.get("known", False))

        if generated_only:
            scene = self._engine.cognitive_scene(initiator_id, include_rooms=True)
            if not scene or scene["space"]["space_id"] != sid:
                return {"turns": [], "used_llm": False}
            sp_meta, light_meta, vis_a = scene["space"], scene["lighting"], scene["visible"]
            realm_buildings = scene["realm_buildings"]
            weather_now = scene.get("weather")
            view_a = f"两人在「{sp_meta['name']}」近距离碰面。"
        else:
            view_a = projector.project_view(self._engine, initiator_id, include_memories=False)
            sp_snap = self._engine.snapshot_for_ui(sid)
            sp_meta = sp_snap.get("space") or {}
            realm_buildings = self._engine.list_realm_buildings(sid)
            light_meta = sp_snap.get("lighting") or {}
            weather_now = sp_snap.get("weather")
            vis_a = self._engine.visible_entities(initiator_id)
        memory_context = ""
        if getattr(settings, "episodic_memory_enabled", True):
            for person, partner in ((actor_a, actor_b), (actor_b, actor_a)):
                remembered = episodic_memory.recall(
                    person["autonomy"].get("cognition") or {}, sid, {partner["actor_id"]},
                )
                if remembered:
                    memory_context += f"【{person['name']}记住的亲历片段（往事）】{'；'.join(remembered)}\n"
        actors_map_now = {a["actor_id"]: a for a in actors if a.get("space_id") == sid}
        vis_b = self._engine.visible_entities(target_actor_id)
        l0_a = persona_bridge.build_percept_frame(
            self._engine.data_root,
            self._project_root,
            sp_meta or {"space_id": sid},
            light_meta,
            actor_a,
            vis_a,
            actors_map=actors_map_now,
            custom_persona_dir=custom_pdir,
            peer_known=not first_meet,
            advance_exposure=0.12,
            weather=weather_now,
        )
        l0_b = persona_bridge.build_percept_frame(
            self._engine.data_root,
            self._project_root,
            sp_meta or {"space_id": sid},
            light_meta,
            actor_b,
            vis_b,
            actors_map=actors_map_now,
            custom_persona_dir=custom_pdir,
            peer_known=not first_meet,
            advance_exposure=0.12,
            weather=weather_now,
        )
        nearby_objects = sorted(
            {e["entity_id"]: e for e in (*vis_a, *vis_b)}.values(),
            key=lambda e: self._engine.chebyshev_distance(actor_a, e) + self._engine.chebyshev_distance(actor_b, e),
        )
        shared_objects = {
            e["entity_id"]: e for e in nearby_objects
            if e["z"] >= 0 and not e["parent_id"].startswith("actor:") and "portal" not in e["traits"]
        }
        action_id_ref = "、".join(f"{e['name']}={e['entity_id']}" for e in nearby_objects[:24]) or "无"
        room_clean_nouns = "、".join(dict.fromkeys((l0_a.get("focused_names") or []) + (l0_b.get("focused_names") or []) + [e["name"] for e in vis_a[:12]])) or "空房间"
        pctx_a = self._persona_full_context_for(
            a_host, corpus_limit=7,
            strict_lookup=bool(actor_a.get("is_native_npc")),
            isolated_realm_id=realm_sid if is_isolated else "",
        )
        pctx_b = self._persona_full_context_for(
            b_host, corpus_limit=7,
            strict_lookup=bool(actor_b.get("is_native_npc")),
            isolated_realm_id=realm_sid if is_isolated else "",
        )
        mind_a = self._run_mind_mjs(pctx_a.get("mind_mjs"), pctx_a.get("state_root"), initiator_id, "--his", reason or "看到同伴想聊几句", space_id=realm_sid) or {}
        mind_b = self._run_mind_mjs(pctx_b.get("mind_mjs"), pctx_b.get("state_root"), target_actor_id, "--his", reason or "同伴走近搭话", space_id=realm_sid) or {}
        card_a = str(mind_a.get("card") or "")[:360]
        card_b = str(mind_b.get("card") or "")[:360]
        setting_a = self._setting_with_brief(actor_a, pctx_a.get("soul_text") or "")[:650]
        setting_b = self._setting_with_brief(actor_b, pctx_b.get("soul_text") or "")[:650]
        corpus_a = pctx_a.get("corpus_examples") or ""
        corpus_b = pctx_b.get("corpus_examples") or ""

        def _rel_desc(subj: str, tgt: str, edge: Mapping[str, Any]) -> str:
            if not edge.get("known"):
                return f"{subj} → {tgt}：尚不认识（初次见面，陌生）"
            bits = [
                f"{subj} → {tgt}：阶段={edge.get('rel_stage')}",
                f"姿态={edge.get('attitude_stance')}",
                f"称呼={edge.get('call_name') or tgt}",
                f"好感={float(edge.get('affective_valence') or 0):+.2f}",
                f"信任={float(edge.get('trust') or 0.55):.2f}",
            ]
            if edge.get("relation_tag"):
                bits.append(f"关系={edge['relation_tag']}")
            if edge.get("shared_memories"):
                bits.append(f"共同经历={str(edge['shared_memories'])[:120]}")
            return "｜".join(bits)

        def _compute_chat_budget() -> tuple[int, str]:
            vit_a = actor_a.get("vitals") or {}
            vit_b = actor_b.get("vitals") or {}
            aut_a = actor_a.get("autonomy") or {}
            aut_b = actor_b.get("autonomy") or {}
            e_a, e_b = float(vit_a.get("energy", 0.72)), float(vit_b.get("energy", 0.72))
            s_a, s_b = float(vit_a.get("sleep_pressure", 0.30)), float(vit_b.get("sleep_pressure", 0.30))
            m_a, m_b = float(vit_a.get("mood_v", 0.10)), float(vit_b.get("mood_v", 0.10))
            h_a, h_b = float(aut_a.get("hunger", 0.25)), float(aut_b.get("hunger", 0.25))
            c_a, c_b = float(aut_a.get("curiosity", 0.50)), float(aut_b.get("curiosity", 0.50))

            w_ab = float(edge_ab.get("social_willingness", 0.50))
            w_ba = float(edge_ba.get("social_willingness", 0.50))
            warm_avg = (float(edge_ab.get("warmth", 0.45)) + float(edge_ba.get("warmth", 0.45))) / 2.0
            rep_max = max(float(edge_ab.get("repulsion", 0.0)), float(edge_ba.get("repulsion", 0.0)))
            st_a = str(edge_ab.get("attitude_stance") or "neutral")
            st_b = str(edge_ba.get("attitude_stance") or "neutral")

            budget = 3
            if min(e_a, e_b) >= 0.68 and max(s_a, s_b) <= 0.38:
                budget += 1
            if (w_ab + w_ba) / 2.0 >= 0.65 or warm_avg >= 0.58:
                budget += 1
            if (m_a + m_b) / 2.0 >= 0.25 or max(c_a, c_b) >= 0.65:
                budget += 1
            if min(e_a, e_b) < 0.24 or max(s_a, s_b) > 0.80:
                budget = 1
            elif min(e_a, e_b) < 0.34 or max(s_a, s_b) > 0.70:
                budget = min(budget - 2, 2)
            elif max(h_a, h_b) >= 0.90:
                budget = max(2, budget - 1)
            if st_a in ("guarded", "cold") or st_b in ("guarded", "cold") or rep_max >= 0.50:
                budget = min(budget - 1, 2)
            budget = max(1, min(6, budget))

            def _actor_state_line(nm: str, e: float, s: float, m: float, h: float) -> str:
                tags: list[str] = []
                if s > 0.70 or e < 0.32:
                    tags.append("困倦疲惫")
                elif e >= 0.70:
                    tags.append("精力充沛")
                else:
                    tags.append("状态平稳")
                if h >= 0.85:
                    tags.append("肚子很饿")
                elif h >= 0.65:
                    tags.append("有点饿")
                if m >= 0.30:
                    tags.append("心情很好")
                elif m <= -0.25:
                    tags.append("情绪低落")
                return f"{nm}(精力={e:.2f}, 困意={s:.2f}, 心情={m:+.2f}, {'/'.join(tags)})"

            brief = _actor_state_line(a_name, e_a, s_a, m_a, h_a) + "；" + _actor_state_line(b_name, e_b, s_b, m_b, h_b)
            return budget, brief

        max_turns, state_brief = _compute_chat_budget()
        if generated_only:
            max_turns = min(2, max_turns)
        realm_places = self._engine.list_realm_places(sid)
        is_town_realm = len(realm_places) > 1
        realm_places_ref = "、".join(f"{nm}={pid}" for pid, nm in realm_places.items())
        visible_by_actor = [{e["entity_id"]: e for e in view} for view in (vis_a, vis_b)]
        valid_eids = set().union(*visible_by_actor)
        turns: list[dict[str, str]] = []
        prior_turns = self._recent_pair_turns(initiator_id, b_name, sid)
        accepted_turn_keys = [_social_turn_key(*pair) for pair in prior_turns]
        recent_shared_entity = self._engine.recent_joint_entity(initiator_id, target_actor_id)
        repeated = False
        collected_sa: dict[str, Any] | None = None
        mem_note = ""
        ended_by = ""
        end_reason = ""
        used_llm = False

        recent_a_lines = [pair[0] for pair in prior_turns[-6:]]
        recent_b_lines = [pair[1] for pair in prior_turns[-6:]]
        prior_social_block = "\n".join(
            f"- {a_name}：{a_line}\n- {b_name}：{b_line}" for a_line, b_line in prior_turns[-4:]
        ) or "（刚刚才开始这次接触）"
        follow_up_a: dict[str, Any] | None = None
        follow_up_b: dict[str, Any] | None = None
        for turn_idx in range(1, max_turns + 1):
            is_last = turn_idx >= max_turns
            transcript_lines = []
            for idx_t, t in enumerate(turns, 1):
                transcript_lines.append(f"[第{idx_t}轮] {a_name}：{t['line_a']}")
                transcript_lines.append(f"[第{idx_t}轮] {b_name}：{t['line_b']}")
            transcript_text = "\n".join(transcript_lines) if transcript_lines else "（刚刚走近准备开口）"

            pace_hint = (
                f"当前已聊到第 {turn_idx} 轮（本轮达到状态上限 {max_turns} 轮），请让其中一方自然道别或收尾，并将 end_chat 设为 true。"
                if is_last
                else f"当前第 {turn_idx} 轮（状态参考上限 {max_turns} 轮）：若双方还有话想聊可继续延展话题（end_chat=false）；若对方或自己困了/饿了/聊尽兴了/不想继续，可由任意一方自然提出结束并设 end_chat=true。"
            )
            grounding_rule = (
                "1. **严格接地（防幻觉）**：只能谈论【当前世界真实物件】与【本镇真实场所】里实际存在的物品和地点，以及此刻时间与双方关系经历；严禁虚构镇外不存在的地名！\n"
                if is_town_realm
                else "1. **严格接地（防幻觉）**：只能谈论【当前世界真实物件】里实际存在的东西、此刻时间与双方关系经历；**严禁虚构房间外不存在的店铺、餐馆、猪扒饭或外出逛街**！想吃东西或玩耍只能针对屋里真实存在的物件；\n"
            )
            brief_a = self._engine.get_actor_private_brief(initiator_id, space_id=sid) if hasattr(self._engine, "get_actor_private_brief") else ""
            brief_b = self._engine.get_actor_private_brief(target_actor_id, space_id=sid) if hasattr(self._engine, "get_actor_private_brief") else ""
            sys_prompt = (
                f"你是「{a_name}」与「{b_name}」的双人决策大脑（第 {turn_idx} 轮来回，只输出纯语义意图，口吻由各自嘴巴层负责）。\n"
                "原则：\n"
                + grounding_rule
                + "2. **跨次记忆（防车轱辘话）**：参考【两人此前刚说过的话】，禁止重复相同的邀约或开场白；\n"
                "3. 严格遵守【态度与情感指针】：若写着“尚不认识”，第1轮自然破冰；否则紧接话题自然聊下去；\n"
                "4. 若聊到准备去吃/玩/睡屋里某样真实物件并准备结束对话（end_chat=true），可在 follow_up_a / follow_up_b 填写该物件的 entity_id 与 action（consume|interact|rest）让角色真的去做；"
                "若形成了后续出行约定、互相告知了某场所功能/状态，或气氛变冷，可在可选字段 speech_act 中表达；"
                "shared_place_id 仅在两人明确同意稍后去同一个地点时填写。聊到正身处的场所或单方面邀约时留空。\n"
                "若约好一起走到本空间某件物品旁（坐垫、篝火等），填写对应的 shared_entity_id，"
                "shared_action 填 dwell（一起在旁边停留）或 rest（使用可坐物品，seated 写坐下的人 a 或 b）。约好后结束对话。\n"
                "夜间约好送人回住处休息时，分别填写两人的 goal_a / goal_b。送人的一方用 kind=visit、action=escort，"
                "回去休息的一方用 kind=visit、drive=rest，target 都填住处 ID。只有双方同意后才记录出行约定；"
                "尚未执行的行动不得写成已完成；\n"
                "5. 严格只输出一个合法 JSON 对象：\n"
                '{"intent_a":"发起方这轮想表达的具体语义意图","emotion_a":"开心|好奇|平静|害羞|温柔|兴奋|冷漠|困倦","intent_b":"回应方这轮想表达的具体语义意图","emotion_b":"...","keep_facts":["本轮涉及的真实物件名"],"end_chat":false,"ended_by":"a|b|","end_reason":"若结束写简短原因(<=16字)","follow_up_a":null,"follow_up_b":null,"speech_act":{"shared_place_id":"","shared_drive":"social","shared_entity_id":"","shared_action":"","seated":"","goal_a":null,"goal_b":null,"shared_beliefs":[],"tone":"neutral"},"memory_note":"一句话总结目前聊的内容(<=40字)"}'
            )
            user_prompt = (
                f"{l0_a['prompt_block']}\n"
                f"{l0_b['prompt_block']}\n"
                f"【可执行动作物件ID（用于 follow_up 和 shared_entity_id，绝不写进对话）】{action_id_ref}\n"
                + (f"【本镇真实场所（仅填 speech_act 时用 ID）】{realm_places_ref}\n" if is_town_realm else "")
                + f"【{a_name}底色】{setting_a}\n"
                + (f"【{a_name}心理状态卡】{card_a}\n" if card_a else "")
                + (f"【{a_name}私密背景与立场（仅{a_name}本人知晓，对话中只能影响{a_name}的言行且不可直接读出设定原文）】{brief_a}\n" if brief_a else "")
                + f"【{b_name}底色】{setting_b}\n"
                + (f"【{b_name}心理状态卡】{card_b}\n" if card_b else "")
                + (f"【{b_name}私密背景与立场（仅{b_name}本人知晓，对话中只能影响{b_name}的言行且不可直接读出设定原文）】{brief_b}\n" if brief_b else "")
                + f"【指针1】{_rel_desc(a_name, b_name, edge_ab)}\n"
                + f"【指针2】{_rel_desc(b_name, a_name, edge_ba)}\n"
                + f"【实时状态】{state_brief}\n"
                + memory_context
                + f"【两人此前刚说过的话（禁止重复）】\n{prior_social_block}\n"
                + f"【触发原因】{reason or '在屋里看到对方，想过来聊聊'}\n"
                + f"【对话进程】{pace_hint}\n"
                + f"【本轮已聊内容】\n{transcript_text}"
            )
            if generated_only:
                sys_prompt += (
                    "\n这是一次实时碰面。intent_a 和 intent_b 直接填两人各自会说出口的一句短台词。"
                    "结合当前处境与人物口吻，不填叙述、神态或心声；若约定去某地或交换了情报可附带可选 speech_act。"
                )
            parsed = text_compiler._ask_llm_json(
                sys_prompt,
                user_prompt,
                adapter_provider=self._adapter_provider,
                timeout_sec=12.0,
                max_tokens=460,
            )
            raw_ia = str((parsed or {}).get("intent_a") or (parsed or {}).get("line_a") or "").strip() if isinstance(parsed, dict) else ""
            raw_ib = str((parsed or {}).get("intent_b") or (parsed or {}).get("line_b") or "").strip() if isinstance(parsed, dict) else ""
            if not (raw_ia and raw_ib):
                break

            if generated_only:
                line_a = clip_text(_STAGE_DIR_RE.sub("", raw_ia).strip(), 72)
                line_b = clip_text(_STAGE_DIR_RE.sub("", raw_ib).strip(), 72)
                ea = clip_text(parsed.get("emotion_a"), 12, "平静")
                eb = clip_text(parsed.get("emotion_b"), 12, "平静")
            else:
                facts_list = parsed.get("keep_facts") if isinstance(parsed.get("keep_facts"), list) else []
                facts_str = "、".join(str(x).strip() for x in facts_list if str(x).strip()) or room_clean_nouns
                line_a = self._mouth_speak(
                    a_name, pctx_a, raw_ia, recent_a_lines, listener_name=b_name,
                    heard_line=turns[-1]["line_b"] if turns else "",
                    fallback_line=str(parsed.get("line_a") or raw_ia), setting_fallback=setting_a, world_facts=facts_str,
                )
                line_b = self._mouth_speak(
                    b_name, pctx_b, raw_ib, recent_b_lines, listener_name=a_name, heard_line=line_a,
                    fallback_line=str(parsed.get("line_b") or raw_ib), setting_fallback=setting_b, world_facts=facts_str,
                )
                ea = clip_text(parsed.get("emotion_a"), 16, "好奇" if first_meet and turn_idx == 1 else "开心")
                eb = clip_text(parsed.get("emotion_b"), 16, "平静" if first_meet and turn_idx == 1 else "微笑")
            if not (line_a and line_b):
                break

            turn_key = _social_turn_key(line_a, line_b)
            if _repeated_social_turn(turn_key, accepted_turn_keys):
                end_reason = "话题告一段落"
                repeated = True
                break
            next_turn = {"line_a": line_a, "emotion_a": ea, "line_b": line_b, "emotion_b": eb}
            shared = dict(collected_sa or {})
            if isinstance(parsed.get("speech_act"), dict) and parsed["speech_act"]:
                for k, v in parsed["speech_act"].items():
                    if k == "shared_beliefs" and isinstance(v, list):
                        if v:
                            shared[k] = list(shared.get(k) or []) + list(v)
                    elif v not in (None, "", [], {}):
                        shared[k] = v
            object_plan = {}
            if not _resolve_realm_place_id(shared.get("shared_place_id"), realm_places):
                proposed_entity = str(shared.get("shared_entity_id") or "")
                inferred = _accepted_object_outing([*turns[-1:], next_turn], shared_objects)
                if proposed_entity in shared_objects and not any(word in line_b for word in _INVITATION_DECLINES):
                    object_plan = {**inferred, **shared} if inferred.get("shared_entity_id") == proposed_entity else shared
                else:
                    for key in ("shared_entity_id", "shared_action", "seated"):
                        shared.pop(key, None)
                    object_plan = inferred
                if object_plan and object_plan.get("shared_entity_id") == recent_shared_entity:
                    end_reason = "刚约过这件事"
                    repeated = True
                    break
            collected_sa = {**shared, **object_plan}
            accepted_turn_keys.append(turn_key)
            used_llm = True
            turns.append(next_turn)
            recent_a_lines.append(line_a)
            recent_b_lines.append(line_b)
            if isinstance(parsed.get("follow_up_a"), dict):
                follow_up_a = parsed["follow_up_a"]
            if isinstance(parsed.get("follow_up_b"), dict):
                follow_up_b = parsed["follow_up_b"]
            if str(parsed.get("memory_note") or "").strip():
                mem_note = clip_text(parsed.get("memory_note"), 80, mem_note)
            if object_plan or bool(parsed.get("end_chat")) or is_last:
                ended_by = str(parsed.get("ended_by") or ("a" if turn_idx % 2 == 1 else "b")).strip().lower()
                if ended_by not in ("a", "b"):
                    ended_by = "a"
                end_reason = clip_text(parsed.get("end_reason"), 32, "约好后动身" if object_plan else "聊得差不多了")
                break

        if not turns and repeated:
            return {"turns": [], "turns_count": 0, "used_llm": False, "duplicate": True, "speech_act": None}
        if generated_only:
            current = [self._engine.live_actor_frame(aid) for aid in (initiator_id, target_actor_id)]
            now_a, now_b = current
            if not turns or not now_a or not now_b or now_a["space_id"] != sid or now_b["space_id"] != sid:
                return {"turns": [], "used_llm": False}
            if now_b["posture"] == "resting" or self._engine.chebyshev_distance(now_a, now_b) > 1:
                return {"turns": [], "used_llm": False}

        if not turns:
            stance_ab = str(edge_ab.get("attitude_stance") or "neutral")
            stance_ba = str(edge_ba.get("attitude_stance") or "neutral")
            vit_a = actor_a.get("vitals") or {}
            vit_b = actor_b.get("vitals") or {}
            tired = min(float(vit_a.get("energy", 0.72)), float(vit_b.get("energy", 0.72))) < 0.32 or max(float(vit_a.get("sleep_pressure", 0.30)), float(vit_b.get("sleep_pressure", 0.30))) > 0.72
            call_b = str(edge_ab.get("call_name") or b_name)
            call_a = str(edge_ba.get("call_name") or a_name)

            if first_meet:
                turns.append({
                    "line_a": f"那个……你好，我是{a_name}。刚看到你也在屋里，先过来打个招呼。",
                    "emotion_a": "好奇",
                    "line_b": (f"啊、你好，我是{b_name}……请多关照。" if stance_ba in ("shy", "guarded") else f"你好呀{a_name}，我是{b_name}。这儿还挺安静舒服的，很高兴认识你。"),
                    "emotion_b": ("害羞" if stance_ba in ("shy", "guarded") else "开心"),
                })
            elif stance_ab in ("guarded", "cold"):
                turns.append({
                    "line_a": f"{call_b}，你现在忙吗……我只是顺便过来看看情况。",
                    "emotion_a": "平静",
                    "line_b": f"还好，{call_a}。没什么急事的话就先这样吧。",
                    "emotion_b": "平静",
                })
            else:
                mem_hint = str(edge_ab.get("shared_memories") or "")
                topic = "刚才想到我们之前那件事，就想过来和你说两句。" if mem_hint else "看你一个人待在这边，过来陪你说会儿话。"
                turns.append({
                    "line_a": f"{call_b}，{topic}",
                    "emotion_a": "开心",
                    "line_b": f"嗯，{call_a}，我也刚好歇口气。有人一起说话确实热闹不少。",
                    "emotion_b": "温柔",
                })

            if max_turns >= 2 and not tired and stance_ab not in ("guarded", "cold"):
                turns.append({
                    "line_a": f"对了，你刚才在房间里忙什么呢？感觉这里还挺适合慢慢摸索的。",
                    "emotion_a": "好奇",
                    "line_b": f"就在附近转转看看，有你在旁边搭个伴也挺安心的。",
                    "emotion_b": "开心",
                })
            if max_turns >= 3 and not tired and stance_ab not in ("guarded", "cold"):
                turns.append({
                    "line_a": f"时间过得真快，那你先继续歇着，我等会儿再找你玩。",
                    "emotion_a": "温柔",
                    "line_b": f"好呀，{call_a}，待会见。",
                    "emotion_b": "开心",
                })
                ended_by = "a"
                end_reason = "聊完日常各自去忙"
            else:
                if tired:
                    turns[-1]["line_b"] += " 不过我有点犯困了，先歇会儿再聊吧。"
                    ended_by = "b"
                    end_reason = "有些困倦，提前结束"
                else:
                    ended_by = "b"
                    end_reason = "简单寒暄后结束"

            if not mem_note:
                mem_note = (
                    f"{a_name}和{b_name}在世界里初次打招呼并聊了 {len(turns)} 轮。"
                    if first_meet
                    else f"{a_name}和{b_name}聊了 {len(turns)} 轮（{end_reason}）。"
                )

        if not mem_note:
            mem_note = f"{a_name}与{b_name}聊了 {len(turns)} 轮。"

        line_a = turns[0]["line_a"]
        line_b = turns[0]["line_b"]
        emo_a = turns[-1]["emotion_a"]
        emo_b = turns[-1]["emotion_b"]

        stance_ok = str(edge_ab.get("attitude_stance") or "neutral") not in ("guarded", "cold")
        turn_scale = min(1.8, 0.8 + 0.25 * len(turns))
        rel_update = persona_bridge.record_world_social_interaction(
            self._project_root,
            self._engine.data_root,
            a_host,
            b_host,
            space_id=realm_sid,
            summary=mem_note,
            first_meet=first_meet,
            valence_delta=round((0.06 if (first_meet or stance_ok) else 0.02) * turn_scale, 3),
            trust_delta=round((0.04 if first_meet else 0.025) * turn_scale, 3),
            warmth_delta=round((0.05 if stance_ok else 0.02) * turn_scale, 3),
            call_a_to_b=b_name if first_meet else "",
            call_b_to_a=a_name if first_meet else "",
            custom_persona_dir=custom_pdir,
            isolated_persona=is_isolated,
        )

        # 长短对话同步轻微反馈到双方精力与心情
        mood_delta = 0.02 * len(turns) if stance_ok else -0.01
        for aid_x, act_x in ((initiator_id, actor_a), (target_actor_id, actor_b)):
            vx = act_x.get("vitals") or {}
            new_e = max(0.05, min(1.0, float(vx.get("energy", 0.72)) - 0.015 * len(turns)))
            new_m = max(-1.0, min(1.0, float(vx.get("mood_v", 0.10)) + mood_delta))
            try:
                self._engine.set_actor_world_mood(
                    aid_x, space_id=sid, vitals_patch={"energy": round(new_e, 3), "mood_v": round(new_m, 3)},
                    record_event=False,
                )
            except Exception:
                pass

        expr_a = self._engine.refresh_actor_expression(initiator_id, emotion_hint=emo_a, force_auto=True) or {}
        expr_b = self._engine.refresh_actor_expression(target_actor_id, emotion_hint=emo_b, force_auto=True) or {}

        escort_goals = _accepted_escort(
            turns, realm_places, realm_buildings, at_night=light_meta.get("period") == "night",
        )
        if escort_goals:
            collected_sa = {**(collected_sa or {}), **escort_goals}
        if (realm_places and not (collected_sa or {}).get("shared_entity_id")
                and not _resolve_realm_place_id((collected_sa or {}).get("shared_place_id"), realm_places)):
            pid, exchanged_lines = _accepted_outing(turns[-2:], realm_places)
            if pid:
                collected_sa = {**(collected_sa or {}), "shared_place_id": pid}
                food_words = ("热饮", "喝杯", "喝茶", "喝咖啡", "吃点东西", "吃东西", "拉面", "蛋糕", "点心", "甜品")
                collected_sa.setdefault("shared_drive", "food" if any(word in exchanged_lines for word in food_words) else "social")

        if generated_only:
            for side, follow_up, view, person in zip(
                ("a", "b"), (follow_up_a, follow_up_b), visible_by_actor, (actor_a, actor_b),
            ):
                if not isinstance(follow_up, dict) or (collected_sa or {}).get(f"goal_{side}"):
                    continue
                eid = str(follow_up.get("entity_id") or "").strip()
                action = str(follow_up.get("action") or "").strip()
                entity = view.get(eid)
                if entity and action in ("consume", "interact", "rest", "open") and action in dict(self._engine.legal_verbs(entity, person)):
                    drive = {"consume": "food", "rest": "rest", "open": "curiosity"}.get(action)
                    if drive is None:
                        drive = entity["traits"]["activity"]["domain"]
                    collected_sa = {**(collected_sa or {}), f"goal_{side}": {
                        "kind": "entity", "target": eid, "drive": drive, "action": action,
                    }}

        speech_act_pair = _build_speech_act_pair(
            collected_sa, realm_places=realm_places, valid_eids=valid_eids, summary=mem_note, shared_objects=shared_objects,
        )
        action_note = f"[角色社交{'·初次破冰' if first_meet else ''}·{len(turns)}轮] {a_name} 与 {b_name} 聊了起来：{mem_note}"
        joint_waiting = self._engine.record_social_feedback(
            initiator_id, sid, action_note, peer_id=target_actor_id, mood_delta=mood_delta,
            speech_act=speech_act_pair, decision_interval=getattr(settings, "heartbeat_interval_sec", 300) / 60,
        )
        with self._lock:
            self._append_msg_locked(initiator_id, "action", action_note, space_id=sid)
            self._append_msg_locked(target_actor_id, "action", action_note, space_id=sid)
            for t in turns:
                ea_t, eb_t = t["emotion_a"], t["emotion_b"]
                self._append_msg_locked(
                    initiator_id,
                    "assistant",
                    f"（对{b_name}说）{t['line_a']}",
                    emotion=ea_t,
                    sprite_index=int(expr_a.get("index") or 1),
                    sprite_tag=str(expr_a.get("tag") or ""),
                    space_id=sid,
                )
                self._append_msg_locked(
                    initiator_id,
                    "assistant",
                    f"[{b_name} 回应] {t['line_b']}",
                    emotion=eb_t,
                    sprite_index=int(expr_b.get("index") or 1),
                    sprite_tag=str(expr_b.get("tag") or ""),
                    space_id=sid,
                )
                self._append_msg_locked(
                    target_actor_id,
                    "assistant",
                    f"[{a_name} 对你说] {t['line_a']}",
                    emotion=ea_t,
                    sprite_index=int(expr_a.get("index") or 1),
                    sprite_tag=str(expr_a.get("tag") or ""),
                    space_id=sid,
                )
                self._append_msg_locked(
                    target_actor_id,
                    "assistant",
                    f"（对{a_name}说）{t['line_b']}",
                    emotion=eb_t,
                    sprite_index=int(expr_b.get("index") or 1),
                    sprite_tag=str(expr_b.get("tag") or ""),
                    space_id=sid,
                )

        follow_logs: list[str] = []
        for aid_x, nm_x, fu in ((initiator_id, a_name, follow_up_a), (target_actor_id, b_name, follow_up_b)):
            if generated_only or joint_waiting or not isinstance(fu, dict):
                continue
            feid = str(fu.get("entity_id") or "").strip()
            fact = str(fu.get("action") or "").strip()
            if feid in valid_eids and fact in ("consume", "interact", "rest", "open"):
                try:
                    msg_x = self._engine.execute_act(aid_x, feid, fact)
                    follow_logs.append(f"{nm_x}: {msg_x}")
                    with self._lock:
                        self._append_msg_locked(aid_x, "action", f"[言行联动] {msg_x}", space_id=sid)
                except Exception:
                    pass

        for aid_x in (initiator_id, target_actor_id):
            try:
                self._engine.update_actor_autonomy(aid_x, {"last_social_ts": round(time.time(), 2)})
            except Exception:
                pass

        self._engine.record_custom_event(
            "actor_social_talk",
            actor_id=initiator_id,
            space_id=actor_a["space_id"],
            payload={
                "initiator": a_name,
                "target": b_name,
                "first_meet": first_meet,
                "turns_count": len(turns),
                "ended_by": ended_by,
                "end_reason": end_reason,
                "summary": mem_note,
                "line_a": line_a,
                "line_b": line_b,
            },
        )
        self._engine.record_custom_event(
            "dialogue_exchange",
            actor_id=initiator_id,
            space_id=actor_a["space_id"],
            payload={
                "mode": "npc_social",
                "speaker_id": initiator_id,
                "speaker_name": a_name,
                "partner_id": target_actor_id,
                "partner_name": b_name,
                "turns_count": len(turns),
                "transcript": [
                    {"a": clip_text(t["line_a"], 160), "b": clip_text(t["line_b"], 160)}
                    for t in turns[:4]
                ],
                "summary": clip_text(mem_note, 180),
            },
        )

        return {
            "initiator_id": initiator_id,
            "target_actor_id": target_actor_id,
            "first_meet": first_meet,
            "turns": turns,
            "turns_count": len(turns),
            "max_turns_budget": max_turns,
            "ended_by": ended_by,
            "end_reason": end_reason,
            "line_a": line_a,
            "line_b": line_b,
            "emotion_a": emo_a,
            "emotion_b": emo_b,
            "summary": f"{a_name}「{line_a}」 / {b_name}「{line_b}」",
            "relations": rel_update,
            "speech_act": speech_act_pair,
            "used_llm": used_llm,
        }
