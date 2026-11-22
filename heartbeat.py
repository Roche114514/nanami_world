"""两级离线与自主生活心跳：L1 物理时钟推进 + L2 十类内心驱力与可选 LLM 自由意志 + 桥接同步。"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import persona_bridge, projector, schema, text_compiler
from .cognitive_engine import CognitiveEngine
from .live_simulation import LiveSimulation
from .schema import clip_text, normalize_autonomy
from .settings import WorldSettings

logger = logging.getLogger(__name__)


class WorldHeartbeat:
    """后台守护心跳与自主欲望驱动步进器（支持纯规则与 LLM 自由意志）。"""

    def __init__(
        self,
        engine: Any,
        settings_provider: Callable[[], WorldSettings],
        *,
        project_root: Path,
        adapter_provider: Callable[[], Any] | None = None,
        chat_service_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._engine = engine
        self._settings_provider = settings_provider
        self._project_root = Path(project_root)
        self._adapter_provider = adapter_provider
        self._chat_service_provider = chat_service_provider
        self._cognitive = CognitiveEngine(engine, self._project_root, adapter_provider=adapter_provider)
        self.live = LiveSimulation(
            engine, settings_provider, self._cognitive, self._cognitive_talk, adapter_provider,
            self.deliver_persona_events, self._respond_to_key_loan, chat_service_provider,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_diary_ts: dict[str, float] = {}
        self._last_persona_ts: dict[str, float] = {}
        self._step_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="nanami-world-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.live.stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            settings = self._settings_provider()
            interval = max(30, int(settings.heartbeat_interval_sec))
            if self._stop_event.wait(interval):
                break
            if not settings.enabled or not settings.heartbeat_enabled:
                continue
            try:
                self.step()
            except Exception:  # noqa: BLE001
                logger.debug("nanami_world: 心跳步进异常（忽略）", exc_info=True)

    def catch_up(self) -> dict[str, Any]:
        """启动或会话初始化时按离线时间差执行一次 L1/L2 补算。"""
        return self.step()

    def step(
        self,
        *,
        advance_minutes: float | None = None,
        force_autonomy: bool = False,
        use_llm: bool | None = None,
        actor_id: str = "",
    ) -> dict[str, Any]:
        with self._step_lock:
            settings = self._settings_provider()
            self._sync_persona_vitals(settings)
            tick_res = self._engine.tick_physics(advance_minutes=advance_minutes)
            self._deliver_persona_events(settings)
            should_act = bool(force_autonomy or advance_minutes is None or float(advance_minutes) > 0.0)
            if should_act:
                actions_taken, decisions = self._run_autonomous_l2(
                    settings,
                    force_autonomy=force_autonomy,
                    use_llm=use_llm,
                    target_actor_id=actor_id,
                )
            else:
                actions_taken, decisions = [], []
            self._deliver_persona_events(settings)
            self._export_bridges()
            return {
                "tick": tick_res,
                "autonomous_actions": actions_taken,
                "decisions": decisions,
            }

    def _sync_persona_vitals(self, settings: WorldSettings) -> None:
        for act in self._engine.list_actors():
            if act.get("is_player"):
                continue
            aid = act["actor_id"]
            sid = str(act.get("space_id") or "room_default")
            realm_sid = self._engine.realm_space_id(sid) if hasattr(self._engine, "realm_space_id") else sid
            host_name = str(act.get("host_character") or act.get("name") or "").strip()
            synced = persona_bridge.read_persona_state(
                self._project_root,
                host_name,
                custom_persona_dir=settings.persona_data_dir,
                space_id=realm_sid,
                actor_id=aid,
            )
            if synced is None:
                continue

            cur_vit = dict(act.get("vitals") or {})
            remote_ts = float(synced.get("updated_at") or 0.0)
            ts_key = f"{sid}::{aid}"
            prev_ts = self._last_persona_ts.get(ts_key, -1.0)
            patch = {"preferred_domains": synced["preferred_domains"]}
            if remote_ts > 0 and cur_vit.get("source") == "builtin":
                patch.update({field: synced[field] for field in ("energy", "sleep_pressure", "mood_v", "agenda", "disengaged")})
                patch["source"] = "nanami_persona"
            if synced["world_state"] and remote_ts > prev_ts:
                patch.update({field: synced[field] for field in ("mood_v", "agenda", "disengaged")})
            if any(cur_vit.get(key) != value for key, value in patch.items()):
                self._engine.set_actor_world_mood(aid, space_id=sid, vitals_patch=patch, record_event=False)
            if synced["world_state"]:
                self._last_persona_ts[ts_key] = max(prev_ts, remote_ts)

    def deliver_persona_events(self) -> None:
        with self._step_lock:
            self._deliver_persona_events(self._settings_provider())

    def _deliver_persona_events(self, settings: WorldSettings) -> None:
        actors = {actor["actor_id"]: actor for actor in self._engine.list_actors()}
        retry = set()
        attempts = 0
        for event in self._engine.pending_persona_events():
            payload = {**event["payload"], "seq": event["seq"], "at": round(event["ts"] * 1000)}
            for aid in event["pending"]:
                key = (event["space_id"], aid)
                if key in retry:
                    continue
                if attempts >= 12:
                    return
                actor = actors.get(aid)
                result = {"status": "unbound"}
                if actor:
                    attempts += 1
                    ev_realm_sid = self._engine.realm_space_id(event["space_id"]) if hasattr(self._engine, "realm_space_id") else event["space_id"]
                    result = persona_bridge.emit_world_event_to_persona(
                        self._project_root, str(actor.get("host_character") or actor["name"]),
                        space_id=ev_realm_sid, actor_id=aid, event_payload=payload,
                        custom_persona_dir=settings.persona_data_dir,
                    ) or {}
                mood = result.get("moodV")
                if result.get("ok") and isinstance(mood, (int, float)):
                    self._engine.acknowledge_persona_event(payload["event_id"], aid, mood, result.get("appraisal"))
                elif result.get("status") == "unbound":
                    self._engine.acknowledge_persona_event(payload["event_id"], aid, None)
                else:
                    retry.add(key)

    # ── 自主决策执行管线 ─────────────────────────────────────────────────
    def _run_autonomous_l2(
        self,
        settings: WorldSettings,
        *,
        force_autonomy: bool = False,
        use_llm: bool | None = None,
        target_actor_id: str = "",
    ) -> tuple[list[str], list[dict[str, Any]]]:
        taken: list[str] = []
        decisions: list[dict[str, Any]] = []
        eff_llm = settings.heartbeat_use_llm if use_llm is None else bool(use_llm)

        for act in self._engine.list_actors():
            if act.get("is_player"):
                continue
            aid = act["actor_id"]
            if target_actor_id and aid != target_actor_id:
                continue
            if not force_autonomy and self.live.managed(aid):
                continue
            aut = normalize_autonomy(act.get("autonomy"))
            if not aut.get("enabled", True) and not force_autonomy:
                continue

            loan_decision = self._respond_to_key_loan(act, settings)
            if loan_decision:
                decisions.append(loan_decision)
                taken.extend(f"{aid}: {line}" for line in loan_decision["actions"])
                continue

            scene = self._engine.cognitive_scene(aid)
            if scene is not None:
                decision = self._cognitive.step(scene, settings, use_llm=eff_llm, talk=self._cognitive_talk)
                decisions.append(decision)
                taken.extend(f"{aid}: {line}" for line in decision["actions"])
                self._log_autonomous_activity(
                    aid, scene["space"]["space_id"], "；".join(decision["actions"]), decision,
                )
                continue

            sp_snap = self._engine.snapshot_for_ui(act["space_id"])
            vis = self._engine.visible_entities(aid)

            plan: dict[str, Any] | None = None
            if eff_llm:
                plan = self._decide_with_llm(act, vis, sp_snap)
            if plan is None:
                plan = self._decide_with_rules(act, vis, sp_snap, settings, force_autonomy=force_autonomy)
            if plan is None:
                continue

            exec_logs = self._execute_plan_actions(aid, plan.get("actions") or [])
            summary = "；".join(exec_logs) if exec_logs else str(plan.get("passive_summary") or "")
            if not exec_logs and plan.get("drive") == "calm":
                self._engine.record_social_feedback(aid, act["space_id"], "独自待了一会儿")
            target_id = str(plan.get("target_id") or "").strip()

            recent = list(aut.get("recent_targets") or [])
            if target_id:
                recent = [target_id] + [r for r in recent if r != target_id]
                recent = recent[:5]

            next_aut = self._engine.update_actor_autonomy(
                aid,
                {
                    "current_goal": plan.get("goal") or aut["current_goal"],
                    "current_drive": plan.get("drive") or aut["current_drive"],
                    "last_thought": plan.get("thought") or "",
                    "last_action_summary": summary,
                    "last_step_ts": round(time.time(), 3),
                    "recent_targets": recent,
                },
                emotion_hint=str(plan.get("emotion") or ""),
            )

            if exec_logs:
                for log_line in exec_logs:
                    taken.append(f"{aid}: {log_line}")
                self._maybe_write_diary(aid, act["name"], summary, vis, settings)

            if not plan.get("skip_auto_chat_log") and (exec_logs or plan.get("record_thought_in_chat")):
                self._log_autonomous_activity(aid, act["space_id"], summary, plan)

            decisions.append({
                "actor_id": aid,
                "goal": next_aut["current_goal"],
                "drive": next_aut["current_drive"],
                "thought": next_aut["last_thought"],
                "emotion": str(plan.get("emotion") or ""),
                "actions": exec_logs,
            })

        return taken, decisions

    def _log_autonomous_activity(self, actor_id: str, space_id: str, summary: str, plan: dict[str, Any]) -> None:
        if not summary and not plan.get("thought"):
            return
        chat = self._chat_service_provider() if self._chat_service_provider else None
        if chat is not None:
            try:
                chat.append_autonomous_activity(
                    actor_id, action_summary=summary, thought=str(plan.get("thought") or ""),
                    emotion=str(plan.get("emotion") or ""), space_id=space_id,
                )
            except Exception:
                logger.debug("nanami_world: 同步自主行动到世界对话历史失败", exc_info=True)

    def _cognitive_talk(self, actor_id: str, peer_id: str, reason: str) -> list[str]:
        return self._execute_plan_actions(
            actor_id, [{"tool": "world_talk", "target_actor_id": peer_id, "reason": reason}],
            local_social=True,
        )

    def respond_to_key_loan(self, actor_id: str) -> dict[str, Any] | None:
        with self._step_lock:
            actor = next((actor for actor in self._engine.list_actors() if actor["actor_id"] == actor_id), None)
            return self._respond_to_key_loan(actor, self._settings_provider()) if actor else None

    def _respond_to_key_loan(self, actor: dict[str, Any], settings: WorldSettings) -> dict[str, Any] | None:
        aid = actor["actor_id"]
        pending = self._engine.pending_key_loan_response(aid)
        if pending is None:
            return None
        profile = persona_bridge.read_persona_behavior_profile(
            self._project_root, str(actor.get("host_character") or actor["name"]),
            custom_persona_dir=settings.persona_data_dir,
        )
        evidence = pending["evidence"]
        vitals = actor["vitals"]
        if vitals["disengaged"] or vitals["energy"] < 0.35 or vitals["sleep_pressure"] >= 0.68 or actor["posture"] == "resting":
            choice = "wait"
        elif evidence == "extension_received":
            choice = "accept" if profile["norm_courtesy"] >= 0.55 else "ask"
        else:
            choice = "ask" if profile["social_initiative"] >= 0.25 else "wait"
        if not self._engine.respond_to_key_loan(aid, pending["loan_id"], evidence, choice):
            return None
        item = pending["item_name"]
        message = {
            "wait": f"暂且等一等「{item}」的消息。",
            "ask": f"准备问问「{item}」什么时候能归还。",
            "accept": f"收到了延期请求，同意晚些归还「{item}」。",
        }[choice]
        return {
            "actor_id": aid, "goal": message, "drive": "social", "thought": "",
            "emotion": "平静", "actions": [message],
            "loan_response": {**pending, "choice": choice},
        }

    def invite_to_puzzle(self, actor_id: str, peer_id: str, entity_id: str) -> str:
        peers = {person["actor_id"]: person for person in self._engine.list_actors()}
        peer = peers.get(peer_id)
        if peer is None:
            return "这位同伴现在不在这里。"
        profile = persona_bridge.read_persona_behavior_profile(
            self._project_root, str(peer.get("host_character") or peer["name"]),
            custom_persona_dir=self._settings_provider().persona_data_dir,
        )
        return self._engine.invite_to_puzzle(
            actor_id, peer_id, entity_id, willing=profile["norm_courtesy"] >= 0.35,
        )

    def _execute_plan_actions(
        self, actor_id: str, raw_actions: list[dict[str, Any]], *, local_social: bool = False,
    ) -> list[str]:
        logs: list[str] = []
        for item in raw_actions[:3]:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "world_act").strip().lower()
            if tool == "world_invite_puzzle":
                logs.append(self.invite_to_puzzle(
                    actor_id, str(item.get("target_actor_id") or ""), str(item.get("entity_id") or ""),
                ))
            elif tool == "world_move":
                try:
                    tx = int(item.get("x"))
                    ty = int(item.get("y"))
                except (TypeError, ValueError):
                    continue
                res = self._engine.execute_move(actor_id, tx, ty)
                logs.append(res)
            elif tool == "world_talk":
                tgt_aid = str(item.get("target_actor_id") or "").strip()
                reason = str(item.get("reason") or "").strip()
                if not tgt_aid or tgt_aid == actor_id:
                    continue
                actors_now = self._engine.list_actors()
                me = next((a for a in actors_now if a["actor_id"] == actor_id), None)
                peer = next((a for a in actors_now if a["actor_id"] == tgt_aid), None)
                if not me or me.get("is_player") or not peer or me["space_id"] != peer["space_id"]:
                    continue
                if peer.get("is_player"):
                    chat_svc = self._chat_service_provider() if self._chat_service_provider else None
                    if chat_svc is not None:
                        greeting = chat_svc.greet_player(actor_id, tgt_aid, reason=reason,
                                                         use_llm=self._settings_provider().heartbeat_use_llm)
                        if greeting:
                            self.live.hold_for_user_chat(actor_id, greeting["wait_seconds"])
                            logs.append(greeting["summary"])
                    continue
                if max(abs(int(me["x"]) - int(peer["x"])), abs(int(me["y"]) - int(peer["y"]))) > 1:
                    if local_social:
                        continue
                    adj = self._find_adjacent_walkable(me, peer)
                    if adj is not None:
                        self._engine.execute_move(actor_id, adj[0], adj[1])
                chat_svc = self._chat_service_provider() if self._chat_service_provider and not local_social else None
                if chat_svc is not None and hasattr(chat_svc, "conduct_actor_social_exchange"):
                    ex = chat_svc.conduct_actor_social_exchange(actor_id, tgt_aid, reason=reason)
                    logs.append(str(ex.get("summary") or f"{me['name']} 和 {peer['name']} 聊了几句"))
                else:
                    a_host = str(me.get("host_character") or me.get("name") or "").strip()
                    b_host = str(peer.get("host_character") or peer.get("name") or "").strip()
                    me_sid = str(me.get("space_id") or "room_default")
                    me_realm = self._engine.realm_space_id(me_sid) if hasattr(self._engine, "realm_space_id") else me_sid
                    settings = self._settings_provider()
                    rels_a = persona_bridge.read_actor_relations(
                        self._project_root,
                        a_host,
                        space_id=me_realm,
                        world_data_root=self._engine.data_root,
                        custom_persona_dir=settings.persona_data_dir,
                        known_actors=[b_host],
                    )
                    edge_ab = rels_a.get(f"char:{b_host}") or {}
                    first_meet = not bool(edge_ab.get("known"))
                    note = (f"{me['name']}走近了{peer['name']}。"
                            if local_social else f"{me['name']}主动与{peer['name']}{'初次打招呼认识' if first_meet else '聊了几句'}。")
                    persona_bridge.record_world_social_interaction(
                        self._project_root,
                        self._engine.data_root,
                        a_host,
                        b_host,
                        space_id=me_realm,
                        summary=note,
                        first_meet=first_meet,
                        call_a_to_b=peer["name"] if first_meet else "",
                        call_b_to_a=me["name"] if first_meet else "",
                        custom_persona_dir=settings.persona_data_dir,
                    )
                    self._engine.record_custom_event(
                        "actor_social_talk",
                        actor_id=actor_id,
                        space_id=me["space_id"],
                        payload={"initiator": me["name"], "target": peer["name"], "first_meet": first_meet, "summary": note},
                    )
                    self._engine.record_social_feedback(actor_id, me_sid, note, peer_id=tgt_aid, spoken=not local_social)
                    logs.append(note)
            elif tool == "world_write":
                eid = str(item.get("entity_id") or "").strip()
                txt = str(item.get("text") or "").strip()
                if eid and txt:
                    res = self._engine.execute_write(actor_id, eid, txt)
                    logs.append(res)
            elif tool == "world_combine":
                src = str(item.get("source_id") or "").strip()
                tgt = str(item.get("target_id") or "").strip()
                if src and tgt:
                    res = self._engine.execute_combine(actor_id, src, tgt)
                    logs.append(res)
            else:
                eid = str(item.get("entity_id") or "").strip()
                action = str(item.get("action") or "").strip()
                tgt = str(item.get("target_id") or "").strip()
                if eid and action:
                    res = self._engine.execute_act(actor_id, eid, action, tgt)
                    logs.append(res)
        return logs

    def _find_adjacent_walkable(self, actor: dict[str, Any], peer: dict[str, Any]) -> tuple[int, int] | None:
        sid = actor["space_id"]
        ax, ay = int(actor["x"]), int(actor["y"])
        px, py = int(peer["x"]), int(peer["y"])
        with self._engine._lock:
            space = self._engine._world["spaces"].get(sid)
            if not space:
                return None
            cands: list[tuple[int, int, int]] = []
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nx, ny = px + dx, py + dy
                if not self._engine._is_floor_walkable_locked(space, nx, ny, ignore_actor_id=actor["actor_id"]):
                    continue
                path = self._engine._bfs_locked(sid, (ax, ay), {(nx, ny)}, ignore_actor_id=actor["actor_id"])
                if path is not None:
                    cands.append((len(path), nx, ny))
            if not cands:
                return None
            cands.sort(key=lambda t: t[0])
            return cands[0][1], cands[0][2]

    def _pick_social_peer(
        self,
        actor: dict[str, Any],
        sp_snap: dict[str, Any],
        recent: list[str],
        settings: WorldSettings,
        *,
        force_autonomy: bool = False,
    ) -> tuple[float, dict[str, Any], dict[str, Any], tuple[int, int] | None] | None:
        aid = actor["actor_id"]
        vit = actor.get("vitals") or {}
        aut = normalize_autonomy(actor.get("autonomy"))
        energy = float(vit.get("energy", 0.72))
        sleep_p = float(vit.get("sleep_pressure", 0.30))
        mood_v = float(vit.get("mood_v", 0.15))
        disengaged = bool(vit.get("disengaged", False))
        agenda = str(vit.get("agenda") or "").lower()
        curiosity = float(aut.get("curiosity", 0.55))

        if disengaged or sleep_p >= 0.68 or energy <= 0.35 or mood_v <= -0.35:
            return None
        if any(k in agenda for k in ("hide", "alone", "quiet")) and not force_autonomy:
            return None

        sid = actor["space_id"]
        peers = [
            a for a in (sp_snap.get("actors") or [])
            if a.get("space_id") == sid and a.get("actor_id") != aid and a.get("posture") != "resting"
        ]
        if not peers:
            return None

        actor_host = str(actor.get("host_character") or actor.get("name") or "").strip()
        bp = persona_bridge.read_persona_behavior_profile(
            self._project_root,
            actor_host,
            custom_persona_dir=settings.persona_data_dir,
        )
        peer_hosts = [str(p.get("host_character") or p.get("name") or "").strip() for p in peers if not p.get("is_player")]
        realm_sid = self._engine.realm_space_id(sid) if hasattr(self._engine, "realm_space_id") else sid
        rels = persona_bridge.read_actor_relations(
            self._project_root,
            actor_host,
            space_id=realm_sid,
            world_data_root=self._engine.data_root,
            custom_persona_dir=settings.persona_data_dir,
            known_actors=peer_hosts,
        )

        best: tuple[float, dict[str, Any], dict[str, Any], tuple[int, int] | None] | None = None
        for peer in peers:
            p_aid = str(peer["actor_id"])
            p_name = str(peer.get("name") or p_aid).strip()
            p_host = str(peer.get("host_character") or p_name).strip()
            edge = rels.get("user") if peer.get("is_player") else rels.get(f"char:{p_host}") or rels.get(f"char:{p_name}")
            edge = edge or {}

            if int(edge.get("cool_off_turns") or 0) > 0:
                continue
            if str(edge.get("attitude_stance") or "") == "cold" or float(edge.get("repulsion") or 0.0) >= 0.45:
                continue
            willing = float(edge.get("social_willingness") or 0.50)
            if willing < 0.25:
                continue

            warmth = float(edge.get("warmth") or 0.45)
            val = float(edge.get("affective_valence") or 0.0)
            trust = float(edge.get("trust") or 0.55)
            score = willing * 3.2 + warmth * 2.0 + max(-0.5, val) * 2.5 + trust * 1.4
            score += (float(bp.get("social_initiative", 0.38)) - 0.5) * 3.0
            score -= (float(bp.get("social_avoidance", 0.63)) - 0.5) * 2.5

            if energy >= 0.60 and mood_v >= 0.05:
                score += 1.4
            if any(k in agenda for k in ("company", "fun", "social", "talk")):
                score += 2.2
            if not edge.get("known"):
                stranger_bias = (float(bp.get("social_initiative", 0.38)) - float(bp.get("social_avoidance", 0.63))) * 3.5
                score += (1.2 + stranger_bias) if (curiosity >= 0.50 or force_autonomy) else (-2.5 + stranger_bias)

            rec_key = f"actor:{p_aid}"
            if recent and recent[0] == rec_key:
                score -= 5.5
            elif rec_key in recent[:2]:
                score -= 3.0
            last_soc = float((actor.get("autonomy") or {}).get("last_social_ts") or 0.0)
            if last_soc > 0 and (time.time() - last_soc) < 120.0:
                score -= 4.0

            dist = max(abs(int(actor["x"]) - int(peer["x"])), abs(int(actor["y"]) - int(peer["y"])))
            if peer.get("is_player") and dist > 1:
                continue
            adj = None if dist <= 1 else self._find_adjacent_walkable(actor, peer)
            if dist > 1 and adj is None:
                continue

            if best is None or score > best[0]:
                best = (score, peer, edge, adj)

        threshold = 3.8 if force_autonomy else 4.5
        if best is None or best[0] < threshold:
            return None
        return best

    def _puzzle_invitation(
        self, actor: dict[str, Any], box: dict[str, Any], settings: WorldSettings,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if (not settings.episodic_memory_enabled or actor["vitals"]["disengaged"]
                or box["entity_id"] in actor["autonomy"]["recent_targets"][:2]):
            return None
        lock = box["traits"].get("container") or {}
        if not lock.get("is_locked") or not lock.get("key_tag"):
            return None
        eligible = set(self._engine.puzzle_peers(actor["actor_id"], box["entity_id"]))
        if not eligible:
            return None
        for memory in self._engine.puzzle_memories(actor["actor_id"], actor["space_id"]):
            helper = next(person for person in memory["participants"] if person["role"] == "helper")
            if memory["key_tag"] == lock["key_tag"] and helper["actor_id"] in eligible:
                return helper, memory
        return None

    # ── 可选 LLM 自由意志决策 ────────────────────────────────────────────
    def _decide_with_llm(
        self,
        actor: dict[str, Any],
        vis: list[dict[str, Any]],
        sp_snap: dict[str, Any],
    ) -> dict[str, Any] | None:
        aid = actor["actor_id"]
        vit = actor.get("vitals") or {}
        aut = normalize_autonomy(actor.get("autonomy"))
        view_txt = projector.project_view(self._engine, aid, consume_diffs=False)
        settings_now = self._settings_provider()
        invitations = {}
        for box in vis:
            invitation = self._puzzle_invitation(actor, box, settings_now)
            if invitation:
                invitations[box["entity_id"]] = invitation
        puzzle_context = "\n".join(
            f"- 上次「{memory['entity_name']}」由{helper['name']}打开。可以邀请对方帮忙开锁，"
            f"使用 world_invite_puzzle，entity_id={eid}，target_actor_id={helper['actor_id']}。"
            for eid, (helper, memory) in invitations.items()
        )
        l0_llm = persona_bridge.build_percept_frame(
            self._engine.data_root,
            self._project_root,
            sp_snap["space"],
            sp_snap["lighting"],
            actor,
            vis,
            actors_map={a["actor_id"]: a for a in (sp_snap.get("actors") or [])},
            custom_persona_dir=settings_now.persona_data_dir,
            advance_exposure=0.0,
            weather=sp_snap.get("weather"),
        )
        sys_prompt = (
            f"你是{actor['name']}。此刻你在自己的像素世界里自由活动。\n"
            "请根据视野里的真实物品、同屋的其他角色及态度与情感指针、精力困意、饱腹感、好奇心，决定接下来想做的一件小事。\n"
            "如果此刻状态不错且想和同屋的某个人说句话（或去认识还不熟的人），可以用 world_talk 走近对方交流；不想社交时就按心意做自己的事。\n"
            "严格只输出合法 JSON：\n"
            '{"goal":"此刻想做的事(<=16字)","drive":"rest|hunger|game|music|reading|hobby|curiosity|explore|comfort|social",'
            '"thought":"简短口语碎碎念(<=28字)","emotion":"开心|好奇|困倦|平静|害羞|兴奋|温柔",'
            '"target_id":"主目标entity_id或actor:xxx或空",'
            '"actions":[{"tool":"world_act|world_move|world_write|world_talk|world_invite_puzzle","entity_id":"...","action":"turn_on|turn_off|rest|leave_rest|consume|interact|open|unlock|take|look|enter","target_actor_id":"同屋角色actor_id","x":0,"y":0,"text":"..."}]}'
        )
        bp_llm = l0_llm.get("behavior_profile") or {}
        user_prompt = (
            f"{l0_llm['prompt_block']}\n\n"
            f"{view_txt}\n"
            f"- 体内状态：精力={vit.get('energy')}, 困意={vit.get('sleep_pressure')}, 心情={vit.get('mood_v')}, "
            f"饥饿={aut['hunger']}, 好奇={aut['curiosity']}, 偏好领域={vit.get('preferred_domains')}\n"
            f"- 人格行为倾向(L1-L8)：安全优先={bp_llm.get('safety_priority')}, 社交主动={bp_llm.get('social_initiative')}, "
            f"社交回避={bp_llm.get('social_avoidance')}, 探索欲={bp_llm.get('exploration_drive')}, 礼貌克制={bp_llm.get('norm_courtesy')}\n"
            f"- 最近刚碰过的目标：{aut['recent_targets']}\n"
            f"- 空间可用尺寸：{sp_snap['space']['width']}x{sp_snap['space']['height']}\n"
            f"{puzzle_context}\n"
            "不要重复连续碰同一目标；若决定好了，只输出 JSON。"
        )
        parsed = text_compiler._ask_llm_json(
            sys_prompt,
            user_prompt,
            adapter_provider=self._adapter_provider,
            timeout_sec=10.0,
            max_tokens=360,
        )
        if not isinstance(parsed, dict) or not isinstance(parsed.get("actions"), list) or not parsed["actions"]:
            return None
        vis_ids = {e["entity_id"] for e in vis}
        peer_ids = {
            a["actor_id"] for a in (sp_snap.get("actors") or [])
            if a.get("space_id") == actor["space_id"] and a.get("actor_id") != aid
        }
        valid_actions: list[dict[str, Any]] = []
        has_talk = False
        for item in parsed["actions"][:2]:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "world_act").strip().lower()
            if tool == "world_move":
                valid_actions.append(item)
            elif tool == "world_invite_puzzle":
                invitation = invitations.get(str(item.get("entity_id") or ""))
                if invitation and item.get("target_actor_id") == invitation[0]["actor_id"]:
                    valid_actions.append(item)
            elif tool == "world_talk" and str(item.get("target_actor_id") or "") in peer_ids:
                valid_actions.append(item)
                has_talk = True
            elif str(item.get("entity_id") or "") in vis_ids:
                valid_actions.append(item)
        if not valid_actions:
            return None
        return {
            "goal": clip_text(parsed.get("goal"), 36, "想随便做点什么"),
            "drive": clip_text(parsed.get("drive"), 24, "curiosity"),
            "thought": clip_text(parsed.get("thought"), 60, ""),
            "emotion": clip_text(parsed.get("emotion"), 16, "平静"),
            "target_id": str(parsed.get("target_id") or valid_actions[0].get("entity_id") or ""),
            "actions": valid_actions,
            "record_thought_in_chat": not has_talk,
            "skip_auto_chat_log": has_talk,
        }

    # ── 十类内心驱力规划器（0 Token 默认内核）───────────────────────────
    def _decide_with_rules(
        self,
        actor: dict[str, Any],
        vis: list[dict[str, Any]],
        sp_snap: dict[str, Any],
        settings: WorldSettings,
        *,
        force_autonomy: bool = False,
    ) -> dict[str, Any] | None:
        aid = actor["actor_id"]
        vit = actor.get("vitals") or {}
        aut = normalize_autonomy(actor.get("autonomy"))
        energy = float(vit.get("energy", 0.7))
        sleep_p = float(vit.get("sleep_pressure", 0.3))
        disengaged = bool(vit.get("disengaged", False))
        agenda = str(vit.get("agenda") or "").lower()
        preferred = list(vit.get("preferred_domains") or ["game"])
        hunger = float(aut.get("hunger", 0.3))
        curiosity = float(aut.get("curiosity", 0.55))
        recent = list(aut.get("recent_targets") or [])
        space = sp_snap["space"]
        lighting = sp_snap["lighting"]
        holding_id = actor.get("holding_id")
        host_char = str(actor.get("host_character") or actor.get("name") or "").strip()
        sid = str(space.get("space_id") or actor.get("space_id") or "room_default")
        peer_known = False
        other_actors = [a for a in (sp_snap.get("actors") or []) if a.get("actor_id") != aid]
        if other_actors:
            realm_sid = self._engine.realm_space_id(sid) if hasattr(self._engine, "realm_space_id") else sid
            rels_chk = persona_bridge.read_actor_relations(
                self._project_root,
                host_char,
                space_id=realm_sid,
                world_data_root=self._engine.data_root,
                custom_persona_dir=settings.persona_data_dir,
                known_actors=[str(a.get("host_character") or a.get("name") or "") for a in other_actors],
            )
            peer_known = any(bool(v.get("known")) for k, v in rels_chk.items() if k.startswith("char:"))
        l0_frame = persona_bridge.build_percept_frame(
            self._engine.data_root,
            self._project_root,
            space,
            lighting,
            actor,
            vis,
            actors_map={a["actor_id"]: a for a in (sp_snap.get("actors") or [])},
            custom_persona_dir=settings.persona_data_dir,
            peer_known=peer_known,
            advance_exposure=0.18 if vis else 0.10,
            weather=sp_snap.get("weather"),
        )

        # 1. 夜晚或室内变暗自动开灯
        if lighting.get("is_dark") and actor.get("posture") != "resting":
            lamp = next(
                (
                    e for e in vis
                    if (e.get("traits") or {}).get("switchable", {}).get("provides_light")
                    and not (e.get("traits") or {}).get("switchable", {}).get("is_on")
                ),
                None,
            )
            if lamp:
                return {
                    "goal": f"把「{lamp['name']}」打开照明",
                    "drive": "comfort",
                    "thought": "屋里暗了，先把灯打开。",
                    "emotion": "平静",
                    "target_id": lamp["entity_id"],
                    "actions": [{"tool": "world_act", "entity_id": lamp["entity_id"], "action": "turn_on"}],
                    "record_thought_in_chat": True,
                }

        # 2. 结合世界昼夜作息 + L0 Borbély 时型偏移判断是否该休息
        hour = float(lighting.get("hour_float", lighting.get("hour", 12)))
        chrono = float(l0_frame.get("chronotype_offset") or 0.0) if l0_frame.get("enabled") else 0.0
        c_str = float(l0_frame.get("circadian_strength") or 0.75) if l0_frame.get("enabled") else 0.75
        bio_hour = (hour - chrono) % 24.0
        is_late_night = (bio_hour >= 23.0 or bio_hour < 6.0) if c_str >= 0.35 else False
        is_daytime = (7.0 <= bio_hour < 22.0) if c_str >= 0.35 else True
        need_sleep = (
            (sleep_p >= 0.45 or energy <= 0.60) if is_late_night
            else ((sleep_p >= 0.85 or energy <= 0.22) if is_daytime else (sleep_p >= 0.62 or energy <= 0.38))
        )
        if need_sleep and actor.get("posture") != "resting":
            free_beds = [
                e for e in vis
                if "restable" in (e.get("traits") or {})
                and (e.get("traits") or {})["restable"].get("occupied_by") is None
            ]
            if free_beds:
                free_beds.sort(
                    key=lambda e: float(((e.get("traits") or {}).get("restable") or {}).get("comfort") or 0.7),
                    reverse=True,
                )
                bed = free_beds[0]
                return {
                    "goal": f"去「{bed['name']}」{'睡觉' if is_late_night else '歇一会'}",
                    "drive": "rest",
                    "thought": f"{'夜深了' if is_late_night else '有点困了'}，去「{bed['name']}」{'睡一觉' if is_late_night else '休息一会儿'}。",
                    "emotion": "困倦",
                    "target_id": bed["entity_id"],
                    "actions": [{"tool": "world_act", "entity_id": bed["entity_id"], "action": "rest"}],
                    "record_thought_in_chat": True,
                }

        # 3. 休息恢复够了自动起身（深夜保持安睡，天亮或精神恢复后才起）；否则继续安睡
        if actor.get("posture") == "resting":
            wake_ready = (
                (force_autonomy and energy >= 0.62 and sleep_p <= 0.48)
                or (not is_late_night and ((energy >= 0.76 and sleep_p <= 0.38) or (hour >= 8 and energy >= 0.60 and sleep_p <= 0.48)))
            )
            if wake_ready and actor.get("resting_on_id"):
                rid = actor["resting_on_id"]
                return {
                    "goal": "睡饱了，起来四处看看",
                    "drive": "rest",
                    "thought": "睡了一觉，精神好多了。",
                    "emotion": "开心",
                    "target_id": rid,
                    "actions": [{"tool": "world_act", "entity_id": rid, "action": "leave_rest"}],
                    "record_thought_in_chat": True,
                }
            return {
                "goal": "正在安稳补觉中",
                "drive": "rest",
                "thought": "呼……再睡一小会儿……",
                "emotion": "熟睡",
                "target_id": str(actor.get("resting_on_id") or ""),
                "actions": [],
                "passive_summary": "",
                "record_thought_in_chat": False,
            }

        # 4. 沉默疏离态保持安静
        if disengaged and not force_autonomy:
            return {
                "goal": "想一个人静一静",
                "drive": "calm",
                "thought": "……我想自己待会儿。",
                "emotion": "冷漠",
                "target_id": "",
                "actions": [],
                "passive_summary": "",
                "record_thought_in_chat": False,
            }

        # 4.5 L0 陌生环境适应期：初入陌生空间时（familiarity < 0.25）优先环顾/确认身边物件，不立刻自来熟玩乐
        if l0_frame.get("enabled") and l0_frame.get("stage") == "unfamiliar":
            look_cand = next(
                (e for e in (l0_frame.get("focused_entities") or vis) if e["entity_id"] not in recent[:2]),
                None,
            )
            if look_cand:
                return {
                    "goal": f"先打量一下周围的「{look_cand['name']}」",
                    "drive": "curiosity",
                    "thought": f"刚到这边还有点陌生……先看看「{look_cand['name']}」是什么。",
                    "emotion": "好奇",
                    "target_id": look_cand["entity_id"],
                    "actions": [{"tool": "world_act", "entity_id": look_cand["entity_id"], "action": "look"}],
                    "record_thought_in_chat": True,
                }

        bp = l0_frame.get("behavior_profile") or {}
        exploration_drive = float(bp.get("exploration_drive", 0.64))
        safety_priority = float(bp.get("safety_priority", 0.55))
        norm_courtesy = float(bp.get("norm_courtesy", 0.75))
        game_eff = float(bp.get("domain_efficacy_game", 0.85))
        has_resting_peer = any(
            a.get("space_id") == sid and a.get("actor_id") != aid and a.get("posture") == "resting"
            for a in (sp_snap.get("actors") or [])
        )

        # 5. 探索组合：拿钥匙开带锁宝箱（受探索欲与安全需要调制；低落或高安全谨慎且探索欲低时延后）
        locked_box = next(
            (e for e in vis if (e.get("traits") or {}).get("container", {}).get("is_locked")),
            None,
        )
        if locked_box and (exploration_drive >= 0.42 or force_autonomy) and not (float(vit.get("mood_v", 0.15)) <= -0.35 and safety_priority >= 0.65):
            need_tag = str((locked_box.get("traits") or {}).get("container", {}).get("key_tag") or "").strip()
            box_id = locked_box["entity_id"]
            invitation = self._puzzle_invitation(actor, locked_box, settings)
            if invitation:
                helper, memory = invitation
                return {
                    "goal": f"请{helper['name']}一起打开「{locked_box['name']}」",
                    "drive": "curiosity",
                    "thought": f"上次「{memory['entity_name']}」是{helper['name']}打开的，再请帮一次吧。",
                    "emotion": "好奇",
                    "target_id": box_id,
                    "actions": [{"tool": "world_invite_puzzle", "entity_id": box_id, "target_actor_id": helper["actor_id"]}],
                    "record_thought_in_chat": True,
                }
            key_ent = next(
                (
                    e for e in vis
                    if e["entity_id"] != locked_box["entity_id"]
                    and (not need_tag or need_tag in (e.get("tags") or []))
                ),
                None,
            )
            if key_ent:
                seq: list[dict[str, Any]] = []
                if holding_id != key_ent["entity_id"] and "portable" in (key_ent.get("traits") or {}):
                    if holding_id:
                        seq.append({"tool": "world_act", "entity_id": holding_id, "action": "place", "target_id": "floor"})
                    seq.append({"tool": "world_act", "entity_id": key_ent["entity_id"], "action": "take"})
                seq.append({"tool": "world_act", "entity_id": locked_box["entity_id"], "action": "unlock"})
                return {
                    "goal": f"用「{key_ent['name']}」打开「{locked_box['name']}」",
                    "drive": "curiosity",
                    "thought": f"用「{key_ent['name']}」打开「{locked_box['name']}」看看。",
                    "emotion": "好奇",
                    "target_id": locked_box["entity_id"],
                    "actions": seq,
                    "record_thought_in_chat": True,
                }

        # 6. 趁热喝茶 / 饿了找吃的 / 开冰箱觅食
        consumables = [
            e for e in vis
            if int(((e.get("traits") or {}).get("consumable") or {}).get("uses_left") or 0) > 0
        ]
        hot_drinks = [
            e for e in consumables
            if ((e.get("traits") or {}).get("thermal") or {}).get("temp") == "hot"
            and e["entity_id"] not in recent[:1]
        ]
        picked_food = None
        if hot_drinks and (energy <= 0.85 or hunger >= 0.25 or force_autonomy):
            picked_food = hot_drinks[0]
        elif consumables and (hunger >= 0.45 or energy <= 0.56 or (force_autonomy and hunger >= 0.35)):
            consumables.sort(key=lambda e: (e["entity_id"] in recent[:2], -float((e.get("effects") or {}).get("mood_v_delta") or 0)))
            picked_food = consumables[0]

        if picked_food is not None:
            cm_food = (picked_food.get("traits") or {}).get("consumable") or {}
            verb = cm_food.get("consume_verb") or "享用"
            food_name, _empty_name = schema.infer_consumable_names(picked_food["name"], str(cm_food.get("empty_name") or ""))
            return {
                "goal": f"{verb}「{food_name}」",
                "drive": "hunger",
                "thought": f"{verb}点「{food_name}」。",
                "emotion": "开心",
                "target_id": picked_food["entity_id"],
                "actions": [{"tool": "world_act", "entity_id": picked_food["entity_id"], "action": "consume"}],
                "record_thought_in_chat": True,
            }

        if hunger >= 0.50:
            closed_box = next(
                (
                    e for e in vis
                    if "container" in (e.get("traits") or {})
                    and not e["traits"]["container"].get("is_locked")
                    and not e["traits"]["container"].get("is_open")
                    and e["entity_id"] not in recent[:2]
                ),
                None,
            )
            if closed_box:
                return {
                    "goal": f"打开「{closed_box['name']}」翻找点心",
                    "drive": "hunger",
                    "thought": f"看看「{closed_box['name']}」里有没有吃的。",
                    "emotion": "好奇",
                    "target_id": closed_box["entity_id"],
                    "actions": [{"tool": "world_act", "entity_id": closed_box["entity_id"], "action": "open"}],
                    "record_thought_in_chat": True,
                }

        # 7. 自主社交（依据当下状态与态度情感指针） vs 兴趣爱好竞争决策
        social_pick = self._pick_social_peer(actor, sp_snap, recent, settings, force_autonomy=force_autonomy)
        activities = [
            e for e in vis
            if "activity" in (e.get("traits") or {})
            and (
                (e.get("traits") or {})["activity"].get("battery") is None
                or int((e.get("traits") or {})["activity"].get("battery") or 0) > 0
            )
        ]
        best_act: tuple[float, dict[str, Any]] | None = None
        if activities and energy >= 0.35:
            scored: list[tuple[float, dict[str, Any]]] = []
            for ent in activities:
                dom = str(((ent.get("traits") or {}).get("activity") or {}).get("domain") or "hobby")
                score = 2.0 + float((ent.get("effects") or {}).get("mood_v_delta") or 0.0) * 5.0
                if dom in preferred:
                    score += 3.0
                if dom == "game":
                    score += (game_eff - 0.5) * 3.0
                if has_resting_peer and norm_courtesy >= 0.60:
                    score += 1.4 if dom in ("reading", "hobby") else (-1.2 if dom in ("music", "game") else 0.0)
                if any(k in agenda for k in ("hide", "calm", "quiet")) and dom in ("reading", "hobby", "music"):
                    score += 1.8
                elif any(k in agenda for k in ("company", "fun", "play")) and dom in ("game", "music"):
                    score += 1.5
                eid = ent["entity_id"]
                if recent and eid == recent[0]:
                    score -= 4.0
                elif eid in recent[:3]:
                    score -= 2.0
                else:
                    score += 0.6
                scored.append((score, ent))

            scored.sort(key=lambda pair: pair[0], reverse=True)
            best_act = scored[0]

        if social_pick is not None and (best_act is None or social_pick[0] >= best_act[0]):
            _s_score, peer, edge, adj = social_pick
            p_name = str(peer.get("name") or peer["actor_id"])
            is_new = not bool(edge.get("known"))
            seq_social: list[dict[str, Any]] = []
            if adj is not None:
                seq_social.append({"tool": "world_move", "x": adj[0], "y": adj[1]})
            reason = (
                f"注意到还不认识的{p_name}，想过去打个招呼认识一下"
                if is_new
                else f"此刻状态不错，想找{edge.get('call_name') or p_name}说几句话"
            )
            seq_social.append({"tool": "world_talk", "target_actor_id": peer["actor_id"], "reason": reason})
            return {
                "goal": f"去和「{p_name}」{'打招呼认识' if is_new else '聊几句'}",
                "drive": "social",
                "thought": f"过去和「{p_name}」{'打个招呼' if is_new else '聊两句'}。",
                "emotion": "好奇" if is_new else "开心",
                "target_id": f"actor:{peer['actor_id']}",
                "actions": seq_social,
                "record_thought_in_chat": False,
                "skip_auto_chat_log": True,
            }

        if best_act is not None:
            best_score, best_ent = best_act
            if best_score > 0.5 or force_autonomy or len(vis) <= 2:
                dom = str(((best_ent.get("traits") or {}).get("activity") or {}).get("domain") or "hobby")
                thought_map = {
                    "game": f"用「{best_ent['name']}」玩一会儿。",
                    "music": f"打开「{best_ent['name']}」听点音乐。",
                    "reading": f"翻看「{best_ent['name']}」。",
                    "hobby": f"去看看「{best_ent['name']}」。",
                }
                emo = "兴奋" if dom == "game" else ("温柔" if dom == "music" else "开心")
                return {
                    "goal": f"玩/用「{best_ent['name']}」",
                    "drive": dom,
                    "thought": thought_map.get(dom, f"去看看「{best_ent['name']}」。"),
                    "emotion": emo,
                    "target_id": best_ent["entity_id"],
                    "actions": [{"tool": "world_act", "entity_id": best_ent["entity_id"], "action": "interact"}],
                    "record_thought_in_chat": True,
                }

        # 8. 自主在笔记本/画架写一句观察感想
        inscribable = next(
            (
                e for e in vis
                if (e.get("traits") or {}).get("inscribable", {}).get("writable_by_ai")
                and e["entity_id"] not in recent[:2]
            ),
            None,
        )
        if inscribable and (settings.allow_ai_diary or curiosity >= 0.45 or force_autonomy):
            note_txt = self._build_contextual_note(actor, space, lighting, vis)
            return {
                "goal": f"在「{inscribable['name']}」写下心情",
                "drive": "expression",
                "thought": f"想在{inscribable['name']}上留一句：「{note_txt}」",
                "emotion": "温柔",
                "target_id": inscribable["entity_id"],
                "actions": [{"tool": "world_write", "entity_id": inscribable["entity_id"], "text": note_txt}],
                "record_thought_in_chat": True,
            }

        # 9. 白天室内随手关掉还亮着的非篝火灯具
        if lighting.get("period") == "day":
            day_lamp = next(
                (
                    e for e in vis
                    if (e.get("traits") or {}).get("switchable", {}).get("provides_light")
                    and (e.get("traits") or {}).get("switchable", {}).get("is_on")
                    and "campfire" not in str(e.get("blueprint_id") or "")
                    and e["entity_id"] not in recent[:2]
                ),
                None,
            )
            if day_lamp:
                return {
                    "goal": f"天亮了，关掉「{day_lamp['name']}」",
                    "drive": "order",
                    "thought": "天亮了，先把灯关掉。",
                    "emotion": "平静",
                    "target_id": day_lamp["entity_id"],
                    "actions": [{"tool": "world_act", "entity_id": day_lamp["entity_id"], "action": "turn_off"}],
                    "record_thought_in_chat": True,
                }

        # 10. 打开关着的门，或推门去联通空间探索，或在好看的风景格子间漫步欣赏
        is_town_realm = space.get("space_kind") in ("town", "building")
        if not is_town_realm:
            closed_door = next(
                (
                    e for e in vis
                    if "portal" in (e.get("traits") or {})
                    and not (e.get("traits") or {})["portal"].get("is_open")
                    and e["entity_id"] not in recent[:2]
                ),
                None,
            )
            if closed_door and (curiosity >= 0.55 or force_autonomy):
                return {
                    "goal": f"打开「{closed_door['name']}」",
                    "drive": "explore",
                    "thought": f"先把「{closed_door['name']}」打开吧，这样两边都能走过去。",
                    "emotion": "好奇",
                    "target_id": closed_door["entity_id"],
                    "actions": [{"tool": "world_act", "entity_id": closed_door["entity_id"], "action": "open"}],
                    "record_thought_in_chat": True,
                }

        portal = next(
            (
                e for e in vis
                if (e.get("traits") or {}).get("portal", {}).get("target_space_id")
                and (is_town_realm or (e.get("traits") or {}).get("portal", {}).get("is_open"))
                and e["entity_id"] not in recent[:2]
            ),
            None,
        )
        if portal and (curiosity >= 0.68 or force_autonomy):
            return {
                "goal": f"推开「{portal['name']}」去那边逛逛",
                "drive": "explore",
                "thought": "推开门看看那边有什么。",
                "emotion": "好奇",
                "target_id": portal["entity_id"],
                "actions": [{"tool": "world_act", "entity_id": portal["entity_id"], "action": "enter"}],
                "record_thought_in_chat": True,
            }

        scenic = self._pick_scenic_cell(actor, space, vis, recent)
        if scenic is not None:
            tx, ty, near_ent = scenic
            seq_move: list[dict[str, Any]] = [{"tool": "world_move", "x": tx, "y": ty}]
            if near_ent and near_ent["entity_id"] not in recent[:2]:
                seq_move.append({"tool": "world_act", "entity_id": near_ent["entity_id"], "action": "look"})
                return {
                    "goal": f"走近看看「{near_ent['name']}」",
                    "drive": "curiosity",
                    "thought": f"去看看「{near_ent['name']}」。",
                    "emotion": "好奇",
                    "target_id": near_ent["entity_id"],
                    "actions": seq_move,
                    "record_thought_in_chat": True,
                }
            return {
                "goal": f"去 ({tx}, {ty}) 附近走走散心",
                "drive": "curiosity",
                "thought": "换个地方走走。",
                "emotion": "平静",
                "target_id": f"cell:{tx},{ty}",
                "actions": seq_move,
                "record_thought_in_chat": True,
            }

        return None

    def _build_contextual_note(
        self,
        actor: dict[str, Any],
        space: dict[str, Any],
        lighting: dict[str, Any],
        vis: list[dict[str, Any]],
    ) -> str:
        period_map = {"dawn": "清晨", "day": "午后", "dusk": "傍晚", "night": "夜里"}
        p_txt = period_map.get(str(lighting.get("period") or "day"), "此刻")
        sample = next((e["name"] for e in vis if e.get("z", 0) >= 0 and "inscribable" not in (e.get("traits") or {})), "")
        if sample:
            return f"{p_txt}待在{space['name']}，旁边的{sample}看着很安稳。"[:24]
        return f"{p_txt}的{space['name']}很安静，心情也挺平和的。"[:24]

    def _pick_scenic_cell(
        self,
        actor: dict[str, Any],
        space: dict[str, Any],
        vis: list[dict[str, Any]],
        recent: list[str],
    ) -> tuple[int, int, dict[str, Any] | None] | None:
        w = int(space["width"])
        ax, ay = int(actor["x"]), int(actor["y"])
        sid = space["space_id"]
        tiles = space.get("tiles") or {}
        cands: list[tuple[float, int, int, dict[str, Any] | None]] = []
        vis_pos = [
            e for e in vis
            if e.get("z", 0) >= 0
        ]

        with self._engine._lock:
            reachable = self._engine._reachable_cells_locked(
                sid,
                (ax, ay),
                max_steps=14,
                ignore_actor_id=actor["actor_id"],
            )
            for (xx, yy), step_dist in reachable.items():
                if step_dist < 2:
                    continue
                tid = tiles.get(f"{xx},{yy}", "")
                score = 1.0
                if tid in ("flower_grass", "tatami", "carpet_pink", "carpet_blue"):
                    score += 3.0
                elif tid in ("cobble", "sand", "grass", "stone_tile"):
                    score += 1.5
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    if tiles.get(f"{xx + dx},{yy + dy}") == "water":
                        score += 2.2
                if yy <= 2 and abs(xx - w // 2) <= 1:
                    score += 1.6
                near_ent = next(
                    (
                        e for e in vis_pos
                        if max(abs(xx - int(e["x"])), abs(yy - int(e["y"]))) <= 1
                    ),
                    None,
                )
                if near_ent:
                    score += 1.5 if near_ent["entity_id"] not in recent else 0.2
                if f"cell:{xx},{yy}" in recent:
                    score -= 3.5
                cands.append((score, xx, yy, near_ent))

        if not cands:
            return None
        cands.sort(key=lambda item: item[0], reverse=True)
        idx = len(recent) % min(3, len(cands))
        _, tx, ty, near_ent = cands[idx]
        return tx, ty, near_ent

    def _maybe_write_diary(
        self,
        actor_id: str,
        actor_name: str,
        event_note: str,
        visible_entities: list[dict[str, Any]],
        settings: WorldSettings,
    ) -> None:
        if not settings.allow_ai_diary:
            return
        now = time.time()
        cooldown = float(settings.diary_cooldown_hours) * 3600.0
        if now - float(self._last_diary_ts.get(actor_id) or 0.0) < cooldown:
            return
        notebook = next(
            (
                e for e in visible_entities
                if (e.get("traits") or {}).get("inscribable", {}).get("writable_by_ai")
            ),
            None,
        )
        if not notebook:
            return
        adapter = self._adapter_provider() if self._adapter_provider else None
        if adapter is None:
            return
        try:
            resp = adapter.chat(
                messages=[
                    {
                        "role": "system",
                        "content": f"你是{actor_name}。用一句极简口语随手记（不超过 22 个字，不要括号动作，不要抒情金句）记下刚才的小事。只输出那一句。",
                    },
                    {"role": "user", "content": event_note},
                ],
                stream=False,
                temperature=0.5,
                max_tokens=48,
            )
            line = str(getattr(adapter.decode_response(resp), "content", "") or "").strip()
            if line:
                self._engine.execute_write(actor_id, notebook["entity_id"], line[:24])
                self._last_diary_ts[actor_id] = now
        except Exception:
            logger.debug("nanami_world: 自主日记写入跳过", exc_info=True)

    def _export_bridges(self) -> None:
        snap = self._engine.snapshot_for_ui()
        for act in snap["actors"]:
            aid = act["actor_id"]
            view = projector.project_view(self._engine, aid, consume_diffs=False)
            persona_bridge.write_world_bridge(self._engine, aid, self._engine.bridge_dir, view_text=view)
