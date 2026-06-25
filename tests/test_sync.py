"""Offline tests for the sync state machine and notification gating.

No network: cofl_client.get_all_player_auctions and notifications.send_raw are
monkeypatched. Each test uses a fresh temp SQLite DB.

Run with:  python -m pytest tests/ -q
       or:  python tests/test_sync.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import cofl_client, db, notifications, sync  # noqa: E402
from app import views  # noqa: E402
from app.config import settings  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _iso(dt):
    return dt.isoformat()


def _future():
    return _iso(datetime.now(timezone.utc) + timedelta(hours=12))


def _past():
    return _iso(datetime.now(timezone.utc) - timedelta(hours=12))


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # let sqlite create it cleanly
    settings.database_path = path
    db.init_db()
    return path


def reset_settings():
    settings.mc_uuid = "testuuid"
    settings.notifications_enabled = True
    settings.sold_alerts = True
    settings.relist_alerts = True
    settings.first_sync_suppress_sold_alerts = True
    settings.stale_after_missed_syncs = 2


class SendCounter:
    def __init__(self):
        self.count = 0

    async def __call__(self, *args, **kwargs):
        self.count += 1
        return True


def patch_send():
    counter = SendCounter()
    notifications.send_raw = counter
    return counter


def set_player_items(items):
    async def _fn(uuid, max_pages=5):
        return items
    cofl_client.get_all_player_auctions = _fn


def set_player_failure():
    async def _fn(uuid, max_pages=5):
        return None
    cofl_client.get_all_player_auctions = _fn


def active_item(uuid, starting=5_000_000, tag="PET_TEST", name="[Lvl 100] Test"):
    return {"uuid": uuid, "tag": tag, "itemName": name, "startingBid": starting,
            "highestBid": 0, "bin": True, "end": _future()}


def sold_item(uuid, highest=5_300_000, starting=5_000_000, tag="PET_TEST", name="[Lvl 100] Test"):
    return {"uuid": uuid, "tag": tag, "itemName": name, "startingBid": starting,
            "highestBid": highest, "bin": True, "end": _past()}


def expired_item(uuid, starting=5_000_000, tag="PET_TEST", name="[Lvl 100] Test"):
    return {"uuid": uuid, "tag": tag, "itemName": name, "startingBid": starting,
            "highestBid": 0, "bin": True, "end": _past()}


def run_sync():
    return asyncio.run(sync.sync_player_auctions())


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_first_sync_old_sold_no_notifications():
    fresh_db(); reset_settings()
    counter = patch_send()
    set_player_items([sold_item("s1")])
    run_sync()
    assert counter.count == 0
    row = db.get_auction("s1")
    assert row["status"] == "SOLD"
    assert row["sold_notified"] == 1
    assert row["notification_eligible"] == 0


def test_active_then_sold_one_notification():
    fresh_db(); reset_settings()
    counter = patch_send()

    set_player_items([active_item("a1")])
    run_sync()
    assert counter.count == 0  # active, nothing to notify
    assert db.get_auction("a1")["status"] == "ACTIVE"

    set_player_items([sold_item("a1")])
    run_sync()
    assert counter.count == 1  # exactly one sold notification on transition
    row = db.get_auction("a1")
    assert row["status"] == "SOLD"
    assert row["sold_price"] == 5_300_000

    # A subsequent sync still showing it sold must NOT re-notify.
    set_player_items([sold_item("a1")])
    run_sync()
    assert counter.count == 1


def test_active_listing_price_from_starting_bid():
    fresh_db(); reset_settings()
    patch_send()
    set_player_items([active_item("p1", starting=5_000_000)])  # highestBid is 0
    run_sync()
    row = db.get_auction("p1")
    assert row["status"] == "ACTIVE"
    assert row["listing_price"] == 5_000_000  # NOT taken from highestBid


def test_expired_unsold_no_notification():
    fresh_db(); reset_settings()
    counter = patch_send()
    set_player_items([expired_item("e1")])
    run_sync()
    row = db.get_auction("e1")
    assert row["status"] == "EXPIRED"
    assert row["sold_notified"] == 0
    assert counter.count == 0


def test_stale_after_two_missed_syncs_and_hidden():
    fresh_db(); reset_settings()
    patch_send()

    set_player_items([active_item("m1")])
    run_sync()
    assert db.get_auction("m1")["status"] == "ACTIVE"

    set_player_items([])  # missing once
    run_sync()
    row = db.get_auction("m1")
    assert row["status"] == "ACTIVE" and row["missed_syncs"] == 1

    set_player_items([])  # missing twice -> STALE
    run_sync()
    assert db.get_auction("m1")["status"] == "STALE"

    # Hidden by default on the dashboard.
    cards = views.build_cards(db.list_auctions(), db.latest_analyses_map())
    default_view = views.filter_cards(cards, "active")
    assert all(c["status"] == "ACTIVE" for c in default_view)
    assert not any(c["uuid"] == "m1" for c in default_view)
    # But visible under the explicit STALE filter.
    stale_view = views.filter_cards(cards, "stale")
    assert any(c["uuid"] == "m1" for c in stale_view)


def test_dashboard_default_excludes_sold_expired_stale():
    fresh_db(); reset_settings()
    sid = 1
    db.upsert_synced(uuid="A", item_tag="T", item_name="A", skycofl_url="u",
                     status="ACTIVE", listing_price=1000, sold_price=None, ends_at=None,
                     sync_id=sid, notification_eligible=1, sold_notified=0)
    db.upsert_synced(uuid="S", item_tag="T", item_name="S", skycofl_url="u",
                     status="SOLD", listing_price=1000, sold_price=2000, ends_at=None,
                     sync_id=sid, notification_eligible=0, sold_notified=1)
    db.upsert_synced(uuid="E", item_tag="T", item_name="E", skycofl_url="u",
                     status="EXPIRED", listing_price=1000, sold_price=None, ends_at=None,
                     sync_id=sid, notification_eligible=0, sold_notified=0)
    db.upsert_synced(uuid="ST", item_tag="T", item_name="ST", skycofl_url="u",
                     status="STALE", listing_price=1000, sold_price=None, ends_at=None,
                     sync_id=sid, notification_eligible=0, sold_notified=0)

    cards = views.build_cards(db.list_auctions(), db.latest_analyses_map())
    default_view = views.filter_cards(cards, "active")
    statuses = {c["status"] for c in default_view}
    assert statuses == {"ACTIVE"}
    assert {c["uuid"] for c in default_view} == {"A"}


def test_notifications_disabled_blocks_everything():
    fresh_db(); reset_settings()
    settings.notifications_enabled = False
    counter = patch_send()

    set_player_items([active_item("d1")])
    run_sync()
    set_player_items([sold_item("d1")])
    run_sync()  # would normally be a sold transition
    assert counter.count == 0
    assert db.get_auction("d1")["status"] == "SOLD"


def test_failed_fetch_does_not_mark_stale():
    fresh_db(); reset_settings()
    patch_send()

    set_player_items([active_item("f1")])
    run_sync()
    assert db.get_auction("f1")["status"] == "ACTIVE"

    set_player_failure()  # transient API failure
    stats = run_sync()
    assert stats["errors"] == 1
    row = db.get_auction("f1")
    assert row["status"] == "ACTIVE"
    assert row["missed_syncs"] == 0  # untouched - we couldn't confirm state


def test_sync_preserves_user_buy_cost():
    """The headline regression: a saved buy cost must survive a sync that
    carries no buy cost (and so must the other user-owned fields)."""
    fresh_db(); reset_settings()
    patch_send()

    # 1) Auction appears and the user saves a buy cost (+ other user fields).
    set_player_items([active_item("keep", starting=5_000_000)])
    run_sync()
    db.set_buy_cost("keep", 5_000_000)
    db.set_min_profit("keep", 1_000_000)
    db.set_notes("keep", "my note")
    db.set_ignored("keep", True)
    db.set_target_sell_price("keep", 9_000_000)

    # 2) Sync the SAME auction again with no buy cost and a new listing price.
    set_player_items([active_item("keep", starting=6_000_000)])
    run_sync()

    # 3) User fields preserved; only market fields updated.
    row = db.get_auction("keep")
    assert row["buy_cost"] == 5_000_000          # <-- the bug being fixed
    assert row["min_profit"] == 1_000_000
    assert row["notes"] == "my note"
    assert row["ignored"] == 1
    assert row["target_sell_price"] == 9_000_000
    assert row["listing_price"] == 6_000_000     # market field DID update


def test_sold_transition_preserves_buy_cost():
    """A buy cost must also survive the ACTIVE -> SOLD transition."""
    fresh_db(); reset_settings()
    patch_send()

    set_player_items([active_item("s", starting=5_000_000)])
    run_sync()
    db.set_buy_cost("s", 4_700_000)

    set_player_items([sold_item("s", highest=5_300_000)])
    run_sync()

    row = db.get_auction("s")
    assert row["status"] == "SOLD"
    assert row["buy_cost"] == 4_700_000
    assert row["sold_price"] == 5_300_000


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
