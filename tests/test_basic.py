"""Offline unit tests for the pure logic (no network).

Run with:  python -m pytest tests/ -q
       or:  python tests/test_basic.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.formatting import format_coins, format_profit, parse_coins, round_clean_price  # noqa: E402
from app.features import extract_item_features  # noqa: E402
from app.scoring import score_comparable  # noqa: E402
from app.analysis import min_safe_price, profit_after_tax, compute_trend  # noqa: E402


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def test_parse_coins():
    assert parse_coins("5000000") == 5_000_000
    assert parse_coins("5,000,000") == 5_000_000
    assert parse_coins("5m") == 5_000_000
    assert parse_coins("1.5m") == 1_500_000
    assert parse_coins("500k") == 500_000
    assert parse_coins("£5,000,000") == 5_000_000
    assert parse_coins("2b") == 2_000_000_000
    assert parse_coins("") is None
    assert parse_coins("abc") is None
    assert parse_coins(4_700_000) == 4_700_000


def test_format():
    assert format_coins(5_300_000) == "5,300,000"
    assert format_coins(None) == "—"
    assert format_profit(500_000) == "+500,000"
    assert format_profit(-120_000) == "-120,000"


def test_round_clean():
    assert round_clean_price(523_400) == 523_000
    assert round_clean_price(5_123_456) == 5_120_000
    assert round_clean_price(42_345_678) == 42_300_000


# --------------------------------------------------------------------------
# Feature extraction
# --------------------------------------------------------------------------

def _silverfish(level, tier, price, uuid="x"):
    return {
        "uuid": uuid,
        "tag": "PET_SILVERFISH",
        "itemName": f"[Lvl {level}] Silverfish",
        "tier": tier,
        "startingBid": price,
        "bin": True,
        "flatNbt": {"petInfo": f'{{"type":"SILVERFISH","tier":"{tier}","exp":100000}}'},
    }


def test_extract_pet():
    f = extract_item_features(_silverfish(100, "LEGENDARY", 5_000_000))
    assert f["is_pet"] is True
    assert f["pet"]["level"] == 100
    assert f["pet"]["tier"] == "LEGENDARY"
    assert f["item_tag"] == "PET_SILVERFISH"
    assert f["price"] == 5_000_000


def test_extract_gear_recomb_stars():
    auction = {
        "uuid": "g1",
        "tag": "HYPERION",
        "itemName": "Heroic Hyperion ✪✪✪",
        "tier": "MYTHIC",
        "startingBid": 900_000_000,
        "reforge": "heroic",
        "flatNbt": {"rarity_upgrades": "1", "upgrade_level": "3", "unlocked_slots": "2", "COMBAT_0": "PERFECT"},
        "enchantments": [{"type": "ultimate_wise", "level": 5}, {"type": "sharpness", "level": 7}],
    }
    f = extract_item_features(auction)
    assert f["recombobulated"] is True
    assert f["stars"] == 3
    assert f["reforge"] == "heroic"
    assert f["gemstones"]["has_gems"] is True
    assert "ultimate_wise" in f["important_enchants"]


# --------------------------------------------------------------------------
# Scoring - THE core safety behaviour
# --------------------------------------------------------------------------

def test_silverfish_not_comparable_to_junk():
    """The headline bug: a 5m Lvl100 Legendary must NOT match a 40k Lvl1 Common."""
    base = extract_item_features(_silverfish(100, "LEGENDARY", 5_000_000, "base"))
    junk = extract_item_features(_silverfish(1, "COMMON", 40_000, "junk"))
    result = score_comparable(base, junk)
    assert result.accepted is False
    assert result.score == 0
    assert any("tier" in r.lower() for r in result.rejections)


def test_silverfish_comparable_to_similar():
    base = extract_item_features(_silverfish(100, "LEGENDARY", 5_000_000, "base"))
    similar = extract_item_features(_silverfish(98, "LEGENDARY", 4_800_000, "sim"))
    result = score_comparable(base, similar)
    assert result.accepted is True
    assert result.score >= 75


def test_clean_gear_comparable():
    a = {"uuid": "a", "tag": "ASPECT_OF_THE_END", "itemName": "Aspect of the End", "tier": "RARE", "startingBid": 100000}
    b = {"uuid": "b", "tag": "ASPECT_OF_THE_END", "itemName": "Aspect of the End", "tier": "RARE", "startingBid": 95000}
    result = score_comparable(extract_item_features(a), extract_item_features(b))
    assert result.accepted is True


def test_recomb_mismatch_gear_rejected():
    a = {"uuid": "a", "tag": "TERMINATOR", "itemName": "Terminator", "tier": "LEGENDARY",
         "startingBid": 100, "flatNbt": {"rarity_upgrades": "1"}}
    b = {"uuid": "b", "tag": "TERMINATOR", "itemName": "Terminator", "tier": "LEGENDARY",
         "startingBid": 100, "flatNbt": {}}
    result = score_comparable(extract_item_features(a), extract_item_features(b))
    assert result.accepted is False


def test_different_tag_rejected():
    a = extract_item_features({"uuid": "a", "tag": "HYPERION", "itemName": "Hyperion", "tier": "MYTHIC"})
    b = extract_item_features({"uuid": "b", "tag": "VALKYRIE", "itemName": "Valkyrie", "tier": "MYTHIC"})
    assert score_comparable(a, b).accepted is False


# --------------------------------------------------------------------------
# Profit math
# --------------------------------------------------------------------------

def test_profit_after_tax():
    # 5,300,000 sale, 2% tax = 106,000 tax; buy 4,700,000 -> profit 494,000.
    # Pin the rate so this formula test is independent of the configured default
    # (sales tax now defaults to 1%).
    from app.config import settings
    prev = settings.ah_sales_tax_rate
    settings.ah_sales_tax_rate = 0.02
    try:
        assert profit_after_tax(5_300_000, 4_700_000) == 494_000
    finally:
        settings.ah_sales_tax_rate = prev


def test_min_safe_price():
    # Need to clear 4,700,000 + 250,000 after 2% tax.
    floor = min_safe_price(4_700_000, 250_000)
    # Selling at floor should yield >= min profit.
    assert profit_after_tax(floor, 4_700_000) >= 250_000 - 5  # rounding slack


def test_compute_trend():
    day = [{"avg": 100, "volume": 5}, {"avg": 110, "volume": 5}]
    week = [{"avg": 100, "volume": 10}, {"avg": 90, "volume": 12}]
    trend, vol = compute_trend(day, week)
    assert trend["day_pct"] == 10.0
    assert trend["week_pct"] == -10.0
    assert vol is not None


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
