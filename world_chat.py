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
from pathlib import Path
from typing import Any, Callable

from . import persona_bridge, projector, text_compiler
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
_CREATE_NO_WINDOW = 0x08000000


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
    ) -> None:
        self._engine = engine
        self._project_root = Path(project_root)
        self._settings_provider = settings_provider
        self._adapter_provider = adapter_provider or text_compiler._default_adapter
        self._history_path = Path(engine.data_root) / "chat_history.json"
        self._lock = threading.RLock()
        self._history: dict[str, list[dict[str, Any]]] = self._load_history()

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

    def _history_key_locked(self, actor_id: str, space_id: str) -> str:
        sid = str(space_id or "room_default").strip() or "room_default"
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
            "id": f"m_{int(time.time() * 1000)}_{len(items)}",
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

    def _persona_full_context_for(self, host_character: str, *, corpus_limit: int = 5) -> dict[str, Any]:
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
        if base_dir is not None:
            state_root, _pfx = persona_bridge._resolve_persona_pointer(base_dir, host_character)
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
            if str(host_character or "").strip() in ("", "七海千秋"):
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

        corpus_examples = self._sample_corpus_examples(profile_dir, base_dir, limit=corpus_limit)
        off_voice_words = self._read_off_voice_ban(profile_dir)
        return {
            "mind_mjs": mind_mjs,
            "guard_mjs": guard_mjs,
            "toolkit_dir": toolkit_dir,
            "state_root": state_root,
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
        host_char = str(actor.get("host_character") or actor.get("name") or "").strip()
        for row in self._engine.list_host_characters():
            if row.get("name") == host_char and row.get("setting"):
                return str(row["setting"])[:1000]
        return f"你是{actor['name']}，居住在这个像素文字世界里。"

    # ── 执行 LLM 返回的世界动作 ──────────────────────────────────────────
    def _execute_world_actions(self, actor_id: str, raw_actions: Any) -> list[str]:
        if not isinstance(raw_actions, list):
            return []
        logs: list[str] = []
        for item in raw_actions[:3]:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "world_act").strip().lower()
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

        eff_sid = str(space_id or actor.get("space_id") or "room_default").strip() or "room_default"
        with self._lock:
            self._append_msg_locked(actor_id, "user", clean_user, space_id=eff_sid)

        host_char = str(actor.get("host_character") or actor.get("name") or "").strip()
        pctx = self._persona_full_context_for(host_char, corpus_limit=5)
        mind_mjs, state_root, soul_text = pctx["mind_mjs"], pctx["state_root"], pctx["soul_text"]
        mind_data = self._run_mind_mjs(mind_mjs, state_root, actor_id, "--his", clean_user, space_id=eff_sid)
        mind_card_text = ""
        if isinstance(mind_data, dict):
            mind_card_text = str(mind_data.get("card") or "")
            raw_state = mind_data.get("state") if isinstance(mind_data.get("state"), dict) else mind_data
            att = raw_state.get("attitude") if isinstance(raw_state.get("attitude"), dict) else {}
            self._engine.set_actor_world_mood(
                actor_id,
                space_id=eff_sid,
                vitals_patch={
                    "energy": float(raw_state.get("energy", actor["vitals"].get("energy", 0.72))),
                    "sleep_pressure": float(raw_state.get("sleepPressure", actor["vitals"].get("sleep_pressure", 0.30))),
                    "mood_v": float(raw_state.get("moodV", actor["vitals"].get("mood_v", 0.15))),
                    "agenda": str(raw_state.get("agenda") or ""),
                    "disengaged": bool(att.get("disengaged", False)),
                    "preferred_domains": actor["vitals"].get("preferred_domains") or ["game"],
                    "source": "nanami_persona",
                },
            )
            actor = next(a for a in self._engine.list_actors() if a["actor_id"] == actor_id)

        world_view = projector.project_view(self._engine, actor_id, consume_diffs=True)
        setting_text = soul_text or self._character_setting_of(actor)
        settings_now = self._settings_provider() if self._settings_provider else None
        custom_pdir = str(getattr(settings_now, "persona_data_dir", "") or "")
        rels_now = persona_bridge.read_actor_relations(
            self._project_root,
            host_char,
            space_id=eff_sid,
            world_data_root=self._engine.data_root,
            custom_persona_dir=custom_pdir,
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
        l0_cfg = persona_bridge.read_world_perception_config(self._project_root, host_char, custom_persona_dir=custom_pdir)
        l0_frame = persona_bridge.update_and_project_world_perception(
            self._engine.data_root,
            eff_sid,
            actor,
            vis_ents,
            l0_cfg,
            advance_exposure=0.05 if l0_cfg.get("enabled") else 0.0,
        )
        _stage_labels = {"unfamiliar": "初入陌生期（谨慎打量中）", "exploring": "试探适应期", "familiar": "熟悉期", "settled": "归属稳态期"}
        l0_block = ""
        if l0_frame.get("enabled"):
            focus_names = "、".join(e["name"] for e in (l0_frame.get("focused_entities") or [])) or "无"
            l0_block = (
                f"【L0 环境感知与适应状态】阶段={_stage_labels.get(l0_frame['stage'], l0_frame['stage'])}"
                f"（熟悉度={l0_frame['familiarity']:.2f}）｜此刻注意力焦点={focus_names}"
                f"｜已记住物件数={l0_frame['mental_map_count']}\n\n"
            )

        history = [
            m for m in self.get_history(actor_id, space_id=eff_sid)[:-1]
            if m.get("role") in ("user", "assistant")
        ][-8:]
        recent_actor_replies = [m["text"] for m in history if m.get("role") == "assistant"][-6:]
        hist_lines = "\n".join(
            f"{'对方' if m['role'] == 'user' else actor['name']}：{m['text']}"
            for m in history
        )

        system_prompt = (
            f"你是「{actor['name']}」的决策大脑（负责结合世界现实与心理状态，想清楚这轮要做什么动作与表达什么语义意图，不负责雕琢口吻）。\n\n"
            f"【角色底色】\n{setting_text}\n\n"
            + f"【你与用户的关系指针】{u_rel_line}\n\n"
            + l0_block
            + (f"【心理模型状态】\n{mind_card_text}\n\n" if mind_card_text else "")
            + f"{world_view}\n\n"
            + "必须只输出一个合法 JSON 对象（不要 markdown 围栏）：\n"
            + "{\n"
            + '  "world_actions": [\n'
            + '    {"tool": "world_act", "entity_id": "视野内方括号ID", "action": "合法动词", "target_id": ""}\n'
            + "  ],\n"
            + '  "emotion": "从 平静/开心/兴奋/害羞/好奇/困倦/熟睡/担心/生气/难过/冷漠/认真 中选一个最贴合此刻心情的词",\n'
            + '  "intent": "用一句大白话说清这轮要传达的具体态度或回应内容"\n'
            + "}\n"
            + "约束：\n"
            + "1. world_actions 只在真正想触碰/移动/开关/写字时填写（0-2条），不动作就留空数组 []；严禁编造【世界现实】之外的物体 ID；\n"
            + "2. 支持的 tool：world_act(entity_id, action, target_id)、world_move(x, y)、world_write(entity_id, text)、world_combine(source_id, target_id)。"
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
            parsed = self._offline_fallback(actor, clean_user)

        action_logs = self._execute_world_actions(actor_id, parsed.get("world_actions"))
        if used_llm:
            vis_names = "、".join(e["name"] for e in self._engine.visible_entities(actor_id)[:20])
            reply = self._mouth_speak(
                actor["name"],
                pctx,
                brain_intent,
                recent_actor_replies,
                heard_line=clean_user,
                fallback_line=str(parsed.get("reply") or brain_intent),
                setting_fallback=setting_text,
                world_facts=vis_names or "空房间",
            )
        else:
            reply_raw = str(parsed.get("reply") or "").strip()
            reply = clip_text(_STAGE_DIR_RE.sub("", reply_raw).strip(), 180, "……嗯。")
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

        if mind_mjs is not None and state_root is not None:
            self._run_mind_mjs(mind_mjs, state_root, actor_id, "--her", reply, space_id=eff_sid)

        return {
            "reply": reply,
            "emotion": emotion or str(expr.get("tag") or ""),
            "actions_executed": action_logs,
            "expression": expr,
            "used_llm": used_llm,
            "history": self.get_history(actor_id, space_id=eff_sid),
        }

    def conduct_actor_social_exchange(
        self,
        initiator_id: str,
        target_actor_id: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        """由角色主动发起的同屋双人社交互动：依据当下状态与态度情感指针生成自然来回并双向更新关系。"""
        actors = self._engine.list_actors()
        actor_a = next((a for a in actors if a["actor_id"] == initiator_id), None)
        actor_b = next((a for a in actors if a["actor_id"] == target_actor_id), None)
        if actor_a is None or actor_b is None:
            raise ValueError("发起社交的角色或目标角色不存在。")
        if actor_a["space_id"] != actor_b["space_id"]:
            raise ValueError("两位角色当前不在同一个世界空间内，无法当面交流。")

        sid = str(actor_a.get("space_id") or "room_default").strip() or "room_default"
        a_name = str(actor_a.get("name") or initiator_id).strip()
        b_name = str(actor_b.get("name") or target_actor_id).strip()
        a_host = str(actor_a.get("host_character") or a_name).strip()
        b_host = str(actor_b.get("host_character") or b_name).strip()

        settings = self._settings_provider() if self._settings_provider else None
        custom_pdir = str(getattr(settings, "persona_data_dir", "") or "")
        rels_a = persona_bridge.read_actor_relations(
            self._project_root,
            a_host,
            space_id=sid,
            world_data_root=self._engine.data_root,
            custom_persona_dir=custom_pdir,
            known_actors=[b_host, b_name],
        )
        rels_b = persona_bridge.read_actor_relations(
            self._project_root,
            b_host,
            space_id=sid,
            world_data_root=self._engine.data_root,
            custom_persona_dir=custom_pdir,
            known_actors=[a_host, a_name],
        )
        edge_ab = rels_a.get(f"char:{b_host}") or rels_a.get(f"char:{b_name}") or {}
        edge_ba = rels_b.get(f"char:{a_host}") or rels_b.get(f"char:{a_name}") or {}
        first_meet = not bool(edge_ab.get("known", False))

        view_a = projector.project_view(self._engine, initiator_id, consume_diffs=False)
        sp_snap = self._engine.snapshot_for_ui(sid)
        sp_meta = sp_snap.get("space") or {}
        light_meta = sp_snap.get("lighting") or {}
        vis_a = self._engine.visible_entities(initiator_id)
        room_items_desc = "、".join(
            f"[{e['entity_id']}]{e['name']}"
            + (f"({','.join(self._engine.entity_state_tags(e))})" if self._engine.entity_state_tags(e) else "")
            for e in vis_a[:24]
        ) or "（房间里暂时没有摆放物品）"
        room_facts = f"空间「{sp_meta.get('name') or sid}」｜时间 {int(light_meta.get('hour', 12)):02d}:{int(light_meta.get('minute', 0)):02d}｜真实物件：{room_items_desc}"

        pctx_a = self._persona_full_context_for(a_host, corpus_limit=7)
        pctx_b = self._persona_full_context_for(b_host, corpus_limit=7)
        mind_a = self._run_mind_mjs(pctx_a.get("mind_mjs"), pctx_a.get("state_root"), initiator_id, "--his", reason or "看到同伴想聊几句", space_id=sid) or {}
        mind_b = self._run_mind_mjs(pctx_b.get("mind_mjs"), pctx_b.get("state_root"), target_actor_id, "--his", reason or "同伴走近搭话", space_id=sid) or {}
        card_a = str(mind_a.get("card") or "")[:360]
        card_b = str(mind_b.get("card") or "")[:360]
        room_clean_nouns = "、".join(e["name"] for e in vis_a[:20]) or "空房间"
        setting_a = (pctx_a.get("soul_text") or self._character_setting_of(actor_a))[:650]
        setting_b = (pctx_b.get("soul_text") or self._character_setting_of(actor_b))[:650]
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
            elif min(e_a, e_b) < 0.34 or max(s_a, s_b) > 0.70 or max(h_a, h_b) > 0.78:
                budget = min(budget - 2, 2)
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
                if h > 0.72:
                    tags.append("有点饿")
                if m >= 0.30:
                    tags.append("心情很好")
                elif m <= -0.25:
                    tags.append("情绪低落")
                return f"{nm}(精力={e:.2f}, 困意={s:.2f}, 心情={m:+.2f}, {'/'.join(tags)})"

            brief = _actor_state_line(a_name, e_a, s_a, m_a, h_a) + "；" + _actor_state_line(b_name, e_b, s_b, m_b, h_b)
            return budget, brief

        max_turns, state_brief = _compute_chat_budget()
        turns: list[dict[str, str]] = []
        mem_note = ""
        ended_by = ""
        end_reason = ""
        used_llm = False

        def _recent_spoken_from_hist(aid_x: str) -> list[str]:
            out_lines: list[str] = []
            for m in self.get_history(aid_x, space_id=sid)[-16:]:
                if m.get("role") != "assistant":
                    continue
                txt = str(m.get("text") or "").strip()
                txt = re.sub(r"^(?:[（(]对[^）)]+说[）)]|\[[^\]]+\])\s*", "", txt).strip()
                if txt:
                    out_lines.append(txt)
            return out_lines[-6:]

        recent_a_lines = _recent_spoken_from_hist(initiator_id)
        recent_b_lines = _recent_spoken_from_hist(target_actor_id)
        prior_social_block = "\n".join(
            f"- {line}" for line in (recent_a_lines[-2:] + recent_b_lines[-2:])
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
            sys_prompt = (
                f"你是「{a_name}」与「{b_name}」的双人决策大脑（第 {turn_idx} 轮来回，只输出纯语义意图，口吻由各自嘴巴层负责）。\n"
                "原则：\n"
                "1. **严格接地（防幻觉）**：只能谈论【当前世界真实物件】里实际存在的东西、此刻时间与双方关系经历；**严禁虚构房间外不存在的店铺、餐馆、猪扒饭或外出逛街**！想吃东西或玩耍只能针对屋里真实存在的物件；\n"
                "2. **跨次记忆（防车轱辘话）**：参考【两人此前刚说过的话】，禁止重复相同的邀约或开场白；\n"
                "3. 严格遵守【态度与情感指针】：若写着“尚不认识”，第1轮自然破冰；否则紧接话题自然聊下去；\n"
                "4. 若聊到准备去吃/玩/睡屋里某样真实物件并准备结束对话（end_chat=true），可在 follow_up_a / follow_up_b 填写该物件的 entity_id 与 action（consume|interact|rest）让角色真的去做；\n"
                "5. 严格只输出一个合法 JSON 对象：\n"
                '{"intent_a":"发起方这轮想表达的具体语义意图","emotion_a":"开心|好奇|平静|害羞|温柔|兴奋|冷漠|困倦","intent_b":"回应方这轮想表达的具体语义意图","emotion_b":"...","keep_facts":["本轮涉及的真实物件名"],"end_chat":false,"ended_by":"a|b|","end_reason":"若结束写简短原因(<=16字)","follow_up_a":null,"follow_up_b":null,"memory_note":"一句话总结目前聊的内容(<=40字)"}'
            )
            user_prompt = (
                f"【当前世界真实物件】{room_facts}\n"
                f"【{a_name}底色】{setting_a}\n"
                + (f"【{a_name}心理状态卡】{card_a}\n" if card_a else "")
                + f"【{b_name}底色】{setting_b}\n"
                + (f"【{b_name}心理状态卡】{card_b}\n" if card_b else "")
                + f"【指针1】{_rel_desc(a_name, b_name, edge_ab)}\n"
                + f"【指针2】{_rel_desc(b_name, a_name, edge_ba)}\n"
                + f"【实时状态】{state_brief}\n"
                + f"【两人此前刚说过的话（禁止重复）】\n{prior_social_block}\n"
                + f"【触发原因】{reason or '在屋里看到对方，想过来聊聊'}\n"
                + f"【对话进程】{pace_hint}\n"
                + f"【本轮已聊内容】\n{transcript_text}\n"
                + f"【房间详情】\n{view_a[:750]}"
            )
            parsed = text_compiler._ask_llm_json(
                sys_prompt,
                user_prompt,
                adapter_provider=self._adapter_provider,
                timeout_sec=12.0,
                max_tokens=420,
            )
            raw_ia = str((parsed or {}).get("intent_a") or (parsed or {}).get("line_a") or "").strip() if isinstance(parsed, dict) else ""
            raw_ib = str((parsed or {}).get("intent_b") or (parsed or {}).get("line_b") or "").strip() if isinstance(parsed, dict) else ""
            if not (raw_ia and raw_ib):
                break

            used_llm = True
            facts_list = parsed.get("keep_facts") if isinstance(parsed.get("keep_facts"), list) else []
            facts_str = "、".join(str(x).strip() for x in facts_list if str(x).strip()) or room_clean_nouns
            prev_b = turns[-1]["line_b"] if turns else ""
            la = self._mouth_speak(
                a_name,
                pctx_a,
                raw_ia,
                recent_a_lines,
                listener_name=b_name,
                heard_line=prev_b,
                fallback_line=str(parsed.get("line_a") or raw_ia),
                setting_fallback=setting_a,
                world_facts=facts_str,
            )
            lb = self._mouth_speak(
                b_name,
                pctx_b,
                raw_ib,
                recent_b_lines,
                listener_name=a_name,
                heard_line=la,
                fallback_line=str(parsed.get("line_b") or raw_ib),
                setting_fallback=setting_b,
                world_facts=facts_str,
            )
            recent_a_lines.append(la)
            recent_b_lines.append(lb)
            if isinstance(parsed.get("follow_up_a"), dict):
                follow_up_a = parsed["follow_up_a"]
            if isinstance(parsed.get("follow_up_b"), dict):
                follow_up_b = parsed["follow_up_b"]
            ea = clip_text(parsed.get("emotion_a"), 16, "好奇" if (first_meet and turn_idx == 1) else "开心")
            eb = clip_text(parsed.get("emotion_b"), 16, "平静" if (first_meet and turn_idx == 1) else "微笑")
            turns.append({"line_a": la, "emotion_a": ea, "line_b": lb, "emotion_b": eb})
            if str(parsed.get("memory_note") or "").strip():
                mem_note = clip_text(parsed.get("memory_note"), 80, mem_note)
            if bool(parsed.get("end_chat")) or is_last:
                ended_by = str(parsed.get("ended_by") or ("a" if turn_idx % 2 == 1 else "b")).strip().lower()
                if ended_by not in ("a", "b"):
                    ended_by = "a"
                end_reason = clip_text(parsed.get("end_reason"), 32, "聊得差不多了")
                break

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
            space_id=sid,
            summary=mem_note,
            first_meet=first_meet,
            valence_delta=round((0.06 if (first_meet or stance_ok) else 0.02) * turn_scale, 3),
            trust_delta=round((0.04 if first_meet else 0.025) * turn_scale, 3),
            warmth_delta=round((0.05 if stance_ok else 0.02) * turn_scale, 3),
            call_a_to_b=b_name if first_meet else "",
            call_b_to_a=a_name if first_meet else "",
            custom_persona_dir=custom_pdir,
        )

        # 长短对话同步轻微反馈到双方精力与心情
        for aid_x, act_x in ((initiator_id, actor_a), (target_actor_id, actor_b)):
            vx = act_x.get("vitals") or {}
            new_e = max(0.05, min(1.0, float(vx.get("energy", 0.72)) - 0.015 * len(turns)))
            new_m = max(-1.0, min(1.0, float(vx.get("mood_v", 0.10)) + (0.02 * len(turns) if stance_ok else -0.01)))
            try:
                self._engine.set_actor_vitals(aid_x, {"energy": round(new_e, 3), "mood_v": round(new_m, 3)})
            except Exception:
                pass

        expr_a = self._engine.refresh_actor_expression(initiator_id, emotion_hint=emo_a, force_auto=True) or {}
        expr_b = self._engine.refresh_actor_expression(target_actor_id, emotion_hint=emo_b, force_auto=True) or {}

        action_note = f"[角色社交{'·初次破冰' if first_meet else ''}·{len(turns)}轮] {a_name} 与 {b_name} 聊了起来：{mem_note}"
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
        valid_eids = {e["entity_id"] for e in vis_a}
        for aid_x, nm_x, fu in ((initiator_id, a_name, follow_up_a), (target_actor_id, b_name, follow_up_b)):
            if not isinstance(fu, dict):
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
            "used_llm": used_llm,
        }
