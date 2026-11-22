"""统一场所语义与角色目录（venue_semantics.py）：收口住宿、饮食、作息与补货判定。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

VALID_VENUE_ROLES: tuple[str, ...] = ("home", "cafe", "shop", "library", "arcade", "public")
VALID_RESTOCK_PROFILES: frozenset[str] = frozenset({"", "cafe", "shop"})
VALID_VENUE_AFFORDANCES: frozenset[str] = frozenset({"rest", "food", "social", "reading", "game", "hobby", "music"})

_LODGING_KEYWORDS: tuple[str, ...] = ("宿舍", "小屋", "卧室", "客房", "旅馆", "公寓", "住处", "民宿", "汤宿", "民居")
_CAFE_KEYWORDS: tuple[str, ...] = ("咖啡", "茶屋", "茶馆", "茶亭", "甜品", "食堂", "餐馆", "拉面", "料理", "酒馆", "烘焙")
_SHOP_KEYWORDS: tuple[str, ...] = ("杂货", "工坊", "商店", "小卖部", "便利店", "市集", "百货", "铺")
_ARCADE_KEYWORDS: tuple[str, ...] = ("电玩", "游戏", "街机", "游乐", "桌游")
_LIBRARY_KEYWORDS: tuple[str, ...] = ("图书", "书屋", "书店", "画室", "自习", "学院", "教室", "阅览", "资料")

VENUE_SPECS: dict[str, dict[str, Any]] = {
    "home": {
        "category": "home",
        "icon": "🏠",
        "affordances": ("rest",),
        "lodging": True,
        "food_venue": False,
        "restock_profile": "",
        "schedule_categories": ("home", "house"),
    },
    "cafe": {
        "category": "cafe",
        "icon": "☕",
        "affordances": ("social", "food"),
        "lodging": False,
        "food_venue": True,
        "restock_profile": "cafe",
        "schedule_categories": ("cafe", "shop"),
    },
    "shop": {
        "category": "shop",
        "icon": "🏪",
        "affordances": ("food",),
        "lodging": False,
        "food_venue": True,
        "restock_profile": "shop",
        "schedule_categories": ("shop", "cafe"),
    },
    "library": {
        "category": "library",
        "icon": "📚",
        "affordances": ("reading",),
        "lodging": False,
        "food_venue": False,
        "restock_profile": "",
        "schedule_categories": ("library", "public"),
    },
    "arcade": {
        "category": "arcade",
        "icon": "🕹️",
        "affordances": ("game",),
        "lodging": False,
        "food_venue": False,
        "restock_profile": "",
        "schedule_categories": ("arcade",),
    },
    "public": {
        "category": "public",
        "icon": "🏛️",
        "affordances": ("social", "reading"),
        "lodging": False,
        "food_venue": False,
        "restock_profile": "",
        "schedule_categories": ("public", "library"),
    },
}


def normalize_venue_role(raw_role: Any = "", *, category: Any = "", name: Any = "") -> str:
    role = str(raw_role or "").strip().lower()
    if role in VALID_VENUE_ROLES:
        return role
    if role == "house":
        return "home"

    cat = str(category or "").strip().lower()
    clean_name = str(name or "").strip()
    lower_name = clean_name.casefold()

    if cat in ("home", "house"):
        return "home"
    if cat == "arcade":
        return "arcade"
    if cat == "library":
        return "library"
    if cat == "cafe":
        return "cafe"
    if cat == "shop":
        if any(kw in clean_name for kw in _CAFE_KEYWORDS) or "cafe" in lower_name:
            return "cafe"
        return "shop"

    if any(kw in clean_name for kw in _LODGING_KEYWORDS) or any(w in lower_name for w in ("inn", "hotel", "dorm", "home")):
        return "home"
    if any(kw in clean_name for kw in _CAFE_KEYWORDS) or any(w in lower_name for w in ("cafe", "tea", "bistro")):
        return "cafe"
    if any(kw in clean_name for kw in _SHOP_KEYWORDS) or any(w in lower_name for w in ("shop", "market", "store")):
        return "shop"
    if any(kw in clean_name for kw in _ARCADE_KEYWORDS) or "arcade" in lower_name:
        return "arcade"
    if any(kw in clean_name for kw in _LIBRARY_KEYWORDS) or any(w in lower_name for w in ("library", "book", "studio")):
        return "library"
    return "public"


def building_role(building: Mapping[str, Any] | None, *, name: str = "") -> str:
    if isinstance(building, Mapping):
        return normalize_venue_role(
            building.get("role"),
            category=building.get("category"),
            name=building.get("name") or name,
        )
    return normalize_venue_role("", name=name)


def default_category_for_role(role: str) -> str:
    norm = normalize_venue_role(role)
    return str(VENUE_SPECS.get(norm, VENUE_SPECS["public"])["category"])


def default_icon_for_role(role: str) -> str:
    norm = normalize_venue_role(role)
    return str(VENUE_SPECS.get(norm, VENUE_SPECS["public"])["icon"])


def building_affordance_hints(building: Mapping[str, Any] | None) -> list[str]:
    if isinstance(building, Mapping):
        raw_aff = building.get("affordances")
        if isinstance(raw_aff, Sequence) and not isinstance(raw_aff, (str, bytes)):
            cleaned = [
                str(x or "").strip()
                for x in raw_aff
                if str(x or "").strip() in VALID_VENUE_AFFORDANCES
            ]
            if cleaned:
                return list(dict.fromkeys(cleaned))
    role = building_role(building)
    return list(VENUE_SPECS.get(role, VENUE_SPECS["public"])["affordances"])


def is_lodging_building(building: Mapping[str, Any] | None, *, name: str = "") -> bool:
    return building_role(building, name=name) == "home"


def is_food_building(building: Mapping[str, Any] | None, *, name: str = "") -> bool:
    if isinstance(building, Mapping):
        prof = str(building.get("restock_profile") or "").strip().lower()
        if prof in ("cafe", "shop"):
            return True
        if "food" in building_affordance_hints(building):
            return True
    role = building_role(building, name=name)
    return bool(VENUE_SPECS.get(role, {}).get("food_venue"))


def restock_profile_for_building(building: Mapping[str, Any] | None) -> str:
    if isinstance(building, Mapping) and "restock_profile" in building:
        raw_prof = str(building.get("restock_profile") or "").strip().lower()
        if raw_prof in ("cafe", "shop"):
            return raw_prof
        if raw_prof == "" and str(building.get("role") or "").strip().lower() in VALID_VENUE_ROLES:
            return ""
    role = building_role(building)
    return str(VENUE_SPECS.get(role, {}).get("restock_profile") or "")


def restock_staples_for_profile(profile: str) -> tuple[str, ...]:
    prof = str(profile or "").strip().lower()
    if prof == "cafe":
        return ("default_cake", "default_ramen", "default_hot_drink")
    if prof == "shop":
        return ("default_snack", "default_hot_drink")
    return ()


def building_matches_schedule_category(building: Mapping[str, Any], target_category: str) -> bool:
    req = str(target_category or "").strip().lower()
    if not req:
        return False
    b_role = building_role(building)
    b_cat = str(building.get("category") or "").strip().lower()
    if b_role == req or b_cat == req:
        return True
    if req == "house" and b_role == "home":
        return True
    if req == "public" and b_role == "library":
        return True
    return False


def building_matches_schedule_categories(building: Mapping[str, Any], categories: Sequence[str]) -> bool:
    return any(building_matches_schedule_category(building, cat) for cat in categories)
