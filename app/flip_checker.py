"""Pre-buy Flip Checker.

Paste a SkyCofl auction URL/UUID + a buy price; the checker fetches the auction
(read-only), compares it to genuinely-similar listings (NEVER raw LBIN), nets out
every fee, models repeated relists, and returns BUY / MAYBE / DO_NOT_BUY /
INCOMPARABLE with proof.

Safety first: if the data is incomplete or comparables are unsafe, it refuses to
say BUY. A false BUY is worse than a missed flip.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

from . import cofl_client, db, decision_support, profit
from .analysis import (
    Comparable,
    _confidence,
    _price_from_listing,
    _rejection_bucket,
    build_market_context,
    compute_trend,
)
from .config import settings
from .features import build_item_identity_key, extract_item_features, features_summary
from .formatting import format_coins, round_clean_price
from .scoring import score_comparable

log = logging.getLogger("flip")

BUY, MAYBE, DO_NOT_BUY, INCOMPARABLE = "BUY", "MAYBE", "DO_NOT_BUY", "INCOMPARABLE"

# Hex run of >=12 chars: SkyBlock auction UUIDs are 32 hex (dashes optional).
_HEX_RE = re.compile(r"^[0-9a-fA-F]{12,}$")


# --------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------

def parse_auction_input(text: Optional[str]) -> Optional[str]:
    """Extract an auction UUID from a SkyCofl URL or a bare UUID. None if invalid."""
    if not text:
        return None
    s = str(text).strip()
    s = s.split("#", 1)[0].split("?", 1)[0].strip()
    if "/" in s:
        parts = [p for p in s.split("/") if p]
        s = parts[-1] if parts else ""
    candidate = s.replace("-", "").strip().lower()
    return candidate if _HEX_RE.match(candidate) else None


# --------------------------------------------------------------------------
# Pure fee math (testable in isolation)
# --------------------------------------------------------------------------

def relist_chain_profits(buy_price: int, list_prices: List[int]) -> List[int]:
    """Profit for selling on the 1st / 2nd / 3rd listing, paying a fee each list.

    list_prices = [first_list, cut_1, cut_2, ...]. Index i = "sold after i relists":
    proceeds at list_prices[i] minus buy, minus sales tax on that price, minus the
    listing fee for EVERY listing up to and including i (this is why repeated
    relisting destroys profit at a 2.5% listing fee).
    """
    out: List[int] = []
    for i, price in enumerate(list_prices):
        fees = sum(profit.listing_fee(p) for p in list_prices[: i + 1])
        out.append(int(price - buy_price - profit.sales_tax(price) - fees))
    return out


def max_safe_buy_price(expected_sell: int, desired_profit: int) -> int:
    """Highest buy price that still clears ``desired_profit`` after one listing."""
    return int(expected_sell - profit.sales_tax(expected_sell) - profit.listing_fee(expected_sell) - desired_profit)


def max_safe_buy_price_after_relist(expected_sell: int, desired_profit: int) -> int:
    """Max buy price that survives ONE failed listing (two listing fees) + sale."""
    return int(
        expected_sell
        - profit.sales_tax(expected_sell)
        - 2 * profit.listing_fee(expected_sell)
        - desired_profit
    )


# --------------------------------------------------------------------------
# Decision (pure)
# --------------------------------------------------------------------------

def decide_flip(
    *,
    safe_count: int,
    confidence: int,
    true_profit: Optional[int],
    profit_after_one_relist: Optional[int],
    min_profit: int,
    volume_per_day: Optional[float],
    competition_label: str,
    has_blocking_wall: bool,
    features_incomplete: bool,
) -> Tuple[str, List[str]]:
    """Map metrics to BUY / MAYBE / DO_NOT_BUY / INCOMPARABLE. Conservative by design."""
    min_matches = max(2, settings.relist_min_comparable_matches)
    score_threshold = settings.relist_min_comparable_score
    reasons: List[str] = []

    # --- INCOMPARABLE: not safe to compare at all. ---
    if true_profit is None or safe_count < min_matches:
        reasons.append(
            f"Only {safe_count} safe comparable(s) found (need {min_matches}); "
            "raw same-tag LBIN would be misleading."
        )
        return INCOMPARABLE, reasons
    if features_incomplete and safe_count < max(min_matches + 1, 3):
        reasons.append("Item NBT/feature data is incomplete and safe comparables are thin.")
        return INCOMPARABLE, reasons

    # --- DO_NOT_BUY: the margin does not survive reality. ---
    if true_profit < min_profit:
        reasons.append(
            f"True profit after fees is only {format_coins(true_profit)}, "
            f"below your {format_coins(min_profit)} minimum."
        )
        return DO_NOT_BUY, reasons
    if profit_after_one_relist is not None and profit_after_one_relist < 0:
        reasons.append(
            "A single relist turns this into a loss "
            f"({format_coins(profit_after_one_relist)}) once two 2.5% listing fees are paid."
        )
        return DO_NOT_BUY, reasons
    if has_blocking_wall and true_profit < 2 * min_profit:
        reasons.append("A price wall sits between you and a sale, and the margin is not large enough to absorb it.")
        return DO_NOT_BUY, reasons
    if competition_label == "High" and (volume_per_day is None or volume_per_day < settings.flip_min_volume_for_buy) and true_profit < 2 * min_profit:
        reasons.append("Lots of cheaper similar listings and weak volume — likely a slow, contested sale.")
        return DO_NOT_BUY, reasons

    # --- BUY vs MAYBE. ---
    good_volume = volume_per_day is not None and volume_per_day >= settings.flip_min_volume_for_buy
    good_conf = confidence >= score_threshold
    relist_safe = profit_after_one_relist is not None and profit_after_one_relist >= min_profit
    huge_margin = true_profit >= 3 * min_profit

    if good_conf and good_volume and (relist_safe or huge_margin) and not features_incomplete:
        reasons.append(
            f"Safe profit of {format_coins(true_profit)} after all fees with {confidence}% confidence "
            f"and acceptable volume (~{volume_per_day:g}/day)."
        )
        if relist_safe:
            reasons.append(f"Still profitable ({format_coins(profit_after_one_relist)}) even after one relist.")
        return BUY, reasons

    # Otherwise it's a MAYBE - good on paper but needs a manual look.
    if not good_volume:
        reasons.append(
            "Margin looks good but volume is "
            + (f"only ~{volume_per_day:g}/day" if volume_per_day is not None else "unknown")
            + " — it may take a long time to sell."
        )
    if not good_conf:
        reasons.append(f"Comparable confidence ({confidence}%) is below the {score_threshold}% needed to be sure.")
    if features_incomplete:
        reasons.append("Some feature data is missing — confidence reduced.")
    if profit_after_one_relist is not None and not relist_safe:
        reasons.append(f"One relist would cut profit to {format_coins(profit_after_one_relist)} — check before buying.")
    if not reasons:
        reasons.append("Profit is acceptable but the market is borderline — inspect manually before buying.")
    return MAYBE, reasons


# --------------------------------------------------------------------------
# Comparable building
# --------------------------------------------------------------------------

def _build_comparables(
    base_features: Dict[str, Any], candidates_raw: List[Dict[str, Any]], base_uuid: Optional[str]
) -> Tuple[List[Comparable], List[Dict[str, Any]], Dict[str, int]]:
    comparables: List[Comparable] = []
    rejected: List[Dict[str, Any]] = []
    rejection_counts: Dict[str, int] = {}
    item_tag = base_features.get("item_tag")
    for cand in candidates_raw:
        cand_features = extract_item_features(cand)
        if cand_features.get("uuid") and cand_features.get("uuid") == base_uuid:
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
            if len(rejected) < 15:
                rejected.append(
                    {
                        "uuid": cand_features.get("uuid"),
                        "price": price,
                        "item_name": cand_features.get("item_name") or "",
                        "score": result.score,
                        "verdict": "rejected",
                        "rejections": result.rejections[:3],
                        "reason": (result.rejections or ["Not a safe comparable"])[0],
                    }
                )
    comparables.sort(key=lambda c: c.price)
    return comparables, rejected, rejection_counts


def _verdict(base_price: int, comp_price: int, score: int) -> str:
    if score >= settings.undercut_better_item_score and comp_price >= base_price:
        return "better"
    if comp_price < base_price:
        return "worse"
    return "similar"


# --------------------------------------------------------------------------
# Suggested options
# --------------------------------------------------------------------------

def _estimate_for_price(price: int, comp_prices: List[int], volume_per_day: Optional[float]) -> Tuple[str, str]:
    """(sale-chance label, time-to-sell text) for listing at ``price``."""
    if volume_per_day is None or volume_per_day <= 0:
        return "Unknown", "Unknown"
    rank = sum(1 for p in comp_prices if p < price) + 1
    hours = (24.0 / max(volume_per_day, 0.1)) * rank
    if volume_per_day < settings.flip_min_volume_for_buy:
        chance = "Unlikely soon"
    elif rank <= 1:
        chance = "Likely"
    elif rank <= 3:
        chance = "Possible"
    else:
        chance = "Unlikely soon"
    return chance, _humanize_hours(hours)


def _humanize_hours(hours: float) -> str:
    if hours <= 0:
        return "soon"
    low, high = hours * 0.6, hours * 1.6
    if high < 48:
        return f"~{max(1, round(low))}-{max(2, round(high))} hours"
    return f"~{max(1, round(low / 24))}-{max(2, round(high / 24))} days"


def _build_options(
    buy_price: int, comp_prices: List[int], volume_per_day: Optional[float]
) -> List[Dict[str, Any]]:
    """Fast / Balanced / Greedy first-listing strategies, each fee-aware."""
    if not comp_prices:
        return []
    cheapest = comp_prices[0]
    median = int(statistics.median(comp_prices))
    synthetic = {"buy_cost": buy_price, "accumulated_listing_fees": 0, "manual_extra_costs": 0}

    specs = [
        ("Fast", round_clean_price(cheapest * 0.98) - 1, "Low"),
        ("Balanced", round_clean_price(cheapest * 0.995) - 1, "Medium"),
        ("Greedy", max(round_clean_price(cheapest * 0.995), median) - 1, "High"),
    ]
    options: List[Dict[str, Any]] = []
    for name, price, base_risk in specs:
        price = max(1, int(price))
        p = profit.profit_after_relist(synthetic, price)
        chance, tts = _estimate_for_price(price, comp_prices, volume_per_day)
        roi = round((p / buy_price) * 100.0, 1) if buy_price and p is not None else None
        risk = base_risk
        if volume_per_day is not None and volume_per_day < settings.flip_min_volume_for_buy:
            risk = "High" if base_risk != "High" else "Very High"
        options.append(
            {
                "name": name,
                "price": price,
                "price_fmt": format_coins(price),
                "profit": p,
                "profit_fmt": format_coins(p) if p is not None else "—",
                "roi_percent": roi,
                "sale_chance": chance,
                "time_to_sell": tts,
                "risk": risk,
            }
        )
    return options


# --------------------------------------------------------------------------
# Risk scoring
# --------------------------------------------------------------------------

def _overall_risk(
    *, liquidity: int, demand: int, competition: int, relist_kills: bool, confidence: int, features_incomplete: bool
) -> str:
    # Higher penalty number = riskier.
    penalty = 0
    penalty += max(0, 60 - liquidity) / 12.0
    penalty += max(0, 60 - demand) / 12.0
    penalty += competition / 25.0
    penalty += max(0, 75 - confidence) / 15.0
    if relist_kills:
        penalty += 3
    if features_incomplete:
        penalty += 1.5
    if penalty >= 7:
        return "Very High"
    if penalty >= 4.5:
        return "High"
    if penalty >= 2.2:
        return "Medium"
    return "Low"


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

async def check_flip(
    *,
    auction_url_or_uuid: str,
    buy_price: int,
    min_profit: Optional[int] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Run the full flip analysis. Never raises for ordinary failures - returns
    a structured dict (``ok: False`` with an error message when something is wrong)."""
    min_profit = int(min_profit) if min_profit is not None else settings.relist_min_profit_after_tax

    uuid = parse_auction_input(auction_url_or_uuid)
    if uuid is None:
        return {"ok": False, "error": "Could not read an auction UUID from that input. Paste a SkyCofl auction URL or UUID."}
    if not buy_price or buy_price <= 0:
        return {"ok": False, "error": "Enter a positive buy price."}

    try:
        detail = await cofl_client.get_auction_detail(uuid)
    except Exception as exc:  # noqa: BLE001
        log.warning("flip fetch failed for %s: %s", uuid, exc)
        detail = None
    if not detail:
        return {"ok": False, "error": "Could not fetch that auction from CoflNet. Check the UUID/URL and try again."}

    base_features = extract_item_features(detail)
    item_tag = base_features.get("item_tag")
    item_name = base_features.get("item_name") or item_tag or "Unknown item"
    current_price = _price_from_listing(detail) or 0
    if not item_tag:
        return {"ok": False, "error": "That auction has no item tag we can compare against."}

    # Comparables + history (read-only).
    candidates_raw = await cofl_client.get_active_bins_pages(item_tag, settings.relist_comparable_pages)
    comparables, rejected, rejection_counts = _build_comparables(base_features, candidates_raw, base_features.get("uuid"))

    day_hist = await cofl_client.get_price_history(item_tag, "day")
    week_hist = await cofl_client.get_price_history(item_tag, "week")
    trend, volume_per_day = compute_trend(day_hist, week_hist)

    market_context = build_market_context(
        item_tag=item_tag, base_features=base_features, candidates_raw=candidates_raw,
        rejected=rejected, rejection_counts=rejection_counts, trend=trend,
        volume_per_day=volume_per_day, day_history=day_hist, week_history=week_hist,
    )

    comp_prices = [c.price for c in comparables]
    safe_count = len(comparables)
    confidence = _confidence(comparables)
    features_incomplete = bool(decision_support._features_incomplete(base_features))

    # Decision-support metrics.
    raw_prices = sorted(p for p in (_price_from_listing(i) for i in candidates_raw) if p)
    walls = decision_support.detect_price_walls(raw_prices)
    cheapest_safe = comp_prices[0] if comp_prices else None
    rank, total = decision_support.price_rank(current_price, comp_prices or raw_prices)
    undercut_coins, undercut_pct = decision_support.undercut_amount(current_price, cheapest_safe)
    cheaper_similar = sum(1 for p in comp_prices if p < (current_price or 0))
    liquidity = decision_support.liquidity_score(volume_per_day, safe_count)
    demand = decision_support.demand_score(volume_per_day, trend)
    competition = decision_support.competition_score(cheaper_similar, len(raw_prices), walls)

    # Suggested options + fee-aware profit.
    options = _build_options(buy_price, comp_prices, volume_per_day)
    options_by_name = {o["name"]: o for o in options}
    balanced = options_by_name.get("Balanced")
    fast = options_by_name.get("Fast")

    expected_sell = (balanced or {}).get("price") or cheapest_safe or current_price
    true_profit = (balanced or {}).get("profit")

    # Relist chain: list balanced, fail -> cut to fast, fail -> cut again.
    chain_profits: List[int] = []
    if cheapest_safe and balanced and fast:
        third = max(1, round_clean_price(fast["price"] * 0.97) - 1)
        chain_profits = relist_chain_profits(buy_price, [balanced["price"], fast["price"], third])
    profit_first = chain_profits[0] if chain_profits else true_profit
    profit_one_relist = chain_profits[1] if len(chain_profits) > 1 else None
    profit_two_relists = chain_profits[2] if len(chain_profits) > 2 else None

    has_blocking_wall = bool(walls and expected_sell and walls[0]["price"] >= expected_sell * 0.99)

    decision, reasons = decide_flip(
        safe_count=safe_count,
        confidence=confidence,
        true_profit=true_profit,
        profit_after_one_relist=profit_one_relist,
        min_profit=min_profit,
        volume_per_day=volume_per_day,
        competition_label=competition.get("label", "Unknown"),
        has_blocking_wall=has_blocking_wall,
        features_incomplete=features_incomplete,
    )

    # Max safe buy prices (only meaningful when we have a sell estimate).
    max_safe = {}
    if expected_sell:
        max_safe = {
            "for_2m_profit": max_safe_buy_price(expected_sell, 2_000_000),
            "for_5m_profit": max_safe_buy_price(expected_sell, 5_000_000),
            "for_10m_profit": max_safe_buy_price(expected_sell, 10_000_000),
            "for_min_profit": max_safe_buy_price(expected_sell, min_profit),
            "for_min_profit_after_relist": max_safe_buy_price_after_relist(expected_sell, min_profit),
        }
    breakeven = None
    if expected_sell:
        # Sale price where profit == 0 on a single listing: S - tax(S) - fee(S) - buy = 0.
        denom = 1.0 - settings.ah_sales_tax_rate - settings.ah_listing_fee_rate
        breakeven = int(round(buy_price / denom)) if denom > 0 else None

    risk_level = _overall_risk(
        liquidity=liquidity["score"], demand=demand["score"], competition=competition["score"],
        relist_kills=(profit_one_relist is not None and profit_one_relist < min_profit),
        confidence=confidence, features_incomplete=features_incomplete,
    )

    # Plain-English headline.
    if decision == BUY:
        headline = f"Buy it: safe profit ~{format_coins(true_profit)} after all fees."
    elif decision == MAYBE:
        headline = f"Maybe — buy only if you can get it for ≤ {format_coins(max_safe.get('for_min_profit'))}."
    elif decision == DO_NOT_BUY:
        headline = "Do not buy: the margin does not survive fees/relisting at this price."
    else:
        headline = "Manual check needed: not enough safe comparable data to judge this flip."

    comparables_payload = [
        {
            "uuid": c.uuid,
            "price": c.price,
            "item_name": c.item_name,
            "score": c.score,
            "verdict": _verdict(current_price or c.price, c.price, c.score),
            "reasons": c.reasons,
            "url": settings.auction_url(c.uuid) if c.uuid else None,
        }
        for c in comparables
    ]

    result = {
        "ok": True,
        "auction_uuid": uuid,
        "item_tag": item_tag,
        "item_name": item_name,
        "current_price": current_price,
        "buy_price": buy_price,
        "min_profit": min_profit,
        "decision": decision,
        "confidence": confidence,
        "risk_level": risk_level,
        "headline": headline,
        "reasons": reasons,
        "suggested": {
            "fast": options_by_name.get("Fast"),
            "balanced": options_by_name.get("Balanced"),
            "greedy": options_by_name.get("Greedy"),
        },
        "expected_profit": profit_first,
        "profit_after_one_relist": profit_one_relist,
        "profit_after_two_relists": profit_two_relists,
        "breakeven_sale_price": breakeven,
        "max_safe_buy_prices": max_safe,
        "roi_percent": round((profit_first / buy_price) * 100.0, 1) if (buy_price and profit_first is not None) else None,
        "scores": {
            "liquidity": liquidity, "demand": demand, "competition": competition,
            "confidence": confidence,
            "relist_risk": "High" if (profit_one_relist is not None and profit_one_relist < min_profit) else "Low",
            "undercut_risk": competition.get("label", "Unknown"),
        },
        "price_rank": rank,
        "price_rank_total": total,
        "undercut_coins": undercut_coins,
        "undercut_percent": undercut_pct,
        "price_walls": walls,
        "trend": trend,
        "trend_label": decision_support.trend_label(trend),
        "volume_per_day": volume_per_day,
        "feature_summary": features_summary(base_features),
        "features": base_features,
        "confidence_notes": decision_support.confidence_explanation(
            comparable_count=safe_count, features=base_features, volume_per_day=volume_per_day, trend=trend
        ),
        "comparables": comparables_payload,
        "rejected": rejected,
        "rejection_counts": rejection_counts,
        "market_context": market_context,
        "safe_comparable_count": safe_count,
    }

    if persist:
        try:
            result["id"] = _persist(result)
        except Exception as exc:  # noqa: BLE001
            log.warning("flip persist failed: %s", exc)

    return result


def _persist(result: Dict[str, Any]) -> int:
    sug = result.get("suggested", {})

    def _price(name):
        opt = sug.get(name)
        return opt.get("price") if opt else None

    return db.insert_flip_check(
        {
            "auction_uuid": result.get("auction_uuid"),
            "item_tag": result.get("item_tag"),
            "item_name": result.get("item_name"),
            "buy_price": result.get("buy_price"),
            "decision": result.get("decision"),
            "suggested_fast_price": _price("fast"),
            "suggested_balanced_price": _price("balanced"),
            "suggested_greedy_price": _price("greedy"),
            "expected_profit": result.get("expected_profit"),
            "profit_after_one_relist": result.get("profit_after_one_relist"),
            "max_safe_buy_price": (result.get("max_safe_buy_prices") or {}).get("for_min_profit"),
            "confidence": result.get("confidence"),
            "risk_level": result.get("risk_level"),
            "reasons_json": json.dumps(result.get("reasons", [])),
            "features_json": json.dumps(result.get("features", {}), default=str),
            "comparables_json": json.dumps(result.get("comparables", [])),
            "rejected_json": json.dumps(result.get("rejected", [])),
            "market_context_json": json.dumps(result.get("market_context", {}), default=str),
        }
    )
