"""The smart relist decision engine.

This ties together feature extraction, comparable scoring, price history and the
profit math to produce one of:
    HOLD, RELIST, INCOMPARABLE, PROFIT_LOW, CUT_LOSS, UNKNOWN

Core principle: NEVER price off raw LBIN. If we can't find enough genuinely
comparable listings we return INCOMPARABLE rather than a dangerous number.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import cofl_client, db
from .config import settings
from .features import extract_item_features
from .formatting import round_clean_price
from .scoring import score_comparable

log = logging.getLogger("analysis")

DECISIONS = ("HOLD", "RELIST", "INCOMPARABLE", "PROFIT_LOW", "CUT_LOSS", "SOLD", "UNKNOWN")


@dataclass
class Comparable:
    uuid: Optional[str]
    price: int
    score: int
    item_name: str
    reasons: List[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    decision: str
    suggested_price: Optional[int]
    expected_profit: Optional[int]
    confidence: int
    comparable_count: int
    comparables: List[Comparable]
    rejected: List[Dict[str, Any]]
    reasons: List[str]
    features: Dict[str, Any]
    trend: Dict[str, Any]
    volume_per_day: Optional[float]


# --------------------------------------------------------------------------
# Profit math
# --------------------------------------------------------------------------

def profit_after_tax(sell_price: int, buy_cost: int) -> int:
    tax = sell_price * settings.ah_tax_rate
    return int(round(sell_price - tax - buy_cost))


def min_safe_price(buy_cost: int, min_profit: int) -> int:
    """Lowest sell price that still clears buy_cost + min_profit AFTER tax.

    profit = S - S*tax - buy >= min_profit  ->  S >= (buy + min_profit)/(1-tax)
    """
    denom = max(0.01, 1.0 - settings.ah_tax_rate)
    return int(round((buy_cost + min_profit) / denom))


# --------------------------------------------------------------------------
# Trend / volume
# --------------------------------------------------------------------------

def _point_price(point: Dict[str, Any]) -> Optional[float]:
    for key in ("avg", "price", "median", "max", "min"):
        val = point.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    return None


def _point_volume(point: Dict[str, Any]) -> Optional[float]:
    for key in ("volume", "count", "sales"):
        val = point.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def compute_trend(day: List[Dict[str, Any]], week: List[Dict[str, Any]]) -> tuple[Dict[str, Any], Optional[float]]:
    trend: Dict[str, Any] = {"day_pct": None, "week_pct": None, "volatility": None}
    volume_per_day: Optional[float] = None

    day_prices = [p for p in (_point_price(pt) for pt in day) if p]
    if len(day_prices) >= 2:
        first, last = day_prices[0], day_prices[-1]
        if first > 0:
            trend["day_pct"] = round((last - first) / first * 100, 1)
        hi, lo = max(day_prices), min(day_prices)
        if last > 0:
            trend["volatility"] = round((hi - lo) / last * 100, 1)

    week_prices = [p for p in (_point_price(pt) for pt in week) if p]
    if len(week_prices) >= 2:
        first, last = week_prices[0], week_prices[-1]
        if first > 0:
            trend["week_pct"] = round((last - first) / first * 100, 1)

    # Volume/day: average of available volume points (prefer week).
    vols = [v for v in (_point_volume(pt) for pt in week) if v is not None]
    if vols:
        volume_per_day = round(sum(vols) / len(vols), 2)
    else:
        vols = [v for v in (_point_volume(pt) for pt in day) if v is not None]
        if vols:
            volume_per_day = round(sum(vols), 2)  # day points roughly sum to a day

    return trend, volume_per_day


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def _confidence(comparables: List[Comparable]) -> int:
    if not comparables:
        return 15
    scores = sorted((c.score for c in comparables), reverse=True)
    top = scores[: max(3, settings.relist_min_comparable_matches)]
    avg = sum(top) / len(top)
    match_target = max(1, settings.relist_min_comparable_matches)
    count_factor = min(1.0, len(comparables) / match_target)
    confidence = avg * (0.55 + 0.45 * count_factor)
    return int(round(max(0, min(95, confidence))))


# --------------------------------------------------------------------------
# Main analyse routine
# --------------------------------------------------------------------------

async def analyse_auction(uuid: str) -> Optional[AnalysisResult]:
    row = db.get_auction(uuid)
    if row is None:
        return None

    item_tag = row["item_tag"]
    listing_price = row["listing_price"] or 0
    buy_cost = row["buy_cost"]
    min_profit = row["min_profit"] if row["min_profit"] is not None else settings.relist_min_profit_after_tax

    # 1. Full detail for the user's own auction.
    detail = await cofl_client.get_auction_detail(uuid)
    base_source = detail or {
        "tag": item_tag,
        "itemName": row["item_name"],
        "startingBid": listing_price,
    }
    base_features = extract_item_features(base_source)
    if not base_features.get("item_tag"):
        base_features["item_tag"] = item_tag

    reasons: List[str] = []

    # 2. Comparable BIN listings.
    comparables: List[Comparable] = []
    rejected: List[Dict[str, Any]] = []
    candidates_raw: List[Dict[str, Any]] = []
    if item_tag:
        candidates_raw = await cofl_client.get_active_bins_pages(
            item_tag, settings.relist_comparable_pages
        )

    for cand in candidates_raw:
        cand_features = extract_item_features(cand)
        if cand_features.get("uuid") == uuid:
            continue
        result = score_comparable(base_features, cand_features)
        price = cand_features.get("price") or 0
        if result.accepted and price > 0:
            comparables.append(
                Comparable(
                    uuid=cand_features.get("uuid"),
                    price=price,
                    score=result.score,
                    item_name=cand_features.get("item_name") or item_tag or "",
                    reasons=result.reasons[:4],
                )
            )
        else:
            if len(rejected) < 12:  # keep the detail page readable
                rejected.append(
                    {
                        "uuid": cand_features.get("uuid"),
                        "price": price,
                        "item_name": cand_features.get("item_name") or "",
                        "score": result.score,
                        "rejections": result.rejections[:3],
                    }
                )

    comparables.sort(key=lambda c: c.price)
    comparable_count = len(comparables)
    confidence = _confidence(comparables)

    # 3. Trend / volume (secondary evidence).
    trend: Dict[str, Any] = {"day_pct": None, "week_pct": None, "volatility": None}
    volume_per_day: Optional[float] = None
    if item_tag:
        day_hist = await cofl_client.get_price_history(item_tag, "day")
        week_hist = await cofl_client.get_price_history(item_tag, "week")
        trend, volume_per_day = compute_trend(day_hist, week_hist)

    # 4. Decide.
    decision, suggested_price, expected_profit, decision_reasons = _decide(
        base_features=base_features,
        listing_price=listing_price,
        buy_cost=buy_cost,
        min_profit=min_profit,
        comparables=comparables,
        confidence=confidence,
        trend=trend,
    )
    reasons.extend(decision_reasons)

    result = AnalysisResult(
        decision=decision,
        suggested_price=suggested_price,
        expected_profit=expected_profit,
        confidence=confidence,
        comparable_count=comparable_count,
        comparables=comparables,
        rejected=rejected,
        reasons=reasons,
        features=base_features,
        trend=trend,
        volume_per_day=volume_per_day,
    )

    _persist(uuid, result)
    return result


def _decide(
    *,
    base_features: Dict[str, Any],
    listing_price: int,
    buy_cost: Optional[int],
    min_profit: int,
    comparables: List[Comparable],
    confidence: int,
    trend: Dict[str, Any],
) -> tuple[str, Optional[int], Optional[int], List[str]]:
    reasons: List[str] = []
    cheapest = comparables[0].price if comparables else None

    # No buy cost: we can still report comparability but not profit.
    if buy_cost is None:
        if len(comparables) >= settings.relist_min_comparable_matches:
            reasons.append("Enter buy cost to enable profit/relist analysis.")
            reasons.append(f"Found {len(comparables)} comparable listings (cheapest {cheapest:,}).")
        else:
            reasons.append("Enter buy cost to enable profit/relist analysis.")
            reasons.append("Not enough comparable listings yet.")
        return "UNKNOWN", None, None, reasons

    # Not enough comparable evidence -> never guess.
    if len(comparables) < settings.relist_min_comparable_matches:
        reasons.append(
            f"Only {len(comparables)} safe comparable(s) found "
            f"(need {settings.relist_min_comparable_matches})."
        )
        reasons.append("Refusing to price off raw LBIN. Marked INCOMPARABLE.")
        return "INCOMPARABLE", None, None, reasons

    # We have a market. Build a candidate relist price by undercutting cheapest.
    undercut = max(
        settings.relist_undercut_coins,
        cheapest * settings.relist_undercut_percent / 100.0,
    )
    raw_price = max(0, cheapest - undercut)
    suggested = round_clean_price(raw_price)

    floor = min_safe_price(buy_cost, min_profit)
    profit_at_suggested = profit_after_tax(suggested, buy_cost)
    profit_at_cheapest = profit_after_tax(cheapest, buy_cost)

    reasons.append(f"{len(comparables)} comparable listings, cheapest {cheapest:,}.")

    day_pct = trend.get("day_pct")
    week_pct = trend.get("week_pct")

    # Case A: even matching the market does not clear min profit.
    if suggested < floor:
        if profit_at_cheapest < 0:
            # We are underwater versus the market.
            down_trend = (week_pct is not None and week_pct < -5) or (
                day_pct is not None and day_pct < -5
            )
            if down_trend and confidence >= settings.relist_min_comparable_score:
                reasons.append("Market is below your cost and trending down.")
                reasons.append("CUT_LOSS is OPTIONAL and risky - only if you want out.")
                # Suggest the market price (undercut) so it actually sells.
                return "CUT_LOSS", suggested, profit_at_suggested, reasons
            reasons.append("Market sits below your cost; holding may be better than dumping.")
            reasons.append("Profit floor not reachable at current market.")
            return "PROFIT_LOW", suggested, profit_at_suggested, reasons

        # Profitable but below your minimum.
        reasons.append(
            f"Relisting clears only {profit_at_suggested:,} after tax "
            f"(< your min {min_profit:,})."
        )
        return "PROFIT_LOW", suggested, profit_at_suggested, reasons

    # Case B: we can relist profitably above the floor.
    # Is the current listing meaningfully above comparable market?
    above_market = listing_price > cheapest
    above_suggested = listing_price > suggested + undercut

    if confidence < settings.relist_min_comparable_score:
        reasons.append(f"Confidence {confidence}% below threshold; holding for now.")
        return "HOLD", suggested, profit_at_suggested, reasons

    if above_market or above_suggested:
        if week_pct is not None and week_pct > 8:
            reasons.append(f"Market trending up {week_pct}% this week - consider waiting.")
            reasons.append("You are above market, but an uptrend may catch up.")
            return "HOLD", suggested, profit_at_suggested, reasons
        reasons.append(f"Your listing {listing_price:,} is above market {cheapest:,}.")
        reasons.append(f"Relist at {suggested:,} -> +{profit_at_suggested:,} after tax.")
        return "RELIST", suggested, profit_at_suggested, reasons

    # Already competitively priced.
    reasons.append("Your price is already at/near the cheapest comparable.")
    if day_pct is not None and day_pct > 0:
        reasons.append(f"24h trend +{day_pct}% - holding is reasonable.")
    return "HOLD", suggested, profit_at_suggested, reasons


def _persist(uuid: str, result: AnalysisResult) -> None:
    try:
        db.insert_analysis(
            {
                "auction_uuid": uuid,
                "decision": result.decision,
                "suggested_price": result.suggested_price,
                "expected_profit": result.expected_profit,
                "confidence": result.confidence,
                "comparable_count": result.comparable_count,
                "comparable_prices_json": json.dumps([asdict(c) for c in result.comparables]),
                "reasons_json": json.dumps(result.reasons),
                "item_features_json": json.dumps(result.features, default=str),
                "trend_json": json.dumps(result.trend),
                "rejected_json": json.dumps(result.rejected),
                "volume_per_day": result.volume_per_day,
            }
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to persist analysis for %s: %s", uuid, exc)
