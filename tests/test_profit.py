"""Offline tests for the fee/relist ledger, fee-aware profit, decision-support
helpers and the urgent-actions dashboard. No network; each test uses a fresh
temp SQLite DB and pins the tax/fee rates (sales 1%, listing 2.5%).

Run with:  python -m pytest tests/ -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, decision_support, profit, views  # noqa: E402
from app.config import settings  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    settings.database_path = path
    db.init_db()
    return path


def set_rates():
    settings.ah_sales_tax_rate = 0.01     # 1% sales tax
    settings.ah_listing_fee_rate = 0.025  # 2.5% listing fee


def active(uuid, price, *, tag="PET_TEST", name="[Lvl 100] Test"):
    db.upsert_synced(
        uuid=uuid, item_tag=tag, item_name=name, skycofl_url="u",
        status="ACTIVE", listing_price=price, sold_price=None, ends_at=None,
        sync_id=1, notification_eligible=1, sold_notified=0,
    )


# --------------------------------------------------------------------------
# Fee ledger
# --------------------------------------------------------------------------

def test_initial_listing_fee_recorded_once():
    fresh_db(); set_rates()
    active("a1", 230_000_000)
    assert db.record_initial_list_fee("a1", 230_000_000) is True
    # A second (and third) sync of the same active auction must not duplicate it.
    assert db.record_initial_list_fee("a1", 230_000_000) is False
    db.record_initial_list_fee("a1", 230_000_000)

    initials = [e for e in db.fee_events_for("a1") if e["event_type"] == "INITIAL_LIST"]
    assert len(initials) == 1
    row = db.get_auction("a1")
    assert row["accumulated_listing_fees"] == 5_750_000      # 230m * 2.5%
    assert row["first_seen_listing_price"] == 230_000_000


def test_multiple_relist_fees_are_accumulated():
    fresh_db(); set_rates()
    # A listed at 230m, relisted to B at 225m, relisted again to C at 220m.
    active("A", 230_000_000); db.record_initial_list_fee("A", 230_000_000)   # 5.75m
    active("B", 225_000_000); db.record_initial_list_fee("B", 225_000_000)   # replaced by RELIST
    db.record_relist_fee("B", "A", 225_000_000)                              # 5.625m + 5.75m
    active("C", 220_000_000); db.record_initial_list_fee("C", 220_000_000)
    db.record_relist_fee("C", "B", 220_000_000)                              # 5.5m + 11.375m

    assert db.get_auction("B")["accumulated_listing_fees"] == 11_375_000
    c = db.get_auction("C")
    # All three listing fees counted exactly once: 5.75 + 5.625 + 5.5 = 16.875m.
    assert c["accumulated_listing_fees"] == 16_875_000
    assert c["relist_count"] == 2     # relisted twice across the chain
    assert db.get_auction("B")["relist_count"] == 1


def test_relist_fee_not_double_counted_on_duplicate_carry():
    fresh_db(); set_rates()
    active("A", 230_000_000); db.record_initial_list_fee("A", 230_000_000)
    active("B", 225_000_000); db.record_initial_list_fee("B", 225_000_000)

    db.record_relist_fee("B", "A", 225_000_000)
    first = db.get_auction("B")["accumulated_listing_fees"]
    first_count = db.get_auction("B")["relist_count"]

    # Accepting the same carry again (or a page reload) must be a no-op.
    db.record_relist_fee("B", "A", 225_000_000)
    db.record_relist_fee("B", "A", 225_000_000)

    after = db.get_auction("B")
    assert after["accumulated_listing_fees"] == first == 11_375_000
    assert after["relist_count"] == first_count == 1


# --------------------------------------------------------------------------
# Fee-aware profit helpers
# --------------------------------------------------------------------------

def test_profit_if_current_sells_uses_accumulated_fees():
    fresh_db(); set_rates()
    active("p1", 225_000_000)
    db.set_buy_cost("p1", 200_000_000)
    # Listing fees already paid across previous listings (230m + 225m = 11.375m).
    db.add_manual_listing_fee("p1", 11_375_000)

    row = db.get_auction("p1")
    # 225m - 200m - 2.25m(1% tax) - 11.375m = 11.375m. No NEW relist fee (already listed).
    assert profit.profit_if_current_sells(row) == 11_375_000


def test_profit_after_relist_adds_new_listing_fee():
    fresh_db(); set_rates()
    active("r1", 225_000_000)
    db.set_buy_cost("r1", 200_000_000)
    db.add_manual_listing_fee("r1", 5_750_000)   # accumulated fees already paid

    row = db.get_auction("r1")
    # 225m - 200m - 2.25m(tax) - 5.75m(paid) - 5.625m(new 2.5% fee) = 11.375m.
    assert profit.profit_after_relist(row, 225_000_000) == 11_375_000


def test_hold_profit_does_not_add_new_relist_fee():
    fresh_db(); set_rates()
    active("h1", 225_000_000)
    db.set_buy_cost("h1", 200_000_000)
    db.add_manual_listing_fee("h1", 5_750_000)

    row = db.get_auction("h1")
    current = profit.profit_if_current_sells(row)          # no new fee
    relisted = profit.profit_after_relist(row, 225_000_000)  # adds the new fee
    # Selling as-listed does NOT pay another listing fee, so it nets more.
    assert current == 17_000_000                            # 225-200-2.25-5.75
    assert relisted == current - 5_625_000                  # exactly the new relist fee
    bd = profit.profit_breakdown(row, 225_000_000, include_new_relist_fee=False)
    assert bd["new_relist_fee"] == 0


# --------------------------------------------------------------------------
# Decision support
# --------------------------------------------------------------------------

def test_fast_balanced_greedy_options_include_fee_aware_profit():
    fresh_db(); set_rates()
    active("o1", 230_000_000)
    db.set_buy_cost("o1", 200_000_000)
    db.add_manual_listing_fee("o1", 5_750_000)
    row = db.get_auction("o1")

    options = profit.build_relist_options(row, 230_000_000)
    names = [o["name"] for o in options]
    assert names == ["Fast", "Balanced", "Greedy"]
    # Fast undercuts the most -> lowest price; Greedy the least -> highest price.
    assert options[0]["price"] < options[1]["price"] < options[2]["price"]
    for o in options:
        # Each option carries a fee-aware profit equal to profit_after_relist.
        assert o["profit"] == profit.profit_after_relist(row, o["price"])
        assert o["profit"] is not None


def test_price_rank_and_undercut_percent():
    prices = [100, 200, 300, 400, 500]
    rank, total = decision_support.price_rank(350, prices)
    assert (rank, total) == (4, 5)            # #4 cheapest of 5 (3 cheaper + itself)

    gap, pct = decision_support.undercut_amount(250_000_000, 245_000_000)
    assert gap == 5_000_000
    assert pct == 2.0                          # 5m / 250m = 2.0%


def test_price_wall_detection():
    set_rates()
    # Five listings packed within 1% of 228m, plus two far-away outliers.
    prices = [228_000_000, 228_500_000, 229_000_000, 229_500_000, 230_000_000,
              260_000_000, 300_000_000]
    walls = decision_support.detect_price_walls(prices, window_percent=1.0, min_count=5)
    assert len(walls) == 1
    assert walls[0]["price"] == 228_000_000
    assert walls[0]["count"] == 5
    # A higher count threshold finds no wall.
    assert decision_support.detect_price_walls(prices, window_percent=1.0, min_count=6) == []


def test_urgent_actions_dashboard_prioritises_undercut_and_cut_loss():
    cards = [
        {"uuid": "hold", "status": "ACTIVE", "decision": "HOLD", "ignored": False,
         "listing_price": 10, "undercut": None, "profit_relist": None, "profit_relist_fmt": "—"},
        {"uuid": "relist", "status": "ACTIVE", "decision": "RELIST", "ignored": False,
         "listing_price": 20, "undercut": None, "profit_relist": 5, "profit_relist_fmt": "+5"},
        {"uuid": "cut", "status": "ACTIVE", "decision": "CUT_LOSS", "ignored": False,
         "listing_price": 30, "undercut": None, "profit_relist": -5, "profit_relist_fmt": "-5"},
        {"uuid": "under", "status": "ACTIVE", "decision": "HOLD", "ignored": False,
         "listing_price": 40, "undercut": {"gap_coins_fmt": "1", "gap_percent": 2},
         "profit_relist": None, "profit_relist_fmt": "—"},
    ]
    result = views.build_urgent_actions(cards)
    order = [c["uuid"] for c in result["items"]]
    # Undercut first, then cut-loss, then relist; plain HOLD is excluded entirely.
    assert order[0] == "under"
    assert order[1] == "cut"
    assert "hold" not in order
    assert result["buckets"]["undercut"]["cards"][0]["uuid"] == "under"
    assert result["buckets"]["cut_loss"]["cards"][0]["uuid"] == "cut"


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
