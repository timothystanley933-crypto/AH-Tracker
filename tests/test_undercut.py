"""Offline tests for undercut alerts and INCOMPARABLE market context."""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analysis, cofl_client, db, notifications, undercut, views  # noqa: E402
from app.config import settings  # noqa: E402


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    settings.database_path = path
    settings.undercut_alerts = True
    settings.undercut_check_enabled = True
    settings.undercut_min_gap_coins = 250_000
    settings.undercut_min_gap_percent = 3
    settings.undercut_min_comparable_score = 75
    settings.undercut_better_item_score = 85
    settings.undercut_cooldown_minutes = 60
    settings.undercut_max_candidates_to_check = 60
    settings.undercut_include_possible = False
    settings.undercut_notify_decisions = ["ACTIVE", "HOLD", "RELIST", "PROFIT_LOW", "INCOMPARABLE"]
    settings.notifications_enabled = True
    settings.relist_min_comparable_matches = 2
    settings.relist_min_comparable_score = 75
    settings.relist_pet_level_tolerance = 5
    settings.relist_star_tolerance = 0
    db.init_db()
    return path


def _future():
    return (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()


def _gear(uuid, price, *, tier="RARE", stars=0, recomb=False, tag="ASPECT_OF_THE_END"):
    flat = {"upgrade_level": str(stars)}
    if recomb:
        flat["rarity_upgrades"] = "1"
    return {
        "uuid": uuid,
        "tag": tag,
        "itemName": "Aspect of the End",
        "tier": tier,
        "startingBid": price,
        "highestBid": 0,
        "bin": True,
        "flatNbt": flat,
    }


def _pet(uuid, price, *, level=100, tier="LEGENDARY"):
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


def _skin(uuid, price):
    return {
        "uuid": uuid,
        "tag": "PET_SKIN_GRIFFIN_REINDRAKE",
        "itemName": "Reindrake Griffon Skin",
        "startingBid": price,
        "highestBid": 0,
        "bin": True,
    }


def _add_active(item, *, buy_cost=1_000_000):
    db.upsert_synced(
        uuid=item["uuid"],
        item_tag=item["tag"],
        item_name=item["itemName"],
        skycofl_url=f"https://example.invalid/auction/{item['uuid']}",
        status="ACTIVE",
        listing_price=item["startingBid"],
        sold_price=None,
        ends_at=_future(),
        sync_id=1,
        notification_eligible=1,
        sold_notified=0,
    )
    db.set_buy_cost(item["uuid"], buy_cost)


def _patch_market(monkeypatch, mine, candidates, day=None, week=None):
    async def _detail(uuid, use_cache=True):
        if uuid == mine["uuid"]:
            return mine
        for c in candidates:
            if c["uuid"] == uuid:
                return c
        return None

    async def _bins(item_tag, pages):
        return candidates

    async def _history(item_tag, span="day"):
        if span == "day":
            return day or []
        return week or []

    monkeypatch.setattr(cofl_client, "get_auction_detail", _detail)
    monkeypatch.setattr(cofl_client, "get_active_bins_pages", _bins)
    monkeypatch.setattr(cofl_client, "get_price_history", _history)


def test_undercut_alert_same_quality_cheaper(monkeypatch):
    fresh_db()
    mine = _gear("mine", 10_000_000)
    cheaper = _gear("cheap", 9_500_000)
    _add_active(mine)
    _patch_market(monkeypatch, mine, [mine, cheaper])

    result = asyncio.run(undercut.check_auction("mine"))

    assert result["undercut"] is True
    assert result["candidate_price"] == 9_500_000
    assert result["gap_coins"] == 500_000
    assert db.latest_undercut_for_auction("mine") is not None


def test_no_undercut_for_worse_cheaper_item(monkeypatch):
    fresh_db()
    mine = _pet("mine", 10_000_000, level=100, tier="LEGENDARY")
    worse = _pet("cheap", 5_000_000, level=1, tier="COMMON")
    _add_active(mine)
    _patch_market(monkeypatch, mine, [mine, worse])

    result = asyncio.run(undercut.check_auction("mine"))

    assert result["undercut"] is False
    assert db.latest_undercut_for_auction("mine") is None


def test_better_item_cheaper_triggers_undercut(monkeypatch):
    fresh_db()
    mine = _gear("mine", 10_000_000, tier="RARE", recomb=False)
    better = _gear("cheap", 9_000_000, tier="EPIC", recomb=True)
    _add_active(mine)
    _patch_market(monkeypatch, mine, [mine, better])

    result = asyncio.run(undercut.check_auction("mine"))

    assert result["undercut"] is True
    assert result["confidence"] >= settings.undercut_better_item_score
    assert "better" in result["reason"].lower()


def test_no_undercut_for_tiny_gap(monkeypatch):
    fresh_db()
    mine = _gear("mine", 10_000_000)
    tiny = _gear("cheap", 9_900_000)
    _add_active(mine)
    _patch_market(monkeypatch, mine, [mine, tiny])

    result = asyncio.run(undercut.check_auction("mine"))

    assert result["undercut"] is False


def test_undercut_cooldown_prevents_duplicate_notification(monkeypatch):
    fresh_db()
    mine = _gear("mine", 10_000_000)
    cheaper = _gear("cheap", 9_500_000)
    _add_active(mine)
    _patch_market(monkeypatch, mine, [mine, cheaper])

    sent = {"count": 0}

    async def _send(title, body, url=None):
        sent["count"] += 1
        return True

    monkeypatch.setattr(notifications, "send_raw", _send)

    first = asyncio.run(undercut.check_auction("mine", notify=True))
    second = asyncio.run(undercut.check_auction("mine", notify=True))

    assert first["notified"] is True
    assert second["cooldown"] is True
    assert sent["count"] == 1


def test_skin_same_tag_market_context(monkeypatch):
    fresh_db()
    settings.relist_min_comparable_matches = 3
    mine = _skin("mine", 229_999_999)
    c1 = _skin("skin1", 220_000_000)
    c2 = _skin("skin2", 240_000_000)
    _add_active(mine, buy_cost=200_000_000)
    _patch_market(monkeypatch, mine, [mine, c1, c2])

    asyncio.run(analysis.analyse_auction("mine"))
    latest = db.latest_analysis("mine")
    context = json.loads(latest["market_context_json"])

    assert latest["decision"] == "INCOMPARABLE"
    assert context["raw_same_tag_lbin"] == 220_000_000
    assert context["raw_same_tag_top"][0]["uuid"] == "skin1"
    assert context["raw_same_tag_label"] == "Raw same-tag, not safe comparable"


def test_incomparable_market_context_includes_rejected_reasons(monkeypatch):
    fresh_db()
    mine = _pet("mine", 10_000_000, level=100, tier="LEGENDARY")
    bad1 = _pet("bad1", 4_000_000, level=1, tier="COMMON")
    bad2 = _pet("bad2", 4_500_000, level=20, tier="EPIC")
    _add_active(mine, buy_cost=8_000_000)
    _patch_market(monkeypatch, mine, [mine, bad1, bad2])

    asyncio.run(analysis.analyse_auction("mine"))
    card = views.build_card(db.get_auction("mine"), db.latest_analysis("mine"))
    counts = card["market_context"]["rejected_reason_counts"]

    assert card["decision"] == "INCOMPARABLE"
    assert card["market_context"]["raw_same_tag_lbin"] == 4_000_000
    assert counts
    assert "rarity mismatch" in counts or "pet level mismatch" in counts
    assert card["market_context"]["rejected_examples"]
