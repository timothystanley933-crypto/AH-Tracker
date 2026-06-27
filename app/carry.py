"""Carry buy cost to a relisted auction (new UUID, same item).

When you cancel/relist an item, Hypixel assigns a new auction UUID, so the app
sees a fresh auction with a blank buy cost. This module looks for a recent
SOLD/EXPIRED/STALE auction of the *same item* that still has a saved buy cost
and offers to carry the user-owned fields across.

Safety:
- Read-only market data only; nothing is bought/listed/cancelled.
- Never silently auto-copies unless RELIST_CARRY_AUTO_APPLY is on AND a single
  near-certain match exists. By default it only *suggests*; the user confirms.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import analysis, cofl_client, db
from .config import settings
from .features import build_item_identity_key, extract_item_features
from .formatting import format_coins
from .scoring import score_comparable

log = logging.getLogger("carry")

# Auto-apply (when enabled) requires near-certainty, well above the suggest score.
CARRY_AUTO_MIN_SCORE = 98

_CLEAR_MISMATCH_TERMS = (
    "different pet tier",
    "pet level too different",
    "rarity too different",
    "different pet skin",
    "star level differs",
    "recomb mismatch",
    "gemstone mismatch",
    "missing/weak matching attributes",
    "candidate has attributes",
    "missing key enchants",
    "candidate has extra key enchants",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def _features_for(uuid: str, row) -> Dict[str, Any]:
    """Best-effort item features for an auction (no extra network when avoidable).

    Prefers features already stored on the auction's latest analysis (old tracked
    auctions with a buy cost have been analysed), then a detail fetch, then a
    minimal fallback from the row.
    """
    a = db.latest_analysis(uuid)
    if a is not None:
        raw = a["item_features_json"]
        if raw:
            try:
                feats = json.loads(raw)
                if isinstance(feats, dict) and feats.get("item_tag"):
                    return feats
            except (ValueError, TypeError):
                pass
    detail = await cofl_client.get_auction_detail(uuid)
    if detail:
        return extract_item_features(detail)
    return extract_item_features(
        {"tag": row["item_tag"], "itemName": row["item_name"], "startingBid": row["listing_price"]}
    )


def _reason(result, old_row) -> str:
    bits = [f"Same item ({old_row['item_tag']})."]
    for r in (result.reasons or [])[:2]:
        bits.append(f"{r}.")
    bits.append(f"Match score {result.score}%.")
    return " ".join(bits)


def _is_clear_mismatch(result) -> bool:
    text = " ".join(result.rejections or []).lower()
    return any(term in text for term in _CLEAR_MISMATCH_TERMS)


def _manual_reason(result, old_row) -> str:
    detail = ""
    if result.rejections:
        detail = f" {result.rejections[0]}."
    elif result.reasons:
        detail = f" {result.reasons[0]}."
    return (
        f"Same item tag ({old_row['item_tag']}) but confidence is below the automatic threshold."
        f"{detail} Check this is the same item before carrying."
    )


def _humanize_age(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _eligible(row) -> bool:
    """Is this new auction a candidate for a carry suggestion?"""
    if row is None:
        return False
    if row["buy_cost"] is not None:
        return False
    if row["carry_suggestion_ignored"]:
        return False
    if row["carried_from_uuid"]:
        return False
    if (row["status"] or "ACTIVE") != "ACTIVE":
        return False
    return bool(row["item_tag"])


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

async def detect_carry_candidates(new_uuid: str, include_manual: bool = False) -> List[Dict[str, Any]]:
    """Return carry-source candidates for a new auction (does not persist)."""
    row = db.get_auction(new_uuid)
    if not _eligible(row):
        return []

    olds = db.get_carry_candidates(new_uuid, settings.relist_carry_lookback_days)
    if not olds:
        return []

    new_features = await _features_for(new_uuid, row)
    if not new_features.get("item_tag"):
        return []

    candidates: List[Dict[str, Any]] = []
    for old in olds:
        old_features = await _features_for(old["auction_uuid"], old)
        result = score_comparable(new_features, old_features)
        if result.score >= settings.relist_carry_min_score:
            candidates.append(
                {
                    "old_uuid": old["auction_uuid"],
                    "item_name": old["item_name"],
                    "buy_cost": old["buy_cost"],
                    "confidence": result.score,
                    "reason": _reason(result, old),
                    "confidence_level": "strong",
                }
            )
        elif include_manual or not _is_clear_mismatch(result):
            candidates.append(
                {
                    "old_uuid": old["auction_uuid"],
                    "item_name": old["item_name"],
                    "buy_cost": old["buy_cost"],
                    "confidence": max(1, result.score),
                    "reason": _manual_reason(result, old),
                    "confidence_level": "manual",
                }
            )
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


async def build_and_store_suggestions(new_uuid: str, include_manual: bool = False) -> List[Any]:
    """Detect and persist pending carry suggestions for a new auction.

    Returns the pending relist_links rows. May auto-apply only when explicitly
    enabled and a single near-certain match exists.
    """
    if not settings.relist_carry_enabled:
        return []
    row = db.get_auction(new_uuid)
    if not _eligible(row):
        return []

    # Don't recompute normal dashboard suggestions if we've already produced links.
    # Manual lookup can add lower-confidence same-tag candidates later.
    if not include_manual and db.has_any_relist_link(new_uuid):
        return db.get_pending_carry_links(new_uuid)

    candidates = await detect_carry_candidates(new_uuid, include_manual=include_manual)
    for c in candidates:
        db.upsert_relist_suggestion(c["old_uuid"], new_uuid, c["confidence"], c["reason"])

    if (
        settings.relist_carry_auto_apply
        and len(candidates) == 1
        and candidates[0].get("confidence_level") == "strong"
        and candidates[0]["confidence"] >= CARRY_AUTO_MIN_SCORE
    ):
        log.info("Auto-applying carry for %s (confidence %s)", new_uuid, candidates[0]["confidence"])
        await carry(new_uuid, candidates[0]["old_uuid"])
        return []

    return db.get_pending_carry_links(new_uuid)


async def run_for_new_auctions(uuids: List[str]) -> int:
    """Run detection for a batch of newly-seen auctions. Returns suggestions made."""
    if not settings.relist_carry_enabled:
        return 0
    created = 0
    for uuid in uuids:
        try:
            before = len(db.get_pending_carry_links(uuid))
            links = await build_and_store_suggestions(uuid)
            created += max(0, len(links) - before)
        except Exception as exc:  # noqa: BLE001 - never let carry crash a sync
            log.warning("Carry detection failed for %s: %s", uuid, exc)
    return created


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def _format_link(link) -> Dict[str, Any]:
    ts = link["old_sold_at"] or link["old_ends_at"] or link["old_last_seen"] or link["old_updated_at"]
    level = "strong" if int(link["confidence"] or 0) >= settings.relist_carry_min_score else "manual"
    return {
        "old_auction_uuid": link["old_auction_uuid"],
        "old_uuid": link["old_auction_uuid"],
        "item_name": link["old_item_name"],
        "buy_cost": link["old_buy_cost"],
        "buy_cost_fmt": format_coins(link["old_buy_cost"]),
        "min_profit": link["old_min_profit"],
        "target_sell_price": link["old_target_sell_price"],
        "notes": link["old_notes"],
        "old_status": link["old_status"],
        "confidence": link["confidence"],
        "confidence_level": level,
        "manual": level == "manual",
        "reason": link["reason"],
        "age": _humanize_age(ts),
    }


async def get_suggestions(new_uuid: str, include_manual: bool = False) -> List[Dict[str, Any]]:
    """Pending suggestions for a new auction, building them if needed."""
    if not settings.relist_carry_enabled:
        return []
    row = db.get_auction(new_uuid)
    if not _eligible(row):
        return []
    await build_and_store_suggestions(new_uuid, include_manual=include_manual)
    return [_format_link(l) for l in db.get_carry_suggestions(new_uuid)]


def pending_for_cards() -> Dict[str, List[Dict[str, Any]]]:
    """Map new_uuid -> formatted pending suggestions, for dashboard cards."""
    if not settings.relist_carry_enabled:
        return {}
    raw = db.pending_carry_links_map()
    return {uuid: [_format_link(l) for l in links] for uuid, links in raw.items()}


async def carry(new_uuid: str, old_uuid: str) -> Dict[str, Any]:
    """Copy user-owned fields from old -> new, link them, and re-analyse."""
    if not settings.relist_carry_enabled:
        return {"ok": False, "error": "carry suggestions are disabled"}
    row = db.get_auction(new_uuid)
    if row is None:
        return {"ok": False, "error": "new auction not found"}
    if row["buy_cost"] is not None:
        return {"ok": False, "error": "new auction already has a buy cost"}
    if (row["status"] or "ACTIVE") != "ACTIVE":
        return {"ok": False, "error": "new auction is not active"}

    await build_and_store_suggestions(new_uuid)
    pending = db.get_carry_suggestions(new_uuid)
    if not any(link["old_auction_uuid"] == old_uuid for link in pending):
        return {"ok": False, "error": "carry suggestion not found"}

    ok = db.copy_user_fields_to_relisted_auction(new_uuid, old_uuid)
    if not ok:
        return {"ok": False, "error": "could not copy user fields"}
    db.accept_carry_suggestion(new_uuid, old_uuid)

    # This new auction IS a relist: record the RELIST fee and carry the previous
    # listing's accumulated fees across. Deduped, so a re-accept / reload is a
    # no-op (no double counting, no extra relist_count).
    try:
        new_features = await _features_for(new_uuid, row)
        db.record_relist_fee(
            new_uuid, old_uuid, row["listing_price"],
            build_item_identity_key(new_features),
        )
    except Exception as exc:  # noqa: BLE001 - fee ledger must not block the carry
        log.warning("relist fee recording failed for %s: %s", new_uuid, exc)

    try:
        await analysis.analyse_auction(new_uuid)
    except Exception as exc:  # noqa: BLE001
        log.warning("Re-analysis after carry failed for %s: %s", new_uuid, exc)
    row = db.get_auction(new_uuid)
    return {
        "ok": True,
        "carried": True,
        "buy_cost": row["buy_cost"] if row else None,
        "buy_cost_fmt": format_coins(row["buy_cost"]) if row else "",
        "carried_from": old_uuid,
    }


def ignore(new_uuid: str) -> None:
    db.ignore_carry_suggestions(new_uuid)
