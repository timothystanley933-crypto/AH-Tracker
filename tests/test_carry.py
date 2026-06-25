"""Focused tests for carrying user fields to a relisted auction."""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import carry, cofl_client, db, sync  # noqa: E402
from app.config import settings  # noqa: E402
from app.features import extract_item_features  # noqa: E402


def _iso(dt):
    return dt.isoformat()


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    settings.database_path = path
    settings.relist_carry_enabled = True
    settings.relist_carry_lookback_days = 14
    settings.relist_carry_min_score = 85
    settings.relist_carry_auto_apply = False
    settings.relist_star_tolerance = 0
    settings.relist_min_comparable_score = 75
    settings.stale_after_missed_syncs = 2
    settings.mc_uuid = "testuuid"
    db.init_db()
    return path


def _item(uuid, *, tag="STORM_LEGGINGS", name="Storm's Leggings", stars=5, tier="LEGENDARY"):
    return {
        "uuid": uuid,
        "tag": tag,
        "itemName": name,
        "tier": tier,
        "startingBid": 30_000_000,
        "bin": True,
        "flatNbt": {"upgrade_level": str(stars), "rarity_upgrades": "1"},
        "enchantments": [{"type": "wisdom", "level": 5}],
    }


def _silverfish(uuid, *, level=100, tier="LEGENDARY", price=5_990_000):
    return {
        "uuid": uuid,
        "tag": "PET_SILVERFISH",
        "itemName": f"[Lvl {level}] Silverfish",
        "tier": tier,
        "startingBid": price,
        "highestBid": 0,
        "bin": True,
        "flatNbt": {"petInfo": f'{{"type":"SILVERFISH","tier":"{tier}","exp":100000}}'},
    }


def _bare_same_tag(uuid, *, price=5_990_000):
    return {
        "uuid": uuid,
        "tag": "PET_SILVERFISH",
        "itemName": "[Lvl 100] Silverfish",
        "startingBid": price,
        "highestBid": 0,
        "bin": True,
    }


def _store_features(uuid, item):
    db.insert_analysis(
        {
            "auction_uuid": uuid,
            "decision": "UNKNOWN",
            "confidence": 90,
            "comparable_count": 0,
            "item_features_json": json.dumps(extract_item_features(item)),
            "reasons_json": "[]",
            "comparable_prices_json": "[]",
            "trend_json": "{}",
            "rejected_json": "[]",
            "sell_estimate_json": "{}",
        }
    )


def _add_auction(uuid, *, status="ACTIVE", buy_cost=None, item=None, sync_id=1):
    item = item or _item(uuid)
    now = datetime.now(timezone.utc)
    ends_at = _iso(now + timedelta(hours=2)) if status == "ACTIVE" else _iso(now - timedelta(hours=2))
    db.upsert_synced(
        uuid=uuid,
        item_tag=item["tag"],
        item_name=item["itemName"],
        skycofl_url=f"https://example.invalid/auction/{uuid}",
        status=status,
        listing_price=item["startingBid"],
        sold_price=item["startingBid"] if status == "SOLD" else None,
        ends_at=ends_at,
        sync_id=sync_id,
        notification_eligible=1 if status == "ACTIVE" else 0,
        sold_notified=1 if status == "SOLD" else 0,
        sold_at=ends_at if status == "SOLD" else None,
    )
    if buy_cost is not None:
        db.set_buy_cost(uuid, buy_cost)
    _store_features(uuid, item)


def _setup_relist(old_uuid="old", new_uuid="new", old_status="STALE", old_item=None, new_item=None):
    old_item = old_item or _item(old_uuid)
    new_item = new_item or _item(new_uuid)
    _add_auction(old_uuid, status=old_status, buy_cost=30_000_000, item=old_item)
    _add_auction(new_uuid, status="ACTIVE", buy_cost=None, item=new_item, sync_id=2)


def _set_missed(uuid, missed=1):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET missed_syncs = ? WHERE auction_uuid = ?",
            (missed, uuid),
        )


def _no_detail_network(monkeypatch):
    async def _fail(uuid, use_cache=True):
        raise AssertionError(f"unexpected auction detail fetch for {uuid}")

    monkeypatch.setattr(cofl_client, "get_auction_detail", _fail)


def test_carry_suggestion_created_for_relisted_item(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    _setup_relist()

    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert len(suggestions) == 1
    assert suggestions[0]["old_auction_uuid"] == "old"
    assert suggestions[0]["buy_cost"] == 30_000_000
    assert suggestions[0]["confidence"] >= 85


def test_carry_suggestion_for_recently_missing_active_old_auction(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    _setup_relist(old_status="ACTIVE", old_item=_silverfish("old"), new_item=_silverfish("new"))
    _set_missed("old", 1)

    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert len(suggestions) == 1
    assert suggestions[0]["old_auction_uuid"] == "old"
    assert suggestions[0]["old_status"] == "ACTIVE"
    assert suggestions[0]["confidence_level"] == "strong"


def test_carry_detection_runs_after_missed_sync_update(monkeypatch):
    fresh_db()
    old_item = _silverfish("old", price=4_999_999)
    new_item = _silverfish("new", price=5_990_000)
    _add_auction("old", status="ACTIVE", buy_cost=4_999_999, item=old_item, sync_id=1)

    async def _player_items(uuid, max_pages=5):
        return [new_item]

    async def _detail(uuid, use_cache=True):
        return new_item if uuid == "new" else old_item

    monkeypatch.setattr(cofl_client, "get_all_player_auctions", _player_items)
    monkeypatch.setattr(cofl_client, "get_auction_detail", _detail)

    asyncio.run(sync.sync_player_auctions())
    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert db.get_auction("old")["missed_syncs"] == 1
    assert len(suggestions) == 1
    assert suggestions[0]["old_auction_uuid"] == "old"
    assert suggestions[0]["buy_cost"] == 4_999_999


def test_accept_carry_suggestion_copies_user_fields(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    _setup_relist()
    db.set_min_profit("old", 500_000)
    db.set_target_sell_price("old", 36_000_000)
    db.set_notes("old", "necrotic 5 star")

    async def _no_analysis(uuid):
        return None

    monkeypatch.setattr(carry.analysis, "analyse_auction", _no_analysis)
    asyncio.run(carry.get_suggestions("new"))
    result = asyncio.run(carry.carry("new", "old"))

    row = db.get_auction("new")
    link = db.get_accepted_carry_link("new")
    assert result["ok"] is True
    assert row["buy_cost"] == 30_000_000
    assert row["min_profit"] == 500_000
    assert row["target_sell_price"] == 36_000_000
    assert row["notes"] == "necrotic 5 star"
    assert row["carried_from_uuid"] == "old"
    assert link is not None
    assert link["accepted"] == 1


def test_low_score_match_no_suggestion(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    old_item = _item("old", stars=1)
    new_item = _item("new", stars=5)
    _setup_relist(old_item=old_item, new_item=new_item)

    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert suggestions == []


def test_same_tag_low_confidence_manual_suggestion(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    _setup_relist(old_item=_bare_same_tag("old"), new_item=_bare_same_tag("new"))

    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert len(suggestions) == 1
    assert suggestions[0]["old_auction_uuid"] == "old"
    assert suggestions[0]["confidence"] < settings.relist_carry_min_score
    assert suggestions[0]["confidence_level"] == "manual"
    assert suggestions[0]["manual"] is True
    assert "Check this is the same item" in suggestions[0]["reason"]


def test_multiple_candidates_no_silent_apply(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    _add_auction("old1", status="STALE", buy_cost=30_000_000, item=_item("old1"))
    _add_auction("old2", status="EXPIRED", buy_cost=28_000_000, item=_item("old2"))
    _add_auction("new", status="ACTIVE", buy_cost=None, item=_item("new"), sync_id=2)

    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert len(suggestions) == 2
    assert db.get_auction("new")["buy_cost"] is None
    assert {s["old_auction_uuid"] for s in suggestions} == {"old1", "old2"}


def test_multiple_active_same_tag_no_auto_apply(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    settings.relist_carry_auto_apply = True
    _add_auction("old1", status="ACTIVE", buy_cost=30_000_000, item=_silverfish("old1"))
    _add_auction("old2", status="ACTIVE", buy_cost=28_000_000, item=_silverfish("old2"))
    _add_auction("new", status="ACTIVE", buy_cost=None, item=_silverfish("new"), sync_id=2)
    _set_missed("old1", 1)
    _set_missed("old2", 1)

    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert len(suggestions) == 2
    assert db.get_auction("new")["buy_cost"] is None
    assert {s["old_auction_uuid"] for s in suggestions} == {"old1", "old2"}


def test_manual_carry_accept_still_copies_fields(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    _setup_relist(old_item=_bare_same_tag("old"), new_item=_bare_same_tag("new"))
    db.set_min_profit("old", 500_000)
    db.set_target_sell_price("old", 6_400_000)
    db.set_notes("old", "lvl 100 silverfish")

    async def _no_analysis(uuid):
        return None

    monkeypatch.setattr(carry.analysis, "analyse_auction", _no_analysis)
    suggestions = asyncio.run(carry.get_suggestions("new"))
    result = asyncio.run(carry.carry("new", suggestions[0]["old_auction_uuid"]))

    row = db.get_auction("new")
    assert result["ok"] is True
    assert row["buy_cost"] == 30_000_000
    assert row["min_profit"] == 500_000
    assert row["target_sell_price"] == 6_400_000
    assert row["notes"] == "lvl 100 silverfish"
    assert row["carried_from_uuid"] == "old"


def test_existing_buy_cost_no_suggestion(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    _setup_relist()
    db.set_buy_cost("new", 31_000_000)

    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert suggestions == []


def test_ignored_suggestion_does_not_return(monkeypatch):
    fresh_db()
    _no_detail_network(monkeypatch)
    _setup_relist()
    assert asyncio.run(carry.get_suggestions("new"))

    carry.ignore("new")
    suggestions = asyncio.run(carry.get_suggestions("new"))

    assert suggestions == []
    assert db.get_auction("new")["carry_suggestion_ignored"] == 1
