"""Undercut / cheaper comparable detection.

Read-only advisory logic: this checks whether another active BIN listing is a
similar-or-better item listed meaningfully cheaper than the user's auction.
It never buys, lists, cancels, or uses raw LBIN alone as an alert.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import cofl_client, db, notifications
from .config import settings
from .features import RARITY_ORDER, extract_item_features
from .formatting import format_coins
from .scoring import score_comparable

log = logging.getLogger("undercut")


@dataclass
class UndercutMatch:
    candidate_uuid: Optional[str]
    candidate_item_name: str
    candidate_price: int
    gap_coins: int
    gap_percent: float
    confidence: int
    reason: str
    possible: bool = False
    better: bool = False


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _rarity_rank(value: Any) -> int:
    if not value:
        return -1
    try:
        return RARITY_ORDER.index(str(value).upper())
    except ValueError:
        return -1


def _price(features: Dict[str, Any]) -> int:
    try:
        return int(features.get("price") or 0)
    except (ValueError, TypeError):
        return 0


def _features_incomplete(features: Dict[str, Any]) -> bool:
    if not features:
        return True
    if features.get("is_pet"):
        pet = features.get("pet") or {}
        return pet.get("level") is None and not pet.get("tier")
    return not any(
        features.get(k)
        for k in ("rarity", "stars", "recombobulated", "attributes", "important_enchants", "skin")
    )


def _is_skin_or_cosmetic(features: Dict[str, Any]) -> bool:
    tag = str(features.get("item_tag") or "").upper()
    name = str(features.get("item_name") or "").upper()
    return "SKIN" in tag or "SKIN" in name or "COSMETIC" in tag


def _attrs_ok(my_attrs: Dict[str, int], cand_attrs: Dict[str, int]) -> Tuple[bool, bool, str]:
    if not my_attrs:
        return True, bool(cand_attrs), "candidate has extra attributes" if cand_attrs else ""
    if not cand_attrs:
        return False, False, "candidate is missing attributes"
    better = False
    for name, my_level in my_attrs.items():
        cand_level = cand_attrs.get(name)
        if cand_level is None:
            return False, False, f"candidate missing attribute {name}"
        if cand_level < my_level:
            return False, False, f"candidate has lower {name}"
        if cand_level > my_level:
            better = True
    if set(cand_attrs) - set(my_attrs):
        better = True
    return True, better, "attributes same or better"


def _enchants_ok(my_enchants: Dict[str, int], cand_enchants: Dict[str, int]) -> Tuple[bool, bool, str]:
    if not my_enchants:
        return True, bool(cand_enchants), "candidate has extra key enchants" if cand_enchants else ""
    if not cand_enchants:
        return False, False, "candidate missing key enchants"
    better = False
    for name, my_level in my_enchants.items():
        cand_level = cand_enchants.get(name)
        if cand_level is None:
            return False, False, f"candidate missing {name}"
        if cand_level < my_level:
            return False, False, f"candidate has lower {name}"
        if cand_level > my_level:
            better = True
    if set(cand_enchants) - set(my_enchants):
        better = True
    return True, better, "key enchants same or better"


def is_similar_or_better_for_undercut(
    my_features: Dict[str, Any], candidate_features: Dict[str, Any]
) -> Tuple[bool, str, bool, bool]:
    """Return (eligible, reason, better, possible) for undercut comparison."""
    my = my_features or {}
    cand = candidate_features or {}
    if not my.get("item_tag") or my.get("item_tag") != cand.get("item_tag"):
        return False, "Different item tag.", False, False

    if _is_skin_or_cosmetic(my):
        if _features_incomplete(my) or _features_incomplete(cand):
            return True, "Same skin/cosmetic item tag; feature data is limited.", False, True
        return True, "Same skin/cosmetic item tag.", False, False

    better = False
    notes: List[str] = []

    if my.get("is_pet"):
        if not cand.get("is_pet"):
            return False, "Candidate is not a pet.", False, False
        mp, cp = my.get("pet") or {}, cand.get("pet") or {}

        my_tier = mp.get("tier") or my.get("rarity")
        cand_tier = cp.get("tier") or cand.get("rarity")
        if my_tier and cand_tier:
            if _rarity_rank(cand_tier) < _rarity_rank(my_tier):
                return False, f"Candidate pet tier is worse ({cand_tier} vs {my_tier}).", False, False
            if _rarity_rank(cand_tier) > _rarity_rank(my_tier):
                better = True
                notes.append("higher pet tier")

        my_level, cand_level = mp.get("level"), cp.get("level")
        if my_level is not None and cand_level is not None:
            if cand_level + settings.relist_pet_level_tolerance < my_level:
                return False, f"Candidate pet level is worse ({cand_level} vs {my_level}).", False, False
            if cand_level > my_level:
                better = True
                notes.append("higher pet level")

        if mp.get("held_item") and cp.get("held_item") and mp.get("held_item") != cp.get("held_item"):
            return False, "Candidate held item differs.", False, False
        if not mp.get("held_item") and cp.get("held_item"):
            better = True
            notes.append("has held item")
        if mp.get("skin") and cp.get("skin") and mp.get("skin") != cp.get("skin"):
            return False, "Candidate pet skin differs.", False, False
        if not mp.get("skin") and cp.get("skin"):
            better = True
            notes.append("has pet skin")

        if _features_incomplete(my) or _features_incomplete(cand):
            return True, "Same pet tag; feature data is incomplete.", better, True
        reason = "Candidate pet is better: " + ", ".join(notes) if better else "Candidate pet is similar quality."
        return True, reason, better, False

    if cand.get("is_pet"):
        return False, "Candidate is a pet, mine is not.", False, False

    my_rank, cand_rank = _rarity_rank(my.get("rarity")), _rarity_rank(cand.get("rarity"))
    if my_rank >= 0 and cand_rank >= 0:
        if cand_rank < my_rank:
            return False, f"Candidate rarity is worse ({cand.get('rarity')} vs {my.get('rarity')}).", False, False
        if cand_rank > my_rank:
            better = True
            notes.append("higher rarity")

    my_stars, cand_stars = int(my.get("stars") or 0), int(cand.get("stars") or 0)
    if cand_stars < my_stars:
        return False, f"Candidate has fewer stars ({cand_stars} vs {my_stars}).", False, False
    if cand_stars > my_stars:
        better = True
        notes.append("higher stars")

    if my.get("recombobulated") and not cand.get("recombobulated"):
        return False, "Candidate is not recombobulated.", False, False
    if cand.get("recombobulated") and not my.get("recombobulated"):
        better = True
        notes.append("recombobulated")

    my_gems, cand_gems = my.get("gemstones") or {}, cand.get("gemstones") or {}
    my_has_gems, cand_has_gems = bool(my_gems.get("has_gems")), bool(cand_gems.get("has_gems"))
    if my_has_gems and not cand_has_gems:
        return False, "Candidate is missing gemstones.", False, False
    if cand_has_gems and not my_has_gems:
        better = True
        notes.append("has gemstones")

    attrs_ok, attrs_better, attrs_reason = _attrs_ok(my.get("attributes") or {}, cand.get("attributes") or {})
    if not attrs_ok:
        return False, attrs_reason, False, False
    better = better or attrs_better
    if attrs_better:
        notes.append(attrs_reason)

    ench_ok, ench_better, ench_reason = _enchants_ok(
        my.get("important_enchants") or {}, cand.get("important_enchants") or {}
    )
    if not ench_ok:
        return False, ench_reason, False, False
    better = better or ench_better
    if ench_better:
        notes.append(ench_reason)

    if _features_incomplete(my) or _features_incomplete(cand):
        return True, "Same item tag; feature data is incomplete.", better, True
    reason = "Candidate is better: " + ", ".join(notes) if better else "Candidate is similar quality."
    return True, reason, better, False


async def _features_for(uuid: str, row) -> Dict[str, Any]:
    latest = db.latest_analysis(uuid)
    if latest is not None:
        raw = latest["item_features_json"]
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("item_tag"):
                    return parsed
            except (TypeError, ValueError):
                pass
    detail = await cofl_client.get_auction_detail(uuid)
    source = detail or {
        "uuid": uuid,
        "tag": row["item_tag"],
        "itemName": row["item_name"],
        "startingBid": row["listing_price"],
        "bin": True,
    }
    return extract_item_features(source)


def _meaningful_gap(my_price: int, candidate_price: int) -> Tuple[bool, int, float]:
    gap = max(0, my_price - candidate_price)
    pct = round((gap / my_price * 100.0), 1) if my_price else 0.0
    return (
        gap >= settings.undercut_min_gap_coins or pct >= settings.undercut_min_gap_percent,
        gap,
        pct,
    )


async def find_undercut(uuid: str) -> Optional[UndercutMatch]:
    if not settings.undercut_check_enabled:
        return None
    row = db.get_auction(uuid)
    if row is None or row["ignored"] or row["sold"] or row["status"] != "ACTIVE":
        return None
    my_price = int(row["listing_price"] or 0)
    if my_price <= 0 or not row["item_tag"]:
        return None

    my_features = await _features_for(uuid, row)
    candidates = await cofl_client.get_active_bins_pages(row["item_tag"], settings.relist_comparable_pages)
    checked = 0
    matches: List[UndercutMatch] = []
    for raw in candidates:
        if checked >= settings.undercut_max_candidates_to_check:
            break
        cand_features = extract_item_features(raw)
        cand_uuid = cand_features.get("uuid")
        if cand_uuid == uuid:
            continue
        cand_price = _price(cand_features)
        if cand_price <= 0 or cand_price >= my_price:
            continue
        checked += 1
        ok_gap, gap, pct = _meaningful_gap(my_price, cand_price)
        if not ok_gap:
            continue
        if cand_features.get("bin") is False:
            continue

        eligible, reason, better, possible = is_similar_or_better_for_undercut(my_features, cand_features)
        if not eligible:
            continue
        score = score_comparable(my_features, cand_features).score
        if better:
            confidence = max(settings.undercut_better_item_score, score)
        elif possible:
            confidence = min(70, max(score, 55))
        else:
            confidence = max(settings.undercut_min_comparable_score, score)
        if possible and not settings.undercut_include_possible:
            continue
        if not possible and confidence < settings.undercut_min_comparable_score:
            continue

        matches.append(
            UndercutMatch(
                candidate_uuid=cand_uuid,
                candidate_item_name=cand_features.get("item_name") or row["item_tag"],
                candidate_price=cand_price,
                gap_coins=gap,
                gap_percent=pct,
                confidence=int(min(99, confidence)),
                reason=f"{reason} Cheaper by more than threshold.",
                possible=possible,
                better=better,
            )
        )

    if not matches:
        return None
    matches.sort(key=lambda m: (m.confidence, m.gap_coins), reverse=True)
    return matches[0]


def _result_payload(uuid: str, match: UndercutMatch, alert_id: Optional[int], notified: bool, cooldown: bool) -> Dict[str, Any]:
    return {
        "ok": True,
        "undercut": True,
        "alert_id": alert_id,
        "auction_uuid": uuid,
        "candidate_uuid": match.candidate_uuid,
        "candidate_item_name": match.candidate_item_name,
        "candidate_price": match.candidate_price,
        "gap_coins": match.gap_coins,
        "gap_percent": match.gap_percent,
        "confidence": match.confidence,
        "reason": match.reason,
        "possible": match.possible,
        "notified": notified,
        "cooldown": cooldown,
    }


def _decision_allows_notify(uuid: str) -> bool:
    latest = db.latest_analysis(uuid)
    decision = latest["decision"] if latest is not None else "ACTIVE"
    return decision in settings.undercut_notify_decisions


async def check_auction(uuid: str, *, notify: bool = False) -> Dict[str, Any]:
    match = await find_undercut(uuid)
    if match is None:
        return {"ok": True, "undercut": False}

    row = db.get_auction(uuid)
    if row is None:
        return {"ok": False, "error": "auction not found"}
    my_price = int(row["listing_price"] or 0)
    mh = _hash(f"undercut:{uuid}:{match.candidate_uuid}:{my_price}:{match.candidate_price}")
    cooldown = False
    notified = False
    alert_id: Optional[int] = None

    if notify and settings.notifications_enabled and settings.undercut_alerts and _decision_allows_notify(uuid):
        cooldown = db.recent_undercut_alert_exists(
            uuid, match.candidate_uuid, mh, settings.undercut_cooldown_minutes, notified_only=True
        )
        if not cooldown:
            alert_id = db.record_undercut_alert(
                auction_uuid=uuid,
                candidate_uuid=match.candidate_uuid,
                item_tag=row["item_tag"],
                my_price=my_price,
                candidate_price=match.candidate_price,
                gap_coins=match.gap_coins,
                gap_percent=match.gap_percent,
                confidence=match.confidence,
                candidate_item_name=match.candidate_item_name,
                reason=match.reason,
                notification_hash=mh,
                notified=False,
            )
            title = f"Undercut detected: {row['item_name'] or row['item_tag']}"
            body = "\n".join(
                [
                    f"Your: {row['item_name'] or row['item_tag']} - {format_coins(my_price)}",
                    f"Cheaper comparable: {format_coins(match.candidate_price)}",
                    f"Gap: -{format_coins(match.gap_coins)} (-{match.gap_percent:g}%)",
                    f"Confidence: {match.confidence}%",
                    "",
                    f"Reason: {match.reason}",
                    "",
                    settings.auction_url(uuid),
                ]
            )
            await notifications.send_raw(title, body, settings.auction_url(uuid))
            db.mark_undercut_alert_notified(alert_id, mh)
            notified = True

    if alert_id is None and not cooldown:
        alert_id = db.record_undercut_alert(
            auction_uuid=uuid,
            candidate_uuid=match.candidate_uuid,
            item_tag=row["item_tag"],
            my_price=my_price,
            candidate_price=match.candidate_price,
            gap_coins=match.gap_coins,
            gap_percent=match.gap_percent,
            confidence=match.confidence,
            candidate_item_name=match.candidate_item_name,
            reason=match.reason,
            notification_hash=mh,
            notified=notified,
        )

    return _result_payload(uuid, match, alert_id, notified, cooldown)


async def check_active_auctions(*, notify: bool = False) -> Dict[str, int]:
    stats = {"checked": 0, "found": 0, "notified": 0, "cooldown": 0, "errors": 0}
    if not settings.undercut_check_enabled:
        return stats
    for row in db.list_auctions(include_inactive=False):
        if row["ignored"] or row["sold"] or not row["listing_price"]:
            continue
        try:
            result = await check_auction(row["auction_uuid"], notify=notify)
            stats["checked"] += 1
            if result.get("undercut"):
                stats["found"] += 1
            if result.get("notified"):
                stats["notified"] += 1
            if result.get("cooldown"):
                stats["cooldown"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Undercut check failed for %s: %s", row["auction_uuid"], exc)
            stats["errors"] += 1
    return stats
