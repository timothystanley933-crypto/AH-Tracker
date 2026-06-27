"""Offline tests for the pre-buy Flip Checker.

No network: cofl_client.get_auction_detail / get_active_bins_pages /
get_price_history are monkeypatched (and restored) per test. Rates are pinned to
sales 1% / listing 2.5%.

Run with:  python -m pytest tests/ -q
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cofl_client, db, flip_checker  # noqa: E402
from app.config import settings  # noqa: E402


# --------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------

def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    settings.database_path = path
    db.init_db()
    return path


def reset():
    settings.relist_min_comparable_matches = 2
    settings.relist_min_comparable_score = 75
    settings.relist_pet_level_tolerance = 5
    settings.relist_star_tolerance = 0
    settings.relist_gemstone_tolerance = 0.80
    settings.relist_comparable_pages = 8
    settings.relist_min_profit_after_tax = 250_000
    settings.undercut_better_item_score = 85
    settings.flip_min_volume_for_buy = 1.0
    settings.ah_sales_tax_rate = 0.01
    settings.ah_listing_fee_rate = 0.025


_ORIG = {}


def patch_market(detail, comps, *, day=None, week=None):
    _ORIG["d"] = cofl_client.get_auction_detail
    _ORIG["b"] = cofl_client.get_active_bins_pages
    _ORIG["h"] = cofl_client.get_price_history

    async def _d(uuid, use_cache=True):
        return detail

    async def _b(tag, pages):
        return comps

    async def _h(tag, span="day"):
        return (week if span == "week" else day) or []

    cofl_client.get_auction_detail = _d
    cofl_client.get_active_bins_pages = _b
    cofl_client.get_price_history = _h


def restore_market():
    if _ORIG:
        cofl_client.get_auction_detail = _ORIG["d"]
        cofl_client.get_active_bins_pages = _ORIG["b"]
        cofl_client.get_price_history = _ORIG["h"]


def run(input_str, buy, min_profit=None):
    return asyncio.run(
        flip_checker.check_flip(
            auction_url_or_uuid=input_str, buy_price=buy, min_profit=min_profit, persist=False
        )
    )


def pet(uuid, level, tier, price):
    return {
        "uuid": uuid, "tag": "PET_SILVERFISH", "itemName": f"[Lvl {level}] Silverfish",
        "tier": tier, "startingBid": price, "bin": True,
        "flatNbt": {"petInfo": f'{{"type":"SILVERFISH","tier":"{tier}","exp":100000}}'},
    }


def attr_gear(uuid, price, mending):
    return {
        "uuid": uuid, "tag": "MOLTEN_CLOAK", "itemName": "Molten Cloak", "tier": "EPIC",
        "startingBid": price, "bin": True, "flatNbt": {"mending": str(mending)},
    }


_VOL_WEEK = [{"avg": 50_000_000, "volume": 5}, {"avg": 50_000_000, "volume": 5}]
_VOL_DAY = [{"avg": 50_000_000, "volume": 2}, {"avg": 50_000_000, "volume": 2}]

# A hex-valid auction UUID for inputs. The patched fetch ignores it and returns
# the configured `base` detail, so its exact value does not matter.
UID = "abcdef0123456789abcdef0123456789"


# --------------------------------------------------------------------------
# URL parsing
# --------------------------------------------------------------------------

def test_parse_auction_input():
    uid = "abcdef0123456789abcdef0123456789"
    assert flip_checker.parse_auction_input(f"https://sky.coflnet.com/auction/{uid}") == uid
    assert flip_checker.parse_auction_input(f"https://sky.coflnet.com/auction/{uid}?x=1#y") == uid
    assert flip_checker.parse_auction_input(uid) == uid
    assert flip_checker.parse_auction_input("not a uuid") is None
    assert flip_checker.parse_auction_input("") is None


# --------------------------------------------------------------------------
# Full pipeline
# --------------------------------------------------------------------------

def test_flip_checker_buy_clear_safe_flip():
    fresh_db(); reset()
    base = pet("base", 100, "LEGENDARY", 30_000_000)            # listed cheap
    comps = [pet(f"c{i}", 100, "LEGENDARY", 50_000_000 + i * 100_000) for i in range(5)]
    patch_market(base, comps, day=_VOL_DAY, week=_VOL_WEEK)
    try:
        d = run(UID, 30_000_000, min_profit=1_000_000)
    finally:
        restore_market()
    assert d["ok"] is True
    assert d["decision"] == "BUY"
    assert d["safe_comparable_count"] >= 2
    assert d["expected_profit"] > 1_000_000


def test_flip_checker_rejects_raw_lbin_only():
    fresh_db(); reset()
    base = pet("base", 100, "LEGENDARY", 50_000_000)
    # Only a junk Lvl1 Common is cheaper - NOT a safe comparable.
    comps = [pet("junk", 1, "COMMON", 40_000)]
    patch_market(base, comps, day=_VOL_DAY, week=_VOL_WEEK)
    try:
        d = run(UID, 30_000_000)
    finally:
        restore_market()
    assert d["decision"] != "BUY"
    assert d["decision"] in ("INCOMPARABLE", "DO_NOT_BUY")
    # The raw same-tag LBIN is visible as context but is NOT used as a price.
    assert d["market_context"]["raw_same_tag_lbin"] == 40_000
    assert any(r["uuid"] == "junk" for r in d["rejected"])


def test_flip_checker_pet_level_mismatch_rejected():
    fresh_db(); reset()
    base = pet("base", 100, "LEGENDARY", 50_000_000)
    comps = [pet("low", 1, "LEGENDARY", 40_000)]   # same tier, wildly different level
    patch_market(base, comps, day=_VOL_DAY, week=_VOL_WEEK)
    try:
        d = run(UID, 30_000_000)
    finally:
        restore_market()
    assert d["decision"] != "BUY"
    assert "low" not in [c["uuid"] for c in d["comparables"]]
    assert any(r["uuid"] == "low" for r in d["rejected"])
    assert any("level" in (r["reason"] or "").lower() for r in d["rejected"])


def test_flip_checker_attributes_mismatch_rejected():
    fresh_db(); reset()
    base = attr_gear("base", 50_000_000, mending=5)
    comps = [
        attr_gear("good1", 50_000_000, mending=5),
        attr_gear("good2", 51_000_000, mending=5),
        attr_gear("bad", 30_000_000, mending=1),   # weak attribute -> rejected
    ]
    patch_market(base, comps, day=_VOL_DAY, week=_VOL_WEEK)
    try:
        d = run(UID, 30_000_000)
    finally:
        restore_market()
    assert "bad" not in [c["uuid"] for c in d["comparables"]]
    assert any(r["uuid"] == "bad" for r in d["rejected"])
    assert any("attribute" in (r["reason"] or "").lower() for r in d["rejected"])


def test_flip_checker_skin_same_tag_context():
    fresh_db(); reset()
    base = {"uuid": "base", "tag": "MIDAS_SWORD_SKIN", "itemName": "Midas Sword Skin",
            "tier": "RARE", "startingBid": 40_000_000, "bin": True}
    comps = [
        {"uuid": "s1", "tag": "MIDAS_SWORD_SKIN", "itemName": "Midas Sword Skin",
         "tier": "RARE", "startingBid": 42_000_000, "bin": True},
        {"uuid": "s2", "tag": "MIDAS_SWORD_SKIN", "itemName": "Midas Sword Skin",
         "tier": "RARE", "startingBid": 43_000_000, "bin": True},
    ]
    patch_market(base, comps, day=_VOL_DAY, week=_VOL_WEEK)
    try:
        d = run(UID, 38_000_000)
    finally:
        restore_market()
    assert d["ok"] is True
    assert d["item_tag"] == "MIDAS_SWORD_SKIN"
    # Same item_tag drives the market context (not a different skin).
    assert d["market_context"]["raw_same_tag_lbin"] is not None
    assert d["market_context"]["raw_same_tag_top"]


def test_flip_checker_incomplete_features_lower_confidence():
    fresh_db(); reset()
    base = {"uuid": "base", "tag": "MYSTERY_SWORD", "itemName": "Mystery Sword",
            "startingBid": 50_000_000, "bin": True}      # no tier / NBT
    comps = [{"uuid": "c1", "tag": "MYSTERY_SWORD", "itemName": "Mystery Sword",
              "startingBid": 49_000_000, "bin": True}]
    patch_market(base, comps, day=_VOL_DAY, week=_VOL_WEEK)
    try:
        d = run(UID, 30_000_000)
    finally:
        restore_market()
    assert any("NBT" in n for n in d["confidence_notes"])
    assert d["decision"] != "BUY"


# --------------------------------------------------------------------------
# Pure fee math / decision
# --------------------------------------------------------------------------

def test_flip_checker_profit_after_one_relist():
    reset()
    res = flip_checker.relist_chain_profits(200_000_000, [230_000_000, 225_000_000])
    # Sold on first listing: 230 - 200 - 2.3(tax) - 5.75(fee) = 21.95m.
    assert res[0] == 21_950_000
    # Sold after one relist: 225 - 200 - 2.25(tax) - 5.75(fee1) - 5.625(fee2) = 11.375m.
    assert res[1] == 11_375_000


def test_flip_checker_max_safe_buy_price():
    reset()
    # 225 - 2.25(tax) - 5.625(fee) - 5(profit) = 212.125m.
    assert flip_checker.max_safe_buy_price(225_000_000, 5_000_000) == 212_125_000
    # Survives one relist (two listing fees): 225 - 2.25 - 11.25 - 5 = 206.5m.
    assert flip_checker.max_safe_buy_price_after_relist(225_000_000, 5_000_000) == 206_500_000


def test_flip_checker_do_not_buy_when_relist_kills_profit():
    reset()
    decision, reasons = flip_checker.decide_flip(
        safe_count=5, confidence=90, true_profit=8_000_000,
        profit_after_one_relist=-500_000,     # one relist turns it into a loss
        min_profit=5_000_000, volume_per_day=5, competition_label="Low",
        has_blocking_wall=False, features_incomplete=False,
    )
    assert decision == "DO_NOT_BUY"
    assert any("relist" in r.lower() for r in reasons)


def test_flip_checker_maybe_low_volume_good_profit():
    reset()
    decision, reasons = flip_checker.decide_flip(
        safe_count=5, confidence=90, true_profit=20_000_000,
        profit_after_one_relist=15_000_000, min_profit=5_000_000,
        volume_per_day=0.3,                   # great margin, terrible volume
        competition_label="Low", has_blocking_wall=False, features_incomplete=False,
    )
    assert decision == "MAYBE"
    assert any("volume" in r.lower() for r in reasons)


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
