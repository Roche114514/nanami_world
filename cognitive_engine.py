"""城镇自主认知：场所记忆、效用竞争、持久意图与具身反馈。"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import episodic_memory, persona_bridge, schedule, text_compiler, venue_semantics, weather
from .schema import clip_text, inscription_is_new


logger = logging.getLogger(__name__)

_DOMAINS = {
    "rest": "休息", "food": "饮食", "game": "游戏", "reading": "阅读",
    "social": "社交", "notice": "导览", "explore": "探索", "expression": "表达",
    "comfort": "照明", "curiosity": "好奇", "hobby": "爱好", "music": "音乐",
}
_PHASES = {"travel": "前往", "approach": "接近", "waiting": "等候同伴", "activity": "在场活动", "dwell": "结伴停留"}
_SOURCES = {"direct": "亲历", "signboard": "布告栏", "social": "同伴告知"}
# 恢复周期为游戏分钟。场所和区块的新鲜感恢复得比单次动作慢。
_HALF_LIFE = {"entity": 95.0, "action": 35.0, "place": 230.0, "frontier": 190.0}
_STRIDE = 7


def _is_notice_board(entity: Mapping[str, Any], space: Mapping[str, Any]) -> bool:
    return "inscribable" in entity.get("traits", {}) and (
        "notice_board" in entity.get("tags", [])
        or (space.get("space_kind") == "town" and "布告栏" in entity["name"])
    )


def _building_hints(building: Mapping[str, Any]) -> list[str]:
    return venue_semantics.building_affordance_hints(building)


def _activation(samples: list[float], now: float, decay: float) -> float:
    if not samples:
        return 0.0
    return math.log(sum(max(1.0, 1.0 + now - value) ** -decay for value in samples))


def _rehearse(record: dict[str, Any], now: float, *, interval: float = 3.0) -> None:
    samples = record.setdefault("samples", [])
    if not samples or now - samples[-1] >= interval:
        samples[:] = [*samples[-7:], now]


def cognitive_summary(actor: Mapping[str, Any]) -> dict[str, Any]:
    autonomy = actor.get("autonomy") or {}
    state = autonomy.get("cognition") or {}
    if not state:
        return {}
    intention = state.get("active_intention") or {}
    zones = state.get("zones", {}).values()
    places = state.get("places", {}).values()
    goal = autonomy.get("current_goal") if intention.get("phase") == "dwell" else intention.get("goal")
    return {
        "goal": goal or intention.get("goal") or "",
        "phase": _PHASES.get(intention.get("phase"), ""),
        "drives": {_DOMAINS.get(k, k): v for k, v in state.get("drives", {}).items()},
        "satiation": {_DOMAINS.get(k, k): v for k, v in state.get("satiation", {}).items() if v >= 0.05},
        "zones": [
            {"name": z["name"], "visited": bool(z.get("visits")), "source": _SOURCES.get(z.get("source"), "")}
            for z in zones
        ],
        "places": [
            {"name": p["name"], "visited": bool(p.get("visits")), "source": _SOURCES.get(p.get("source"), ""),
             "affordances": [_DOMAINS.get(k, k) for k, v in p.get("affordances", {}).items() if v["confidence"] >= 0.3]}
            for p in places if p.get("kind") != "town"
        ],
        "prediction_error": state.get("prediction_error", ""),
        "agenda": (state.get("modulation") or {}).get("agenda", ""),
        "commitment": (intention.get("goal") or "") if intention.get("pfc_committed") else "",
        "arbitration": state.get("arbitration") or "稳态/习惯",
        "memories": episodic_memory.recent_summaries(state),
        "peers": [
            {
                "name": p["name"],
                "space": (state.get("places", {}).get(p.get("space", ""), {}).get("name") or p.get("space", "")),
            }
            for p in (state.get("peers") or {}).values()
            if isinstance(p, Mapping) and p.get("space")
        ],
    }


class CognitiveEngine:
    def __init__(
        self, engine: Any, project_root: Any, *, adapter_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.engine = engine
        self.project_root = project_root
        self.adapter_provider = adapter_provider

    def step(
        self, scene: dict[str, Any], settings: Any, *, use_llm: bool,
        talk: Callable[[str, str, str], list[str]],
    ) -> dict[str, Any]:
        actor, space, visible = scene["actor"], scene["space"], scene["visible"]
        sid, aid, now = actor["space_id"], actor["actor_id"], scene["minutes"]
        aut, vit = actor["autonomy"], actor["vitals"]
        state = aut.get("cognition") or {}
        for key in ("places", "zones", "portals", "tracks", "blocked", "read", "peers"):
            state.setdefault(key, {})
        player_ids = scene["player_ids"]
        for player_id in player_ids:
            state["peers"].pop(player_id, None)
        for key in ("active_intention", "suspended_intention"):
            intention = state.get(key) or {}
            if intention.get("kind") in ("social", "seek_peer") and intention.get("target") in player_ids:
                state.pop(key, None)
        state.setdefault("last_social_at", now)
        state.setdefault("arbitration", "稳态/习惯")
        joint_visit = scene.get("joint_visit") or {}
        scene["joint_wait_minutes"] = 4.0 if "engage" in scene else max(4.0, settings.heartbeat_interval_sec / 12)
        scene["joint_activity_minutes"] = 2.0 if "engage" in scene else max(2.0, settings.heartbeat_interval_sec / 15)
        for key in ("active_intention", "suspended_intention"):
            visit_id = (state.get(key) or {}).get("joint_visit_id")
            if visit_id and visit_id != joint_visit.get("id"):
                state.pop(key, None)

        lang_packet = state.pop("pending_modulation", None)
        if isinstance(lang_packet, dict) and lang_packet.get("id"):
            state["last_language_mod_id"] = lang_packet["id"]
        live_packet = scene.get("modulation")
        mod_candidates = [p for p in (lang_packet, live_packet) if isinstance(p, dict) and p.get("id")]
        cur_mod_id = int((state.get("modulation") or {}).get("id") or 0)
        newest = max(mod_candidates, key=lambda p: int(p.get("id") or 0), default=None)
        if newest and int(newest["id"]) > cur_mod_id and (newest.get("source") == "language" or now >= newest.get("at", 0)):
            state["modulation"] = dict(newest)
            if newest.get("semantic"):
                state["semantic"] = dict(list({**state.get("semantic", {}), **newest["semantic"]}.items())[-160:])
            if newest.get("thought"):
                state["reflection_thought"] = newest["thought"]
            if newest.get("beliefs"):
                self._apply_beliefs(state, scene, newest["beliefs"], source=newest.get("source") or "language")
            if newest.get("peer_beliefs"):
                self._apply_peer_beliefs(state, scene, newest["peer_beliefs"], source=newest.get("source") or "language")

        applied_ids = {str(x) for x in (state.get("applied_commitments") or ()) if x}
        if state.get("applied_commitment_id"):
            applied_ids.add(str(state["applied_commitment_id"]))
        raw_commit = (state.get("modulation") or {}).get("commitment")
        raw_cid = str(raw_commit.get("id") or "") if isinstance(raw_commit, Mapping) else ""
        pending_commitment: dict[str, Any] | None = (
            dict(raw_commit)
            if raw_cid and raw_cid not in applied_ids
            else None
        )

        appraisal = state.get("last_appraisal") or {}
        if appraisal and appraisal["seq"] != state.get("reflected_appraisal"):
            state["reflected_appraisal"] = appraisal["seq"]
            if appraisal["deflection"] >= 0.5 or appraisal["delta_mood"] <= -0.10:
                state["reflection"] = "刚才的行动带来了意外感受"
                if state.get("modulation") and appraisal["delta_mood"] <= -0.10:
                    state["modulation"]["strength"] = state["modulation"].get("strength", 1.0) * 0.65
        state["step"] = int(state.get("step", 0)) + 1
        state["blocked"] = {key: until for key, until in state["blocked"].items() if until >= state["step"]}
        host = actor.get("host_character") or actor["name"]
        peers = [p for p in scene["actors"] if p["actor_id"] != aid and p["actor_id"] not in player_ids]
        seen_peer_ids = (
            set(scene["visible_peer_ids"]) if "visible_peer_ids" in scene
            else {p["actor_id"] for p in peers}
        )
        seen_peers = [p for p in peers if p["actor_id"] in seen_peer_ids]
        known_hosts = list(dict.fromkeys([
            *(p.get("host_character") or p["name"] for p in peers),
            *(p.get("host") or p.get("name") for p in state["peers"].values()
              if isinstance(p, Mapping) and p.get("space")),
        ]))
        relations = persona_bridge.read_actor_relations(
            self.project_root, host, space_id=scene["realm"], world_data_root=self.engine.data_root,
            custom_persona_dir=settings.persona_data_dir, known_actors=known_hosts,
        ) if known_hosts or player_ids & seen_peer_ids else {}
        same_space_known = any(
            relations.get(f"char:{p.get('host_character') or p['name']}", {}).get("known")
            for p in seen_peers
        )
        percept = persona_bridge.build_percept_frame(
            self.engine.data_root, self.project_root, space, scene["lighting"], actor, visible,
            actors_map={p["actor_id"]: p for p in scene["actors"]},
            visible_peer_ids=seen_peer_ids,
            custom_persona_dir=settings.persona_data_dir,
            peer_known=same_space_known,
            advance_exposure=0.18 * scene.get("exposure_scale", 1.0),
            weather=scene.get("weather"),
        )
        profile = percept["behavior_profile"]
        scene["percept"] = percept
        scene["schedule"] = schedule.build_actor_schedule(
            actor,
            profile,
            percept,
            scene.get("realm_buildings") or (),
            home_space_id=str(scene.get("home_space_id") or ""),
        )
        focus = {e["entity_id"] for e in percept["focused_entities"]}
        self._observe_position(state, scene, scene.get("walked") or [(actor["x"], actor["y"])])
        self._observe_entities(state, scene, focus)
        self._observe_peers(state, scene, peers, seen_peer_ids)
        scene["seen_peer_ids"] = seen_peer_ids

        social_signal = state.pop("social_signal", None)
        recently_spoke = bool(social_signal and now - social_signal["minute"] <= 15)
        if social_signal:
            state["last_social_signal"] = social_signal["seq"]
        if recently_spoke:
            name = social_signal["name"]
            spoken = social_signal.get("spoken", True)
            state["last_social_at"] = now
            state["last_exchange_at"] = now
            speaker_id = social_signal["actor"]
            if speaker_id not in player_ids:
                peer_rec = state["peers"].setdefault(speaker_id, {"actor_id": speaker_id, "name": name})
                peer_rec["spoke_at"] = now
            sa = social_signal.get("speech_act") if isinstance(social_signal.get("speech_act"), Mapping) else {}
            exp_text = str(sa.get("summary") or "").strip()
            self._experience(state, exp_text or f"和{name}{'聊了几句' if spoken else '碰了面'}。")
            has_plan = bool(sa.get("shared_place_id") or sa.get("shared_entity_id") or sa.get("personal_goal"))
            state["reflection"] = f"和{name}聊出了约定" if has_plan else f"刚和{name}{'交流' if spoken else '碰面'}"
            self._reward(state, social_signal["actor"], "social", now)
            speaker = next((p for p in peers if p["actor_id"] == social_signal["actor"]), None)
            if speaker:
                self._share_place(state, speaker, now, scene["realm"])
            if sa.get("beliefs"):
                self._apply_beliefs(state, scene, sa["beliefs"], source="social", partner=speaker)
            realm_places = scene.get("realm_places") or {}
            spid = sa.get("shared_place_id")
            pg = sa.get("personal_goal") if isinstance(sa.get("personal_goal"), Mapping) else {}
            target_is_lodging = bool(spid and self._place_is_lodging(str(spid), scene))
            personal_arrival = (
                pg.get("kind") == "visit" and pg.get("target") == spid
                and (pg.get("action") == "escort" or (pg.get("drive") == "rest" and target_is_lodging))
            )
            if spid in realm_places and not personal_arrival and not social_signal.get("joint_visit_id"):
                self._ensure_portal_known(state, scene, spid, "social", now, partner=speaker)
                sdrive = sa.get("shared_drive") if sa.get("shared_drive") in _DOMAINS else "social"
                pending_commitment = {
                    "id": f"sa:{social_signal['seq']}",
                    "kind": "visit", "target": spid, "drive": sdrive,
                    "reason": f"和{name}一起去「{realm_places[spid]}」",
                    "companion_id": social_signal["actor"],
                    "priority": "high", "source": "speech_act",
                }
            elif pg and not social_signal.get("joint_visit_id"):
                pg_kind = str(pg.get("kind") or "").strip()
                pg_tgt = str(pg.get("target") or "").strip()
                pg_drv = str(pg.get("drive") or "").strip()
                if pg_kind == "visit" and pg_tgt in realm_places:
                    self._ensure_portal_known(state, scene, pg_tgt, "social", now, partner=speaker)
                    pending_commitment = {
                        "id": f"sa:{social_signal['seq']}",
                        "kind": "visit", "target": pg_tgt,
                        "drive": pg_drv if pg_drv in _DOMAINS else "explore",
                        "reason": clip_text(pg.get("reason"), 48) or f"去「{realm_places[pg_tgt]}」",
                        "priority": "high", "source": "speech_act",
                    }
                    if pg.get("action") == "escort":
                        pending_commitment.update(action="escort", companion_id=speaker_id)
                elif pg_kind == "entity":
                    ent_obj = next((e for e in visible if e["entity_id"] == pg_tgt), None)
                    if ent_obj is not None:
                        pending_commitment = {
                            "id": f"sa:{social_signal['seq']}",
                            "kind": "entity", "target": pg_tgt,
                            "drive": pg_drv if pg_drv in _DOMAINS else "curiosity",
                            "action": str(pg.get("action") or ""),
                            "reason": clip_text(pg.get("reason"), 48) or f"去看看「{ent_obj['name']}」",
                            "priority": "high", "source": "speech_act",
                        }
            if sa.get("tone") == "tense":
                mod = dict(state.get("modulation") or {
                    "id": time.monotonic_ns(), "realm": scene["realm"], "at": now,
                    "half_life": 12.0, "domains": {}, "places": {}, "strength": 1.0,
                })
                mod_dom = dict(mod.get("domains") or {})
                mod_dom["social"] = -0.45
                mod["domains"] = mod_dom
                mod["at"] = now
                state["modulation"] = mod

        joint_id = str(joint_visit.get("id") or "")
        local_rest = joint_visit.get("seated_id") == aid
        if (joint_id and actor["posture"] != "resting"
                and (local_rest or (vit["energy"] >= 0.25 and vit["sleep_pressure"] < 0.85))
                and (state.get("active_intention") or {}).get("joint_visit_id") != joint_id
                and (pending_commitment or {}).get("source") not in ("language", "speech_act")):
            target = joint_visit["target"]
            name = joint_visit.get("entity_name") or scene["realm_places"][target]
            pending_commitment = {
                "id": joint_id, "kind": "visit", "target": target, "drive": joint_visit["drive"],
                "reason": f"和{joint_visit['peer_name']}一起去「{name}」",
                "companion_id": joint_visit["peer_id"], "joint_visit_id": joint_id,
                "priority": "high", "source": "speech_act",
            }

        self._update_drives(state, scene)
        if "engage" in scene and not actor.get("is_player"):
            scene["pack"] = self.engine.npc_pack(aid)
            scene["persona_dir"] = settings.persona_data_dir
        diary_ready = settings.allow_ai_diary and time.time() >= (
            state.get("last_written_at", 0) + settings.diary_cooldown_hours * 3600
        )
        candidates = self._candidates(state, scene, peers, relations, diary_ready, seen_peer_ids=seen_peer_ids)
        intention = state.get("active_intention")
        if intention and intention["action"] == "write" and not diary_ready:
            intention = None
        if recently_spoke and (
            social_signal["mood_delta"] <= -0.15
            or (isinstance(social_signal.get("speech_act"), Mapping) and social_signal["speech_act"].get("tone") == "tense")
        ) and intention and intention["drive"] != "rest":
            intention = None
        if intention and intention["kind"] not in ("visit", "seek_peer") and intention["space"] != sid:
            intention = None
        if intention and intention["kind"] in ("visit", "seek_peer"):
            dest_space = intention["target"] if intention["kind"] == "visit" else intention.get("target_space", "")
            if sid != dest_space:
                hop = f"{sid}:{dest_space}" if sid == scene["realm"] else f"{sid}:{scene['realm']}"
                if hop not in state["portals"]:
                    intention = None

        if pending_commitment is not None:
            cid = str(pending_commitment["id"])
            prev_cid = str(state.get("applied_commitment_id") or "")
            seen_cids = [str(x) for x in (state.get("applied_commitments") or ()) if x]
            for item_cid in (prev_cid, cid):
                if item_cid:
                    seen_cids = [x for x in seen_cids if x != item_cid]
                    seen_cids.append(item_cid)
            state["applied_commitments"] = seen_cids[-12:]
            arrival_in_progress = self._is_agreed_arrival(intention)
            new_arrival = self._is_agreed_arrival(pending_commitment)
            speech_action_in_progress = bool(
                intention and intention.get("source") == "speech_act" and intention.get("kind") in ("visit", "entity")
            )
            if arrival_in_progress:
                may_replace = new_arrival or pending_commitment.get("source") == "language"
            elif speech_action_in_progress:
                may_replace = pending_commitment.get("source") in ("language", "speech_act")
            else:
                may_replace = True
            going_to_rest = pending_commitment.get("drive") == "rest" and (
                new_arrival or pending_commitment.get("action") == "rest"
                or (local_rest and pending_commitment.get("joint_visit_id") == joint_id)
            )
            going_to_food = (
                pending_commitment.get("drive") == "food"
                or (
                    pending_commitment.get("kind") == "visit"
                    and self._place_is_food_venue(str(pending_commitment.get("target") or ""), state, scene)
                )
            )
            has_food_candidate = any(c["drive"] == "food" for c in candidates)
            speech_act_ok = (
                pending_commitment.get("source") == "speech_act"
                and vit["energy"] >= 0.25 and vit["sleep_pressure"] < 0.85
            )
            rest_ok = state["drives"]["rest"] < 0.82 or speech_act_ok or going_to_rest
            food_ok = (
                state["drives"]["food"] < 0.85
                or speech_act_ok
                or going_to_food
                or going_to_rest
                or not has_food_candidate
            )
            if may_replace and rest_ok and food_ok:
                resolved = self._resolve_commitment(state, scene, pending_commitment, peers, seen_peer_ids=seen_peer_ids)
                if resolved is not None:
                    low_urgency = intention is None or intention.get("drive") in ("explore", "curiosity", "comfort", "notice")
                    not_sleeping = not (
                        intention and intention.get("drive") == "rest"
                        and (actor.get("posture") == "resting" or (state["drives"]["rest"] >= 0.65 and not going_to_rest))
                    )
                    if (pending_commitment.get("priority") == "high" or low_urgency) and not_sleeping:
                        if joint_id and not resolved.get("joint_visit_id") and resolved["source"] in ("language", "speech_act"):
                            self.engine.finish_joint_visit(aid, joint_id, together=True)
                        if intention is not None and not low_urgency and intention.get("key") != resolved.get("key"):
                            state["suspended_intention"] = intention
                        intention = resolved
                        state["applied_commitment_id"] = cid
                        state["arbitration"] = "社会脑/语言主导" if resolved.get("source") in ("language", "speech_act") else "前额叶主导"

        shield = 0.65 * profile["effortful_control"] + 0.55 * profile["hysteresis_inertia"]
        committed_protected = bool(
            intention and intention.get("pfc_committed")
            and vit["energy"] >= 0.25 and vit["sleep_pressure"] < 0.85
        )
        if intention and intention["drive"] != "rest" and not committed_protected:
            eff_shield = shield
            if "engage" in scene and state.get("modulation") and state["drives"]["rest"] < 0.85:
                mood_bias = state["modulation"]
                eff_shield += mood_bias.get("persistence", 0) - mood_bias.get("relax", 0)
            emergency = next((c for c in candidates if c["drive"] == "rest" and c["utility"] >= 4.4 + eff_shield), None)
            if emergency:
                state["suspended_intention"] = intention
                intention = emergency
                state["reflection"] = "困倦加重，先找地方休息"
                state["arbitration"] = "生理稳态主导"

        if ("engage" in scene and intention is not None
                and intention["kind"] not in ("social", "seek_peer")
                and intention["drive"] not in ("rest", "food")
                and not intention.get("pfc_committed")
                and state["drives"]["rest"] < 0.72 and not vit["disengaged"]):
            van_threshold = 3.3 + 0.6 * shield * (0.6 + 0.6 * profile["social_avoidance"])
            near_social = next(
                (c for c in candidates
                 if c["kind"] == "social" and c["utility"] >= van_threshold
                 and max(abs(actor["x"] - c["x"]), abs(actor["y"] - c["y"])) <= (8 if sid != scene["realm"] else 5)),
                None,
            )
            if near_social:
                if intention["drive"] not in ("explore", "curiosity"):
                    state["suspended_intention"] = intention
                intention = near_social
                state["arbitration"] = "社会脑/语言主导"

        logs: list[str] = []
        picked = intention
        if actor.get("posture") == "resting" and (
            state["drives"]["rest"] >= 0.35 or now < state.get("agreed_rest_until", -1)
            or (local_rest and joint_visit.get("met"))
        ):
            goal, drive, thought = "休息一会儿", "rest", "再坐一会儿。"
            state["arbitration"] = "生理稳态主导"
        elif vit["disengaged"]:
            goal, drive, thought = "独自待一会儿", "calm", "想安静一下。"
            state["arbitration"] = "生理稳态主导"
        else:
            if picked is None and candidates:
                picked = candidates[0]
                reason = state.pop("reflection", "")
                if reason and use_llm:
                    picked = self._reflect(scene, state, candidates, reason)
                if scene.get("request_reflection"):
                    self._request_reflection(scene, state, candidates, reason, include_memories=settings.episodic_memory_enabled)
            elif (picked is not None and picked.get("source") != "speech_act"
                  and scene.get("request_reflection") and candidates):
                reason = state.get("reflection", "")
                if reason and not str(reason).startswith("完成了"):
                    state.pop("reflection", "")
                    self._request_reflection(scene, state, candidates, reason, include_memories=settings.episodic_memory_enabled)
            if picked:
                if picked.get("pfc_committed"):
                    state["arbitration"] = "社会脑/语言主导" if picked.get("source") in ("language", "speech_act") else "前额叶主导"
                elif picked["drive"] == "rest" and state["drives"]["rest"] >= 0.72:
                    state["arbitration"] = "生理稳态主导"
                elif picked["drive"] == "food" and state["drives"]["food"] >= 0.82:
                    state["arbitration"] = "生理稳态主导"
                previous_error = state.get("prediction_error")
                status, logs = self._execute(picked, state, scene, talk, candidates)
                goal, drive = picked["goal"], picked["drive"]
                if status == "progress" and picked.get("joint_visit_id") and picked["phase"] == "waiting":
                    name = joint_visit.get("entity_name")
                    goal = f"在{name + '旁' if name else space['name']}等{joint_visit['peer_name']}"
                elif status == "progress" and picked["phase"] == "dwell":
                    name = joint_visit.get("entity_name")
                    peer_name = joint_visit["peer_name"]
                    goal = f"和{peer_name}在{name}旁歇一会儿" if name else f"陪{peer_name}在{space['name']}待一会儿"
                thought = ""
                if status == "blocked":
                    state["blocked"][picked["key"]] = state["step"] + 10
                    if picked.get("pfc_committed") and state.get("modulation"):
                        state["modulation"]["strength"] = state["modulation"].get("strength", 1.0) * 0.65
                    error = state.get("prediction_error")
                    thought = error if error and error != previous_error else "这条路暂时走不通，换个目标。"
                    if picked.get("pfc_committed"):
                        state["reflection"] = thought
                if status in ("done", "blocked"):
                    if picked.get("joint_visit_id"):
                        if sid != picked["target"]:
                            self.engine.finish_joint_visit(aid, picked["joint_visit_id"], together=True)
                        suspended = state.get("suspended_intention") or {}
                        if suspended.get("joint_visit_id") == picked["joint_visit_id"]:
                            state.pop("suspended_intention", None)
                    intention = state.pop("suspended_intention", None) if drive in ("rest", "social") else None
                    if status == "done" and picked["kind"] != "exit":
                        state.setdefault("reflection", "完成了" + goal)
                else:
                    intention = picked
            else:
                goal, drive, thought = "四处观察", "explore", ""
        state["active_intention"] = intention
        if "engage" in scene:
            thought = state.pop("reflection_thought", "")
        state["drives"]["expression"] = 0.85 if state.get("experience") else 0.0
        self._update_satiation(state, now)
        target = str((picked or {}).get("target") or "")
        recent = list(aut.get("recent_targets", []))
        if logs and target:
            recent = [target, *(value for value in recent if value != target)][:5]
        is_resting = actor.get("posture") == "resting"
        emotion_hint = "" if is_resting else ("困倦" if drive == "rest" else "")
        next_aut = self.engine.update_actor_autonomy(
            aid, {
                "cognition": state, "current_goal": goal, "current_drive": drive,
                "last_thought": thought, "last_action_summary": "；".join(logs),
                "last_step_ts": round(time.time(), 3), "recent_targets": recent,
            },
            emotion_hint=emotion_hint,
            expected_realm=scene["realm"],
            persist="engage" not in scene,
        )
        result = {"actor_id": aid, "goal": next_aut["current_goal"], "drive": next_aut["current_drive"],
                  "thought": thought, "emotion": "熟睡" if is_resting else emotion_hint, "actions": logs}
        if "engage" in scene:
            result["cognition"] = cognitive_summary({"autonomy": next_aut})
        return result

    def _observe_position(
        self, state: dict[str, Any], scene: dict[str, Any], cells: list[tuple[int, int]],
    ) -> None:
        space, now = scene["space"], scene["minutes"]
        sid = space["space_id"]
        self._arrive(state, sid, space["name"], space["space_kind"], now)
        recent_blocks = list(state.get("recent_blocks") or [])
        for x, y in cells:
            block = f"{sid}:{x // 4}:{y // 4}"
            self._accumulate(state, "frontier", block, now, 1.0, cap=1.0)
            if not recent_blocks or recent_blocks[-1] != block:
                recent_blocks.append(block)
            zone = self.engine.get_zone_at(space, x, y)
            if zone:
                zid = zone["zone_id"]
                known = state["zones"].setdefault(f"{sid}:{zid}", {})
                if zid != state.get("last_zone"):
                    known.update(zone, space=sid, visits=known.get("visits", 0) + 1, last_visited=now, source="direct")
                    state["last_zone"] = zid
                else:
                    known["last_visited"] = now
        state["recent_blocks"] = recent_blocks[-8:]

    def _arrive(self, state: dict[str, Any], sid: str, name: str, kind: str, now: float) -> None:
        place = state["places"].setdefault(sid, {"name": name})
        previous = state.get("last_space")
        if not place.get("visits"):
            state["reflection"] = "初次来到" + name
            if kind == "building":
                self._experience(state, f"第一次走进了{name}。")
        place.update(name=name, kind=kind, source="direct", last_seen=now)
        if previous != sid:
            place["visits"] = place.get("visits", 0) + 1
            place["arrived"] = now
            if previous in state["places"]:
                state["places"][previous]["last_left"] = now
            state["last_space"] = sid
            state["last_zone"] = ""
        _rehearse(place, now)

    def _remember_affordance(
        self, place: dict[str, Any], domain: str, source: str, now: float, confidence: float,
    ) -> None:
        record = place.setdefault("affordances", {}).setdefault(domain, {})
        if source == "direct" or not record:
            record.update(source=source, confidence=confidence)
        _rehearse(record, now)

    def _observe_entities(self, state: dict[str, Any], scene: dict[str, Any], focus: set[str]) -> None:
        space, now = scene["space"], scene["minutes"]
        sid = space["space_id"]
        place = state["places"][sid]
        buildings = {b["interior_space_id"]: b for b in space.get("buildings", [])}
        seen_domains: set[str] = set()
        depleted_domains: set[str] = set()
        for ent in scene["visible"]:
            traits, eid = ent["traits"], ent["entity_id"]
            if eid in focus:
                self._accumulate(
                    state, "entity", eid, now,
                    0.12 * scene["percept"]["habituation_rate"] * scene.get("exposure_scale", 1.0),
                )
            target = traits.get("portal", {}).get("target_space_id")
            if target in scene["destinations"]:
                name = scene["destinations"][target]
                portal = state["portals"].setdefault(f"{sid}:{target}", {})
                portal.update(space=sid, target=target, name=name, x=ent["x"], y=ent["y"], entity=eid,
                              source="direct", confidence=1.0)
                _rehearse(portal, now)
                known = state["places"].setdefault(target, {"name": name, "source": "direct"})
                if not known.get("visits"):
                    known.update(name=name, source="direct")
                for domain in _building_hints(buildings[target]) if target in buildings else ():
                    self._remember_affordance(known, domain, "signboard", now, 0.65)
            verbs = self._affordances(ent, space)
            for domain, action in verbs:
                if domain == "reading" and action == "read":
                    continue
                seen_domains.add(domain)
                if domain not in ("comfort", "curiosity"):
                    self._remember_affordance(place, domain, "direct", now, 1.0)
            if "consumable" in traits and int(traits["consumable"].get("uses_left", 0)) <= 0:
                depleted_domains.add("food")
            if "activity" in traits:
                act_trait = traits["activity"]
                if act_trait.get("battery") is not None and float(act_trait["battery"]) <= 0:
                    depleted_domains.add(str(act_trait.get("domain") or ""))
        if space.get("space_kind") == "building":
            aff = place.get("affordances") or {}
            is_night = scene["lighting"].get("period") == "night"
            for dom in ("game", "food", "reading"):
                rec = aff.get(dom)
                if (
                    dom not in seen_domains
                    and (dom in depleted_domains or not is_night)
                    and isinstance(rec, dict)
                    and rec.get("source") == "direct"
                ):
                    rec["confidence"] = min(float(rec.get("confidence", 0.0)), 0.20)

    def _observe_peers(
        self, state: dict[str, Any], scene: dict[str, Any], peers: list[dict[str, Any]],
        seen_peer_ids: set[str] | None = None,
    ) -> None:
        space, actor, now = scene["space"], scene["actor"], scene["minutes"]
        sid = space["space_id"]
        is_large = space.get("space_kind") == "town" or max(int(space.get("width") or 0), int(space.get("height") or 0)) >= 24
        seen_set = {p["actor_id"] for p in peers} if seen_peer_ids is None else seen_peer_ids
        active_ids: set[str] = {p["actor_id"] for p in peers}
        for peer in peers:
            pid = peer["actor_id"]
            if pid not in seen_set:
                continue
            prev = state["peers"].get(pid) if isinstance(state["peers"].get(pid), Mapping) else {}
            state["peers"][pid] = {
                "actor_id": pid,
                "name": peer["name"],
                "host": peer.get("host_character") or peer["name"],
                "space": sid,
                "x": peer["x"],
                "y": peer["y"],
                "seen_at": now,
                "spoke_at": float(prev.get("spoke_at", -1e9)),
                "posture": peer["posture"],
            }
        for pid, rec in list(state["peers"].items()):
            if not isinstance(rec, dict) or rec.get("space") != sid or pid in active_ids:
                continue
            if not is_large or (
                "x" in rec and "y" in rec
                and max(abs(int(actor["x"]) - int(rec["x"])), abs(int(actor["y"]) - int(rec["y"]))) <= 12
            ):
                rec["space"] = ""

    def _ensure_portal_known(
        self, state: dict[str, Any], scene: dict[str, Any], place_id: str, source: str, now: float,
        *, partner: Mapping[str, Any] | None = None,
    ) -> None:
        realm = scene["realm"]
        places = scene.get("realm_places") or {}
        pname = places.get(place_id) or (state["places"].get(place_id) or {}).get("name") or place_id
        place = state["places"].setdefault(place_id, {"name": pname, "source": source})
        if place_id == realm:
            return
        key = f"{realm}:{place_id}"
        if key not in state["portals"]:
            if partner:
                p_cog = (partner.get("autonomy") or {}).get("cognition") or {}
                p_portal = (p_cog.get("portals") or {}).get(key)
                if isinstance(p_portal, Mapping) and "x" in p_portal and "y" in p_portal:
                    state["portals"][key] = {
                        **p_portal, "source": source,
                        "confidence": max(0.7, float(p_portal.get("confidence", 0.7))),
                        "samples": [now],
                    }
                    p_place = (p_cog.get("places") or {}).get(place_id) or {}
                    for domain, memory in (p_place.get("affordances") or {}).items():
                        if isinstance(memory, Mapping) and memory.get("confidence", 0) >= 0.4:
                            self._remember_affordance(place, domain, source, now, 0.7)
            if key not in state["portals"]:
                building = next(
                    (b for b in (scene.get("realm_buildings") or ()) if b.get("interior_space_id") == place_id),
                    None,
                )
                if building is not None:
                    bname = str(building.get("name") or pname)
                    place["name"] = bname
                    state["portals"][key] = {
                        "space": realm, "target": place_id, "name": bname,
                        "x": int(building["door_x"]), "y": int(building["door_y"]),
                        "source": source, "confidence": 0.75, "samples": [now],
                    }
                    for domain in _building_hints(building):
                        self._remember_affordance(place, domain, source, now, 0.7)

    def _apply_beliefs(
        self, state: dict[str, Any], scene: dict[str, Any], beliefs: Any, *,
        source: str, partner: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(beliefs, list):
            return
        now = scene["minutes"]
        realm_places = scene.get("realm_places") or {}
        for item in beliefs[:8]:
            if not isinstance(item, Mapping):
                continue
            pid = str(item.get("place_id") or "").strip()
            dom = str(item.get("domain") or "").strip()
            if not pid or pid not in realm_places:
                continue
            self._ensure_portal_known(state, scene, pid, source, now, partner=partner)
            if dom in _DOMAINS and pid in state["places"]:
                self._remember_affordance(state["places"][pid], dom, source, now, 0.72)

    def _apply_peer_beliefs(
        self, state: dict[str, Any], scene: dict[str, Any], peer_beliefs: Any, *, source: str,
    ) -> None:
        if not isinstance(peer_beliefs, list):
            return
        now = scene["minutes"]
        realm_places = scene.get("realm_places") or {}
        realm_actors = {a["actor_id"]: a for a in (scene.get("realm_actors") or ()) if a.get("actor_id")}
        for item in peer_beliefs[:6]:
            if not isinstance(item, Mapping):
                continue
            pid = str(item.get("actor_id") or "").strip()
            place_id = str(item.get("place_id") or "").strip()
            if not pid or pid in scene["player_ids"] or place_id not in realm_places:
                continue
            self._ensure_portal_known(state, scene, place_id, source, now)
            prev = state["peers"].get(pid) if isinstance(state["peers"].get(pid), Mapping) else {}
            row = realm_actors.get(pid) or {}
            name = str(prev.get("name") or row.get("name") or pid)
            host = str(prev.get("host") or row.get("host_character") or name)
            updated = {
                "actor_id": pid,
                "name": name,
                "host": host,
                "space": place_id,
                "seen_at": now,
                "spoke_at": float(prev.get("spoke_at", -1e9)),
                "posture": str(prev.get("posture") or "standing"),
                "source": source,
            }
            if prev.get("space") == place_id and "x" in prev and "y" in prev:
                updated["x"] = prev["x"]
                updated["y"] = prev["y"]
            state["peers"][pid] = updated

    def _resolve_commitment(
        self, state: dict[str, Any], scene: dict[str, Any], commitment: Mapping[str, Any], peers: list[dict[str, Any]],
        *, seen_peer_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        space, sid, now = scene["space"], scene["space"]["space_id"], scene["minutes"]
        kind = str(commitment.get("kind") or "").strip()
        target = str(commitment.get("target") or "").strip()
        drive = str(commitment.get("drive") or "").strip()
        source = str(commitment.get("source") or "pfc")
        reason = clip_text(commitment.get("reason"), 48)
        cid = str(commitment.get("id") or "")
        realm_places = scene.get("realm_places") or {}

        if kind == "visit" and target in realm_places:
            self._ensure_portal_known(state, scene, target, source, now)
            if sid != target:
                hop = f"{sid}:{target}" if sid == scene["realm"] else f"{sid}:{scene['realm']}"
                if hop not in state["portals"]:
                    return None
                portal = state["portals"][hop]
                px, py = portal["x"], portal["y"]
            else:
                px, py = space["width"] // 2, space["height"] // 2
            use_drive = drive if drive in _DOMAINS else "explore"
            out = {
                "kind": "visit", "key": f"visit:{sid}:{target}:", "target": target, "space": sid,
                "x": px, "y": py, "goal": reason or f"去「{realm_places[target]}」",
                "drive": use_drive, "utility": 8.0,
                "action": "escort" if commitment.get("action") == "escort" else "",
                "phase": "activity" if sid == target else "travel",
                "pfc_committed": True, "commitment_id": cid, "source": source,
            }
            if commitment.get("companion_id"):
                out["companion_id"] = str(commitment["companion_id"])
            if commitment.get("joint_visit_id"):
                out["joint_visit_id"] = str(commitment["joint_visit_id"])
            return out

        if kind == "social" and target not in scene["player_ids"]:
            seen_set = {p["actor_id"] for p in peers} if seen_peer_ids is None else seen_peer_ids
            same_peer = next((p for p in peers if p["actor_id"] == target and p["actor_id"] in seen_set), None)
            if same_peer is not None:
                return {
                    "kind": "social", "key": f"social:{sid}:{target}:", "target": target, "space": sid,
                    "x": same_peer["x"], "y": same_peer["y"],
                    "goal": reason or f"和{same_peer['name']}聊一聊",
                    "drive": "social", "utility": 8.0, "action": "", "phase": "approach",
                    "pfc_committed": True, "commitment_id": cid, "source": source,
                }
            known_rec = state["peers"].get(target) if isinstance(state["peers"].get(target), Mapping) else {}
            peer_space = str(known_rec.get("space") or "").strip()
            peer_name = str(known_rec.get("name") or "")
            if peer_space == sid:
                if "x" in known_rec and "y" in known_rec:
                    return {
                        "kind": "social", "key": f"social:{sid}:{target}:", "target": target, "space": sid,
                        "x": int(known_rec["x"]), "y": int(known_rec["y"]),
                        "goal": reason or f"去找{peer_name or '同伴'}",
                        "drive": "social", "utility": 8.0, "action": "", "phase": "approach",
                        "pfc_committed": True, "commitment_id": cid, "source": source,
                    }
                same_any = next((p for p in peers if p["actor_id"] == target), None)
                if same_any is not None and source in ("language", "speech_act"):
                    return {
                        "kind": "social", "key": f"social:{sid}:{target}:", "target": target, "space": sid,
                        "x": same_any["x"], "y": same_any["y"],
                        "goal": reason or f"去找{same_any['name']}",
                        "drive": "social", "utility": 8.0, "action": "", "phase": "approach",
                        "pfc_committed": True, "commitment_id": cid, "source": source,
                    }
            if not peer_space and source in ("language", "speech_act"):
                actor_row = next((a for a in (scene.get("realm_actors") or ()) if a["actor_id"] == target), None)
                if actor_row is not None:
                    peer_space = str(actor_row.get("space_id") or "")
                    peer_name = str(actor_row.get("name") or "")
                    if peer_space == sid:
                        return {
                            "kind": "social", "key": f"social:{sid}:{target}:", "target": target, "space": sid,
                            "x": int(actor_row["x"]), "y": int(actor_row["y"]),
                            "goal": reason or f"去找{peer_name or '同伴'}",
                            "drive": "social", "utility": 8.0, "action": "", "phase": "approach",
                            "pfc_committed": True, "commitment_id": cid, "source": source,
                        }
            if peer_space and peer_space != sid and peer_space in realm_places:
                if source in ("language", "speech_act"):
                    self._ensure_portal_known(state, scene, peer_space, source, now)
                hop = f"{sid}:{peer_space}" if sid == scene["realm"] else f"{sid}:{scene['realm']}"
                if hop in state["portals"] and (peer_space == scene["realm"] or sid == scene["realm"] or f"{scene['realm']}:{peer_space}" in state["portals"]):
                    portal = state["portals"][hop]
                    return {
                        "kind": "seek_peer", "key": f"seek_peer:{sid}:{target}:", "target": target,
                        "target_space": peer_space, "space": sid, "x": portal["x"], "y": portal["y"],
                        "goal": reason or f"去「{realm_places[peer_space]}」找{peer_name or '同伴'}",
                        "drive": "social", "utility": 8.0, "action": "", "phase": "travel",
                        "pfc_committed": True, "commitment_id": cid, "source": source,
                    }
            return None

        if kind == "entity":
            ent = next((e for e in scene["visible"] if e["entity_id"] == target), None)
            if ent is None:
                return None
            affordances = self._affordances(ent, space)
            if not affordances:
                return None
            picked_drive, picked_action = next(
                ((d, a) for d, a in affordances if d == drive or a == commitment.get("action")),
                affordances[0],
            )
            return {
                "kind": "entity", "key": f"entity:{sid}:{target}:{picked_action}",
                "target": target, "space": sid, "x": ent["x"], "y": ent["y"],
                "goal": reason or f"看看「{ent['name']}」",
                "drive": picked_drive, "utility": 8.0, "action": picked_action, "phase": "approach",
                "pfc_committed": True, "commitment_id": cid, "source": source,
            }
        return None

    def _read_directory(self, state: dict[str, Any], scene: dict[str, Any]) -> str:
        space, now = scene["space"], scene["minutes"]
        sid = space["space_id"]
        names = []
        for zone in space.get("zones", []):
            state["zones"].setdefault(f"{sid}:{zone['zone_id']}", {**zone, "space": sid, "source": "signboard"})
        for building in space.get("buildings", []):
            target, name = building["interior_space_id"], building["name"]
            if not target:
                continue
            portal = state["portals"].setdefault(f"{sid}:{target}", {
                "space": sid, "target": target, "name": name, "x": building["door_x"], "y": building["door_y"],
                "source": "signboard", "confidence": 0.7,
            })
            _rehearse(portal, now, interval=10)
            known = state["places"].setdefault(target, {"name": name, "source": "signboard"})
            for domain in _building_hints(building):
                self._remember_affordance(known, domain, "signboard", now, 0.65)
            names.append(name)
        return "导览上标出了" + "、".join(names) + "。" if names else ""

    @staticmethod
    def _affordances(entity: dict[str, Any], space: dict[str, Any]) -> list[tuple[str, str]]:
        traits = entity["traits"]
        affordances = []
        if traits.get("consumable", {}).get("uses_left", 0) > 0:
            affordances.append(("food", "consume"))
        if "restable" in traits and not traits["restable"]["occupied_by"]:
            affordances.append(("rest", "rest"))
        if "activity" in traits:
            activity = traits["activity"]
            if activity["battery"] is None or activity["battery"] > 0:
                affordances.append((activity["domain"], "interact"))
        if "inscribable" in traits:
            affordances.append(("notice" if _is_notice_board(entity, space) else "reading", "read"))
        if "container" in traits and not traits["container"]["is_open"] and not traits["container"]["is_locked"]:
            affordances.append(("curiosity", "open"))
        switch = traits.get("switchable") or {}
        if switch.get("provides_light") and not switch["is_on"]:
            affordances.append(("comfort", "turn_on"))
        return affordances

    def _update_drives(self, state: dict[str, Any], scene: dict[str, Any]) -> None:
        actor, percept, now = scene["actor"], scene["percept"], scene["minutes"]
        vit, aut, profile = actor["vitals"], actor["autonomy"], percept["behavior_profile"]
        night = (0.35 if percept["circadian_phase"] == "evening" else 0.65
                 if percept["circadian_phase"] == "deep_night" else 0.0)
        night *= percept.get("circadian_strength", 0)
        rest = max(vit["sleep_pressure"], 1 - vit["energy"]) + night * profile["safety_need"]
        base_social = profile["relatedness_need"] * profile["social_initiative"]
        if "engage" in scene or state["peers"]:
            solitude_min = max(0.0, now - float(state.get("last_social_at", now)))
            solitude_bonus = min(
                0.52,
                (solitude_min / 30.0)
                * (0.35 + 0.65 * profile["relatedness_need"])
                * (0.45 + 0.55 * (1.0 - profile["social_avoidance"])),
            )
            social = base_social + solitude_bonus
        else:
            social = base_social
        state["drives"] = {
            "rest": round(min(1.0, rest), 3),
            "food": aut["hunger"],
            "explore": round(0.55 * aut["curiosity"] + 0.45 * profile["exploration_drive"], 3),
            "social": round(min(1.0, social), 3),
            "expression": 0.85 if state.get("experience") else 0.0,
        }

    @staticmethod
    def _level(state: dict[str, Any], group: str, key: str, now: float) -> float:
        value, then = state["tracks"].get(f"{group}|{key}", (0.0, now))
        return value * 2 ** (-max(0, now - then) / _HALF_LIFE[group])

    def _accumulate(
        self, state: dict[str, Any], group: str, key: str, now: float, amount: float, *, cap: float = 3.0,
    ) -> None:
        state["tracks"][f"{group}|{key}"] = [min(cap, self._level(state, group, key, now) + amount), now]

    def _update_satiation(self, state: dict[str, Any], now: float) -> None:
        state["satiation"] = {
            domain: round(min(1.0, self._level(state, "action", domain, now) / 1.6), 3)
            for domain in _DOMAINS
        }
        tracks = state["tracks"]
        if len(tracks) > 720:
            state["tracks"] = dict(sorted(tracks.items(), key=lambda item: item[1][1])[-640:])

    def _candidates(
        self, state: dict[str, Any], scene: dict[str, Any], peers: list[dict[str, Any]],
        relations: dict[str, Any], allow_diary: bool, *, seen_peer_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        actor, space, now = scene["actor"], scene["space"], scene["minutes"]
        percept, sid = scene["percept"], scene["space"]["space_id"]
        profile, drives = percept["behavior_profile"], state["drives"]
        here = state["places"][sid]
        inside = sid != scene["realm"]
        seen_set = {p["actor_id"] for p in peers} if seen_peer_ids is None else seen_peer_ids
        seen_peers = [p for p in peers if p["actor_id"] in seen_set]
        results, fallback_results = [], []
        modulation = state.get("modulation") or {}
        vitals = actor["vitals"]
        in_crisis = min(vitals["energy"], 1 - vitals["sleep_pressure"]) <= 0.15
        bias_scale = 0.0
        if in_crisis:
            state["arbitration"] = "生理稳态主导"
        else:
            recent_exchange = (now - float(state.get("last_exchange_at", -1e9))) <= 8.0
            decay = (
                modulation.get("strength", 1.0)
                * 2 ** (-max(0, now - modulation["at"]) / modulation["half_life"])
            ) if modulation else 0.0
            if (modulation and modulation.get("source") == "language") or seen_peers or recent_exchange:
                arb = 1.35
                state["arbitration"] = "社会脑/语言主导"
            elif modulation and decay >= 0.2:
                arb = 1.10
                state["arbitration"] = "前额叶调节"
            else:
                arb = 0.85
                state["arbitration"] = "稳态/习惯"
            if modulation:
                base_control = 1.35 + 0.85 * profile["effortful_control"] + 0.45 * profile["hysteresis_inertia"]
                if vitals["energy"] < 0.35:
                    base_control *= max(0.35, vitals["energy"] / 0.35)
                bias_scale = base_control * arb * decay

        pref_zone = str(modulation.get("preferred_zone") or "").strip() if modulation else ""
        w_mod = weather.weather_cognitive_modifiers(scene.get("weather"), profile, is_outdoor=(not inside))
        sched_ctx = schedule.active_schedule_context(
            scene.get("schedule"),
            float(percept.get("bio_hour", 12.0)),
            circadian_strength=float(percept.get("circadian_strength", 0.75)),
            effortful_control=float(profile["effortful_control"]),
            arbitration=str(state.get("arbitration") or "稳态/习惯"),
            has_active_modulation=bool(modulation and bias_scale > 0),
            in_crisis=in_crisis,
        )
        scene["schedule_ctx"] = sched_ctx
        building_cats = {
            str(b.get("interior_space_id") or ""): str(b.get("category") or "").strip().lower()
            for b in (scene.get("realm_buildings") or ())
            if isinstance(b, Mapping) and b.get("interior_space_id")
        }
        building_roles = {
            str(b.get("interior_space_id") or ""): venue_semantics.building_role(b)
            for b in (scene.get("realm_buildings") or ())
            if isinstance(b, Mapping) and b.get("interior_space_id")
        }
        home_sid = str(scene.get("home_space_id") or sched_ctx.get("home_space_id") or "")

        def add(kind: str, target: str, drive: str, goal: str, x: int, y: int,
                practical: float, information: float, *, action: str = "", target_space: str = "",
                item_id: str = "") -> None:
            key = f"{kind}:{sid}:{target}:{action}"
            if key in state["blocked"]:
                return
            effort = (abs(actor["x"] - x) + abs(actor["y"] - y)) * (0.047 + 0.035 * drives["rest"])
            if "engage" in scene and kind in ("social", "seek_peer") and max(abs(actor["x"] - x), abs(actor["y"] - y)) > 10:
                effort = min(1.15, effort)
            elif "engage" in scene and not inside and kind == "visit":
                effort = min(1.30, effort)
            if kind in ("visit", "exit"):
                raw_inh = self._level(state, "place", target, now)
                inhibition = min(2.4, 1.45 * raw_inh) if "engage" in scene else 2.5 * raw_inh
            else:
                inhibition = 1.1 * self._level(state, "entity", target, now)
            if drive in ("rest", "food"):
                sat_weight = 0.35
            elif drive == "social" and "engage" in scene:
                sat_weight = 0.42
            else:
                sat_weight = 0.95
            raw_sat = sat_weight * self._level(state, "action", drive, now)
            satiation = min(1.65, raw_sat) if "engage" in scene else raw_sat
            utility = practical + (0.5 + 0.9 * percept["novelty_weight"]) * information - effort - inhibition - satiation
            utility += bias_scale * (modulation.get("domains", {}).get(drive, 0)
                                     + modulation.get("places", {}).get(target, 0))
            if pref_zone and bias_scale > 0:
                if kind == "explore" and target == f"zone:{pref_zone}":
                    utility += 0.45 * bias_scale
                elif sid == scene["realm"] and kind in ("visit", "explore"):
                    z_at = self.engine.get_zone_at(space, x, y)
                    if z_at and z_at.get("zone_id") == pref_zone:
                        utility += 0.45 * bias_scale
            impression = state.get("semantic", {}).get(target, {})
            if drive in impression.get("domains", []):
                utility += 0.18 * impression.get("salience", 0.0)
            utility += schedule.routine_candidate_bonus(
                sched_ctx,
                kind=kind,
                target=target,
                drive=drive,
                target_category=building_cats.get(target, ""),
                target_role=building_roles.get(target, ""),
                is_home_space=(bool(home_sid) and target == home_sid),
                is_inside_home=(bool(home_sid) and sid == home_sid),
            )
            if kind == "explore":
                utility += w_mod["outdoor_explore_delta"]
            elif kind == "visit":
                utility += w_mod["shelter_visit_bonus"]
            elif kind == "exit":
                utility += w_mod["exit_rain_penalty"]
            elif kind == "entity" and drive in ("reading", "game", "rest"):
                utility += w_mod["cozy_indoor_bonus"]
            item = {
                "kind": kind, "key": key, "target": target, "space": sid, "x": x, "y": y,
                "goal": goal, "drive": drive, "utility": round(utility, 4), "action": action,
                "phase": "travel" if kind in ("visit", "exit", "explore", "seek_peer") else "approach",
            }
            if target_space:
                item["target_space"] = target_space
            if item_id:
                item["item_id"] = item_id
            fallback_results.append(item)
            if utility > 0.10:
                results.append(item)

        rest = drives["rest"]
        travel_confidence = 1.0 - 0.55 * profile["safety_need"] * rest
        if pack := scene.get("pack"):
            items = pack["items"]
            food = [item for item in items if item["traits"].get("consumable", {}).get("uses_left", 0) > 0]
            held_id = actor.get("holding_id")
            nearby_food = any(ent["z"] >= 0 and ent["traits"].get("consumable", {}).get("uses_left", 0) > 0
                              and self.engine.chebyshev_distance(actor, ent) <= 1 for ent in scene["visible"])
            if food and drives["food"] >= 0.52 and not nearby_food:
                snack = food[0]
                warm_b = w_mod["warm_drink_bonus"] if (snack["traits"].get("thermal") or {}).get("temp") in ("hot", "warm") else 0.0
                add("pack_action", snack["entity_id"], "food", f"吃随身的「{snack['name']}」",
                    actor["x"], actor["y"], 1.0 + self._domain_value("food", state, scene) + warm_b, 0, action="consume")
            for carried in items:
                activity = carried["traits"].get("activity")
                if activity and activity["battery"] != 0:
                    domain = activity["domain"]
                    add("pack_action", carried["entity_id"], domain, f"用随身的「{carried['name']}」",
                        actor["x"], actor["y"], self._domain_value(domain, state, scene), 0.14, action="interact")
            if held_id and len(items) < pack["capacity"] and self.engine.can_stow(held_id):
                add("pack_action", held_id, "comfort", "收起手里的物品", actor["x"], actor["y"], 2.5, 0, action="stow")
            if not food and not held_id and len(items) < pack["capacity"] and drives["food"] < 0.52:
                snack = next((item for item in scene["visible"] if item["z"] >= 0
                              and item["traits"].get("consumable", {}).get("uses_left", 0) > 0
                              and item["traits"].get("consumable", {}).get("on_empty") == "vanish"
                              and self.engine.can_stow(item["entity_id"])), None)
                if snack:
                    add("pack_action", snack["entity_id"], "food", f"带上「{snack['name']}」",
                        snack["x"], snack["y"], 2.6, 0.1, action="pickup")
            if len(items) >= pack["capacity"]:
                stash = next((item for item in reversed(items) if item not in food and self.engine.can_stow(item["entity_id"])), None)
                if stash is None and len(food) > 1:
                    stash = food[-1]
                if stash:
                    for chest in scene["visible"]:
                        traits = chest["traits"].get("container")
                        if chest["z"] >= 0 and traits and not traits["is_locked"] and traits.get("owner_actor_id") in (
                            None, "", actor["actor_id"],
                        ):
                            add("pack_action", chest["entity_id"], "comfort", f"把「{stash['name']}」存进「{chest['name']}」",
                                chest["x"], chest["y"], 4.0, 0, action="deposit", item_id=stash["entity_id"])
            spare = food[-1:] if drives["food"] < 0.40 else food[1:]
            spare += [item for item in items if item["traits"].get("activity")
                      and sum(other["blueprint_id"] == item["blueprint_id"] for other in items) > 1]
            if spare and rest < 0.60 and now - state.get("last_gift_attempt", -1e9) >= 12:
                for peer in scene["actors"]:
                    pid = peer["actor_id"]
                    if pid == actor["actor_id"] or pid not in seen_set or peer["posture"] == "resting":
                        continue
                    if max(abs(peer["x"] - actor["x"]), abs(peer["y"] - actor["y"])) > 1:
                        continue
                    key = "user" if peer.get("is_player") else f"char:{peer.get('host_character') or peer['name']}"
                    edge = relations.get(key) or {}
                    if edge.get("attitude_stance") in ("warm", "friendly") and not edge.get("cool_off_turns"):
                        gift = spare[0]
                        add("gift", pid, "social", f"把「{gift['name']}」送给{peer['name']}", peer["x"], peer["y"],
                            3.1 + float(edge.get("warmth", 0.45)), 0, item_id=gift["entity_id"])
        for ent in scene["visible"]:
            eid = ent["entity_id"]
            board = _is_notice_board(ent, space)
            entries = ent["traits"].get("inscribable", {}).get("entries", [])
            for drive, action in self._affordances(ent, space):
                information = 0.15
                value = self._domain_value(drive, state, scene)
                if action == "consume" and (ent["traits"].get("thermal") or {}).get("temp") in ("hot", "warm"):
                    value += w_mod["warm_drink_bonus"]
                if action == "read":
                    read_this = state["read"].get(eid)
                    if read_this == self._read_signature(entries):
                        continue
                    information = 5.2 if board and read_this is None else (0.95 if board else 0.8)
                    value = 0.5 if board else 0.75
                if action == "turn_on":
                    if not scene["lighting"]["is_dark"]:
                        continue
                    public = space["space_kind"] == "town" or "public_utility" in ent.get("tags", [])
                    value = 1.7 - (3.5 * profile["norm_compliance"] if public else 0.0)
                    information = 0
                verb = {"rest": "在此休息", "read": "阅读", "consume": "尝尝", "interact": "使用",
                        "open": "打开", "turn_on": "点亮"}[action]
                add("entity", eid, drive, f"{verb}「{ent['name']}」", ent["x"], ent["y"], value, information, action=action)
            text = self._expression_text(state, scene)
            if allow_diary and text and ent["traits"].get("inscribable", {}).get("writable_by_ai"):
                if board and eid not in state["read"]:
                    continue
                if inscription_is_new(text, entries):
                    add("entity", eid, "expression", f"在「{ent['name']}」留一句话", ent["x"], ent["y"],
                        1.25 if board else 2.2, 0, action="write")

        for portal in state["portals"].values():
            target = portal["target"]
            if portal["space"] != sid or target == sid or portal["confidence"] < 0.25:
                continue
            if target == scene["realm"]:
                dwell = now - float(here.get("arrived", now))
                add("exit", target, "explore", "回到镇上", portal["x"], portal["y"],
                    2.90 + min(1.8, dwell / 12) if inside else 0.0, 0.55)
                continue
            place = state["places"].get(target, {})
            since_left = now - float(place.get("last_left", -1e9))
            affordances = {
                domain: self._domain_value(domain, state, scene) * memory["confidence"] * (
                    0.85 + 0.15 * (1 + math.tanh(_activation(memory["samples"], now, percept["memory_decay"]))) / 2
                ) for domain, memory in place.get("affordances", {}).items() if memory["confidence"] >= 0.3
            }
            drive = max(affordances, key=affordances.get, default="explore")
            if "engage" in scene and 0.0 <= since_left < 1.2 and not (
                (drive == "rest" and drives["rest"] >= 0.72) or (drive == "food" and drives["food"] >= 0.75)
            ):
                continue
            visited = place.get("visits", 0)
            novelty = 3.4 - 1.55 * bool(visited) - min(0.85, visited * 0.18)
            recall = 0.80 + 0.20 * (1 + math.tanh(_activation(portal["samples"], now, percept["memory_decay"]))) / 2
            add("visit", target, drive, f"去「{portal['name']}」", portal["x"], portal["y"],
                1.25 + 0.78 * affordances.get(drive, 0.7),
                novelty * travel_confidence * portal["confidence"] * recall)

        if not inside or not any(c["kind"] == "exit" for c in results):
            for x, y, target, goal, novelty in self._frontiers(state, scene):
                add("explore", target, "explore", goal, x, y, 0.8 + drives["explore"],
                    novelty * travel_confidence)

        if rest < 0.72 and not actor["vitals"]["disengaged"]:
            user_relation = relations.get("user", {})
            if (
                (drives["food"] < 0.90 or not any(c["drive"] == "food" and c["kind"] in ("entity", "pack_action") for c in fallback_results))
                and now - state.get("last_player_greeting", -1e9) >= 2
                and user_relation.get("attitude_stance") not in ("cold", "guarded")
                and not user_relation.get("cool_off_turns")
            ):
                for player in scene["actors"]:
                    pid = player["actor_id"]
                    if pid not in scene["player_ids"] or pid not in seen_set or player["posture"] == "resting":
                        continue
                    if max(abs(player["x"] - actor["x"]), abs(player["y"] - actor["y"])) <= 1:
                        add("player_greeting", pid, "social", "和眼前的玩家打招呼", player["x"], player["y"],
                            2.6 + 2 * drives["social"] + profile["social_initiative"], 0)
            for peer in peers:
                pid = peer["actor_id"]
                peer_rec = state["peers"].get(pid) if isinstance(state["peers"].get(pid), Mapping) else {}
                if "engage" in scene and now - float(peer_rec.get("spoke_at", -1e9)) < 2.5:
                    continue
                edge = relations.get(f"char:{peer.get('host_character') or peer['name']}", {})
                in_sight = pid in seen_set
                if in_sight:
                    if peer["posture"] == "resting":
                        continue
                elif peer_rec.get("posture") == "resting" and now - float(peer_rec.get("seen_at", -1e9)) < 25.0:
                    continue
                if edge.get("cool_off_turns") or edge.get("attitude_stance") == "cold":
                    continue
                distance = max(abs(actor["x"] - peer["x"]), abs(actor["y"] - peer["y"]))
                social_radius = max(space["width"], space["height"]) if (inside and "engage" in scene) else (10 if "engage" in scene else 3)
                distant_seek = distance > social_radius
                if distant_seek:
                    if not ("engage" in scene and not inside and (edge.get("known") or pid in state["peers"]) and drives["social"] >= 0.30):
                        continue
                first_meeting = not edge.get("known")
                greeting = 1.65 if "engage" in scene and first_meeting and distance <= 4 else 0.0
                warmth = float(edge.get("warmth", 0.4))
                if distant_seek:
                    presence = (1.55 + 1.10 * warmth) * (0.55 + 0.45 * (1.0 - profile["social_avoidance"]))
                    goal_txt = f"去找{peer['name']}聊一聊"
                else:
                    presence = (
                        (2.35 if first_meeting else 1.65 + 1.15 * warmth)
                        * (0.55 + 0.45 * (1.0 - profile["social_avoidance"]))
                        + (0.65 if inside else 0.0)
                    )
                    goal_txt = f"和{peer['name']}聊一聊"
                add("social", pid, "social", goal_txt, peer["x"], peer["y"],
                    1.0 + greeting + 3.3 * drives["social"] + presence, 0.6 if first_meeting else 0.15)

        if "engage" in scene and rest < 0.68 and not actor["vitals"]["disengaged"] and drives["social"] >= 0.35:
            realm_places = scene.get("realm_places") or {}
            for peer_id, peer_rec in state["peers"].items():
                if not isinstance(peer_rec, Mapping):
                    continue
                p_space = str(peer_rec.get("space") or "").strip()
                if not p_space or p_space == sid or peer_rec.get("posture") == "resting":
                    continue
                if now - float(peer_rec.get("seen_at", -1e9)) > 60.0 or now - float(peer_rec.get("spoke_at", -1e9)) < 8.0:
                    continue
                edge = relations.get(f"char:{peer_rec.get('host') or peer_rec.get('name')}", {})
                if edge.get("cool_off_turns") or edge.get("attitude_stance") == "cold":
                    continue
                hop_key = f"{sid}:{p_space}" if sid == scene["realm"] else f"{sid}:{scene['realm']}"
                portal = state["portals"].get(hop_key)
                if not portal or portal.get("confidence", 0) < 0.25:
                    continue
                if sid != scene["realm"] and p_space != scene["realm"] and f"{scene['realm']}:{p_space}" not in state["portals"]:
                    continue
                warmth = float(edge.get("warmth", 0.45))
                p_space_name = (state["places"].get(p_space) or {}).get("name") or realm_places.get(p_space) or p_space
                add("seek_peer", peer_id, "social", f"去「{p_space_name}」找{peer_rec['name']}",
                    portal["x"], portal["y"],
                    1.65 + 4.0 * drives["social"] + warmth * (0.85 + 0.65 * (1.0 - profile["social_avoidance"])),
                    0.45, target_space=p_space)
        chosen = results or fallback_results
        return sorted(chosen, key=lambda c: (-c["utility"], c["key"]))

    def _domain_value(self, domain: str, state: dict[str, Any], scene: dict[str, Any]) -> float:
        drives = state["drives"]
        profile = scene["percept"]["behavior_profile"]
        vitals = scene["actor"]["vitals"]
        preference = 0.45 if domain in vitals.get("preferred_domains", []) else 0.0
        rest = drives["rest"]
        if domain == "rest":
            return 0.1 + (4.7 + 2.0 * profile["safety_need"]) * rest ** 2.1
        if domain == "food":
            return 0.7 + 5.2 * drives["food"] ** 2.0
        if domain == "game":
            return 0.3 + 3.5 * profile["domain_efficacy_game"] * profile["competence_need"] + preference - 0.7 * rest
        if domain == "social":
            return 0.6 + 4.1 * drives["social"] - 0.7 * rest
        if domain == "reading":
            return 0.8 + 1.5 * profile["domain_efficacy_non_game"] + 0.45 * profile["safety_need"] + preference
        return 0.7 + 0.7 * drives["explore"] + preference

    def _frontiers(self, state: dict[str, Any], scene: dict[str, Any]) -> list[tuple[int, int, str, str, float]]:
        actor, space, now = scene["actor"], scene["space"], scene["minutes"]
        sid, ax, ay = space["space_id"], actor["x"], actor["y"]
        recent_blocks = list(state.get("recent_blocks") or [])
        cur_zone = (self.engine.get_zone_at(space, ax, ay) or {}).get("zone_id", "")
        frontiers = []
        offsets = ((0, 8), (8, 0), (0, -8), (-8, 0), (6, 6), (-6, 6), (6, -6), (-6, -6))
        if "engage" in scene and space.get("space_kind") == "town":
            offsets = (*offsets, (0, 12), (12, 0), (0, -12), (-12, 0))
        for dx, dy in offsets:
            x = max(0, min(space["width"] - 1, ax + dx))
            y = max(space["wall_rows"], min(space["height"] - 1, ay + dy))
            if max(abs(x - ax), abs(y - ay)) < 4:
                continue
            block = f"{sid}:{x // 4}:{y // 4}"
            familiarity = self._level(state, "frontier", block, now)
            raw_novelty = 2.28 * (1.0 - familiarity)
            if "engage" in scene:
                ior = 0.95 if block in recent_blocks[-3:] else (0.45 if block in recent_blocks[-6:-3] else 0.0)
                novelty = max(0.45, raw_novelty) - ior
            else:
                novelty = raw_novelty
            frontiers.append((x, y, f"{x}:{y}", "沿陌生街道走一走", novelty))
        for zone in state["zones"].values():
            if zone["space"] != sid:
                continue
            x, y = zone["x"] + zone["w"] // 2, zone["y"] + zone["h"] // 2
            if not zone.get("visits"):
                frontiers.append((x, y, f"zone:{zone['zone_id']}", f"去{zone['name']}走走", 2.58))
            elif "engage" in scene and zone.get("zone_id") != cur_zone:
                idle_min = max(0.0, now - float(zone.get("last_visited", 0.0)))
                if idle_min >= 0.35:
                    frontiers.append((x, y, f"zone:{zone['zone_id']}", f"去{zone['name']}转转", round(0.72 + min(1.25, idle_min / 12.0), 3)))
        return frontiers

    def _cross_portal(
        self, state: dict[str, Any], scene: dict[str, Any], destination: str,
    ) -> tuple[str, list[str]]:
        actor, now = scene["actor"], scene["minutes"]
        sid, aid = actor["space_id"], actor["actor_id"]
        portal = state["portals"].get(f"{sid}:{destination}")
        if not portal:
            return "blocked", []
        status, logs = self._walk(scene, state, portal["x"], portal["y"], 1, cardinal=True)
        if status != "ready":
            if status == "blocked":
                door_busy = any(
                    p["actor_id"] != aid and max(abs(p["x"] - portal["x"]), abs(p["y"] - portal["y"])) <= 1
                    for p in scene["actors"]
                )
                if door_busy:
                    return "progress", []
                self._prediction_error(state, portal, f"走不到{portal['name']}的门前。")
            return status, logs
        door = next(
            (e for e in scene["visible"]
             if e["entity_id"] == portal.get("entity")
             or e.get("traits", {}).get("portal", {}).get("target_space_id") == destination),
            None,
        )
        if door is None:
            self._prediction_error(state, portal, f"没在记忆的位置找到通往{portal['name']}的门。")
            return "blocked", []
        ok, line = self.engine.cognitive_act(aid, sid, door["entity_id"], "enter")
        if not ok:
            self._prediction_error(state, portal, f"暂时进不了{portal['name']}。")
            return "blocked", [line] if line else []
        self._arrive(state, destination, portal["name"], "town" if destination == scene["realm"] else "building", now)
        return "entered", [line]

    def _execute(
        self, intention: dict[str, Any], state: dict[str, Any], scene: dict[str, Any],
        talk: Callable[[str, str, str], list[str]], candidates: list[dict[str, Any]],
    ) -> tuple[str, list[str]]:
        kind, target, drive = intention["kind"], intention["target"], intention["drive"]
        actor, space, now = scene["actor"], scene["space"], scene["minutes"]
        sid = space["space_id"]
        if kind == "player_greeting":
            state["last_player_greeting"] = now
            logs = talk(actor["actor_id"], target, intention["goal"])
            return ("done" if logs else "blocked"), logs
        if kind == "gift":
            state["last_gift_attempt"] = now
            try:
                result = self.engine.give_item(actor["actor_id"], target, intention["item_id"],
                                               custom_persona_dir=scene.get("persona_dir", ""))
            except ValueError:
                return "blocked", []
            return "done", [result["message"]]
        if kind == "pack_action":
            operation = intention["action"]
            aid = actor["actor_id"]
            args = {"entity_id": target}
            if operation in ("pickup", "deposit"):
                status, logs = self._walk(scene, state, intention["x"], intention["y"], 1)
                if status != "ready":
                    return status, logs
                if operation == "deposit":
                    chest = next((e for e in scene["visible"] if e["entity_id"] == target), None)
                    if not chest:
                        return "blocked", []
                    if not chest["traits"]["container"]["is_open"]:
                        self.engine.execute_act(aid, target, "open")
                    args = {"entity_id": intention["item_id"], "chest_id": target}
            elif operation in ("consume", "interact"):
                args["verb"] = operation
                if engage := scene.get("engage"):
                    if not engage(target, operation, drive, scene["percept"]["behavior_profile"]):
                        return "progress", []
                operation = "use"
            try:
                message = self.engine.inventory_action(aid, operation, **args)
            except ValueError:
                return "blocked", []
            self._accumulate(state, "action", drive, now, 0.25)
            return "done", [message]
        if kind == "explore":
            x, y = intention["x"], intention["y"]
            region = (x // 4 * 4, y // 4 * 4, x // 4 * 4 + 3, y // 4 * 4 + 3)
            status, logs = self._walk(scene, state, x, y, 2, region=region)
            if status == "ready":
                self._accumulate(state, "action", "explore", now, 0.35)
                return "done", logs
            return status, logs
        if kind == "seek_peer":
            seen_set = scene.get("seen_peer_ids")
            seen_peer = next(
                (
                    p for p in scene["actors"]
                    if p["actor_id"] == target and (seen_set is None or p["actor_id"] in seen_set)
                ),
                None,
            )
            if seen_peer is not None:
                intention["kind"] = "social"
                intention["target_space"] = sid
                intention["x"] = seen_peer["x"]
                intention["y"] = seen_peer["y"]
                intention["phase"] = "approach"
                return self._talk_to_peer(target, intention["goal"], state, scene, talk)
            target_space = str(intention.get("target_space") or "")
            if sid != target_space:
                destination = scene["realm"] if (sid != scene["realm"] and target_space != scene["realm"]) else target_space
                status, logs = self._cross_portal(state, scene, destination)
                return ("progress", logs) if status == "entered" else (status, logs)
            peer = next((p for p in scene["actors"] if p["actor_id"] == target), None)
            if peer is None:
                peer_rec = state["peers"].get(target)
                if isinstance(peer_rec, dict):
                    peer_rec["space"] = ""
                pname = (peer_rec or {}).get("name") or "同伴"
                message = f"到了这里，{pname}已经不在这里了。"
                state["prediction_error"] = message
                state["reflection"] = message
                return "blocked", [message]
            return self._talk_to_peer(target, intention["goal"], state, scene, talk)
        if kind == "visit" and intention["action"] == "escort":
            if sid == target:
                return "done", []
            if sid == scene["realm"]:
                return self._escort_to_door(intention, state, scene)
        if kind == "visit" and sid == target:
            if intention.get("joint_visit_id"):
                with self.engine._lock:
                    status, logs = self._joint_visit_activity(intention, state, scene, talk)
                    if status in ("done", "blocked"):
                        self.engine.finish_joint_visit(
                            actor["actor_id"], intention["joint_visit_id"], together=drive == "social",
                        )
                    return status, logs
            if drive == "social":
                peer_goal = next((c for c in candidates if c["kind"] == "social" and c["utility"] >= 2.0), None)
                if peer_goal:
                    return self._talk_to_peer(peer_goal["target"], peer_goal["goal"], state, scene, talk)
            return self._visit_activity(intention, state, scene)
        if kind in ("visit", "exit"):
            destination = scene["realm"] if (sid != scene["realm"] and target != sid) else target
            status, logs = self._cross_portal(state, scene, destination)
            if status != "entered":
                return status, logs
            if kind == "visit" and destination == target:
                if target == scene["realm"] and not intention.get("joint_visit_id"):
                    return "done", logs
                intention["phase"] = "activity"
                self._accumulate(state, "place", target, now, 1.0)
                return "progress", logs
            return ("done" if kind == "exit" else "progress"), logs

        if kind == "social":
            return self._talk_to_peer(target, intention["goal"], state, scene, talk)

        entity = next((e for e in scene["visible"] if e["entity_id"] == target), None)
        if entity is None:
            return "blocked", []
        return self._use_entity(state, scene, entity, drive, intention["action"])

    def _joint_visit_activity(
        self, intention: dict[str, Any], state: dict[str, Any], scene: dict[str, Any],
        talk: Callable[[str, str, str], list[str]],
    ) -> tuple[str, list[str]]:
        actor, space = scene["actor"], scene["space"]
        aid, peer_id = actor["actor_id"], intention["companion_id"]
        if scene["joint_visit"].get("done"):
            intention["phase"] = "dwell"
            return "progress", []
        entity = scene.get("joint_entity")
        sitter_id = scene["joint_visit"].get("seated_id")
        same_tile_as_peer = entity and aid > peer_id and any(
            person["actor_id"] == peer_id and (person["x"], person["y"]) == (actor["x"], actor["y"])
            for person in scene["actors"]
        )
        on_seat = entity and sitter_id and aid != sitter_id and (actor["x"], actor["y"]) == (entity["x"], entity["y"])
        if on_seat or same_tile_as_peer:
            for dx, dy in ((0, 1), (1, 0), (-1, 0), (0, -1)):
                x, y = actor["x"] + dx, actor["y"] + dy
                if self.engine.chebyshev_distance({**actor, "x": x, "y": y}, entity) > 1:
                    continue
                status, logs = self._walk(scene, state, x, y, 0, avoid_actors=True)
                if status == "progress":
                    return status, logs
        if entity and self.engine.chebyshev_distance(actor, entity) > 1:
            intention["phase"] = "approach"
            status, logs = self._walk(scene, state, entity["x"], entity["y"], 1, avoid_actors=True)
            if status == "progress":
                intention.pop("waiting_since", None)
                return status, logs
            if status == "blocked":
                blocked_since = intention.setdefault("waiting_since", scene["minutes"])
                if scene["minutes"] - blocked_since >= scene["joint_wait_minutes"]:
                    return "blocked", [f"暂时走不到「{entity['name']}」旁。"]
                return "progress", []
        peer = next(
            (person for person in scene["actors"] if person["actor_id"] == peer_id
             and peer_id in scene["seen_peer_ids"] and person["posture"] != "resting"),
            None,
        )
        if not self.engine.confirm_joint_visit(
            aid, intention["joint_visit_id"], activity_minutes=(
                (0.25 if "engage" in scene else scene["joint_activity_minutes"] / 2)
                if entity else scene["joint_activity_minutes"]
            ),
        ):
            intention["phase"] = "waiting"
            waiting_since = intention.setdefault("waiting_since", scene["minutes"])
            if scene["minutes"] - waiting_since >= scene["joint_wait_minutes"]:
                return "done", [f"在{space['name']}等了一会儿，和同伴还没能汇合。"]
            if entity:
                return "progress", []
            if peer and aid < peer_id:
                _, logs = self._walk(scene, state, peer["x"], peer["y"], 1)
                return "progress", logs
            if peer is None:
                portal = state["portals"].get(f"{space['space_id']}:{scene['realm']}")
                if portal:
                    x, y = portal["x"], portal["y"]
                else:
                    x, y = space["width"] // 2, space["height"] // 2
                _, logs = self._walk(scene, state, x, y, 2)
                return "progress", logs
            return "progress", []

        if entity:
            intention["phase"] = "dwell"
            return "progress", []
        intention["phase"] = "activity"
        if intention["drive"] != "social":
            return self._visit_activity(intention, state, scene)
        if aid > peer_id or peer is None:
            intention["phase"] = "waiting"
            return "progress", []
        status, logs = self._walk(scene, state, peer["x"], peer["y"], 1)
        if status != "ready":
            return ("progress", logs) if status == "blocked" else (status, logs)
        reason = f"按约和{peer['name']}在{space['name']}汇合，想聊几句"
        logs = scene.get("rendezvous_talk", talk)(aid, peer_id, reason)
        if not logs:
            return "progress", []
        state["last_social_at"] = scene["minutes"]
        self._reward(state, peer_id, "social", scene["minutes"])
        return "done", logs

    def _talk_to_peer(
        self, target: str, goal: str, state: dict[str, Any], scene: dict[str, Any],
        talk: Callable[[str, str, str], list[str]],
    ) -> tuple[str, list[str]]:
        peer = next((p for p in scene["actors"] if p["actor_id"] == target), None)
        if peer is None:
            return "blocked", []
        status, logs = self._walk(scene, state, peer["x"], peer["y"], 1)
        if status != "ready":
            return status, logs
        if peer.get("posture") == "resting":
            now = scene["minutes"]
            prev = state["peers"].get(target) if isinstance(state["peers"].get(target), Mapping) else {}
            state["peers"][target] = {
                "actor_id": target,
                "name": peer["name"],
                "host": peer.get("host_character") or peer["name"],
                "space": scene["space"]["space_id"],
                "x": peer["x"],
                "y": peer["y"],
                "seen_at": now,
                "spoke_at": float(prev.get("spoke_at", -1e9)),
                "posture": "resting",
            }
            note = f"走近看了一眼，{peer['name']}正在休息，便安静地没有打扰。"
            self._experience(state, note)
            state["reflection"] = f"看到{peer['name']}正在休息，便没有打扰"
            state["last_social_at"] = now
            self._accumulate(state, "entity", target, now, 0.85)
            return "done", [note]
        logs = talk(scene["actor"]["actor_id"], target, goal)
        if not logs:
            return "blocked", []
        if "engage" in scene:
            return "done", logs
        now = scene["minutes"]
        self._experience(state, f"和{peer['name']}聊了几句。")
        self._share_place(state, peer, now, scene["realm"])
        state["reflection"] = "与同伴交流后"
        self._reward(state, target, "social", now)
        return "done", logs

    @staticmethod
    def _is_agreed_arrival(plan: Mapping[str, Any] | None) -> bool:
        return bool(
            plan and plan.get("source") == "speech_act" and plan.get("kind") == "visit"
            and not plan.get("joint_visit_id") and (plan.get("drive") == "rest" or plan.get("action") == "escort")
        )

    @staticmethod
    def _place_is_lodging(target: str, scene: Mapping[str, Any]) -> bool:
        if not target:
            return False
        if target == str(scene.get("home_space_id") or ""):
            return True
        for b in scene.get("realm_buildings") or ():
            if isinstance(b, Mapping) and str(b.get("interior_space_id") or "") == target:
                return venue_semantics.is_lodging_building(b)
        space_name = (scene.get("space") or {}).get("name") if (scene.get("space") or {}).get("space_id") == target else ""
        name = str((scene.get("realm_places") or {}).get(target) or space_name or "")
        return venue_semantics.is_lodging_building(None, name=name)

    @staticmethod
    def _place_is_food_venue(target: str, state: Mapping[str, Any], scene: Mapping[str, Any]) -> bool:
        if not target:
            return False
        for b in scene.get("realm_buildings") or ():
            if isinstance(b, Mapping) and str(b.get("interior_space_id") or "") == target:
                if venue_semantics.is_food_building(b):
                    return True
        if "food" in ((state.get("places") or {}).get(target) or {}).get("affordances", {}):
            return True
        name = str((scene.get("realm_places") or {}).get(target) or "")
        return venue_semantics.is_food_building(None, name=name)

    def _escort_to_door(
        self, intention: dict[str, Any], state: dict[str, Any], scene: dict[str, Any],
    ) -> tuple[str, list[str]]:
        target = intention["target"]
        portal = state["portals"].get(f"{scene['realm']}:{target}")
        if portal is None:
            return "blocked", []
        x, y = portal["x"], portal["y"]
        status, logs = self._walk(scene, state, x, y, 2, cardinal=True)
        if status != "ready":
            return status, logs
        companion = next(
            (a for a in scene["realm_actors"] if a["actor_id"] == intention.get("companion_id")), None,
        )
        if companion is None or companion["space_id"] == target:
            return "done", []
        deadline = intention.setdefault("door_wait_until", scene["minutes"] + 1)
        if scene["minutes"] >= deadline:
            return "done", []
        actor = scene["actor"]
        dx, dy = actor["x"] - x, actor["y"] - y
        if abs(dx) + abs(dy) == 1:
            # Leave the tile immediately in front of the portal free for the companion.
            for sx, sy in ((dx, dy), (-dy, dx), (dy, -dx)):
                status, logs = self._walk(scene, state, actor["x"] + sx, actor["y"] + sy, 0)
                if status == "progress":
                    return status, logs
        return "progress", []

    def _visit_activity(
        self, intention: dict[str, Any], state: dict[str, Any], scene: dict[str, Any],
    ) -> tuple[str, list[str]]:
        drive = intention["drive"]
        place = state["places"][intention["target"]]
        expected = place.get("affordances", {}).get(drive)
        options = [
            (e, action) for e in scene["visible"] for domain, action in self._affordances(e, scene["space"])
            if domain == drive and action != "read"
        ]
        agreed_rest = (
            drive == "rest"
            and self._is_agreed_arrival(intention)
            and self._place_is_lodging(str(intention.get("target") or ""), scene)
        )
        if agreed_rest:
            beds = [(e, action) for e, action in options if "bedroom" in e.get("tags", ())]
            if beds:
                options = beds
            else:
                status, logs = self._look_for_bed(intention, state, scene)
                if status == "progress":
                    return status, logs
        if drive == "social":
            options = [(e, action) for e in scene["visible"] for domain, action in self._affordances(e, scene["space"])
                       if domain == "food"]
        if options:
            actor = scene["actor"]
            entity, action = min(options, key=lambda item: self.engine.chebyshev_distance(actor, item[0]))
            status, logs = self._use_entity(state, scene, entity, "food" if drive == "social" else drive, action)
            if agreed_rest and status == "done":
                state["agreed_rest_until"] = scene["minutes"] + 3
            return status, logs
        if expected and drive != "social":
            x = scene["space"]["width"] // 2
            y = scene["space"]["height"] // 2
            status, logs = self._walk(scene, state, x, y, 2)
            if status == "progress":
                return status, logs
            message = f"到了{place['name']}，没有找到可用的{_DOMAINS.get(drive, drive)}设施。"
            self._prediction_error(state, expected, message)
            expected["confidence"] = min(float(expected.get("confidence", 0.0)), 0.20)
            return "done", [message]
        return "done", []

    def _look_for_bed(
        self, intention: dict[str, Any], state: dict[str, Any], scene: dict[str, Any],
    ) -> tuple[str, list[str]]:
        space = scene["space"]
        w, h = space["width"], space["height"]
        # Check both sides beyond the entrance without assuming furniture locations.
        waypoints = ((w // 2, h // 2), (w // 4, h // 4), (w * 3 // 4, h // 4),
                     (w * 3 // 4, h * 3 // 4), (w // 4, h * 3 // 4))
        index = intention.get("bed_search_index", 0)
        while index < len(waypoints):
            x, y = waypoints[index]
            status, logs = self._walk(scene, state, x, max(y, space["wall_rows"]), 1)
            if status == "progress":
                return status, logs
            index += 1
            intention["bed_search_index"] = index
        return "done", []

    def _use_entity(
        self, state: dict[str, Any], scene: dict[str, Any], entity: dict[str, Any], drive: str, action: str,
    ) -> tuple[str, list[str]]:
        actor, space, now = scene["actor"], scene["space"], scene["minutes"]
        eid = entity["entity_id"]
        if entity.get("parent_id") != f"actor:{actor['actor_id']}" and self.engine.chebyshev_distance(actor, entity) > 1:
            return self._walk(scene, state, entity["x"], entity["y"], 1)
        entries = entity["traits"].get("inscribable", {}).get("entries", [])
        line = self._expression_text(state, scene) if action == "write" else ""
        if action == "write" and not inscription_is_new(line, entries):
            return "blocked", []
        engage = scene.get("engage")
        if engage and not engage(eid, action, drive, scene["percept"]["behavior_profile"]):
            return "progress", []
        ok, feedback = self.engine.cognitive_act(actor["actor_id"], space["space_id"], eid, action, text=line)
        if not ok:
            return "blocked", [feedback] if feedback else []
        if action == "read":
            state["read"][eid] = self._read_signature(entries)
            if _is_notice_board(entity, space):
                directory = self._read_directory(state, scene)
                if directory:
                    feedback += " " + directory
        elif action == "open":
            self._experience(state, f"打开了{entity['name']}，看了看里面。")
        elif action == "write":
            state.pop("experience", None)
            state["last_written_at"] = time.time()
            if state.get("modulation"):
                state["modulation"]["diary"] = ""
        self._reward(state, eid, drive, now)
        return "done", [feedback]

    def _walk(
        self, scene: dict[str, Any], state: dict[str, Any], x: int, y: int, reach: int,
        *, cardinal: bool = False, region: tuple[int, int, int, int] | None = None, avoid_actors: bool = False,
    ) -> tuple[str, list[str]]:
        actor = scene["actor"]
        move = scene.get("stage_walk", self.engine.move_actor_toward)
        path = move(
            actor["actor_id"], actor["space_id"], x, y, reach=reach, stride=scene.get("stride", _STRIDE),
            cardinal=cardinal, region=region, avoid_actors=avoid_actors,
        )
        if not path:
            return "blocked", []
        if len(path) == 1:
            return "ready", []
        if "stage_walk" not in scene:
            self._observe_position(state, scene, path)
        nx, ny = path[-1]
        return "progress", [f"走了{len(path) - 1}格，来到 ({nx}, {ny})。"]

    def _reward(self, state: dict[str, Any], target: str, drive: str, now: float) -> None:
        self._accumulate(state, "entity", target, now, 0.85)
        self._accumulate(state, "action", drive, now, 0.90)

    @staticmethod
    def _experience(state: dict[str, Any], text: str) -> None:
        state["experience"] = text

    @staticmethod
    def _expression_text(state: dict[str, Any], scene: dict[str, Any]) -> str:
        if not state.get("experience"):
            return ""
        if "engage" in scene:
            packet = state.get("modulation") or {}
            return packet.get("diary", "") if packet.get("experience") == state["experience"] else ""
        return state["experience"]

    def _request_reflection(
        self, scene: dict[str, Any], state: dict[str, Any], candidates: list[dict[str, Any]], reason: str,
        *, include_memories: bool,
    ) -> None:
        actor = scene["actor"]
        vitals = actor["vitals"]
        if vitals["energy"] < 0.25 or vitals["sleep_pressure"] > 0.85 or vitals["disengaged"]:
            return
        near_tie = len(candidates) >= 2 and any(
            c["drive"] != candidates[0]["drive"] and candidates[0]["utility"] - c["utility"] < 0.35
            for c in candidates[1:5]
        )
        unfamiliar = {
            e["entity_id"]: e["name"] for e in scene["percept"]["focused_entities"]
            if e["entity_id"] not in state.get("semantic", {}) and not str(e.get("blueprint_id") or "").startswith("default_")
        }
        modulation = state.get("modulation") or {}
        agenda_expired = not modulation or scene["minutes"] - modulation["at"] >= modulation["half_life"]
        notable = bool(reason and not reason.startswith("完成了"))
        if not (notable or near_tie or unfamiliar or agenda_expired):
            return
        space = scene["space"]
        memories = (
            episodic_memory.recall(state, space["space_id"], scene.get("seen_peer_ids", set()), query=reason)
            if include_memories else []
        )
        active = state.get("active_intention") or {}
        brief_zones = {
            z["zone_id"]: z["name"]
            for z in (*(space.get("zones") or ()), *state["zones"].values())
            if isinstance(z, Mapping) and z.get("zone_id") and z.get("name")
        }
        same_space_ids = {p["actor_id"] for p in scene["actors"] if p["actor_id"] != actor["actor_id"]}
        brief_peers = {}
        for pid, prec in state["peers"].items():
            if not isinstance(prec, Mapping):
                continue
            loc = str(prec.get("space") or "")
            if not loc and pid not in same_space_ids:
                continue
            loc_name = (scene.get("realm_places") or {}).get(loc) or (state["places"].get(loc) or {}).get("name") or ""
            brief_peers[pid] = {"name": prec["name"], "same_space": pid in same_space_ids, "space": loc_name}
        scene["request_reflection"]({
            "realm": scene["realm"], "minute": scene["minutes"], "name": actor["name"],
            "reason": reason or ("遇到陌生物件" if unfamiliar else "两类目标吸引力接近" if near_tie else "需要形成新的议程"),
            "active_goal": active.get("goal") or "",
            "experience": state.get("experience", ""), "drives": state["drives"],
            "memories": memories,
            "satiation": state.get("satiation", {}), "vitals": vitals,
            "appraisal": state.get("last_appraisal", {}), "host": actor.get("host_character") or actor["name"],
            "choices": [(c["goal"], c["drive"], c["utility"]) for c in candidates[:5]],
            "places": {key: row["name"] for key, row in state["places"].items()},
            "zones": brief_zones,
            "peers": brief_peers,
            "objects": unfamiliar,
            "weather": str((scene.get("weather") or {}).get("description") or ""),
            "routine": str((scene.get("schedule_ctx") or {}).get("prompt_line") or ""),
        })

    @staticmethod
    def _read_signature(entries: list[dict[str, Any]]) -> list[list[str]]:
        return [[e["by"], e["text"]] for e in entries[-5:]]

    @staticmethod
    def _prediction_error(state: dict[str, Any], memory: dict[str, Any], message: str) -> None:
        memory["confidence"] = round(memory["confidence"] * 0.55, 3)
        state["prediction_error"] = message
        state["reflection"] = message
        if state.get("modulation"):
            state["modulation"]["strength"] = state["modulation"].get("strength", 1.0) * 0.65

    def _share_place(self, state: dict[str, Any], peer: dict[str, Any], now: float, sid: str) -> None:
        other = peer["autonomy"].get("cognition") or {}
        for key, portal in other.get("portals", {}).items():
            if portal["confidence"] < 0.5 or portal["space"] != sid or key in state["portals"]:
                continue
            place = other.get("places", {}).get(portal["target"], {})
            if not place.get("visits"):
                continue
            state["portals"][key] = {**portal, "source": "social", "confidence": 0.55, "samples": [now]}
            target = state["places"].setdefault(portal["target"], {"name": portal["name"], "source": "social"})
            for domain, memory in place.get("affordances", {}).items():
                if memory["source"] == "direct" and memory["confidence"] >= 0.5:
                    self._remember_affordance(target, domain, "social", now, 0.55)
            break

    def _reflect(
        self, scene: dict[str, Any], state: dict[str, Any], candidates: list[dict[str, Any]], reason: str,
    ) -> dict[str, Any]:
        best = candidates[0]
        choices = [c for c in candidates[:7] if c["utility"] >= best["utility"] - 1.15]
        if len(choices) < 2 or (best["drive"] in ("food", "rest") and best["utility"] >= 4.0):
            return best
        actor = {**scene["actor"], "autonomy": {"cognition": state}}
        summary = cognitive_summary(actor)
        options = "\n".join(f"{i + 1}. {c['goal']}（{_DOMAINS.get(c['drive'], c['drive'])}）" for i, c in enumerate(choices))
        try:
            parsed = text_compiler._ask_llm_json(
                "你负责角色的阶段性反思。只从给出的目标中选择一个序号，输出 JSON {\"choice\": 1}。",
                f"{scene['percept']['prompt_block']}\n反思原因：{reason}\n"
                f"认知地图与内驱力：{summary}\n{options}",
                adapter_provider=self.adapter_provider, timeout_sec=8.0, max_tokens=48,
            )
        except Exception:
            logger.debug("nanami_world: 阶段反思失败，沿用本地目标", exc_info=True)
            return best
        choice = parsed.get("choice") if isinstance(parsed, dict) else None
        return choices[choice - 1] if type(choice) is int and 1 <= choice <= len(choices) else best
