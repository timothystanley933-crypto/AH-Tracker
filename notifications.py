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

from . import db
from .config import settings
from .formatting import format_coins, format_profit

log = logging.getLogger("notify")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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
    if not settings.notifications_enabled or not settings.sold_alerts:
        return False

    uuid = auction_row["auction_uuid"]
    name = auction_row["item_name"] or auction_row["item_tag"] or "Item"
    buy_cost = auction_row["buy_cost"]
    url = settings.auction_url(uuid)

    lines = [f"Item: {name}"]
    if sold_price:
        lines.append(f"Sold for: {format_coins(sold_price)}")
    if buy_cost is not None:
        lines.append(f"Bought for: {format_coins(buy_cost)}")
        if sold_price:
            tax = sold_price * settings.ah_tax_rate
            profit = int(round(sold_price - tax - buy_cost))
            lines.append(f"Profit after tax: {format_profit(profit)}")
    if sold_time:
        lines.append(f"Time sold: {sold_time}")
    lines.append("")
    lines.append(url)

    body = "\n".join(lines)
    mh = _hash(f"sold:{uuid}:{sold_price}")

    # Avoid double-sending the same sale.
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    if db.message_hash_exists(mh, since):
        return False

    ok = await send_raw("💰 AH SOLD", body, url)
    db.record_notification(uuid, "sold", "SOLD", mh)
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
    if not settings.notifications_enabled or not settings.relist_alerts:
        return False

    decision = result.decision
    if decision not in settings.relist_alert_decisions:
        return False

    uuid = auction_row["auction_uuid"]
    name = auction_row["item_name"] or auction_row["item_tag"] or "Item"
    listing_price = auction_row["listing_price"] or 0
    buy_cost = auction_row["buy_cost"]
    url = settings.auction_url(uuid)

    # INCOMPARABLE alerts only for expensive tracked items (avoid noise).
    if decision == "INCOMPARABLE":
        value = max(listing_price or 0, buy_cost or 0)
        if value < settings.incomparable_alert_threshold:
            return False

    if _within_cooldown(uuid, decision, settings.relist_alert_cooldown_minutes):
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
        if result.expected_profit is not None:
            lines.append(f"Profit after tax: {format_profit(result.expected_profit)}")
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
        return False

    ok = await send_raw(title, body, url)
    db.record_notification(uuid, "relist", decision, mh)
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
