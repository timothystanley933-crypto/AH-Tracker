"""Offline tests for decision logic, sale-time prediction, and sold sorting.

Run with:  python -m pytest tests/ -q
       or:  python tests/test_decision.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analysis, views  # noqa: E402
from app.analysis import Comparable, compute_sell_estimate  # noqa: E402
from app.config import settings  # noqa: E402


def reset():
    settings.relist_min_comparable_matches = 2
    settings.relist_min_comparable_score = 75
    settings.relist_price_gap_percent = 5
    settings.relist_price_gap_coins = 250000
    settings.relist_decent_volume_per_day = 3
    settings.relist_strong_down_trend_24h = -3
    settings.relist_strong_up_trend_24h = 5
    settings.relist_undercut_coins = 10000
    settings.relist_undercut_percent = 0.20
    settings.ah_tax_rate = 0.02


def comps(cheapest, count, step=100000, score=90):
    """Build `count` comparables with the given cheapest price."""
    return [Comparable(uuid=str(i), price=cheapest + i * step, score=score, item_name="x")
            for i in range(count)]


def decide(**kw):
    base = dict(base_features={}, trend={"day_pct": None, "week_pct": None}, volume_per_day=None)
    base.update(kw)
    return analysis._decide(**base)


# --------------------------------------------------------------------------
# Decision logic
# --------------------------------------------------------------------------

def test_storm_leggings_relist():
    reset()
    decision, suggested, profit, reasons = decide(
        listing_price=35_100_000,
        buy_cost=30_000_000,
        min_profit=250_000,
        comparables=comps(33_000_000, 22),
        confidence=95,
        trend={"day_pct": -31.7, "week_pct": -10.0},
        volume_per_day=6.36,
    )
    assert decision == "RELIST"
    assert suggested == 32_900_000
    assert profit == 2_242_000
    assert any("above" in r.lower() for r in reasons)


def test_hooverius_tiny_gap_hold():
    reset()
    decision, suggested, profit, reasons = decide(
        listing_price=11_150_000,
        buy_cost=8_000_000,
        min_profit=250_000,
        comparables=comps(11_150_000, 11),
        confidence=83,
        trend={"day_pct": 0.0, "week_pct": 0.0},
        volume_per_day=8.87,
    )
    assert decision == "HOLD"
    assert suggested == 11_100_000
    # The reason must explain the tiny gap.
    assert any("only" in r.lower() or "too small" in r.lower() for r in reasons)


def test_profit_below_min_is_profit_low():
    reset()
    decision, suggested, profit, reasons = decide(
        listing_price=5_500_000,
        buy_cost=4_700_000,
        min_profit=250_000,
        comparables=comps(5_000_000, 6),
        confidence=90,
        trend={"day_pct": 0.0},
        volume_per_day=4,
    )
    assert decision == "PROFIT_LOW"
    assert 0 < profit < 250_000


def test_low_confidence_not_relist():
    reset()
    decision, *_ = decide(
        listing_price=35_100_000,
        buy_cost=30_000_000,
        min_profit=250_000,
        comparables=comps(33_000_000, 6, score=40),
        confidence=50,
        trend={"day_pct": -10.0},
        volume_per_day=6,
    )
    assert decision in ("HOLD", "INCOMPARABLE")
    assert decision != "RELIST"


def test_too_few_comparables_not_relist():
    reset()
    decision, suggested, *_ = decide(
        listing_price=35_100_000,
        buy_cost=30_000_000,
        min_profit=250_000,
        comparables=comps(33_000_000, 1),
        confidence=95,
        trend={"day_pct": -10.0},
        volume_per_day=6,
    )
    assert decision in ("INCOMPARABLE", "HOLD")
    assert decision != "RELIST"
    assert suggested is None  # INCOMPARABLE suggests no price


def test_uptrend_with_small_gap_holds():
    reset()
    decision, *_ = decide(
        listing_price=33_500_000,
        buy_cost=30_000_000,
        min_profit=250_000,
        comparables=comps(33_000_000, 10),
        confidence=90,
        trend={"day_pct": 12.0},  # strong uptrend
        volume_per_day=5,
    )
    assert decision == "HOLD"


# --------------------------------------------------------------------------
# Sale-time prediction
# --------------------------------------------------------------------------

def test_sale_time_rank1_faster_than_rank10():
    reset()
    prices = [1_000_000 * i for i in range(1, 13)]  # 1m .. 12m
    cmps = [Comparable(uuid=str(i), price=p, score=90, item_name="x") for i, p in enumerate(prices)]

    est_rank1 = compute_sell_estimate(
        decision="RELIST", listing_price=500_000, suggested_price=900_000,
        comparables=cmps, comparable_count=len(cmps), volume_per_day=6,
        trend={"day_pct": None},
    )
    est_rank10 = compute_sell_estimate(
        decision="RELIST", listing_price=9_500_000, suggested_price=900_000,
        comparables=cmps, comparable_count=len(cmps), volume_per_day=6,
        trend={"day_pct": None},
    )
    assert est_rank1["sale_likelihood_current"] in ("likely", "possible")
    assert est_rank10["sale_likelihood_current"] == "unlikely soon"
    assert est_rank1["estimated_sell_time_current"] != est_rank10["estimated_sell_time_current"]


def test_incomparable_no_confident_sale_time():
    reset()
    est = compute_sell_estimate(
        decision="INCOMPARABLE", listing_price=5_000_000, suggested_price=None,
        comparables=[], comparable_count=0, volume_per_day=6,
        trend={"day_pct": None},
    )
    assert est["estimated_sell_time_current"] == "Unknown"
    assert est["sale_likelihood_current"] == "unknown"


def test_low_volume_is_low_confidence():
    reset()
    cmps = comps(5_000_000, 4)
    est = compute_sell_estimate(
        decision="RELIST", listing_price=6_000_000, suggested_price=4_900_000,
        comparables=cmps, comparable_count=len(cmps), volume_per_day=0.3,
        trend={"day_pct": None},
    )
    assert "low confidence" in est["sale_likelihood_current"]


# --------------------------------------------------------------------------
# Sold tab sorting
# --------------------------------------------------------------------------

def test_sold_sorting_newest_first_with_fallback():
    cards = [
        {"uuid": "c1", "sold_at": "2026-06-20T10:00:00+00:00", "ended_at": None, "updated_at": None, "last_seen": None},
        {"uuid": "c2", "sold_at": "2026-06-22T10:00:00+00:00", "ended_at": None, "updated_at": None, "last_seen": None},
        {"uuid": "c3", "sold_at": None, "ended_at": "2026-06-21T10:00:00+00:00", "updated_at": None, "last_seen": None},
        {"uuid": "c4", "sold_at": None, "ended_at": None, "updated_at": "2026-06-19T10:00:00+00:00", "last_seen": None},
    ]
    ordered = [c["uuid"] for c in views.sort_sold(cards)]
    assert ordered == ["c2", "c3", "c1", "c4"]


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
