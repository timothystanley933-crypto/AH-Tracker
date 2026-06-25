"""Background scheduler loop.

Runs inside the FastAPI process. Every CHECK_INTERVAL_SECONDS it:
  1. Syncs the player's auctions (and fires sold alerts).
  2. Analyses tracked auctions that have a buy cost.
  3. Fires relist/decision alerts when appropriate.

Errors in one auction never crash the loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from . import analysis, db, notifications, sync, undercut
from .config import settings

log = logging.getLogger("scheduler")

# Exposed so the dashboard can show "last refresh time".
last_run: Optional[str] = None
last_stats: dict = {}
_running = False


async def run_once() -> dict:
    """One full cycle. Safe to call manually from an endpoint too."""
    global last_run, last_stats
    stats = {
        "synced": {},
        "analysed": 0,
        "alerts": 0,
        "undercuts_checked": 0,
        "undercuts_found": 0,
        "undercut_alerts": 0,
        "undercut_cooldowns": 0,
        "errors": 0,
    }

    try:
        stats["synced"] = await sync.sync_player_auctions()
    except Exception as exc:  # noqa: BLE001
        log.warning("Sync failed: %s", exc)
        stats["errors"] += 1

    # Analyse tracked auctions with a buy cost that are still active & not ignored.
    try:
        for row in db.list_auctions(include_inactive=False):
            if row["ignored"] or row["sold"]:
                continue
            if row["buy_cost"] is None:
                continue
            try:
                result = await analysis.analyse_auction(row["auction_uuid"])
                if result is None:
                    continue
                stats["analysed"] += 1
                fresh = db.get_auction(row["auction_uuid"])
                if fresh is not None:
                    sent = await notifications.notify_decision(fresh, result)
                    if sent:
                        stats["alerts"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("Analysis failed for %s: %s", row["auction_uuid"], exc)
                stats["errors"] += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("Analysis loop failed: %s", exc)
        stats["errors"] += 1

    try:
        undercut_stats = await undercut.check_active_auctions(notify=True)
        stats["undercuts_checked"] = undercut_stats["checked"]
        stats["undercuts_found"] = undercut_stats["found"]
        stats["undercut_alerts"] = undercut_stats["notified"]
        stats["undercut_cooldowns"] = undercut_stats["cooldown"]
        stats["errors"] += undercut_stats["errors"]
    except Exception as exc:  # noqa: BLE001
        log.warning("Undercut loop failed: %s", exc)
        stats["errors"] += 1

    last_run = datetime.now(timezone.utc).isoformat()
    last_stats = stats
    log.info("Cycle complete: %s", stats)
    return stats


async def background_loop() -> None:
    global _running
    if _running:
        return
    _running = True
    interval = max(30, settings.check_interval_seconds)
    log.info("Background loop started (interval=%ss).", interval)
    # Small delay so the web server is fully up before the first sync.
    await asyncio.sleep(3)
    while _running:
        try:
            await run_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("Background cycle error: %s", exc)
        await asyncio.sleep(interval)


def stop() -> None:
    global _running
    _running = False
