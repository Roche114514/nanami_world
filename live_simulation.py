"""Real-time motor scheduler and the asynchronous reflection channel."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import persona_bridge, text_compiler, weather
from .schema import clamp_float, clip_text

if TYPE_CHECKING:
    from .cognitive_engine import CognitiveEngine
    from .engine import WorldEngine
    from .settings import WorldSettings

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 15
_STEP_SECONDS = 0.035
_DOMAINS = frozenset({"food", "game", "reading", "rest", "social", "explore", "notice", "hobby", "music", "curiosity"})
_ACTION_SECONDS = {
    "read": 4, "write": 4, "consume": 9, "rest": 2, "open": 1.2, "turn_on": 0.8,
    "interact": 16,
}


@dataclass
class LiveActor:
    owner: str
    frame: dict[str, Any]
    expiry: float
    next_at: float
    speed: int = 1
    phase: str = "idle"
    duration: float = 0.0
    walk_speed: float = 5.5
    engagement: tuple[str, str] | None = None
    route: list[tuple[int, int]] = field(default_factory=list)
    walked: list[tuple[int, int]] = field(default_factory=list)
    moves: list[tuple[int, list[list[int]]]] = field(default_factory=list)
    revision: int = 0
    world_revision: int = 0
    reflection_at: float = 0.0
    last_plan_at: float = 0.0
    reflecting: bool = False
    packet: dict[str, Any] | None = None
    reflection_log: list[dict[str, str]] = field(default_factory=list)
    route_id: int = 0
    order: dict[str, Any] | None = None
    order_status: str = ""
    speech_queue: list[tuple[str, float, float]] = field(default_factory=list)
    seen_preempt_seq: int = 0
    user_chat_until: float = 0.0


class LiveSimulation:
    def __init__(
        self, engine: WorldEngine, settings_provider: Callable[[], WorldSettings], cognition: CognitiveEngine,
        talk: Callable[[str, str, str], list[str]], adapter_provider: Callable[[], Any] | None,
        maintain: Callable[[], None], handle_loan: Callable[[dict[str, Any], WorldSettings], dict[str, Any] | None],
        social_service: Callable[[], Any] | None = None,
    ) -> None:
        self.engine = engine
        self.settings_provider = settings_provider
        self.cognition = cognition
        self.talk = talk
        self.adapter_provider = adapter_provider
        self.maintain = maintain
        self.handle_loan = handle_loan
        self.social_service = social_service
        self._actors: dict[str, LiveActor] = {}
        self._anchors: dict[str, str] = {}
        self._chatting: set[str] = set()
        self._social_until: dict[tuple[str, str], float] = {}
        self._noticed_until: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nanami-reflection")
        self._maintenance = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nanami-live-bridge")
        self._social = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nanami-live-social")
        self._outstanding = 0

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            with self.engine._lock:
                self.engine._save_world_locked()
        self._workers.shutdown(wait=False, cancel_futures=True)
        self._maintenance.shutdown(wait=False, cancel_futures=True)
        self._social.shutdown(wait=False, cancel_futures=True)

    def _start_locked(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="nanami-live-motor", daemon=True)
            self._thread.start()

    def managed(self, actor_id: str) -> bool:
        with self._lock:
            actor = self._actors.get(actor_id)
            return bool(actor and actor.expiry > time.monotonic())

    def hold_for_user_chat(self, actor_id: str, seconds: float = 20.0) -> None:
        """Pause autonomous walking while the player is actively conversing with this actor."""
        now = time.monotonic()
        pf = self.engine.live_actor_frame("player_avatar")
        with self._lock:
            actor = self._actors.get(actor_id)
            if not actor or actor.expiry <= now:
                return
            actor.user_chat_until = now + max(1.0, float(seconds))
            actor.route = []
            actor.route_id += 1
            actor.engagement = None
            actor.phase = "conversing"
            actor.next_at = now + 0.4
            if pf and pf.get("space_id") == actor.frame.get("space_id"):
                dx = int(pf.get("x", 0)) - int(actor.frame.get("x", 0))
                dy = int(pf.get("y", 0)) - int(actor.frame.get("y", 0))
                if dx or dy:
                    actor.frame["facing"] = (
                        ("right" if dx > 0 else "left") if abs(dx) >= abs(dy)
                        else ("down" if dy > 0 else "up")
                    )
                    actor.revision += 1

    def release_user_chat(self, actor_id: str) -> None:
        """Resume autonomous life when the player closes the dialogue dock or walks away."""
        now = time.monotonic()
        with self._lock:
            actor = self._actors.get(actor_id)
            if not actor:
                return
            actor.user_chat_until = 0.0
            if actor.phase == "conversing" and actor_id not in self._chatting:
                actor.phase = "idle"
            actor.next_at = min(actor.next_at, now + 0.25) if actor.speed else actor.next_at

    def control(self, actor_id: str, owner: str, command: str, speed: int) -> dict[str, Any]:
        frame = self.engine.live_actor_frame(actor_id) if command == "start" else None
        now = time.monotonic()
        with self._lock:
            actor = self._actors.get(actor_id)
            if actor and actor.expiry <= now:
                del self._actors[actor_id]
                actor = None
            if actor and actor.owner and actor.owner != owner:
                raise ValueError("该角色已在另一个窗口开启实时生活。")
            if command == "stop":
                self._anchors.pop(owner, None)
                for aid, item in list(self._actors.items()):
                    if item.owner == owner:
                        if item.order:
                            item.owner, item.speed, item.expiry = "", 1, now + 90
                            item.next_at = now
                        else:
                            del self._actors[aid]
                return {}
            if command == "start" and actor is None:
                if not frame or not self.settings_provider().enabled:
                    raise ValueError("当前角色无法开启实时生活。")
                actor = self._actors[actor_id] = LiveActor(owner, frame, now + _LEASE_SECONDS, now, speed=speed)
                self._start_locked()
            if actor is None:
                return {}
            if command == "start":
                self._anchors[owner] = actor_id
                actor.owner = owner
            for item in self._actors.values():
                if item.owner == owner and speed != item.speed:
                    remaining = max(0.0, item.next_at - now) if item.speed else item.next_at
                    remaining *= (item.speed or 1) / (speed or 1)
                    item.next_at = now + remaining if speed else remaining
                    item.duration *= (item.speed or 1) / (speed or 1)
                    item.speed = speed
            return self._view(actor, now)

    def frame(self, actor_id: str, owner: str, after: int, peer_after: dict[str, int] | None = None) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            actor = self._actors.get(actor_id)
            if actor is None or actor.expiry <= now:
                return {}
            if actor.owner == owner:
                for item in self._actors.values():
                    if item.owner == owner:
                        item.expiry = now + _LEASE_SECONDS
            view = self._delta(actor, now, after)
            view["peers"] = [
                self._delta(item, now, (peer_after or {}).get(aid, -1))
                for aid, item in self._actors.items()
                if aid != actor_id and item.expiry > now
                and (item.frame["space_id"] == actor.frame["space_id"] or (actor.owner and item.owner == actor.owner))
            ]
            return view

    def _delta(self, actor: LiveActor, now: float, after: int) -> dict[str, Any]:
        view = self._view(actor, now)
        if after == actor.revision:
            view.pop("actor")
        elif isinstance(after, int) and after >= 0:
            path: list[list[int]] = []
            for revision, cells in actor.moves:
                if revision > after:
                    path = [*path, *cells[1:]] if path and path[-1] == cells[0] else cells
            if path and actor.frame["last_path"] and path[-1] == actor.frame["last_path"][-1]:
                view["actor"] = {**actor.frame, "last_path": path}
        return view

    def _view(self, actor: LiveActor, now: float) -> dict[str, Any]:
        speech_text = next((text for text, start, end in actor.speech_queue if start <= now < end), "")
        return {
            "actor": actor.frame, "actor_id": actor.frame["actor_id"], "space_id": actor.frame["space_id"],
            "revision": actor.revision, "order_status": actor.order_status, "ordered": bool(actor.order),
            "world_revision": actor.world_revision, "speed": actor.speed,
            "speech": speech_text,
            "activity": {
                "phase": actor.phase if actor.speed else "paused",
                "remaining": (max(0.0, actor.next_at - now) if actor.speed else actor.next_at)
                + max(0, len(actor.route) - 1) / (actor.walk_speed * (actor.speed or 1)),
                "duration": actor.duration, "walk_speed": actor.walk_speed,
                "verb": actor.engagement[1] if actor.engagement else "",
                "route_id": actor.route_id, "path": [[actor.frame["x"], actor.frame["y"]], *actor.route] if actor.route else [],
                "next_cell": max(0, actor.next_at - now) if actor.speed else actor.next_at,
            },
            "reflection": list(actor.reflection_log),
            "llm_enabled": bool(self.settings_provider().heartbeat_use_llm),
        }

    def _loop(self) -> None:
        last_maintenance = 0.0
        maintenance = None
        previous_scale = 1
        last_roster = 0.0
        while not self._stop.wait(_STEP_SECONDS):
            now = time.monotonic()
            if now - last_roster >= 0.7:
                self._sync_rosters(now)
                last_roster = now
            with self._lock:
                for aid in [aid for aid, actor in self._actors.items() if actor.expiry <= now]:
                    del self._actors[aid]
                actors = [(aid, actor) for aid, actor in self._actors.items() if actor.speed and aid not in self._chatting]
            scale = max((actor.speed for _, actor in actors), default=1)
            if previous_scale != scale:
                self.engine.tick_physics(
                    time_scale=previous_scale, consolidate_memory=self.settings_provider().episodic_memory_enabled,
                )
                previous_scale = scale
            if not actors:
                self._stop.wait(0.15)
                continue
            try:
                settings = self.settings_provider()
                if not settings.enabled:
                    continue
                self.engine.tick_physics(
                    minimum_elapsed_minutes=0.05, time_scale=scale, consolidate_memory=settings.episodic_memory_enabled,
                )
                if now - last_maintenance >= 4 and (maintenance is None or maintenance.done()):
                    maintenance = self._maintenance.submit(self.maintain)
                    last_maintenance = now
                for aid, actor in actors:
                    if actor.speed and actor.next_at <= now:
                        try:
                            self._advance(aid, actor, settings)
                        except Exception:
                            with self._lock:
                                actor.next_at = time.monotonic() + 1.0
                            logger.debug("nanami_world: 角色实时行动失败（%s）", aid, exc_info=True)
            except Exception:
                logger.debug("nanami_world: 实时生活执行失败", exc_info=True)

    def _sync_rosters(self, now: float) -> None:
        with self._lock:
            anchors = list(self._anchors.items())
        for owner, anchor in anchors:
            peers = self.engine.live_realm_peers(anchor)
            with self._lock:
                lead = self._actors.get(anchor)
                if not lead or lead.expiry <= now:
                    self._anchors.pop(owner, None)
                    continue
                missing = [aid for aid in peers if aid not in self._actors]
                for aid, item in list(self._actors.items()):
                    if item.owner == owner and aid != anchor and aid not in peers and not item.order:
                        del self._actors[aid]
            for aid in missing:
                frame = self.engine.live_actor_frame(aid)
                with self._lock:
                    if frame and self._anchors.get(owner) == anchor and aid not in self._actors:
                        self._actors[aid] = LiveActor(owner, frame, lead.expiry, now, speed=lead.speed)

    def queue_action(self, aid: str, action: dict[str, Any]) -> tuple[bool, str]:
        with self.engine._lock:
            frame = self.engine.live_actor_frame(aid)
            if not frame or not self.settings_provider().enabled:
                return False, "角色当前无法行动。"
            sid = frame["space_id"]
            target_id = str(action.get("target_actor_id") or "")
            tool = action.get("tool")
            if tool in ("world_approach", "world_talk"):
                peer = self.engine.live_actor_frame(target_id)
                if not peer or aid == target_id or peer["space_id"] != sid:
                    return False, "对方现在不在同一空间。"
                x, y, reach = peer["x"], peer["y"], 1
                if tool == "world_talk" and peer["posture"] == "resting":
                    tool = "world_approach"
            else:
                try:
                    x, y, reach = int(action["x"]), int(action["y"]), 0
                except (TypeError, KeyError, ValueError):
                    return False, "没有可用的目的地。"
            path = self.engine.plan_actor_route(aid, sid, x, y, reach=reach, stride=1)
            if not path:
                return False, "通往目的地的路被挡住了。"
            now = time.monotonic()
            with self._lock:
                actor = self._actors.setdefault(aid, LiveActor("", frame, now + 90, now))
                actor.order = {"tool": tool, "space": sid, "target": target_id, "x": x, "y": y, "until": now + 90}
                actor.order_status = "queued"
                actor.route, actor.engagement = [], None
                actor.route_id += 1
                actor.next_at = now if actor.speed else 0
                actor.expiry = max(actor.expiry, now + (90 if not actor.owner else _LEASE_SECONDS))
                self._start_locked()
        return True, "已安排前往对方身边。" if target_id else "已安排走向目的地。"

    def _run_order(self, aid: str, order: dict[str, Any], scene: dict[str, Any], walk: Callable) -> str:
        sid = scene["actor"]["space_id"]
        if sid != order["space"] or time.monotonic() >= order["until"]:
            return "blocked"
        x, y, reach = order["x"], order["y"], 0
        if order["target"]:
            peer = next((p for p in scene["actors"] if p["actor_id"] == order["target"]), None)
            if not peer:
                return "blocked"
            x, y, reach = peer["x"], peer["y"], 1
        path = walk(aid, sid, x, y, reach=reach, stride=4)
        if not path:
            return "blocked"
        if len(path) > 1:
            return "walking"
        if order["tool"] == "world_talk" and peer["posture"] != "resting":
            self._meet(aid, order["target"], "对方请我过去说几句", user_requested=True)
        return "arrived"

    def _meet(
        self, aid: str, peer_id: str, reason: str, *, user_requested: bool = False, rendezvous: bool = False,
    ) -> list[str]:
        first, second = self.engine.live_actor_frame(aid), self.engine.live_actor_frame(peer_id)
        if not first or not second or first["space_id"] != second["space_id"] or second["posture"] == "resting":
            return []
        if first.get("is_player"):
            return []
        if not (rendezvous or user_requested) and any(
            person["reflection_state"].get("joint_visit_id") for person in (first, second)
        ):
            return []
        if max(abs(first["x"] - second["x"]), abs(first["y"] - second["y"])) > 1:
            return []
        key = tuple(sorted((aid, peer_id)))
        now = time.monotonic()
        service = self.social_service() if self.social_service else None
        use_model = service is not None and (user_requested or self.settings_provider().heartbeat_use_llm)
        greeting_player = second.get("is_player", False)
        if greeting_player and service is None:
            return []
        with self._lock:
            peer_runtime = self._actors.get(peer_id)
            if (len(self._chatting) >= 4 or aid in self._chatting or peer_id in self._chatting
                    or (not rendezvous and self._social_until.get(key, 0) > now)
                    or (rendezvous and peer_runtime and peer_runtime.user_chat_until > now)):
                return []
            self._social_until = {pair: expiry for pair, expiry in self._social_until.items() if expiry > now}
            self._social_until[key] = now + 25
            if use_model or greeting_player:
                participants = (aid,) if greeting_player else key
                self._chatting.update(participants)
                for participant in participants:
                    if participant in self._actors:
                        runtime = self._actors[participant]
                        runtime.route, runtime.engagement = [], None
                        runtime.phase = "conversing"
                if greeting_player:
                    self._social.submit(self._greet_player, service, aid, peer_id, reason, use_model)
                    return ["停下来，准备向主控打招呼。"]
                self._social.submit(self._speak, service, aid, peer_id, reason, rendezvous=rendezvous)
                return ["停下，准备与同伴交流。"]
        return self.talk(aid, peer_id, reason)

    def _greet_player(self, service: Any, aid: str, player_id: str, reason: str, use_model: bool) -> None:
        try:
            greeting = service.greet_player(aid, player_id, reason=reason, use_llm=use_model)
            if greeting:
                self.hold_for_user_chat(aid, greeting["wait_seconds"])
        except Exception:
            logger.debug("nanami_world: 面对面搭话失败", exc_info=True)
        finally:
            with self._lock:
                self._chatting.discard(aid)

    def _speak(self, service: Any, aid: str, peer: str, reason: str, *, rendezvous: bool = False) -> None:
        result = {}
        try:
            result = service.conduct_actor_social_exchange(aid, peer, reason=reason, generated_only=True)
        except Exception:
            logger.debug("nanami_world: 实时同伴对话失败", exc_info=True)
        finally:
            if rendezvous and not (result.get("turns") or (result.get("line_a") and result.get("line_b"))):
                try:
                    self.talk(aid, peer, reason)
                except Exception:
                    logger.debug("nanami_world: 汇合后的本地互动失败", exc_info=True)
            with self._lock:
                now = time.monotonic()
                raw_turns = result.get("turns") if isinstance(result.get("turns"), list) else []
                turns = [
                    t for t in raw_turns
                    if isinstance(t, dict) and str(t.get("line_a") or "").strip() and str(t.get("line_b") or "").strip()
                ]
                if not turns and result.get("line_a") and result.get("line_b"):
                    turns = [{"line_a": result["line_a"], "line_b": result["line_b"]}]
                sa = result.get("speech_act") if isinstance(result.get("speech_act"), dict) else {}
                has_turns = bool(turns)
                max_duration = 0.0
                for index, participant in enumerate((aid, peer)):
                    self._chatting.discard(participant)
                    actor = self._actors.get(participant)
                    if actor:
                        spd = actor.speed or 1
                        duration = (4.4 * len(turns) if has_turns else 1.0) / spd
                        max_duration = max(max_duration, duration)
                        actor.next_at = now + duration if actor.speed else duration
                        actor.duration = duration
                        actor.phase = "conversing" if has_turns else "dwell"
                        if has_turns:
                            step_sec = 2.2 / spd
                            key_line = "line_a" if index == 0 else "line_b"
                            actor.speech_queue = [
                                (
                                    str(t[key_line]),
                                    now + (turn_i * 2 + index) * step_sec,
                                    now + (turn_i * 2 + index + 1) * step_sec,
                                )
                                for turn_i, t in enumerate(turns)
                            ]
                            if sa:
                                side_sa = sa.get("a" if index == 0 else "b") if ("a" in sa or "b" in sa) else sa
                                if isinstance(side_sa, dict) and (
                                    side_sa.get("shared_place_id") or side_sa.get("shared_entity_id")
                                    or side_sa.get("beliefs") or side_sa.get("personal_goal")
                                ):
                                    detail = clip_text(side_sa.get("summary") or result.get("summary") or "", 48)
                                    self._log(actor, "social_act", detail)
                        else:
                            actor.speech_queue = []
                self._social_until[tuple(sorted((aid, peer)))] = now + max_duration + 40.0

    def _advance(self, aid: str, actor: LiveActor, settings: WorldSettings) -> None:
        now_m = time.monotonic()
        with self._lock:
            speed, pending, packet = actor.speed, actor.engagement, actor.packet
            order = actor.order
            if self._actors.get(aid) is not actor or not speed or aid in self._chatting or (not actor.owner and not order):
                return
            if now_m < actor.user_chat_until and not order:
                actor.route = []
                actor.phase = "conversing"
                actor.next_at = now_m + 0.4
                return
        engagement = None
        seconds = 0.0
        proposed: list[tuple[int, int]] = []

        if actor.route:
            self._walk_cell(aid, actor)
            return

        def stage_walk(*args: Any, **kwargs: Any) -> list[tuple[int, int]]:
            nonlocal walk_speed
            profile = (scene.get("percept") or {}).get("behavior_profile", {})
            walk_speed += 0.8 * (profile.get("arousal_baseline", 0.5) - 0.5)
            if scene.get("space", {}).get("space_kind") == "town":
                w_mod = weather.weather_cognitive_modifiers(scene.get("weather"), profile, is_outdoor=True)
                walk_speed *= w_mod["outdoor_walk_speed_mult"]
            kwargs["stride"] = max(2, round(walk_speed * 0.8))
            path = self.engine.plan_actor_route(*args, **kwargs)
            if len(path) > 1:
                proposed.extend(path[1:])
            return path

        def engage(eid: str, verb: str, domain: str, profile: dict[str, float]) -> bool:
            nonlocal engagement, seconds
            if pending == (eid, verb):
                return True
            engagement = (eid, verb)
            focus = profile["effortful_control"] + profile["hysteresis_inertia"]
            proficiency = profile["domain_efficacy_game"] if domain == "game" else profile["domain_efficacy_non_game"]
            seconds = _ACTION_SECONDS.get(verb, 1.5) * (0.75 + 0.18 * focus + 0.24 * proficiency)
            return False

        with self.engine._lock:
            scene = self.engine.cognitive_scene(aid, include_rooms=True)
            if scene is None:
                with self._lock:
                    self._actors.pop(aid, None)
                return
            before = scene["actor"]
            if before["move_seq"] != actor.frame["move_seq"]:
                pending = None
            vitals = before["vitals"]
            walk_speed = 3.4 + 2.0 * vitals["energy"] - 0.9 * vitals["sleep_pressure"]
            scene["engage"] = engage
            scene["stride"] = max(2, round(walk_speed * 0.8))
            scene["stage_walk"] = stage_walk
            scene["rendezvous_talk"] = lambda first, peer, goal: self._meet(first, peer, goal, rendezvous=True)
            scene["walked"] = actor.walked if before["move_seq"] == actor.frame["move_seq"] else []
            now = time.monotonic()
            scene["exposure_scale"] = max(0.04, min(1.0, (now - (actor.last_plan_at or now)) * speed / 12))
            actor.last_plan_at = now
            if settings.heartbeat_use_llm:
                if packet and packet["realm"] == scene["realm"]:
                    scene["modulation"] = packet
                scene["request_reflection"] = lambda brief: self._request(aid, actor, brief)
            decision = {}
            order_status = ""
            if order:
                order_status = self._run_order(aid, order, scene, stage_walk)
            elif before["autonomy"]["enabled"] and not self.engine.awaiting_gift_reply(aid):
                decision = self.handle_loan(before, settings) or self.cognition.step(scene, settings, use_llm=False, talk=self._meet)
            frame = self.engine.live_actor_frame(aid)
            if decision.get("cognition"):
                frame["cognition"] = decision["cognition"]

        phase = "engaging" if engagement else "idle"
        acted = frame["last_act"]["seq"] != before["last_act"]["seq"]
        if proposed:
            phase, seconds = "walking", 1 / walk_speed
        elif frame["space_id"] != before["space_id"]:
            phase, seconds = "dwell", 0.45
        elif not engagement:
            if decision.get("drive") == "social" and decision.get("actions"):
                if any("正在休息" in str(a) for a in decision["actions"]):
                    phase, seconds = "dwell", 3.2
                else:
                    phase, seconds = "conversing", 2.5
            elif acted or decision.get("actions"):
                phase, seconds = "dwell", 1.5
            elif frame["posture"] == "resting":
                phase, seconds = "resting", 2.0
            elif decision.get("cognition", {}).get("phase") == "等候同伴":
                phase, seconds = "waiting", 0.75
            elif decision.get("cognition", {}).get("phase") == "结伴停留":
                phase, seconds = "dwell", 0.75
            else:
                seconds = 0.35

        with self._lock:
            if self._actors.get(aid) is not actor or order is not actor.order:
                return
            if order_status:
                actor.order_status = order_status
                if order_status in ("arrived", "blocked"):
                    actor.order = None
                    if not actor.owner:
                        actor.expiry = time.monotonic() + 8
                    phase, seconds = "dwell", 3.0
            if frame["space_id"] != actor.frame["space_id"] or not frame["last_path"]:
                actor.moves.clear()
            previous_feedback, feedback = actor.frame["reflection_state"], frame["reflection_state"]
            if feedback["id"] and feedback["id"] != previous_feedback["id"]:
                self._log(actor, "applied", "")
            elif feedback["strength"] < previous_feedback["strength"]:
                self._log(actor, "adjusted", "")
            actor.frame = frame
            actor.seen_preempt_seq = max(actor.seen_preempt_seq, int(feedback.get("preempt_seq", 0)))
            actor.revision += 1
            if acted or frame["space_id"] != before["space_id"]:
                actor.world_revision += 1
            actor.phase = "conversing" if aid in self._chatting else phase
            actor.engagement, actor.walk_speed = engagement, walk_speed
            actor.route = proposed
            if proposed:
                actor.route_id += 1
            actor.walked = []
            actor.duration = seconds * (len(proposed) or 1) / (actor.speed or 1)
            delay = seconds / (actor.speed or 1)
            actor.next_at = time.monotonic() + delay if actor.speed else delay

    def _walk_cell(self, aid: str, actor: LiveActor) -> None:
        now = time.monotonic()
        nearby_peers: list[tuple[str, float]] = []
        with self._lock:
            if self._actors.get(aid) is not actor or not actor.speed or not actor.route or aid in self._chatting:
                return
            if now < actor.user_chat_until and not actor.order:
                actor.route = []
                actor.route_id += 1
                actor.phase = "conversing"
                actor.next_at = now + 0.4
                return
            order, seen_preempt = actor.order, actor.seen_preempt_seq
            cell, frame, route_id = actor.route[0], actor.frame, actor.route_id
            feedback = frame["reflection_state"]
            if not order and not (feedback.get("joint_visit_id") or feedback.get("speech_action")):
                sid = frame["space_id"]
                ax, ay = frame["x"], frame["y"]
                seek_target = str((frame.get("reflection_state") or {}).get("seek_peer_target") or "")
                self._noticed_until = {k: v for k, v in self._noticed_until.items() if v > now}
                for pid, peer in self._actors.items():
                    if pid == aid or peer.expiry <= now or pid in self._chatting or peer.frame.get("is_player"):
                        continue
                    pf = peer.frame
                    if pf["space_id"] != sid:
                        continue
                    is_seek_target = bool(seek_target and pid == seek_target)
                    if not is_seek_target and pf["posture"] == "resting":
                        continue
                    notice_radius = 10 if is_seek_target else 4
                    if max(abs(ax - pf["x"]), abs(ay - pf["y"])) > notice_radius:
                        continue
                    pair = tuple(sorted((aid, pid)))
                    if self._social_until.get(pair, 0) > now or (not is_seek_target and self._noticed_until.get((aid, pid), 0) > now):
                        continue
                    nearby_peers.append((pid, now + 12.0))

        preempt_seq = self.engine.pending_preempt_seq(aid) if not order else seen_preempt
        if not order and (preempt_seq > seen_preempt or nearby_peers):
            with self._lock:
                if self._actors.get(aid) is not actor or actor.route_id != route_id or not actor.route:
                    return
                if preempt_seq > actor.seen_preempt_seq:
                    actor.seen_preempt_seq = preempt_seq
                    self._log(actor, "preempted", "根据对话/想法调整去向")
                for pid, expiry in nearby_peers:
                    self._noticed_until[(aid, pid)] = expiry
                actor.route = []
                actor.route_id += 1
                actor.next_at = time.monotonic() if actor.speed else 0
                return

        updated = self.engine.advance_live_route(
            aid, frame["space_id"], frame["move_seq"], cell, persist=len(actor.route) == 1,
        )
        with self._lock:
            if self._actors.get(aid) is not actor or actor.route_id != route_id or not actor.route:
                return
            now = time.monotonic()
            if updated is None:
                actor.route = []
                actor.route_id += 1
                actor.next_at = now if actor.speed else 0
                return
            if actor.frame.get("cognition"):
                updated["cognition"] = actor.frame["cognition"]
            actor.frame = updated
            actor.revision += 1
            actor.moves = [*actor.moves[-15:], (actor.revision, updated["last_path"])]
            actor.walked.append(cell)
            actor.route.pop(0)
            delay = 1 / (actor.walk_speed * actor.speed) if actor.route and actor.speed else 0
            actor.next_at = max(now, actor.next_at + delay) if delay else now if actor.speed else 0

    def _request(self, aid: str, actor: LiveActor, brief: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            if actor.reflecting or now < actor.reflection_at or self._stop.is_set() or self._outstanding >= 4:
                return
            self._outstanding += 1
            actor.reflecting = True
            actor.reflection_at = now + (50 if brief["vitals"]["energy"] < 0.5 else 25)
            self._log(actor, "requested", brief["reason"])
            self._workers.submit(self._reflect, aid, actor, brief)

    @staticmethod
    def _format_reflection_brief(brief: dict[str, Any]) -> str:
        vit = brief.get("vitals") or {}
        drv = brief.get("drives") or {}
        places = ", ".join(f"{nm}({pid})" for pid, nm in (brief.get("places") or {}).items()) or "无"
        zones = ", ".join(f"{nm}({zid})" for zid, nm in (brief.get("zones") or {}).items()) or "无"
        peers = ", ".join(
            f"{p.get('name')}({pid}: {'就在眼前' if p.get('same_space') else ('记得在' + p['space'] if p.get('space') else '位置未知')})"
            for pid, p in (brief.get("peers") or {}).items()
        ) or "无"
        objs = ", ".join(f"{nm}({eid})" for eid, nm in (brief.get("objects") or {}).items()) or "无"
        choices = "; ".join(f"{g}[{d}]={u:.2f}" for g, d, u in (brief.get("choices") or ())) or "无"
        extra_env = "｜".join(x for x in (brief.get("weather"), brief.get("routine")) if x)
        env_line = f"\n天气与节律：{extra_env}" if extra_env else ""
        memories = "；".join(brief.get("memories") or ())
        memory_line = f"休息后记住的亲历片段（往事）：{memories}\n" if memories else ""
        return (
            f"角色：{brief.get('name')}｜触发原因：{brief.get('reason')}｜当前目标：{brief.get('active_goal') or '无'}\n"
            f"刚才经历：{brief.get('experience') or '日常行进'}\n"
            f"{memory_line}"
            f"身体状态：精力={vit.get('energy', 0):.2f} 困意={vit.get('sleep_pressure', 0):.2f} 心情={vit.get('mood_v', 0):+.2f}"
            f"{env_line}\n"
            f"内驱力：{drv}\n"
            f"记得场所：{places}\n街区：{zones}\n记住的同伴：{peers}\n关注物件：{objs}\n候选行动：{choices}"
        )

    def _reflect(self, aid: str, actor: LiveActor, brief: dict[str, Any]) -> None:
        packet = None
        try:
            raw = text_compiler._ask_llm_json(
                "为角色反思当前经历，返回 JSON。可选键 agenda、half_life_min、domain_bias、place_bias、preferred_zone、"
                "commitment（对象：kind 为 visit|social|entity，target 为对应ID，drive，reason，priority 为 normal|high）、"
                "semantic_appraisal（数组，每项 entity_id、domains、personal_salience、epa）、"
                "reappraisal（mode: release 或 ruminate，可带 carry_relax、persistence_boost 和 persist_think）、"
                "thought、diary_seed。"
                "可根据 peers 与 places 主动决定去找同伴交流或改去某处；"
                "领域与场所偏置在 -0.6 到 0.6 之间。只评估已提供的物件、场所、街区与同伴。"
                "仅当经历值得表达才写 thought 或 diary_seed。",
                self._format_reflection_brief(brief), adapter_provider=self.adapter_provider, timeout_sec=8, max_tokens=420,
            )
            if isinstance(raw, dict):
                packet = self._validate(raw, brief)
        except Exception:
            logger.debug("nanami_world: 后台反思未完成", exc_info=True)
        with self._lock:
            actor.reflecting = False
            self._outstanding -= 1
            if self._actors.get(aid) is actor:
                actor.packet = packet or actor.packet
                self._log(actor, "received" if packet else "unavailable", (packet or {}).get("agenda", ""))
                if (packet and (packet.get("commitment") or {}).get("priority") == "high"
                        and not actor.order and actor.phase == "walking"
                        and not actor.frame["reflection_state"].get("joint_visit_id")
                        and not actor.frame["reflection_state"].get("speech_action")):
                    actor.route = []
                    actor.route_id += 1
                    actor.next_at = time.monotonic() if actor.speed else 0
                    self._log(actor, "preempted", packet.get("agenda") or "前额叶切换目标")
            else:
                return
        if packet and packet["persist_think"] and packet["thought"]:
            self._record_thought(aid, brief, packet)

    def _record_thought(self, aid: str, brief: dict[str, Any], packet: dict[str, Any]) -> None:
        frame = self.engine.live_actor_frame(aid)
        if not frame or self.engine.realm_space_id(frame["space_id"]) != brief["realm"]:
            return
        settings = self.settings_provider()
        if not settings.enabled or not settings.heartbeat_use_llm:
            return
        scope = {"space_id": brief["realm"], "actor_id": aid, "custom_persona_dir": settings.persona_data_dir}
        root, host = self.engine.project_root, brief["host"]
        before = persona_bridge.read_persona_state(root, host, **scope)
        result = persona_bridge.emit_world_event_to_persona(
            root, host, **scope, thought=True, event_payload={
                "kind": "reflection", "at": time.time_ns() // 1_000_000,
                "text": packet["thought"], "about": brief["experience"], "want": packet["agenda"],
            },
        )
        if not before or not before["world_state"] or not result or not result.get("ok"):
            return
        after = persona_bridge.read_persona_state(root, host, **scope)
        if not after:
            return
        cost = clamp_float(before["energy"] - after["energy"], 0, 0.15, 0)
        with self.engine._lock:
            frame = self.engine.live_actor_frame(aid)
            if cost and frame and self.engine.realm_space_id(frame["space_id"]) == brief["realm"]:
                self.engine.set_actor_world_mood(
                    aid, space_id=frame["space_id"], record_event=False,
                    vitals_patch={"energy": frame["vitals"]["energy"] - cost},
                )

    @staticmethod
    def _log(actor: LiveActor, status: str, detail: str) -> None:
        actor.reflection_log = [*actor.reflection_log[-5:], {"status": status, "detail": detail}]

    @staticmethod
    def _validate(raw: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
        def biases(key: str, allowed: Any) -> dict[str, float]:
            values = raw.get(key)
            return {
                target: clamp_float(value, -0.6, 0.6, 0)
                for target, value in values.items() if target in allowed
            } if isinstance(values, dict) else {}

        semantic = {}
        rows = raw.get("semantic_appraisal")
        for row in rows[:8] if isinstance(rows, list) else ():
            if isinstance(row, dict) and isinstance(row.get("entity_id"), str) and row["entity_id"] in brief["objects"]:
                tags = row.get("domains")
                epa = row.get("epa")
                semantic[row["entity_id"]] = {
                    "salience": clamp_float(row.get("personal_salience"), 0, 1, 0),
                    "domains": [tag for tag in tags[:5] if isinstance(tag, str) and tag in _DOMAINS] if isinstance(tags, list) else [],
                    "epa": {key: clamp_float(epa[key], -0.65, 0.65, 0)
                            for key in ("e", "p", "a") if key in epa} if isinstance(epa, dict) else {},
                }
        reappraisal = raw.get("reappraisal")
        reappraisal = reappraisal if isinstance(reappraisal, dict) else {}
        mode = reappraisal.get("mode")
        pref_zone = str(raw.get("preferred_zone") or "").strip()
        if pref_zone not in (brief.get("zones") or {}):
            pref_zone = ""

        commitment: dict[str, Any] | None = None
        raw_commit = raw.get("commitment")
        if isinstance(raw_commit, dict):
            ckind = str(raw_commit.get("kind") or "").strip()
            ctgt = str(raw_commit.get("target") or "").strip()
            cdrive = str(raw_commit.get("drive") or "").strip()
            allowed_tgt = (
                (ckind == "visit" and ctgt in (brief.get("places") or {}))
                or (ckind == "social" and ctgt in (brief.get("peers") or {}))
                or (ckind == "entity" and ctgt in (brief.get("objects") or {}))
            )
            if allowed_tgt:
                commitment = {
                    "id": f"ref:{time.monotonic_ns()}",
                    "kind": ckind,
                    "target": ctgt,
                    "drive": "social" if ckind == "social" else (cdrive if cdrive in _DOMAINS else "explore"),
                    "reason": clip_text(raw_commit.get("reason"), 48) or clip_text(raw.get("agenda"), 48),
                    "priority": "high" if str(raw_commit.get("priority") or "").lower() == "high" else "normal",
                    "source": "pfc",
                }

        out: dict[str, Any] = {
            "id": time.monotonic_ns(), "realm": brief["realm"], "at": brief["minute"],
            "agenda": clip_text(raw.get("agenda"), 64), "half_life": clamp_float(raw.get("half_life_min"), 5, 45, 20),
            "domains": biases("domain_bias", _DOMAINS), "places": biases("place_bias", brief["places"]),
            "preferred_zone": pref_zone,
            "semantic": semantic, "experience": brief["experience"],
            "thought": clip_text(raw.get("thought"), 60) if brief["experience"] else "",
            "diary": clip_text(raw.get("diary_seed"), 60) if brief["experience"] else "",
            "relax": clamp_float(reappraisal.get("carry_relax"), 0, 0.35, 0) if mode == "release" else 0,
            "persistence": clamp_float(reappraisal.get("persistence_boost"), 0, 0.25, 0) if mode == "ruminate" else 0,
            "persist_think": mode == "ruminate" and reappraisal.get("persist_think") is True,
        }
        if commitment is not None:
            out["commitment"] = commitment
        return out
