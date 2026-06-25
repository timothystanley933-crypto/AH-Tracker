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
from .formatting import format_coins, format_profit, round_clean_price
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
    sell_estimate: Dict[str, Any] = field(default_factory=dict)
    market_context: Dict[str, Any] = field(default_factory=dict)


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


def _price_from_listing(item: Dict[str, Any]) -> Optional[int]:
    try:
        for key in ("startingBid", "price", "highestBidAmount"):
            val = item.get(key)
            if val is not None and not isinstance(val, bool):
                price = int(float(val))
                if price > 0:
                    return price
    except (ValueError, TypeError):
        return None
    return None


def _rejection_bucket(text: str) -> str:
    lower = (text or "").lower()
    if "rarity" in lower or "tier" in lower:
        return "rarity mismatch"
    if "pet level" in lower or "level too different" in lower:
        return "pet level mismatch"
    if "star" in lower:
        return "stars mismatch"
    if "recomb" in lower:
        return "recomb mismatch"
    if "gem" in lower:
        return "gemstone mismatch"
    if "attribute" in lower:
        return "attribute mismatch"
    if "enchant" in lower:
        return "enchant mismatch"
    if "score" in lower or "unknown" in lower:
        return "missing NBT/features"
    return "other mismatch"


def _history_summary(day: List[Dict[str, Any]], week: List[Dict[str, Any]]) -> Dict[str, Any]:
    prices = [p for p in (_point_price(pt) for pt in (week or day)) if p]
    if not prices:
        return {"min": None, "avg": None, "max": None}
    return {
        "min": int(round(min(prices))),
        "avg": int(round(sum(prices) / len(prices))),
        "max": int(round(max(prices))),
    }


def build_market_context(
    *,
    item_tag: Optional[str],
    base_features: Dict[str, Any],
    candidates_raw: List[Dict[str, Any]],
    rejected: List[Dict[str, Any]],
    rejection_counts: Dict[str, int],
    trend: Dict[str, Any],
    volume_per_day: Optional[float],
    day_history: List[Dict[str, Any]],
    week_history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    raw = []
    for item in candidates_raw:
        price = _price_from_listing(item)
        if not price:
            continue
        raw.append(
            {
                "uuid": item.get("uuid") or item.get("auctionId") or item.get("auction_uuid"),
                "item_name": item.get("itemName") or item.get("item_name") or item.get("name") or item_tag or "",
                "price": price,
            }
        )
    raw.sort(key=lambda r: r["price"])

    pet = base_features.get("pet") or {}
    gems = base_features.get("gemstones") or {}
    return {
        "safe_wording": "No confident relist price because comparable data is unsafe. Raw LBIN may be misleading for this item.",
        "raw_same_tag_lbin": raw[0]["price"] if raw else None,
        "raw_same_tag_top": raw[:5],
        "raw_same_tag_label": "Raw same-tag, not safe comparable",
        "rejected_reason_counts": rejection_counts,
        "rejected_examples": rejected[:5],
        "volume_per_day": volume_per_day,
        "trend": trend,
        "history": _history_summary(day_history, week_history),
        "features": {
            "item_tag": base_features.get("item_tag") or item_tag,
            "rarity": base_features.get("rarity"),
            "pet_level": pet.get("level"),
            "pet_tier": pet.get("tier"),
            "stars": base_features.get("stars"),
            "recombobulated": bool(base_features.get("recombobulated")),
            "skin": base_features.get("skin") or pet.get("skin"),
            "attributes": base_features.get("attributes") or {},
            "gemstones": gems,
            "important_enchants": base_features.get("important_enchants") or {},
        },
    }


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
    rejection_counts: Dict[str, int] = {}
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
            bucket = _rejection_bucket("; ".join(result.rejections or []))
            rejection_counts[bucket] = rejection_counts.get(bucket, 0) + 1
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
    day_hist: List[Dict[str, Any]] = []
    week_hist: List[Dict[str, Any]] = []
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
        volume_per_day=volume_per_day,
    )
    reasons.extend(decision_reasons)

    # 5. Sale-time prediction (cautious, market-data based).
    sell_estimate = compute_sell_estimate(
        decision=decision,
        listing_price=listing_price,
        suggested_price=suggested_price,
        comparables=comparables,
        comparable_count=comparable_count,
        volume_per_day=volume_per_day,
        trend=trend,
    )
    market_context = build_market_context(
        item_tag=item_tag,
        base_features=base_features,
        candidates_raw=candidates_raw,
        rejected=rejected,
        rejection_counts=rejection_counts,
        trend=trend,
        volume_per_day=volume_per_day,
        day_history=day_hist,
        week_history=week_hist,
    )

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
        sell_estimate=sell_estimate,
        market_context=market_context,
    )

    _persist(uuid, result)
    return result


def _fmt(value) -> str:
    return format_coins(int(value)) if value is not None else "—"


# --------------------------------------------------------------------------
# Sale-time prediction
# --------------------------------------------------------------------------

_UNKNOWN_SELL_ESTIMATE = {
    "estimated_sell_time_current": "Unknown",
    "estimated_sell_time_suggested": "Unknown",
    "sale_likelihood_current": "unknown",
    "sale_likelihood_suggested": "unknown",
    "sell_time_reason": "Sell time unknown — not enough comparable volume data.",
}


def _humanize_hours(hours: float) -> str:
    """Turn an hours estimate into a cautious human range (e.g. '~4-8 hours')."""
    if hours <= 0:
        return "soon"
    low = hours * 0.6
    high = hours * 1.6
    if high < 1:
        return "<1 hour"
    if high < 48:
        lo, hi = max(1, round(low)), max(2, round(high))
        if lo == hi:
            hi = lo + 1
        return f"~{lo}-{hi} hours"
    lo_d = max(1, round(low / 24))
    hi_d = max(lo_d + 1, round(high / 24))
    return f"~{lo_d}-{hi_d} days"


def compute_sell_estimate(
    *,
    decision: str,
    listing_price: int,
    suggested_price: Optional[int],
    comparables: List[Comparable],
    comparable_count: int,
    volume_per_day: Optional[float],
    trend: Dict[str, Any],
) -> Dict[str, Any]:
    """Cautious, market-data-based estimate of how long an item may take to sell.

    Never promises a sale - uses words like 'estimated' / 'likely' / 'roughly'.
    Returns UNKNOWN when there is not enough data to be honest.
    """
    # Honesty gates: no fake certainty.
    if decision in ("INCOMPARABLE", "UNKNOWN"):
        return dict(_UNKNOWN_SELL_ESTIMATE)
    if comparable_count < settings.relist_min_comparable_matches:
        return dict(_UNKNOWN_SELL_ESTIMATE)
    if volume_per_day is None or volume_per_day <= 0:
        return dict(_UNKNOWN_SELL_ESTIMATE)

    baseline_hours = 24.0 / max(volume_per_day, 0.1)

    prices = [c.price for c in comparables]
    rank_current = sum(1 for p in prices if p < listing_price) + 1
    rank_suggested = (sum(1 for p in prices if p < (suggested_price or listing_price)) + 1)

    day_pct = trend.get("day_pct")
    is_uptrend = day_pct is not None and day_pct >= settings.relist_strong_up_trend_24h
    is_downtrend = day_pct is not None and day_pct <= settings.relist_strong_down_trend_24h

    hours_current = baseline_hours * max(rank_current, 1)
    hours_suggested = baseline_hours * max(rank_suggested, 1)

    # Trend adjustments: a high current price in a downtrend sells slower.
    if is_downtrend:
        hours_current *= 1.6
    elif is_uptrend:
        hours_current *= 0.85

    low_volume = volume_per_day < 0.5

    def likelihood(rank: int) -> str:
        if low_volume:
            return "low confidence (slow market)"
        if rank <= 1:
            return "likely"
        if rank <= 3:
            return "possible"
        return "unlikely soon"

    est_current = _humanize_hours(hours_current)
    est_suggested = _humanize_hours(hours_suggested)
    like_current = likelihood(rank_current)
    like_suggested = likelihood(rank_suggested)
    if is_downtrend and like_current in ("likely", "possible"):
        like_current = "unlikely soon"

    position = (
        "near the cheapest comparable"
        if rank_current <= 1
        else f"above {rank_current - 1} cheaper comparable(s)"
    )
    reason = (
        f"Based on ~{volume_per_day:g} sales/day and {comparable_count} comparable listings, "
        f"your current price sits {position}. "
        f"The suggested relist would place it near the cheapest comparable."
    )
    if low_volume:
        reason += " Volume is low, so this is a rough, low-confidence estimate."
    elif is_downtrend:
        reason += " A 24h downtrend means the current high price is likely to sell slower."

    return {
        "estimated_sell_time_current": est_current,
        "estimated_sell_time_suggested": est_suggested,
        "sale_likelihood_current": like_current,
        "sale_likelihood_suggested": like_suggested,
        "sell_time_reason": reason,
    }


def _decide(
    *,
    base_features: Dict[str, Any],
    listing_price: int,
    buy_cost: Optional[int],
    min_profit: int,
    comparables: List[Comparable],
    confidence: int,
    trend: Dict[str, Any],
    volume_per_day: Optional[float],
) -> tuple[str, Optional[int], Optional[int], List[str]]:
    reasons: List[str] = []
    cheapest = comparables[0].price if comparables else None
    min_matches = settings.relist_min_comparable_matches
    score_threshold = settings.relist_min_comparable_score

    # No buy cost: we can still report comparability but not profit.
    if buy_cost is None:
        reasons.append("Enter your buy cost to enable profit and relist analysis.")
        if len(comparables) >= min_matches:
            reasons.append(f"Found {len(comparables)} comparable listings (cheapest {_fmt(cheapest)}).")
        else:
            reasons.append("Not enough comparable listings yet to compare safely.")
        return "UNKNOWN", None, None, reasons

    # Not enough comparable evidence -> never guess (INCOMPARABLE).
    if len(comparables) < min_matches:
        reasons.append("Raw LBIN does not match this item's rarity / pet level / upgrades.")
        reasons.append(f"Only {len(comparables)} safe comparable(s) found (need {min_matches}).")
        reasons.append("No relist price suggested, to avoid a bad undercut.")
        return "INCOMPARABLE", None, None, reasons

    # Build a candidate relist price by undercutting the cheapest comparable.
    undercut = max(
        settings.relist_undercut_coins,
        cheapest * settings.relist_undercut_percent / 100.0,
    )
    suggested = round_clean_price(max(0, cheapest - undercut))

    profit_at_suggested = profit_after_tax(suggested, buy_cost)
    profit_at_cheapest = profit_after_tax(cheapest, buy_cost)

    price_gap = listing_price - suggested
    price_gap_percent = (price_gap / suggested * 100.0) if suggested > 0 else 0.0
    meaningful_gap = (
        price_gap >= settings.relist_price_gap_coins
        or price_gap_percent >= settings.relist_price_gap_percent
    )
    very_large_gap = price_gap_percent >= 10.0

    day_pct = trend.get("day_pct")
    is_uptrend = day_pct is not None and day_pct >= settings.relist_strong_up_trend_24h
    is_downtrend = day_pct is not None and day_pct <= settings.relist_strong_down_trend_24h
    decent_volume = volume_per_day is not None and volume_per_day >= settings.relist_decent_volume_per_day
    flat_or_down = (day_pct is None) or (day_pct <= 0)
    market_supports = flat_or_down or decent_volume or very_large_gap

    # --- 1) Low confidence: we have a market but can't trust it enough to act.
    if confidence < score_threshold:
        reasons.append(f"Confidence {confidence}% is below the {score_threshold}% needed to act.")
        reasons.append(f"{len(comparables)} comparables found, but the match quality is weak.")
        reasons.append("Holding until clearer comparable evidence appears.")
        return "HOLD", suggested, profit_at_suggested, reasons

    # --- 2) Profit too low (or negative): PROFIT_LOW / optional CUT_LOSS.
    if profit_at_suggested < min_profit:
        if profit_at_cheapest < 0 and is_downtrend:
            reasons.append("The market now sits below your cost and is trending down.")
            reasons.append(f"Best case relist still loses {_fmt(abs(profit_at_suggested))} after tax.")
            reasons.append("CUT_LOSS is optional and risky — only if you want out.")
            return "CUT_LOSS", suggested, profit_at_suggested, reasons
        reasons.append(
            f"Relisting clears only {format_profit(profit_at_suggested)} after tax, "
            f"below your {_fmt(min_profit)} minimum."
        )
        reasons.append(f"{len(comparables)} comparables, confidence {confidence}%.")
        reasons.append("Holding instead of relisting below your profit target.")
        return "PROFIT_LOW", suggested, profit_at_suggested, reasons

    # From here profit clears the minimum and confidence is good.

    # --- 3) Already competitively priced.
    if listing_price <= suggested:
        reasons.append("Your price is already at or below the suggested relist.")
        reasons.append(f"Cheapest comparable is {_fmt(cheapest)}; you are already competitive.")
        if day_pct is not None and day_pct > 0:
            reasons.append(f"24h trend is +{day_pct}% — holding is reasonable.")
        return "HOLD", suggested, profit_at_suggested, reasons

    # --- 4) Gap too small to bother (the Hooverius case).
    if not meaningful_gap:
        reasons.append(f"Your listing is only {_fmt(price_gap)} above the suggested relist.")
        reasons.append(
            f"Relist gap is below your {_fmt(settings.relist_price_gap_coins)} / "
            f"{settings.relist_price_gap_percent:g}% threshold."
        )
        reasons.append("Confidence is good, but the price difference is too small to justify relisting.")
        return "HOLD", suggested, profit_at_suggested, reasons

    # --- 5) Market trending up and gap not huge: waiting may capture more.
    if is_uptrend and not very_large_gap:
        reasons.append(f"24h trend is up +{day_pct}% — waiting may capture a higher price.")
        reasons.append(f"You are {_fmt(price_gap)} above suggested, but not enough to rush.")
        return "HOLD", suggested, profit_at_suggested, reasons

    # --- 6) Market conditions don't support a faster sale.
    if not market_supports:
        reasons.append(f"Your listing is {_fmt(price_gap)} above the suggested relist.")
        reasons.append(
            f"But volume is thin (~{volume_per_day:g}/day) and the market is not falling."
        )
        reasons.append("Relisting may not sell meaningfully faster right now.")
        return "HOLD", suggested, profit_at_suggested, reasons

    # --- RELIST: all conditions satisfied.
    reasons.append(f"Your listing is {_fmt(listing_price - cheapest)} above the cheapest comparable.")
    reasons.append(f"Cheapest comparable is {_fmt(cheapest)} and suggested relist is {_fmt(suggested)}.")
    reasons.append(
        f"Profit after tax stays at {format_profit(profit_at_suggested)}, above your {_fmt(min_profit)} minimum."
    )
    if is_downtrend:
        reasons.append(f"24h trend is down {day_pct}%, so waiting is riskier.")
    elif decent_volume:
        reasons.append(f"Healthy demand (~{volume_per_day:g} sales/day) supports relisting now.")
    elif very_large_gap:
        reasons.append(f"You are {price_gap_percent:.0f}% above market — a clear overprice.")
    reasons.append(f"{len(comparables)} comparable listings found, confidence {confidence}%.")
    return "RELIST", suggested, profit_at_suggested, reasons


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
                "sell_estimate_json": json.dumps(result.sell_estimate),
                "market_context_json": json.dumps(result.market_context),
            }
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to persist analysis for %s: %s", uuid, exc)
