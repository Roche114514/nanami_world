"""文字世界内嵌对话引擎：把对话、世界动作（world_*）与心情立绘切换打通成同一条互动环。"""

from __future__ import annotations

import json
import logging
import os
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

    # ── 可选联动 nanami_persona 的 mind.mjs 与 soul.md ─────────────────────
    def _persona_context_for(self, host_character: str) -> tuple[Path | None, Path | None, str]:
        """只读探测 nanami_persona 的 mind.mjs、state_root 与 soul.md（零 Python import）。"""
        settings = self._settings_provider()
        custom = str(getattr(settings, "persona_data_dir", "") or "").strip()
        base_candidates: list[Path] = []
        if custom:
            base_candidates.append(Path(custom).expanduser())
        base_candidates.extend([self._project_root / "data" / "nanami", Path("data/nanami")])
        base_dir = next((p for p in base_candidates if p.is_dir()), None)

        mind_mjs_candidates = [
            self._project_root / "plugins" / "nanami_persona" / "toolkit" / "mind.mjs",
            Path("plugins/nanami_persona/toolkit/mind.mjs"),
        ]
        mind_mjs = next((p for p in mind_mjs_candidates if p.is_file()), None)

        state_root: Path | None = None
        soul_text = ""
        if base_dir is not None:
            state_root = persona_bridge._resolve_persona_state_root(base_dir, host_character)
            for cand in (
                state_root / "soul.md",
                self._project_root / "plugins" / "nanami_persona" / "soul" / "chiaki-soul.md",
            ):
                if cand.is_file():
                    try:
                        soul_text = cand.read_text(encoding="utf-8")[:1000]
                        break
                    except OSError:
                        pass
        return mind_mjs, state_root, soul_text

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
        mind_mjs, state_root, soul_text = self._persona_context_for(host_char)
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
        history = [
            m for m in self.get_history(actor_id, space_id=eff_sid)[:-1]
            if m.get("role") in ("user", "assistant")
        ][-8:]
        hist_lines = "\n".join(
            f"{'对方' if m['role'] == 'user' else actor['name']}：{m['text']}"
            for m in history
        )

        system_prompt = (
            f"你在文字世界里扮演「{actor['name']}」。请保持自然真人口语，严禁输出括号动作/舞台指示（如“（微笑）”），也不要使用客服腔。\n\n"
            f"【角色底色】\n{setting_text}\n\n"
            + (f"【心理模型状态】\n{mind_card_text}\n\n" if mind_card_text else "")
            + f"{world_view}\n\n"
            + "必须只输出一个合法 JSON 对象（不要 markdown 围栏）：\n"
            + "{\n"
            + '  "world_actions": [\n'
            + '    {"tool": "world_act", "entity_id": "视野内方括号ID", "action": "合法动词", "target_id": ""}\n'
            + "  ],\n"
            + '  "emotion": "从 平静/开心/兴奋/害羞/好奇/困倦/熟睡/担心/生气/难过/冷漠/认真 中选一个最贴合此刻心情的词",\n'
            + '  "reply": "你要说的一两句自然口语"\n'
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
        used_llm = isinstance(parsed, dict) and bool(str(parsed.get("reply") or "").strip())
        if not used_llm:
            parsed = self._offline_fallback(actor, clean_user)

        action_logs = self._execute_world_actions(actor_id, parsed.get("world_actions"))
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
        setting_a = self._character_setting_of(actor_a)[:480]
        setting_b = self._character_setting_of(actor_b)[:480]

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

        sys_prompt = (
            f"你在像素世界里同时撰写「{a_name}」主动找「{b_name}」交流的 1 轮自然简短对话。\n"
            "原则：\n"
            "1. 严格遵守【态度与情感指针】：若写着“尚不认识”，两人绝不可假装本来就熟，应自然打招呼破冰或询问对方名字；\n"
            "2. 若本来就相识，贴合彼此称呼、好感与共同经历来搭话；\n"
            "3. 口语自然简短，禁止括号动作描写与客服腔；\n"
            "4. 严格只输出一个合法 JSON 对象：\n"
            '{"line_a":"发起方说的一句话","emotion_a":"开心|好奇|平静|害羞|温柔|兴奋|冷漠","line_b":"回应方说的一句话","emotion_b":"...","memory_note":"一句话总结这次小互动(<=40字)"}'
        )
        user_prompt = (
            f"【{a_name}底色】{setting_a}\n"
            f"【{b_name}底色】{setting_b}\n"
            f"【指针1】{_rel_desc(a_name, b_name, edge_ab)}\n"
            f"【指针2】{_rel_desc(b_name, a_name, edge_ba)}\n"
            f"【触发原因】{reason or '在屋里看到对方，状态不错想聊两句'}\n"
            f"【房间现况】\n{view_a[:700]}"
        )
        parsed = text_compiler._ask_llm_json(
            sys_prompt,
            user_prompt,
            adapter_provider=self._adapter_provider,
            timeout_sec=12.0,
            max_tokens=360,
        )
        used_llm = isinstance(parsed, dict) and bool(str(parsed.get("line_a") or "").strip()) and bool(str(parsed.get("line_b") or "").strip())

        if used_llm:
            line_a = clip_text(_STAGE_DIR_RE.sub("", str(parsed.get("line_a") or "")).strip(), 140, "")
            line_b = clip_text(_STAGE_DIR_RE.sub("", str(parsed.get("line_b") or "")).strip(), 140, "")
            emo_a = clip_text(parsed.get("emotion_a"), 16, "好奇" if first_meet else "开心")
            emo_b = clip_text(parsed.get("emotion_b"), 16, "平静" if first_meet else "微笑")
            mem_note = clip_text(parsed.get("memory_note"), 80, "")
        else:
            stance_ab = str(edge_ab.get("attitude_stance") or "neutral")
            stance_ba = str(edge_ba.get("attitude_stance") or "neutral")
            if first_meet:
                line_a = f"那个……你好，我是{a_name}。刚看到你也在屋里，先过来打个招呼。"
                if stance_ba in ("shy", "guarded"):
                    line_b = f"啊、你好，我是{b_name}……请多关照。"
                    emo_b = "害羞"
                else:
                    line_b = f"你好呀{a_name}，我是{b_name}。这儿还挺安静舒服的，很高兴认识你。"
                    emo_b = "开心"
                emo_a = "好奇"
                mem_note = f"{a_name}和{b_name}在世界里初次打招呼认识了。"
            else:
                call_b = str(edge_ab.get("call_name") or b_name)
                call_a = str(edge_ba.get("call_name") or a_name)
                if stance_ab == "guarded":
                    line_a = f"{call_b}，你现在忙吗……我只是顺便过来看看情况。"
                    line_b = f"还好，{call_a}。有什么事直接说就行。"
                    emo_a, emo_b = "平静", "平静"
                else:
                    mem_hint = str(edge_ab.get("shared_memories") or "")
                    topic = "刚才想到我们之前那件事，就想过来和你说两句。" if mem_hint else "看你一个人待在这边，过来陪你说会儿话。"
                    line_a = f"{call_b}，{topic}"
                    line_b = f"嗯，{call_a}，我也刚好歇口气。待在一起确实热闹不少。"
                    emo_a, emo_b = "开心", "温柔"
                mem_note = f"{a_name}主动走近{b_name}聊了几句。"

        if not mem_note:
            mem_note = f"{a_name}与{b_name}交流了一句。"

        stance_ok = str(edge_ab.get("attitude_stance") or "neutral") not in ("guarded", "cold")
        rel_update = persona_bridge.record_world_social_interaction(
            self._project_root,
            self._engine.data_root,
            a_host,
            b_host,
            space_id=sid,
            summary=mem_note,
            first_meet=first_meet,
            valence_delta=0.08 if (first_meet or stance_ok) else 0.03,
            trust_delta=0.05 if first_meet else 0.03,
            warmth_delta=0.07 if stance_ok else 0.02,
            call_a_to_b=b_name if first_meet else "",
            call_b_to_a=a_name if first_meet else "",
            custom_persona_dir=custom_pdir,
        )

        expr_a = self._engine.refresh_actor_expression(initiator_id, emotion_hint=emo_a, force_auto=True) or {}
        expr_b = self._engine.refresh_actor_expression(target_actor_id, emotion_hint=emo_b, force_auto=True) or {}

        action_note = f"[角色社交{'·初次破冰' if first_meet else ''}] {a_name} 与 {b_name} 聊了起来：{mem_note}"
        with self._lock:
            self._append_msg_locked(initiator_id, "action", action_note, space_id=sid)
            self._append_msg_locked(
                initiator_id,
                "assistant",
                f"（对{b_name}说）{line_a}",
                emotion=emo_a,
                sprite_index=int(expr_a.get("index") or 1),
                sprite_tag=str(expr_a.get("tag") or ""),
                space_id=sid,
            )
            self._append_msg_locked(
                initiator_id,
                "assistant",
                f"[{b_name} 回应] {line_b}",
                emotion=emo_b,
                sprite_index=int(expr_b.get("index") or 1),
                sprite_tag=str(expr_b.get("tag") or ""),
                space_id=sid,
            )

            self._append_msg_locked(target_actor_id, "action", action_note, space_id=sid)
            self._append_msg_locked(
                target_actor_id,
                "assistant",
                f"[{a_name} 对你说] {line_a}",
                emotion=emo_a,
                sprite_index=int(expr_a.get("index") or 1),
                sprite_tag=str(expr_a.get("tag") or ""),
                space_id=sid,
            )
            self._append_msg_locked(
                target_actor_id,
                "assistant",
                f"（对{a_name}说）{line_b}",
                emotion=emo_b,
                sprite_index=int(expr_b.get("index") or 1),
                sprite_tag=str(expr_b.get("tag") or ""),
                space_id=sid,
            )

        self._engine.record_custom_event(
            "actor_social_talk",
            actor_id=initiator_id,
            space_id=actor_a["space_id"],
            payload={
                "initiator": a_name,
                "target": b_name,
                "first_meet": first_meet,
                "summary": mem_note,
                "line_a": line_a,
                "line_b": line_b,
            },
        )

        return {
            "initiator_id": initiator_id,
            "target_actor_id": target_actor_id,
            "first_meet": first_meet,
            "line_a": line_a,
            "line_b": line_b,
            "emotion_a": emo_a,
            "emotion_b": emo_b,
            "summary": f"{a_name}「{line_a}」 / {b_name}「{line_b}」",
            "relations": rel_update,
            "used_llm": used_llm,
        }
