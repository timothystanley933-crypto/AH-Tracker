"""Item feature extraction.

`extract_item_features` turns a raw CoflNet auction/listing dict into a
normalised feature dict the scoring engine can compare. It is deliberately
defensive: SkyBlock items vary wildly and endpoints don't always return the
same shape, so every lookup degrades gracefully.

The goal is NOT perfect detection of every item in the game. It is to capture
the value-defining traits (rarity, pet level/tier, stars, recomb, important
enchants, gemstones, attributes, reforge, skin) well enough that we never
compare a 5m upgraded pet to a 40k junk pet.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

RARITY_ORDER = [
    "COMMON",
    "UNCOMMON",
    "RARE",
    "EPIC",
    "LEGENDARY",
    "MYTHIC",
    "SPECIAL",
    "VERY_SPECIAL",
    "DIVINE",
    "SUPREME",
]

# Enchants that meaningfully move price. Weighted: higher = matters more.
# Easy to adjust - just edit this map.
IMPORTANT_ENCHANTS: Dict[str, int] = {
    "chimera": 10,
    "soul_eater": 6,
    "legion": 5,
    "wisdom": 4,
    "swarm": 4,
    "overload": 6,
    "ultimate_wise": 5,
    "ultimate_jerry": 4,
    "fatal_tempo": 10,
    "ultimate_fatal_tempo": 10,
    "one_for_all": 8,
    "ultimate_one_for_all": 8,
    "duplex": 7,
    "ultimate_reiterate": 7,
    "ultimate_inferno": 6,
    "ultimate_soul_eater": 6,
    "ultimate_combo": 4,
    "ultimate_swarm": 4,
    "ultimate_legion": 5,
    "ultimate_rend": 4,
    "ultimate_bank": 4,
    "ultimate_last_stand": 4,
    "ultimate_no_pain_no_gain": 3,
    "efficiency": 4,  # eff 10 books are valuable
    "fortune": 3,
    "pristine": 5,
    "compact": 5,
    "cultivating": 5,
    "rejuvenate": 3,
    "growth": 2,
    "protection": 2,
    "sharpness": 2,
    "smite": 2,
    "scavenger": 2,
    "looting": 2,
    "critical": 2,
    "giant_killer": 3,
    "first_strike": 2,
    "telekinesis": 0,
}

# Any enchant key containing "ultimate" is treated as important even if not listed.
_ULTIMATE_HINT = "ultimate"

_PET_LEVEL_RE = re.compile(r"\[lvl\s*(\d+)\]", re.IGNORECASE)
_STAR_RE = re.compile(r"✪|➊|➋|➌|➍|➎|⚝")


def _to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


def _strip_color_codes(text: str) -> str:
    return re.sub(r"§.", "", text or "")


def _lower(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _normalise_rarity(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip().upper().replace(" ", "_")
    return raw if raw else None


# --------------------------------------------------------------------------
# Sub-extractors
# --------------------------------------------------------------------------

def _extract_enchants(auction: Dict[str, Any], flat_nbt: Dict[str, Any]) -> Dict[str, int]:
    """Return {enchant_name: level}. Handles list-of-dicts, dict, and flatNbt."""
    enchants: Dict[str, int] = {}

    raw = auction.get("enchantments")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = _lower(item.get("type") or item.get("name") or item.get("enchantment"))
                level = _as_int(item.get("level") or item.get("lvl"))
                if name:
                    enchants[name] = level or 1
    elif isinstance(raw, dict):
        for name, level in raw.items():
            lvl = _as_int(level)
            enchants[_lower(name)] = lvl or 1

    # flatNbt sometimes carries enchants as enchant_<name> keys.
    for key, value in flat_nbt.items():
        kl = _lower(key)
        if kl.startswith("enchantment_") or kl.startswith("enchant_"):
            name = kl.split("_", 1)[1]
            lvl = _as_int(value)
            if name and name not in enchants:
                enchants[name] = lvl or 1

    return enchants


def _important_enchants(enchants: Dict[str, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for name, level in enchants.items():
        if name in IMPORTANT_ENCHANTS and IMPORTANT_ENCHANTS[name] > 0:
            out[name] = level
        elif _ULTIMATE_HINT in name:
            out[name] = level
    return out


def _extract_stars(auction: Dict[str, Any], flat_nbt: Dict[str, Any], item_name: str) -> int:
    """Dungeon/upgrade stars (0-10)."""
    for key in ("upgrade_level", "dungeon_item_level"):
        val = _as_int(flat_nbt.get(key))
        if val is not None:
            return val
    val = _as_int(auction.get("upgradeLevel"))
    if val is not None:
        return val
    # Fall back to counting star glyphs in the name.
    stars = len(_STAR_RE.findall(item_name or ""))
    return stars


def _extract_recomb(flat_nbt: Dict[str, Any], auction: Dict[str, Any]) -> bool:
    val = flat_nbt.get("rarity_upgrades")
    if val is None:
        val = auction.get("rarity_upgrades")
    iv = _as_int(val)
    return bool(iv and iv >= 1)


def _extract_pet(auction: Dict[str, Any], flat_nbt: Dict[str, Any], item_name: str, tag: str) -> Optional[Dict[str, Any]]:
    """Detect & describe a pet. Returns None for non-pets."""
    pet_info = _to_dict(flat_nbt.get("petInfo") or auction.get("petInfo"))
    is_pet = bool(pet_info) or (tag or "").upper().startswith("PET_") or "[lvl" in _lower(item_name)
    if not is_pet:
        return None

    pet_type = pet_info.get("type")
    if not pet_type and (tag or "").upper().startswith("PET_"):
        pet_type = tag.upper().replace("PET_", "")
    pet_type = _lower(pet_type)

    # Pet level: prefer the name (authoritative), then explicit field.
    level = None
    m = _PET_LEVEL_RE.search(item_name or "")
    if m:
        level = _as_int(m.group(1))
    if level is None:
        level = _as_int(pet_info.get("level"))

    tier = _normalise_rarity(pet_info.get("tier") or auction.get("tier"))

    return {
        "type": pet_type or None,
        "tier": tier,
        "level": level,
        "exp": _as_int(pet_info.get("exp")),
        "candy_used": _as_int(pet_info.get("candyUsed")) or 0,
        "held_item": _lower(pet_info.get("heldItem")) or None,
        "skin": _lower(pet_info.get("skin")) or None,
    }


def _extract_gemstones(flat_nbt: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort gemstone read.

    flatNbt may expose unlocked slots and gem qualities. We capture slot count
    and a list of qualities so scoring can avoid comparing gem'd vs clean.
    """
    unlocked = _as_int(flat_nbt.get("unlocked_slots"))
    qualities: List[str] = []
    gem_keys = 0
    for key, value in flat_nbt.items():
        kl = _lower(key)
        sval = _lower(value)
        if sval in ("rough", "flawed", "fine", "flawless", "perfect"):
            qualities.append(sval)
            gem_keys += 1
        elif kl.endswith("_gem") and isinstance(value, str) and value:
            gem_keys += 1
    return {
        "unlocked_slots": unlocked,
        "qualities": qualities,
        "gem_count": gem_keys,
        "has_gems": gem_keys > 0 or bool(qualities),
    }


# Common SkyBlock attribute names (shards / kuudra gear etc).
_KNOWN_ATTRIBUTES = {
    "mending", "life_regeneration", "blazing_resistance", "speed", "experience",
    "mana_pool", "fishing_experience", "double_hook", "magic_find", "veteran",
    "dominance", "lifeline", "elite", "ignition", "combo", "ferocious_mana",
    "midas_touch", "mana_regeneration", "fortitude", "life_recovery",
    "breeze", "warrior", "deadeye", "arachno", "attack_speed", "undead",
    "blazing", "ender", "trophy_hunter", "fisherman",
}


def _extract_attributes(flat_nbt: Dict[str, Any], auction: Dict[str, Any]) -> Dict[str, int]:
    """Return {attribute: level}."""
    attrs: Dict[str, int] = {}

    nested = auction.get("attributes")
    if isinstance(nested, dict):
        for name, level in nested.items():
            lvl = _as_int(level)
            if lvl:
                attrs[_lower(name)] = lvl

    for key, value in flat_nbt.items():
        kl = _lower(key)
        name = None
        if kl.startswith("attribute_"):
            name = kl.split("_", 1)[1]
        elif kl in _KNOWN_ATTRIBUTES:
            name = kl
        if name:
            lvl = _as_int(value)
            if lvl:
                attrs[name] = lvl
    return attrs


def _extract_hot_potato(flat_nbt: Dict[str, Any]) -> Dict[str, int]:
    total = _as_int(flat_nbt.get("hot_potato_count")) or 0
    # Beyond 10 are fuming potato books.
    hpb = min(total, 10)
    fuming = max(0, total - 10)
    return {"total": total, "hpb": hpb, "fuming": fuming}


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def extract_item_features(auction_json: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a normalised feature dict from any auction/listing dict."""
    auction = auction_json if isinstance(auction_json, dict) else {}

    flat_nbt = _to_dict(auction.get("flatNbt") or auction.get("flatnbt") or auction.get("nbt"))

    tag = auction.get("tag") or auction.get("itemTag") or auction.get("item_tag")
    tag = str(tag).upper().strip() if tag else None

    item_name_raw = (
        auction.get("itemName")
        or auction.get("item_name")
        or auction.get("name")
        or ""
    )
    item_name = _strip_color_codes(str(item_name_raw)).strip()

    rarity = _normalise_rarity(auction.get("tier") or auction.get("rarity") or flat_nbt.get("rarity"))

    reforge = _lower(auction.get("reforge") or flat_nbt.get("modifier")) or None
    if reforge in ("none", "0"):
        reforge = None

    count = _as_int(auction.get("count")) or 1

    enchants = _extract_enchants(auction, flat_nbt)
    important = _important_enchants(enchants)

    pet = _extract_pet(auction, flat_nbt, item_name, tag or "")
    stars = _extract_stars(auction, flat_nbt, item_name)
    recomb = _extract_recomb(flat_nbt, auction)
    gemstones = _extract_gemstones(flat_nbt)
    attributes = _extract_attributes(flat_nbt, auction)
    hot_potato = _extract_hot_potato(flat_nbt)

    bin_flag = bool(auction.get("bin", auction.get("isBin", False)))

    skin = _lower(flat_nbt.get("skin")) or None

    # Price on this listing (used when treating a listing as a comparable).
    price = (
        _as_int(auction.get("startingBid"))
        or _as_int(auction.get("price"))
        or _as_int(auction.get("highestBidAmount"))
    )

    features: Dict[str, Any] = {
        "item_tag": tag,
        "item_name": item_name,
        "rarity": rarity,
        "reforge": reforge,
        "count": count,
        "is_pet": pet is not None,
        "pet": pet,
        "stars": stars,
        "recombobulated": recomb,
        "hot_potato": hot_potato,
        "gemstones": gemstones,
        "attributes": attributes,
        "enchants": enchants,
        "important_enchants": important,
        "skin": skin,
        "bin": bin_flag,
        "price": price,
        "uuid": auction.get("uuid") or auction.get("auctionId") or auction.get("auction_uuid"),
    }
    return features


def _normalise_identity_name(name: str) -> str:
    """Lower-case, strip colour codes/star glyphs, collapse whitespace."""
    text = _strip_color_codes(name or "")
    text = _STAR_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def build_item_identity_key(features: Dict[str, Any]) -> str:
    """Stable identity for an item across relists (UUID changes, identity does not).

    Built from the value-defining traits so the same item relisted under a new
    auction UUID maps to the same key. For skins/cosmetics the item tag alone is
    usually enough, so we keep the key deliberately coarse there.
    """
    f = features or {}
    tag = str(f.get("item_tag") or "").upper()
    parts: List[str] = [f"tag={tag}"]

    name = _normalise_identity_name(str(f.get("item_name") or ""))

    is_skin = "SKIN" in tag or "COSMETIC" in tag or "skin" in name
    if is_skin:
        # Same skin tag is enough; do not over-split on incidental name noise.
        return "|".join(parts)

    if f.get("is_pet"):
        pet = f.get("pet") or {}
        parts.append("pet")
        parts.append(f"lvl={pet.get('level')}")
        parts.append(f"tier={pet.get('tier') or f.get('rarity')}")
        if pet.get("held_item"):
            parts.append(f"held={pet.get('held_item')}")
        if pet.get("skin"):
            parts.append(f"petskin={pet.get('skin')}")
    else:
        parts.append(f"name={name}")
        parts.append(f"rarity={f.get('rarity')}")
        parts.append(f"stars={int(f.get('stars') or 0)}")
        parts.append(f"recomb={1 if f.get('recombobulated') else 0}")
        gems = f.get("gemstones") or {}
        parts.append(f"gems={1 if gems.get('has_gems') else 0}")
        attrs = f.get("attributes") or {}
        if attrs:
            parts.append("attr=" + ",".join(f"{k}{v}" for k, v in sorted(attrs.items())))
        ench = f.get("important_enchants") or {}
        if ench:
            parts.append("ench=" + ",".join(f"{k}{v}" for k, v in sorted(ench.items())))
        if f.get("skin"):
            parts.append(f"skin={f.get('skin')}")
    return "|".join(parts)


def features_summary(features: Dict[str, Any]) -> str:
    """A short human tag line describing the item, e.g. for cards."""
    parts: List[str] = []
    if features.get("rarity"):
        parts.append(str(features["rarity"]).title().replace("_", " "))
    pet = features.get("pet")
    if pet:
        if pet.get("level"):
            parts.append(f"Lvl {pet['level']}")
        if pet.get("held_item"):
            parts.append(pet["held_item"].replace("_", " ").title())
    if features.get("stars"):
        parts.append(f"{features['stars']}★")
    if features.get("recombobulated"):
        parts.append("Recomb")
    if features.get("reforge"):
        parts.append(str(features["reforge"]).title())
    gems = features.get("gemstones") or {}
    if gems.get("has_gems"):
        parts.append("Gems")
    if features.get("attributes"):
        parts.append("Attr")
    imp = features.get("important_enchants") or {}
    if imp:
        parts.append(f"{len(imp)} key ench")
    return " · ".join(parts) if parts else "—"
