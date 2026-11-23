"""把亲历的探索、场所活动与交往经历整理为可回忆的情景记忆。"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

from .schema import clip_text


REST_MINUTES = 2.0
PLAYER_SUBJECT_ID = "player_avatar"
_RECENT_LIMIT = 24
_ARCHIVE_LIMIT = 12
_IMPRESSION_LIMIT = 20
_KINDS = ("social", "activity", "exploration")


def record(
    memory: dict[str, Any], minute: float, space: Mapping[str, Any], kind: str, summary: str, *,
    subject_id: str = "", subject_name: str = "",
) -> None:
    summary = clip_text(summary, 80)
    if not summary:
        return
    episodes = memory.setdefault("recent", [])
    place_id = space["space_id"]
    for index in range(len(episodes) - 1, -1, -1):
        row = episodes[index]
        if (row["kind"], row["place_id"], row["subject_id"], row["summary"]) == (kind, place_id, subject_id, summary):
            episodes.append(episodes.pop(index))
            row["count"] += 1
            row["minute"] = round(minute, 3)
            return
    episodes.append({
        "kind": kind, "minute": round(minute, 3), "place_id": place_id, "place_name": space["name"],
        "subject_id": subject_id, "subject_name": subject_name, "summary": summary, "count": 1,
    })
    del episodes[:-_RECENT_LIMIT]


def consolidate(memory: dict[str, Any], minute: float) -> str:
    episodes = memory.pop("recent", [])
    if not episodes:
        return ""

    def update(index: dict[str, Any], key: str, name: str, episode: dict[str, Any]) -> None:
        impression = index.pop(key, {})
        impression.update(name=name, count=impression.get("count", 0) + episode["count"], minute=episode["minute"])
        if episode["kind"] != "exploration" or not impression.get("summary"):
            impression["summary"] = episode["summary"]
        index[key] = impression
        if len(index) > _IMPRESSION_LIMIT:
            del index[next(iter(index))]

    places = memory.setdefault("places", {})
    people = memory.setdefault("people", {})
    for row in episodes:
        update(places, row["place_id"], row["place_name"], row)
        if row["kind"] == "social" and row["subject_id"]:
            update(people, row["subject_id"], row["subject_name"], row)

    # 给三类亲历各留一个片段，剩余位置留给最新经历。
    selected = []
    for kind in _KINDS:
        latest = next((row for row in reversed(episodes) if row["kind"] == kind), None)
        if latest:
            selected.append(latest)
    selected.extend([row for row in reversed(episodes) if row not in selected][:3 - len(selected)])
    selected.sort(key=lambda row: row["minute"])
    archive = memory.setdefault("archive", [])
    archive.append({"minute": round(minute, 3), "episodes": selected})
    del archive[:-_ARCHIVE_LIMIT]
    return "；".join(clip_text(row["summary"].rstrip("。"), 25) for row in selected)


def _archived_episodes(memory: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for entry in reversed(memory.get("archive") or []):
        yield from reversed(entry["episodes"])


def recent_summaries(cognition: Mapping[str, Any]) -> list[str]:
    memory = cognition.get("episodic_memory") or {}
    summaries = dict.fromkeys(row["summary"] for row in _archived_episodes(memory))
    return list(summaries)[:3]


def recall(
    cognition: Mapping[str, Any], space_id: str, peer_ids: set[str], *, query: str = "",
) -> list[str]:
    memory = cognition.get("episodic_memory") or {}
    cue = query.strip().casefold()
    parts = re.split(
        r"还记得|记不记得|记得|上次|之前|那次|提过|做了什么|说了什么|聊了什么|有什么印象|我们|你们|他们|[^\w]+",
        cue,
    )
    terms = [term for part in parts if len(term := part.rstrip("吗呢呀么")) >= 2]

    def matches_cue(value: str) -> bool:
        value = value.casefold()
        if not value or not cue:
            return False
        if value in cue or any(term in value for term in terms):
            return True
        return any(
            value[index:index + 3] in term
            for term in terms for index in range(len(value) - 2)
        )

    matches, nearby_memories = [], []
    for bucket, nearby in (("people", peer_ids), ("places", {space_id})):
        for key, row in reversed(list((memory.get(bucket) or {}).items())):
            if matches_cue(row["name"]):
                matches.append(row["summary"])
            elif key in nearby:
                nearby_memories.append(row["summary"])
    archived_matches = [
        row["summary"] for row in _archived_episodes(memory)
        if matches_cue(row["subject_name"])
        or matches_cue(row["place_name"])
        or matches_cue(row["summary"])
    ]
    return list(dict.fromkeys([
        *archived_matches, *matches, *nearby_memories, *recent_summaries(cognition),
    ]))[:3]
