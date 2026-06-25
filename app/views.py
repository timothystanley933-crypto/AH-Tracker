"""Build template-friendly view models from DB rows + latest analysis."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import settings
from .formatting import format_coins, format_profit

DECISION_LABELS = {
    "RELIST": "Relist",
    "HOLD": "Hold",
    "INCOMPARABLE": "Incomparable",
    "CUT_LOSS": "Cut Loss",
    "PROFIT_LOW": "Profit Low",
    "SOLD": "Sold",
    "EXPIRED": "Expired",
    "STALE": "Stale",
    "UNKNOWN": "Unknown",
}


def _row_get(row, key, default=None):
    """Safe column access for sqlite3.Row (no .get, raises on missing column)."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _row_status(row) -> str:
    """Status with a safe fallback for rows predating the status column."""
    try:
        status = row["status"]
    except (IndexError, KeyError):
        status = None
    if status:
        return status
    if row["sold"]:
        return "SOLD"
    return "ACTIVE" if row["active"] else "EXPIRED"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = value
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _time_left(ends_at: Optional[str]) -> Optional[str]:
    dt = _parse_iso(ends_at)
    if dt is None:
        return None
    delta = dt - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "Ended"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _ago(value: Optional[str]) -> str:
    dt = _parse_iso(value)
    if dt is None:
        return "—"
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _trend_summary(trend: Dict[str, Any]) -> str:
    if not trend:
        return "No trend data"
    parts = []
    d = trend.get("day_pct")
    w = trend.get("week_pct")
    if d is not None:
        parts.append(f"24h {'+' if d >= 0 else ''}{d}%")
    if w is not None:
        parts.append(f"7d {'+' if w >= 0 else ''}{w}%")
    return " · ".join(parts) if parts else "No trend data"


def _volume_summary(volume_per_day: Optional[float]) -> str:
    if volume_per_day is None:
        return "Volume unknown"
    return f"~{volume_per_day:g}/day"


def build_card(row: sqlite3.Row, analysis_row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    """Assemble a single auction card view model."""
    status = _row_status(row)
    if status == "SOLD":
        decision_display = "SOLD"
    elif status in ("EXPIRED", "STALE"):
        decision_display = status
    else:
        decision_display = analysis_row["decision"] if analysis_row else "UNKNOWN"

    trend = {}
    reasons: List[str] = []
    sell_estimate: Dict[str, Any] = {}
    volume_per_day = None
    if analysis_row:
        try:
            trend = json.loads(analysis_row["trend_json"] or "{}")
        except (ValueError, TypeError):
            trend = {}
        try:
            reasons = json.loads(analysis_row["reasons_json"] or "[]")
        except (ValueError, TypeError):
            reasons = []
        try:
            sell_estimate = json.loads(_row_get(analysis_row, "sell_estimate_json") or "{}")
        except (ValueError, TypeError):
            sell_estimate = {}
        volume_per_day = analysis_row["volume_per_day"]

    expected_profit = analysis_row["expected_profit"] if analysis_row else None
    suggested_price = analysis_row["suggested_price"] if analysis_row else None
    confidence = analysis_row["confidence"] if analysis_row else None
    comparable_count = analysis_row["comparable_count"] if analysis_row else 0

    return {
        "uuid": row["auction_uuid"],
        "item_name": row["item_name"] or row["item_tag"] or "Unknown item",
        "item_tag": row["item_tag"] or "",
        "listing_price": row["listing_price"] or 0,
        "listing_price_fmt": format_coins(row["listing_price"]),
        "buy_cost": row["buy_cost"],
        "buy_cost_fmt": format_coins(row["buy_cost"]) if row["buy_cost"] is not None else "",
        "min_profit": row["min_profit"] if row["min_profit"] is not None else settings.relist_min_profit_after_tax,
        "expected_profit": expected_profit,
        "expected_profit_fmt": format_profit(expected_profit) if expected_profit is not None else "—",
        "suggested_price": suggested_price,
        "suggested_price_fmt": format_coins(suggested_price) if suggested_price else "—",
        "decision": decision_display,
        "decision_label": DECISION_LABELS.get(decision_display, decision_display.title()),
        "confidence": confidence if confidence is not None else "—",
        "comparable_count": comparable_count if comparable_count is not None else 0,
        "trend_summary": _trend_summary(trend),
        "volume_summary": _volume_summary(volume_per_day),
        "reason": reasons[0] if reasons else _default_reason(status),
        "reasons": reasons,
        "skycofl_url": row["skycofl_url"] or settings.auction_url(row["auction_uuid"]),
        "time_left": _time_left(row["ends_at"]) or "—",
        "last_checked": _ago(row["updated_at"]),
        "ignored": bool(row["ignored"]),
        "status": status,
        "sold": status == "SOLD",
        "active": status == "ACTIVE",
        "missing_buy_cost": row["buy_cost"] is None and status == "ACTIVE",
        "carried_from_uuid": _row_get(row, "carried_from_uuid"),
        "carry_suggestions": [],
        "sold_price_fmt": format_coins(row["sold_price"]) if row["sold_price"] else "—",
        # Sale-time prediction (cautious; may be "Unknown").
        "sell_current": sell_estimate.get("estimated_sell_time_current", "Unknown"),
        "sell_suggested": sell_estimate.get("estimated_sell_time_suggested", "Unknown"),
        "sell_like_current": sell_estimate.get("sale_likelihood_current", "unknown"),
        "sell_like_suggested": sell_estimate.get("sale_likelihood_suggested", "unknown"),
        "sell_reason": sell_estimate.get("sell_time_reason", ""),
        "has_sell_estimate": bool(sell_estimate) and sell_estimate.get("estimated_sell_time_current") not in (None, "Unknown"),
        # Raw timestamps for sorting the Sold tab.
        "sold_at": _row_get(row, "sold_at"),
        "ended_at": _row_get(row, "ends_at"),
        "updated_at": _row_get(row, "updated_at"),
        "last_seen": _row_get(row, "last_seen"),
    }


def _default_reason(status: str) -> str:
    if status == "SOLD":
        return "Item sold."
    if status == "EXPIRED":
        return "Auction expired without selling."
    if status == "STALE":
        return "No longer seen in your auctions (hidden by default)."
    return "Run analyse to get a recommendation."


def build_cards(rows: List[sqlite3.Row], analyses: Dict[str, sqlite3.Row]) -> List[Dict[str, Any]]:
    return [build_card(r, analyses.get(r["auction_uuid"])) for r in rows]


def compute_summary(cards: List[Dict[str, Any]], last_refresh: Optional[str]) -> Dict[str, Any]:
    active = [c for c in cards if c["status"] == "ACTIVE" and not c["ignored"]]
    tracked = [c for c in active if c["buy_cost"] is not None]
    missing = [c for c in active if c["missing_buy_cost"]]
    relist_warnings = [c for c in active if c["decision"] in ("RELIST", "CUT_LOSS", "PROFIT_LOW")]
    incomparable = [c for c in active if c["decision"] == "INCOMPARABLE"]

    potential_profit = sum(
        c["expected_profit"] for c in active
        if c["expected_profit"] is not None and c["expected_profit"] > 0 and not c["ignored"]
    )

    return {
        "total_active": len(active),
        "tracked": len(tracked),
        "missing_buy_cost": len(missing),
        "potential_profit_fmt": format_coins(potential_profit),
        "relist_warnings": len(relist_warnings),
        "incomparable": len(incomparable),
        "last_refresh": _ago(last_refresh) if last_refresh else "never",
    }


def sort_cards(cards: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    if sort == "value":
        return sorted(cards, key=lambda c: c["listing_price"] or 0, reverse=True)
    if sort == "urgent":
        order = {"RELIST": 0, "CUT_LOSS": 1, "PROFIT_LOW": 2, "INCOMPARABLE": 3, "HOLD": 4, "UNKNOWN": 5, "SOLD": 6}
        return sorted(cards, key=lambda c: (order.get(c["decision"], 9), -(c["listing_price"] or 0)))
    if sort == "confidence":
        return sorted(cards, key=lambda c: c["confidence"] if isinstance(c["confidence"], int) else 999)
    if sort == "missing":
        return sorted(cards, key=lambda c: (not c["missing_buy_cost"], -(c["listing_price"] or 0)))
    # default: recently updated (rows already come ordered by updated_at desc)
    return cards


def _sold_sort_key(card: Dict[str, Any]) -> str:
    """Best available timestamp for ordering sold items, newest first.

    Priority: sold_at -> ended_at -> updated_at -> last_seen. ISO strings sort
    chronologically, so a plain string comparison works.
    """
    for key in ("sold_at", "ended_at", "updated_at", "last_seen"):
        val = card.get(key)
        if val:
            return str(val)
    return ""


def sort_sold(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest sold first (Fix 4)."""
    return sorted(cards, key=_sold_sort_key, reverse=True)


def filter_cards(cards: List[Dict[str, Any]], flt: str) -> List[Dict[str, Any]]:
    """Default view shows ONLY current ACTIVE auctions (not ignored).

    SOLD / EXPIRED / STALE are hidden unless explicitly requested.
    """
    flt = flt or "active"
    if flt == "active":
        return [c for c in cards if c["status"] == "ACTIVE" and not c["ignored"]]
    if flt == "missing":
        return [c for c in cards if c["status"] == "ACTIVE" and c["missing_buy_cost"]]
    if flt == "sold":
        return [c for c in cards if c["status"] == "SOLD"]
    if flt == "expired":
        return [c for c in cards if c["status"] == "EXPIRED"]
    if flt == "stale":
        return [c for c in cards if c["status"] == "STALE"]
    if flt == "ignored":
        return [c for c in cards if c["ignored"]]
    if flt in ("RELIST", "HOLD", "INCOMPARABLE", "PROFIT_LOW", "CUT_LOSS"):
        return [c for c in cards if c["status"] == "ACTIVE" and c["decision"] == flt]
    if flt == "all":
        return cards
    # Unknown filter -> safe default (active only).
    return [c for c in cards if c["status"] == "ACTIVE" and not c["ignored"]]
