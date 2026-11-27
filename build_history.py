"""Undo records for construction, scoped to the spaces a mutation touches."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable

from .schema import TILE_CATALOG

if TYPE_CHECKING:
    from .engine import WorldEngine

_MISSING = object()
_ACTOR_FIELDS = ("space_id", "x", "y", "holding_id", "resting_on_id", "posture")


def build_action(
    label: str, *, entity: bool = False, inbound: bool = False, space_arg: int = 0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap existing public build APIs, including nested calls during a stroke."""
    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(method)
        def wrapped(engine: WorldEngine, *args: Any, **kwargs: Any) -> Any:
            with engine._lock:
                if entity:
                    eid = args[0] if args else kwargs["entity_id"]
                    sid = (engine._world["entities"].get(eid) or {}).get("space_id")
                else:
                    sid = args[space_arg] if len(args) > space_arg else kwargs.get("space_id")
                sid = str(sid or engine._world["active_space_id"]).strip()
                affected = engine._inbound_portal_spaces_locked(sid) if inbound else set()
                with engine._build_mutation_locked(label, sid, affected):
                    return method(engine, *args, **kwargs)
        return wrapped
    return decorate


def capture_build_state(world: dict[str, Any], spaces: set[str], actor_ids: set[str] | None = None) -> dict[str, Any]:
    # Keep full space dictionaries so every relief layer moves with its terrain edit.
    entities = {eid: ent for eid, ent in world["entities"].items() if ent["space_id"] in spaces}
    return deepcopy({
        "spaces": {sid: world["spaces"][sid] for sid in spaces if sid in world["spaces"]},
        "entities": entities,
        "actors": {
            aid: {field: actor[field] for field in _ACTOR_FIELDS if field in actor}
            for aid, actor in world["actors"].items()
            if actor["space_id"] in spaces or aid in (actor_ids or ())
            or actor.get("holding_id") in entities or actor.get("resting_on_id") in entities
        },
        "key_loans": world["key_loans"],
    })


@dataclass(frozen=True)
class Change:
    path: tuple[str, ...]
    before: Any
    after: Any


def diff_build_state(before: Any, after: Any, path: tuple[str, ...] = ()) -> Iterator[Change]:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in dict.fromkeys((*before, *after)):
            yield from diff_build_state(before.get(key, _MISSING), after.get(key, _MISSING), (*path, key))
    elif before != after:
        yield Change(path, before, after)


@dataclass(eq=False)
class BuildEntry:
    label: str
    primary: str
    spaces: set[str]
    changes: list[Change]

    def applicable(self, world: dict[str, Any], *, undo: bool) -> bool:
        """Reject stale edits and deltas that would strand runtime occupants."""
        overrides = {change.path: change.before if undo else change.after for change in self.changes}
        disappearing_spaces: set[str] = set()
        disappearing_entities: set[str] = set()
        terrain_spaces: set[str] = set()
        resized: set[str] = set()
        candidates: set[str] = set()

        def upcoming(path: tuple[str, ...], default: Any = _MISSING) -> Any:
            current = world
            for i, key in enumerate(path, 1):
                if not isinstance(current, dict):
                    return default
                current = overrides.get(path[:i], current.get(key, _MISSING))
            return default if current is _MISSING else current

        for change in self.changes:
            expected = change.after if undo else change.before
            root, key, *fields = change.path
            target = overrides[change.path]
            current = world
            for part in change.path:
                if not isinstance(current, dict):
                    return False
                current = current.get(part, _MISSING)

            if root == "entities" and not fields and target is _MISSING:
                if not isinstance(current, dict) or any(
                    current.get(field) != expected.get(field) for field in ("space_id", "parent_id", "x", "y", "z")
                ):
                    return False
                disappearing_entities.add(key)
            elif current != expected:
                return False

            if root == "spaces":
                if not fields and target is _MISSING:
                    disappearing_spaces.add(key)
                if fields and fields[0] in ("tiles", "wall_rows"):
                    terrain_spaces.add(key)
                if fields and fields[0] in ("width", "height"):
                    resized.add(key)
            elif root == "entities" and (not fields or fields[0] in ("x", "y", "z", "space_id")):
                candidates.add(key)

        if any(upcoming(("spaces", sid)) is _MISSING for sid in self.spaces - disappearing_spaces):
            return False
        if disappearing_spaces and any(
            sid not in disappearing_spaces and space.get("parent_space_id") in disappearing_spaces
            for sid, space in world["spaces"].items()
        ):
            return False
        parent_tags = {f"entity:{eid}" for eid in disappearing_entities}
        for eid, ent in world["entities"].items():
            if eid in disappearing_entities:
                continue
            if ent["space_id"] in disappearing_spaces or ent["parent_id"] in parent_tags:
                return False
            if disappearing_spaces and upcoming(("entities", eid, "traits", "portal", "target_space_id")) in disappearing_spaces:
                return False

        actor_cells: set[tuple[str, int, int]] = set()
        for aid in world["actors"]:
            path = ("actors", aid)
            sid = upcoming((*path, "space_id"))
            x, y = upcoming((*path, "x")), upcoming((*path, "y"))
            if sid in disappearing_spaces or any(
                upcoming((*path, field)) in disappearing_entities for field in ("holding_id", "resting_on_id")
            ):
                return False
            actor_cells.add((sid, x, y))
            if sid in resized and not (0 <= x < upcoming(("spaces", sid, "width"))
                                       and 0 <= y < upcoming(("spaces", sid, "height"))):
                return False
            if sid in terrain_spaces:
                space = world["spaces"][sid]
                cell = f"{x},{y}"
                old_tile = space["tiles"].get(cell, "wood_wall" if y < space["wall_rows"] else "wood_floor")
                wall_rows = upcoming(("spaces", sid, "wall_rows"))
                new_tile = upcoming(("spaces", sid, "tiles", cell), "wood_wall" if y < wall_rows else "wood_floor")
                if TILE_CATALOG[old_tile]["walkable"] and not TILE_CATALOG[new_tile]["walkable"]:
                    return False

        for eid in candidates | (set(world["entities"]) if resized else set()):
            entity = ("entities", eid)
            if eid in disappearing_entities or upcoming(entity) is _MISSING:
                continue
            sid = upcoming((*entity, "space_id"))
            x, y, z = (upcoming((*entity, field)) for field in ("x", "y", "z"))
            w, h = upcoming((*entity, "footprint"))
            if z < 0:
                w = h = 1
            if sid in resized and not (0 <= x <= upcoming(("spaces", sid, "width")) - w
                                       and 0 <= y <= upcoming(("spaces", sid, "height")) - h):
                return False
            if eid in candidates and z == 0 and upcoming((*entity, "solid")) and any(
                (sid, x + dx, y + dy) in actor_cells for dx in range(w) for dy in range(h)
            ):
                return False
        return True

    def apply(self, world: dict[str, Any], *, undo: bool) -> None:
        for change in self.changes:
            value = change.before if undo else change.after
            parent = world
            for key in change.path[:-1]:
                parent = parent[key]
            if value is _MISSING:
                parent.pop(change.path[-1], None)
            else:
                parent[change.path[-1]] = deepcopy(value)


class BuildHistory:
    limit = 40

    def __init__(self) -> None:
        self.undo: list[BuildEntry] = []
        self.redo: list[BuildEntry] = []

    def record(self, entry: BuildEntry) -> None:
        self.redo[:] = [old for old in self.redo if old.spaces.isdisjoint(entry.spaces)]
        self.undo.append(entry)
        for sid in entry.spaces:
            local = [old for old in self.undo if sid in old.spaces]
            if len(local) > self.limit:
                self.undo.remove(local[0])

    def head(self, sid: str, *, undo: bool) -> BuildEntry | None:
        stack = self.undo if undo else self.redo
        newer_spaces: set[str] = set()
        for entry in reversed(stack):
            if sid in entry.spaces:
                return entry if entry.spaces.isdisjoint(newer_spaces) else None
            newer_spaces.update(entry.spaces)
        return None

    def discard_spaces(self, spaces: set[str]) -> None:
        for stack in (self.undo, self.redo):
            stack[:] = [entry for entry in stack if entry.spaces.isdisjoint(spaces)]

    def advance(self, entry: BuildEntry, *, undo: bool) -> None:
        source, target = (self.undo, self.redo) if undo else (self.redo, self.undo)
        source.remove(entry)
        target.append(entry)
