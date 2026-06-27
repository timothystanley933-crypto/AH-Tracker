"""Phone / Discord notifications.

Both channels are optional. Secrets are read from config and never logged.
A short message hash + cooldown prevents duplicate spam.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx

from . import db, profit
from .config import settings
from .formatting import format_coins, format_profit

log = logging.getLogger("notify")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def log_notification_decision(
    notif_type: str,
    auction_uuid: Optional[str],
    item_name: Optional[str],
    *,
    alert_type_enabled: bool,
    skipped_first_sync: bool = False,
    skipped_cooldown: bool = False,
    sent: bool = False,
) -> None:
    """One structured, secret-free log line explaining why an alert fired or not.

    Emitted for sold / relist / undercut alerts so the Railway logs make it
    obvious which gate stopped a notification (master switch, alert toggle,
    missing channel, first-sync suppression, or cooldown/dedup).
    """
    log.info(
        "notify type=%s auction_uuid=%s item_name=%r notifications_enabled=%s "
        "%s_alerts=%s pushover_configured=%s discord_configured=%s "
        "skipped_first_sync=%s skipped_cooldown=%s sent=%s",
        notif_type,
        auction_uuid,
        item_name,
        settings.notifications_enabled,
        notif_type,
        alert_type_enabled,
        settings.pushover_configured,
        settings.discord_configured,
        skipped_first_sync,
        skipped_cooldown,
        sent,
    )


async def _send_discord(title: str, body: str) -> bool:
    if not settings.discord_configured:
        return False
    content = f"**{title}**\n{body}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(settings.discord_webhook, json={"content": content[:1900]})
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        log.warning("Discord notification failed: %s", type(exc).__name__)
        return False


async def _send_pushover(title: str, body: str, url: Optional[str] = None) -> bool:
    if not settings.pushover_configured:
        return False
    payload = {
        "token": settings.pushover_app_token,
        "user": settings.pushover_user_key,
        "title": title[:250],
        "message": body[:1000],
        "priority": settings.pushover_priority,
        "sound": settings.pushover_sound,
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "Open on SkyCofl"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post("https://api.pushover.net/1/messages.json", data=payload)
        return resp.status_code < 300
    except Exception as exc:  # noqa: BLE001
        log.warning("Pushover notification failed: %s", type(exc).__name__)
        return False


async def send_raw(title: str, body: str, url: Optional[str] = None) -> bool:
    """Send to all configured channels. Returns True if any succeeded."""
    if not settings.notifications_enabled:
        log.info("Notifications disabled; not sending: %s", title)
        return False
    sent_any = False
    if await _send_discord(title, f"{body}\n{url or ''}".strip()):
        sent_any = True
    if await _send_pushover(title, body, url):
        sent_any = True
    if not (settings.discord_configured or settings.pushover_configured):
        log.info("No notification channel configured; skipping: %s", title)
    return sent_any


def _within_cooldown(uuid: str, decision: Optional[str], minutes: int) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return db.recent_notification_exists(uuid, decision, since)


# --------------------------------------------------------------------------
# Sold alert
# --------------------------------------------------------------------------

async def notify_sold(auction_row, sold_price: Optional[int], sold_time: Optional[str] = None) -> bool:
    uuid = auction_row["auction_uuid"]
    name = auction_row["item_name"] or auction_row["item_tag"] or "Item"

    if not settings.notifications_enabled or not settings.sold_alerts:
        log_notification_decision("sold", uuid, name, alert_type_enabled=settings.sold_alerts)
        return False

    buy_cost = auction_row["buy_cost"]
    url = settings.auction_url(uuid)

    lines = [f"Item: {name}"]
    if sold_price:
        lines.append(f"Sold for: {format_coins(sold_price)}")
    if buy_cost is not None:
        lines.append(f"Bought for: {format_coins(buy_cost)}")
        if sold_price:
            # True profit nets out sales tax AND every listing fee already paid.
            for extra in profit.fee_aware_lines(auction_row, sale_price=sold_price):
                lines.append(extra)
    if sold_time:
        lines.append(f"Time sold: {sold_time}")
    lines.append("")
    lines.append(url)

    body = "\n".join(lines)
    mh = _hash(f"sold:{uuid}:{sold_price}")

    # Avoid double-sending the same sale.
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    if db.message_hash_exists(mh, since):
        log_notification_decision(
            "sold", uuid, name, alert_type_enabled=settings.sold_alerts, skipped_cooldown=True
        )
        return False

    ok = await send_raw("💰 AH SOLD", body, url)
    db.record_notification(uuid, "sold", "SOLD", mh)
    log_notification_decision("sold", uuid, name, alert_type_enabled=settings.sold_alerts, sent=ok)
    return ok


# --------------------------------------------------------------------------
# Relist / decision alert
# --------------------------------------------------------------------------

_DECISION_ICON = {
    "RELIST": "⚠️",
    "CUT_LOSS": "🔻",
    "PROFIT_LOW": "🟡",
    "INCOMPARABLE": "⚪",
    "HOLD": "🟢",
}


async def notify_decision(auction_row, result) -> bool:
    """Send a decision alert if it qualifies (decision allowed + cooldown ok)."""
    uuid = auction_row["auction_uuid"]
    name = auction_row["item_name"] or auction_row["item_tag"] or "Item"

    if not settings.notifications_enabled or not settings.relist_alerts:
        log_notification_decision("relist", uuid, name, alert_type_enabled=settings.relist_alerts)
        return False

    decision = result.decision
    if decision not in settings.relist_alert_decisions:
        return False

    listing_price = auction_row["listing_price"] or 0
    buy_cost = auction_row["buy_cost"]
    url = settings.auction_url(uuid)

    # INCOMPARABLE alerts only for expensive tracked items (avoid noise).
    if decision == "INCOMPARABLE":
        value = max(listing_price or 0, buy_cost or 0)
        if value < settings.incomparable_alert_threshold:
            return False

    if _within_cooldown(uuid, decision, settings.relist_alert_cooldown_minutes):
        log_notification_decision(
            "relist", uuid, name, alert_type_enabled=settings.relist_alerts, skipped_cooldown=True
        )
        return False

    icon = _DECISION_ICON.get(decision, "ℹ️")
    title = f"{icon} {decision}: {name}"

    lines: List[str] = []
    if decision == "INCOMPARABLE":
        lines.append("No safe relist price suggested.")
        lines.append("Comparable listings did not match this item's quality.")
        lines.append("Do not undercut raw LBIN.")
    else:
        lines.append(f"Current: {format_coins(listing_price)}")
        if result.suggested_price:
            lines.append(f"Suggested: {format_coins(result.suggested_price)}")
        if buy_cost is not None:
            lines.append(f"Bought: {format_coins(buy_cost)}")
            # Fee-aware true profit (sales tax + every listing fee already paid).
            for extra in profit.fee_aware_lines(
                auction_row, sale_price=listing_price, relist_price=result.suggested_price
            ):
                lines.append(extra)
        lines.append(f"Comparables: {result.comparable_count}")
        lines.append(f"Confidence: {result.confidence}%")
        top = [format_coins(c.price) for c in result.comparables[:3]]
        if top:
            lines.append("Top comparables: " + ", ".join(top))

    if result.reasons:
        lines.append("")
        lines.append("Reason:")
        for r in result.reasons[:4]:
            lines.append(f"- {r}")
    lines.append("")
    lines.append(url)

    body = "\n".join(lines)
    mh = _hash(f"{decision}:{uuid}:{result.suggested_price}:{result.comparable_count}")

    since = (datetime.now(timezone.utc) - timedelta(minutes=settings.relist_alert_cooldown_minutes)).isoformat()
    if db.message_hash_exists(mh, since):
        log_notification_decision(
            "relist", uuid, name, alert_type_enabled=settings.relist_alerts, skipped_cooldown=True
        )
        return False

    ok = await send_raw(title, body, url)
    db.record_notification(uuid, "relist", decision, mh)
    log_notification_decision("relist", uuid, name, alert_type_enabled=settings.relist_alerts, sent=ok)
    return ok


async def send_startup_message() -> None:
    if not settings.notifications_enabled or not settings.startup_message:
        return
    if not (settings.discord_configured or settings.pushover_configured):
        return
    await send_raw(
        "✅ SkyCofl Relist Dashboard online",
        "Monitoring your auctions for sales and smart relist opportunities.\n"
        "Advisory only - all actions remain manual.",
    )


# --------------------------------------------------------------------------
# Diagnostics (no secrets ever leave this function)
# --------------------------------------------------------------------------

def diagnostics() -> dict:
    """Secret-free snapshot of notification config + scheduler state.

    Safe to render in templates or return from the API: only booleans,
    non-secret config values, and the last scheduler run/stats. The Discord
    webhook and Pushover keys themselves are NEVER included.
    """
    from . import scheduler  # local import avoids a circular import at module load

    return {
        "notifications_enabled": settings.notifications_enabled,
        "sold_alerts": settings.sold_alerts,
        "relist_alerts": settings.relist_alerts,
        "undercut_alerts": settings.undercut_alerts,
        "startup_message": settings.startup_message,
        "first_sync_suppress_sold_alerts": settings.first_sync_suppress_sold_alerts,
        "discord_configured": settings.discord_configured,
        "pushover_configured": settings.pushover_configured,
        "database_path": settings.database_path,
        "check_interval_seconds": settings.check_interval_seconds,
        "last_run": scheduler.last_run,
        "last_stats": scheduler.last_stats,
    }


async def send_test_notification() -> dict:
    """Send a test notification through the same channels used by real alerts.

    Never raises. Returns a structured, secret-free dict describing exactly what
    happened so a user can prove whether this service (e.g. running on Railway)
    can actually reach Discord / Pushover, and which alert toggles are active.
    """
    result = {
        "notifications_enabled": settings.notifications_enabled,
        "sold_alerts": settings.sold_alerts,
        "relist_alerts": settings.relist_alerts,
        "undercut_alerts": settings.undercut_alerts,
        "discord_configured": settings.discord_configured,
        "pushover_configured": settings.pushover_configured,
        "sent_discord": False,
        "sent_pushover": False,
        "errors": [],
    }

    if not settings.notifications_enabled:
        result["errors"].append("NOTIFICATIONS_ENABLED is false; no notification sent.")
        log_notification_decision("test", None, "test notification", alert_type_enabled=False)
        return result

    if not (settings.discord_configured or settings.pushover_configured):
        result["errors"].append("No notification channel configured (Discord or Pushover).")
        log_notification_decision("test", None, "test notification", alert_type_enabled=True)
        return result

    title = "🔔 SkyCofl test notification"
    body = (
        "Test notification from your SkyCofl dashboard.\n"
        "If you can read this, the service can reach this channel."
    )

    if settings.discord_configured:
        try:
            ok = await _send_discord(title, body)
            result["sent_discord"] = ok
            if not ok:
                result["errors"].append("Discord did not accept the message.")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"Discord error: {type(exc).__name__}")

    if settings.pushover_configured:
        try:
            ok = await _send_pushover(title, body)
            result["sent_pushover"] = ok
            if not ok:
                result["errors"].append("Pushover did not accept the message.")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"Pushover error: {type(exc).__name__}")

    log_notification_decision(
        "test",
        None,
        "test notification",
        alert_type_enabled=True,
        sent=bool(result["sent_discord"] or result["sent_pushover"]),
    )
    return result
