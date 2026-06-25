"""Auction sync with a proper state machine.

Read-only. We never act on Hypixel - we only observe and record.

The player auctions endpoint returns *recent* auctions, not only active ones,
so we must classify every returned auction rather than assuming it is active:

    ACTIVE   - BIN (when known), no bids, end in the future, startingBid > 0
    SOLD     - has a winning bid (highestBid > 0)
    EXPIRED  - end time passed with no bids
    STALE    - was ACTIVE in the DB but missing from N consecutive syncs

Notifications only fire for a real ACTIVE -> SOLD transition we actually
observed. Historical sales (present on the first sync, or appearing already
sold) are recorded but flagged handled, so they never spam.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import carry, cofl_client, db, notifications
from .config import settings

log = logging.getLogger("sync")


# --------------------------------------------------------------------------
# Defensive field parsing
# --------------------------------------------------------------------------

def _first(item: Dict[str, Any], *keys):
    for k in keys:
        if k in item and item[k] is not None:
            return item[k]
    return None


def _to_int(value: Any) -> int:
    try:
        if value is None or isinstance(value, bool):
            return 0
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def _to_bool(value: Any) -> Optional[bool]:
    """Returns True/False, or None when the field is absent/unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n"):
        return False
    return None


def _parse_end(value: Any) -> Optional[datetime]:
    """Parse an end time defensively, always returning UTC-aware datetimes."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def parse_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    """Robustly extract the fields we need, tolerating field-name casing."""
    uuid = _first(item, "uuid", "auctionId", "auction_uuid", "auctionUuid", "Uuid")
    tag = _first(item, "tag", "itemTag", "item_tag", "Tag")
    name = _first(item, "itemName", "item_name", "name", "ItemName")
    # Active listing price ONLY ever comes from the starting bid.
    starting = _first(item, "startingBid", "StartingBid", "starting_bid", "startingbid")
    # Sold price ONLY ever comes from the highest/winning bid.
    highest = _first(item, "highestBid", "HighestBid", "highestBidAmount", "highest_bid", "HighestBidAmount")
    bin_flag = _first(item, "bin", "Bin", "isBin", "is_bin", "BIN")
    end = _first(item, "end", "End", "ends", "endTime", "endingAt", "end_time")
    return {
        "uuid": str(uuid) if uuid else None,
        "tag": str(tag).upper() if tag else None,
        "name": name,
        "starting": _to_int(starting),
        "highest": _to_int(highest),
        "bin": _to_bool(bin_flag),
        "end": _parse_end(end),
    }


def classify(f: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """Classify a parsed auction as ACTIVE / SOLD / EXPIRED."""
    now = now or datetime.now(timezone.utc)
    # A winning bid means it sold (BIN buys register as the highest bid).
    if f["highest"] and f["highest"] > 0:
        return "SOLD"
    ended = f["end"] is not None and f["end"] <= now
    if ended:
        return "EXPIRED"
    # ACTIVE requires a real price and, when the BIN flag is present, that it is BIN.
    if f["starting"] and f["starting"] > 0 and f["bin"] is not False:
        return "ACTIVE"
    # No price / non-BIN with no bids and no past end -> not something we track as active.
    return "EXPIRED"


def _detail_says_sold(detail: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(detail, dict):
        return False
    f = parse_fields(detail)
    return f["highest"] > 0


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

async def sync_player_auctions() -> Dict[str, int]:
    """Sync the configured player's auctions. Returns a stats dict."""
    stats = {
        "seen": 0,
        "active": 0,
        "sold": 0,
        "expired": 0,
        "stale": 0,
        "notified": 0,
        "carry_suggestions": 0,
        "errors": 0,
    }

    if not settings.mc_uuid:
        log.warning("MC_UUID not configured; skipping sync.")
        return stats

    first_sync = db.count_tracked() == 0
    sync_id = int(datetime.now(timezone.utc).timestamp())
    now = datetime.now(timezone.utc)

    items = await cofl_client.get_all_player_auctions(settings.mc_uuid)
    if items is None:
        # Fetch failed - do NOT run the stale pass or we'd wrongly age everything out.
        log.warning("Player auctions fetch failed; skipping this cycle (no state changes).")
        stats["errors"] += 1
        return stats

    seen_uuids = set()
    active_uuids = []

    for item in items:
        try:
            f = parse_fields(item)
            if not f["uuid"]:
                continue
            seen_uuids.add(f["uuid"])
            stats["seen"] += 1

            status = classify(f, now)
            prev = db.get_auction(f["uuid"])
            url = settings.auction_url(f["uuid"])
            ends_iso = f["end"].isoformat() if f["end"] else None

            if status == "ACTIVE":
                db.upsert_synced(
                    uuid=f["uuid"], item_tag=f["tag"], item_name=f["name"], skycofl_url=url,
                    status="ACTIVE", listing_price=f["starting"], sold_price=None,
                    ends_at=ends_iso, sync_id=sync_id,
                    notification_eligible=1, sold_notified=0,
                )
                stats["active"] += 1
                active_uuids.append(f["uuid"])

            elif status == "SOLD":
                # Notify ONLY for an observed ACTIVE -> SOLD transition.
                prev_active = prev is not None and prev["status"] == "ACTIVE"
                prev_eligible = prev is not None and bool(prev["notification_eligible"])
                prev_notified = prev is not None and bool(prev["sold_notified"])
                first_sync_block = first_sync and settings.first_sync_suppress_sold_alerts

                should_notify = (
                    not first_sync_block
                    and prev_active
                    and prev_eligible
                    and not prev_notified
                    and settings.notifications_enabled
                    and settings.sold_alerts
                )

                # Record as SOLD and flag handled so it can never re-notify.
                # sold_at = auction end time if known, else now (UTC).
                sold_at = ends_iso or datetime.now(timezone.utc).isoformat()
                db.upsert_synced(
                    uuid=f["uuid"], item_tag=f["tag"], item_name=f["name"], skycofl_url=url,
                    status="SOLD", listing_price=f["starting"] or None, sold_price=f["highest"],
                    ends_at=ends_iso, sync_id=sync_id,
                    notification_eligible=0, sold_notified=1, sold_at=sold_at,
                )
                stats["sold"] += 1

                if should_notify:
                    fresh = db.get_auction(f["uuid"])
                    if fresh is not None:
                        ok = await notifications.notify_sold(fresh, f["highest"], ends_iso)
                        if ok:
                            stats["notified"] += 1

            else:  # EXPIRED
                db.upsert_synced(
                    uuid=f["uuid"], item_tag=f["tag"], item_name=f["name"], skycofl_url=url,
                    status="EXPIRED", listing_price=f["starting"] or None, sold_price=None,
                    ends_at=ends_iso, sync_id=sync_id,
                    notification_eligible=0, sold_notified=0,
                )
                stats["expired"] += 1

        except Exception as exc:  # noqa: BLE001 - never let one auction crash the sync
            log.warning("Error processing an auction: %s", exc)
            stats["errors"] += 1

    # Age out ACTIVE auctions that vanished from the listing.
    stats["stale"] = db.stale_pass(seen_uuids, threshold=settings.stale_after_missed_syncs)

    if active_uuids and settings.relist_carry_enabled:
        try:
            stats["carry_suggestions"] = await carry.run_for_new_auctions(active_uuids)
        except Exception as exc:  # noqa: BLE001 - carry must not block sync
            log.warning("Carry suggestion pass failed: %s", exc)
            stats["errors"] += 1

    log.info("Sync %s complete: %s", sync_id, stats)
    return stats
