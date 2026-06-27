"""SQLite storage layer.

A new connection is opened per operation (SQLite handles this well in WAL mode)
which keeps things simple across the request handlers and the background loop.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from .config import settings


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    _ensure_parent_dir(settings.database_path)
    conn = sqlite3.connect(settings.database_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables on first run."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracked_auctions (
                auction_uuid     TEXT PRIMARY KEY,
                item_tag         TEXT,
                item_name        TEXT,
                skycofl_url      TEXT,
                listing_price    INTEGER,
                buy_cost         INTEGER,
                min_profit       INTEGER DEFAULT 250000,
                target_sell_price INTEGER,
                notes            TEXT,
                ignored          INTEGER DEFAULT 0,
                active           INTEGER DEFAULT 1,
                sold             INTEGER DEFAULT 0,
                sold_price       INTEGER,
                ends_at          TEXT,
                status           TEXT DEFAULT 'ACTIVE',
                last_sync_seen   INTEGER,
                missed_syncs     INTEGER DEFAULT 0,
                sold_notified    INTEGER DEFAULT 0,
                notification_eligible INTEGER DEFAULT 0,
                sold_at          TEXT,
                carried_from_uuid TEXT,
                carry_suggestion_ignored INTEGER DEFAULT 0,
                first_seen       TEXT,
                last_seen        TEXT,
                updated_at       TEXT
            );

            CREATE TABLE IF NOT EXISTS auction_analysis (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_uuid           TEXT,
                decision               TEXT,
                suggested_price        INTEGER,
                expected_profit        INTEGER,
                confidence             INTEGER,
                comparable_count       INTEGER,
                comparable_prices_json TEXT,
                reasons_json           TEXT,
                item_features_json     TEXT,
                trend_json             TEXT,
                rejected_json          TEXT,
                volume_per_day         REAL,
                sell_estimate_json     TEXT,
                market_context_json    TEXT,
                created_at             TEXT
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_uuid      TEXT,
                notification_type TEXT,
                decision          TEXT,
                message_hash      TEXT,
                sent_at           TEXT
            );

            CREATE TABLE IF NOT EXISTS relist_links (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                old_auction_uuid  TEXT,
                new_auction_uuid  TEXT,
                confidence        INTEGER,
                reason            TEXT,
                accepted          INTEGER DEFAULT 0,
                ignored           INTEGER DEFAULT 0,
                created_at        TEXT,
                accepted_at       TEXT
            );

            CREATE TABLE IF NOT EXISTS auction_fee_events (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_uuid            TEXT NOT NULL,
                item_identity_key       TEXT,
                event_type              TEXT NOT NULL,
                listed_price            INTEGER,
                fee_rate                REAL,
                fee_amount              INTEGER,
                source_old_auction_uuid TEXT,
                source_new_auction_uuid TEXT,
                notes                   TEXT,
                created_at              DATETIME
            );

            CREATE TABLE IF NOT EXISTS undercut_alerts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                auction_uuid        TEXT NOT NULL,
                candidate_uuid      TEXT,
                item_tag            TEXT,
                my_price            INTEGER,
                candidate_price     INTEGER,
                gap_coins           INTEGER,
                gap_percent         REAL,
                confidence          INTEGER,
                candidate_item_name TEXT,
                reason              TEXT,
                created_at          TEXT,
                notified            INTEGER DEFAULT 0,
                notification_hash   TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_analysis_uuid ON auction_analysis(auction_uuid);
            CREATE INDEX IF NOT EXISTS idx_notif_uuid ON notifications(auction_uuid);
            CREATE INDEX IF NOT EXISTS idx_relink_new ON relist_links(new_auction_uuid);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_relink_pair ON relist_links(old_auction_uuid, new_auction_uuid);
            CREATE INDEX IF NOT EXISTS idx_undercut_uuid ON undercut_alerts(auction_uuid);
            CREATE INDEX IF NOT EXISTS idx_undercut_hash ON undercut_alerts(notification_hash);
            CREATE INDEX IF NOT EXISTS idx_fee_uuid ON auction_fee_events(auction_uuid);
            CREATE INDEX IF NOT EXISTS idx_fee_identity ON auction_fee_events(item_identity_key);
            """
        )

    _migrate()


def _migrate() -> None:
    """Add columns introduced after the first release (best effort)."""
    with get_conn() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracked_auctions)")}
        if "sold_price" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN sold_price INTEGER")
        if "ends_at" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN ends_at TEXT")
        if "status" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN status TEXT DEFAULT 'ACTIVE'")
            # Back-fill status from the legacy active/sold flags.
            conn.execute("UPDATE tracked_auctions SET status='SOLD' WHERE sold=1")
            conn.execute("UPDATE tracked_auctions SET status='ACTIVE' WHERE sold=0 AND active=1")
            conn.execute("UPDATE tracked_auctions SET status='EXPIRED' WHERE sold=0 AND active=0")
        if "last_sync_seen" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN last_sync_seen INTEGER")
        if "missed_syncs" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN missed_syncs INTEGER DEFAULT 0")
        if "sold_notified" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN sold_notified INTEGER DEFAULT 0")
            # Existing sold rows must never re-notify after an upgrade.
            conn.execute("UPDATE tracked_auctions SET sold_notified=1 WHERE sold=1")
        if "notification_eligible" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN notification_eligible INTEGER DEFAULT 0")
        if "sold_at" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN sold_at TEXT")
            # Best-effort back-fill for already-sold rows so the Sold tab can sort.
            conn.execute(
                "UPDATE tracked_auctions SET sold_at = COALESCE(ends_at, updated_at) WHERE sold = 1 AND sold_at IS NULL"
            )
        if "carried_from_uuid" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN carried_from_uuid TEXT")
        if "carry_suggestion_ignored" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN carry_suggestion_ignored INTEGER DEFAULT 0")
        # Fee / relist ledger columns (multi-relist profit tracking).
        if "relist_count" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN relist_count INTEGER DEFAULT 0")
        if "accumulated_listing_fees" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN accumulated_listing_fees INTEGER DEFAULT 0")
        if "manual_extra_costs" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN manual_extra_costs INTEGER DEFAULT 0")
        if "first_seen_listing_price" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN first_seen_listing_price INTEGER")
        if "last_relist_price" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN last_relist_price INTEGER")
        if "item_identity_key" not in cols:
            conn.execute("ALTER TABLE tracked_auctions ADD COLUMN item_identity_key TEXT")
        # relist_links is created by init_db's CREATE TABLE IF NOT EXISTS.
        acols = {row["name"] for row in conn.execute("PRAGMA table_info(auction_analysis)")}
        if "rejected_json" not in acols:
            conn.execute("ALTER TABLE auction_analysis ADD COLUMN rejected_json TEXT")
        if "sell_estimate_json" not in acols:
            conn.execute("ALTER TABLE auction_analysis ADD COLUMN sell_estimate_json TEXT")
        if "market_context_json" not in acols:
            conn.execute("ALTER TABLE auction_analysis ADD COLUMN market_context_json TEXT")
        if "decision_support_json" not in acols:
            conn.execute("ALTER TABLE auction_analysis ADD COLUMN decision_support_json TEXT")


# --------------------------------------------------------------------------
# tracked_auctions
# --------------------------------------------------------------------------

# User-owned fields are NEVER written by sync. They are only set through the
# explicit user routes/setters (set_buy_cost, set_min_profit, set_target_sell_price,
# set_notes, set_ignored). All market sync writes go through upsert_synced() below,
# which is the single upsert path and updates market fields only. This is what keeps
# saved buy costs from being wiped on sync/restart/redeploy.
USER_OWNED_FIELDS = ("buy_cost", "min_profit", "target_sell_price", "notes", "ignored")


def count_tracked() -> int:
    """Total tracked auctions - used to detect a fresh/empty DB (first sync)."""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM tracked_auctions").fetchone()
        return int(row["n"]) if row else 0


def upsert_synced(
    *,
    uuid: str,
    item_tag: Optional[str],
    item_name: Optional[str],
    skycofl_url: Optional[str],
    status: str,
    listing_price: Optional[int],
    sold_price: Optional[int],
    ends_at: Optional[str],
    sync_id: int,
    notification_eligible: int,
    sold_notified: int,
    sold_at: Optional[str] = None,
) -> None:
    """Insert/update an auction observed during a sync.

    Updates MARKET fields only. The user-owned fields (buy_cost, min_profit,
    target_sell_price, notes, ignored) are deliberately absent from the UPDATE
    below, so a sync can never overwrite a saved buy cost with null/blank or
    reset a user-changed min_profit. New rows insert buy_cost as NULL.
    notification_eligible / sold_notified are sticky: once 1 they never drop to 0.
    sold_at is set once (COALESCE) when an auction first becomes SOLD.
    """
    now = utcnow()
    active = 1 if status == "ACTIVE" else 0
    sold = 1 if status == "SOLD" else 0
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT auction_uuid FROM tracked_auctions WHERE auction_uuid = ?", (uuid,)
        ).fetchone()
        if existing:
            # MARKET FIELDS ONLY. Do NOT add buy_cost / min_profit / target_sell_price
            # / notes / ignored here - they are user-owned and must survive sync.
            conn.execute(
                """
                UPDATE tracked_auctions SET
                    item_tag = COALESCE(?, item_tag),
                    item_name = COALESCE(?, item_name),
                    skycofl_url = COALESCE(?, skycofl_url),
                    listing_price = COALESCE(?, listing_price),
                    sold_price = COALESCE(?, sold_price),
                    ends_at = COALESCE(?, ends_at),
                    sold_at = COALESCE(sold_at, ?),
                    status = ?, active = ?, sold = ?,
                    last_sync_seen = ?, missed_syncs = 0,
                    notification_eligible = CASE WHEN ? = 1 THEN 1 ELSE notification_eligible END,
                    sold_notified = CASE WHEN ? = 1 THEN 1 ELSE sold_notified END,
                    last_seen = ?, updated_at = ?
                 WHERE auction_uuid = ?
                """,
                (
                    item_tag, item_name, skycofl_url, listing_price, sold_price, ends_at,
                    sold_at, status, active, sold, sync_id,
                    notification_eligible, sold_notified, now, now, uuid,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO tracked_auctions
                    (auction_uuid, item_tag, item_name, skycofl_url, listing_price,
                     buy_cost, min_profit, ignored, active, sold, sold_price, ends_at, sold_at,
                     status, last_sync_seen, missed_syncs, notification_eligible, sold_notified,
                     first_seen, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, 0, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    uuid, item_tag, item_name, skycofl_url, listing_price,
                    settings.relist_min_profit_after_tax, active, sold, sold_price, ends_at, sold_at,
                    status, sync_id, notification_eligible, sold_notified, now, now, now,
                ),
            )


def stale_pass(seen_uuids, threshold: int = 2) -> int:
    """After a successful sync, age out ACTIVE auctions that were not seen.

    Increments missed_syncs; once it reaches `threshold` the auction is STALE.
    Returns the number newly marked STALE.
    """
    now = utcnow()
    marked = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT auction_uuid, missed_syncs FROM tracked_auctions WHERE status = 'ACTIVE'"
        ).fetchall()
        for r in rows:
            if r["auction_uuid"] in seen_uuids:
                continue
            missed = (r["missed_syncs"] or 0) + 1
            if missed >= threshold:
                conn.execute(
                    "UPDATE tracked_auctions SET missed_syncs = ?, status = 'STALE', active = 0, updated_at = ? WHERE auction_uuid = ?",
                    (missed, now, r["auction_uuid"]),
                )
                marked += 1
            else:
                conn.execute(
                    "UPDATE tracked_auctions SET missed_syncs = ?, updated_at = ? WHERE auction_uuid = ?",
                    (missed, now, r["auction_uuid"]),
                )
    return marked


def get_auction(uuid: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tracked_auctions WHERE auction_uuid = ?", (uuid,)
        ).fetchone()


def list_auctions(include_inactive: bool = True) -> List[sqlite3.Row]:
    with get_conn() as conn:
        if include_inactive:
            rows = conn.execute("SELECT * FROM tracked_auctions ORDER BY updated_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tracked_auctions WHERE active = 1 ORDER BY updated_at DESC"
            ).fetchall()
        return list(rows)


def set_buy_cost(uuid: str, buy_cost: Optional[int]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET buy_cost = ?, updated_at = ? WHERE auction_uuid = ?",
            (buy_cost, utcnow(), uuid),
        )


def set_min_profit(uuid: str, min_profit: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET min_profit = ?, updated_at = ? WHERE auction_uuid = ?",
            (min_profit, utcnow(), uuid),
        )


def set_target_sell_price(uuid: str, target_sell_price: Optional[int]) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET target_sell_price = ?, updated_at = ? WHERE auction_uuid = ?",
            (target_sell_price, utcnow(), uuid),
        )


def set_notes(uuid: str, notes: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET notes = ?, updated_at = ? WHERE auction_uuid = ?",
            (notes, utcnow(), uuid),
        )


def set_ignored(uuid: str, ignored: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET ignored = ?, updated_at = ? WHERE auction_uuid = ?",
            (1 if ignored else 0, utcnow(), uuid),
        )


def mark_sold(uuid: str, sold_price: Optional[int] = None) -> None:
    """Manually mark an auction sold (also flags it handled so it never re-notifies)."""
    now = utcnow()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE tracked_auctions
               SET sold = 1, active = 0, status = 'SOLD', sold_notified = 1,
                   sold_price = COALESCE(?, sold_price),
                   sold_at = COALESCE(sold_at, ends_at, ?), updated_at = ?
             WHERE auction_uuid = ?
            """,
            (sold_price, now, now, uuid),
        )


# --------------------------------------------------------------------------
# auction_fee_events  (multi-relist fee / profit ledger)
# --------------------------------------------------------------------------

# Event types that count as listing fees and roll up into accumulated_listing_fees.
# Manual *extra costs* are NOT listing fees - they live in their own column.
LISTING_FEE_EVENT_TYPES = ("INITIAL_LIST", "RELIST", "MANUAL_FEE", "CARRY_FROM_PREVIOUS")


def _listing_fee(price: Optional[int]) -> int:
    """Up-front listing fee for a price. Single source of truth lives in profit.py."""
    from .profit import listing_fee  # local import avoids a circular import

    return listing_fee(price)


def _recompute_accumulated(conn: sqlite3.Connection, uuid: str) -> int:
    placeholders = ",".join("?" for _ in LISTING_FEE_EVENT_TYPES)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(fee_amount), 0) AS total
              FROM auction_fee_events
             WHERE auction_uuid = ? AND event_type IN ({placeholders})""",
        (uuid, *LISTING_FEE_EVENT_TYPES),
    ).fetchone()
    total = int(row["total"] or 0)
    conn.execute(
        "UPDATE tracked_auctions SET accumulated_listing_fees = ?, updated_at = ? WHERE auction_uuid = ?",
        (total, utcnow(), uuid),
    )
    return total


def add_fee_event(
    auction_uuid: str,
    event_type: str,
    *,
    listed_price: Optional[int] = None,
    fee_rate: Optional[float] = None,
    fee_amount: Optional[int] = None,
    source_old: Optional[str] = None,
    source_new: Optional[str] = None,
    item_identity_key: Optional[str] = None,
    notes: Optional[str] = None,
    dedup: bool = True,
) -> bool:
    """Insert a fee event (deduped) and recompute the auction's accumulated fees.

    With ``dedup`` (the default) an event matching the same
    auction_uuid/event_type/listed_price/source link is NOT inserted again, so
    repeated syncs or a re-accepted carry never double-count a fee. Returns True
    when a new row was actually inserted.
    """
    created = False
    with get_conn() as conn:
        existing = None
        if dedup:
            existing = conn.execute(
                """
                SELECT id FROM auction_fee_events
                 WHERE auction_uuid = ? AND event_type = ?
                   AND IFNULL(listed_price, -1) = IFNULL(?, -1)
                   AND IFNULL(source_old_auction_uuid, '') = IFNULL(?, '')
                   AND IFNULL(source_new_auction_uuid, '') = IFNULL(?, '')
                 LIMIT 1
                """,
                (auction_uuid, event_type, listed_price, source_old, source_new),
            ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO auction_fee_events
                    (auction_uuid, item_identity_key, event_type, listed_price,
                     fee_rate, fee_amount, source_old_auction_uuid, source_new_auction_uuid,
                     notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    auction_uuid, item_identity_key, event_type, listed_price,
                    fee_rate, fee_amount, source_old, source_new, notes, utcnow(),
                ),
            )
            created = True
        _recompute_accumulated(conn, auction_uuid)
    return created


def total_listing_fees_for_auction(uuid: str) -> int:
    """Authoritative sum of listing-fee events for an auction (from the ledger)."""
    placeholders = ",".join("?" for _ in LISTING_FEE_EVENT_TYPES)
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(fee_amount), 0) AS total
                  FROM auction_fee_events
                 WHERE auction_uuid = ? AND event_type IN ({placeholders})""",
            (uuid, *LISTING_FEE_EVENT_TYPES),
        ).fetchone()
        return int(row["total"] or 0)


def fee_events_for(uuid: str, limit: int = 100) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return list(
            conn.execute(
                "SELECT * FROM auction_fee_events WHERE auction_uuid = ? ORDER BY id ASC LIMIT ?",
                (uuid, limit),
            ).fetchall()
        )


def _set_listing_meta(uuid: str, listing_price: Optional[int], identity: Optional[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE tracked_auctions
               SET first_seen_listing_price = COALESCE(first_seen_listing_price, ?),
                   item_identity_key = COALESCE(item_identity_key, ?),
                   updated_at = ?
             WHERE auction_uuid = ?
            """,
            (listing_price, identity, utcnow(), uuid),
        )


def record_initial_list_fee(
    uuid: str, listing_price: Optional[int], item_identity_key: Optional[str] = None
) -> bool:
    """Record the one-time INITIAL_LIST fee when an auction is first tracked active.

    Idempotent: at most one INITIAL_LIST event per auction, so repeated syncs
    never duplicate it. Also stores first_seen_listing_price / item_identity_key.
    """
    if not listing_price or listing_price <= 0:
        _set_listing_meta(uuid, listing_price, item_identity_key)
        return False
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM auction_fee_events WHERE auction_uuid = ? AND event_type = 'INITIAL_LIST' LIMIT 1",
            (uuid,),
        ).fetchone()
    if exists:
        _set_listing_meta(uuid, listing_price, item_identity_key)
        return False
    add_fee_event(
        uuid, "INITIAL_LIST",
        listed_price=listing_price, fee_rate=settings.ah_listing_fee_rate,
        fee_amount=_listing_fee(listing_price), source_new=uuid,
        item_identity_key=item_identity_key,
    )
    _set_listing_meta(uuid, listing_price, item_identity_key)
    return True


def record_relist_fee(
    new_uuid: str,
    old_uuid: str,
    new_listing_price: Optional[int],
    item_identity_key: Optional[str] = None,
) -> bool:
    """Record a RELIST when buy cost is carried from old_uuid to new_uuid.

    The new listing IS the relist, so any INITIAL_LIST already recorded for the
    new auction is replaced by a RELIST event (its fee is counted exactly once).
    The previous listing's accumulated fees are carried across via a single
    CARRY_FROM_PREVIOUS event. All inserts are deduped, so accepting the same
    carry twice (or a page reload) never double-counts. Increments relist_count
    only when a new RELIST event was actually created. Returns that flag.
    """
    old_accumulated = total_listing_fees_for_auction(old_uuid)
    new_fee = _listing_fee(new_listing_price) if new_listing_price else 0
    old_row = get_auction(old_uuid)
    old_relist_count = (
        int(old_row["relist_count"] or 0)
        if old_row is not None and "relist_count" in old_row.keys() else 0
    )

    with get_conn() as conn:
        conn.execute(
            "DELETE FROM auction_fee_events WHERE auction_uuid = ? AND event_type = 'INITIAL_LIST'",
            (new_uuid,),
        )

    relist_created = add_fee_event(
        new_uuid, "RELIST",
        listed_price=new_listing_price, fee_rate=settings.ah_listing_fee_rate,
        fee_amount=new_fee, source_old=old_uuid, source_new=new_uuid,
        item_identity_key=item_identity_key,
    )
    add_fee_event(
        new_uuid, "CARRY_FROM_PREVIOUS",
        listed_price=None, fee_rate=None, fee_amount=old_accumulated,
        source_old=old_uuid, source_new=new_uuid, item_identity_key=item_identity_key,
        notes=f"Carried {old_accumulated} in prior listing fees from {old_uuid}",
    )

    if relist_created:
        # Relist count is cumulative across the whole relist chain.
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE tracked_auctions
                   SET relist_count = ?,
                       last_relist_price = ?,
                       carried_from_uuid = COALESCE(carried_from_uuid, ?),
                       item_identity_key = COALESCE(item_identity_key, ?),
                       updated_at = ?
                 WHERE auction_uuid = ?
                """,
                (old_relist_count + 1, new_listing_price, old_uuid, item_identity_key, utcnow(), new_uuid),
            )
    return relist_created


def add_manual_listing_fee(uuid: str, amount: int, notes: Optional[str] = None) -> bool:
    """User-entered listing fee. Always recorded (never deduped against itself)."""
    if amount is None or int(amount) <= 0:
        return False
    return add_fee_event(
        uuid, "MANUAL_FEE", fee_amount=int(amount), source_new=uuid,
        notes=notes or "Manual listing fee", dedup=False,
    )


def add_manual_extra_cost(uuid: str, amount: int) -> bool:
    """User-entered extra cost (not a listing fee). Tracked in its own column."""
    if amount is None:
        return False
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET manual_extra_costs = COALESCE(manual_extra_costs, 0) + ?, updated_at = ? WHERE auction_uuid = ?",
            (int(amount), utcnow(), uuid),
        )
    return True


def reset_fee_ledger(uuid: str) -> None:
    """Wipe the fee ledger for an auction and zero the derived columns."""
    with get_conn() as conn:
        conn.execute("DELETE FROM auction_fee_events WHERE auction_uuid = ?", (uuid,))
        conn.execute(
            """
            UPDATE tracked_auctions
               SET accumulated_listing_fees = 0, relist_count = 0,
                   manual_extra_costs = 0, last_relist_price = NULL, updated_at = ?
             WHERE auction_uuid = ?
            """,
            (utcnow(), uuid),
        )


def set_item_identity_key(uuid: str, identity: Optional[str]) -> None:
    """Set the cross-relist identity key (latest analysis wins / refines it)."""
    if not identity:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET item_identity_key = ?, updated_at = ? WHERE auction_uuid = ?",
            (identity, utcnow(), uuid),
        )


# --------------------------------------------------------------------------
# auction_analysis
# --------------------------------------------------------------------------

def insert_analysis(row: Dict[str, Any]) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO auction_analysis
                (auction_uuid, decision, suggested_price, expected_profit, confidence,
                 comparable_count, comparable_prices_json, reasons_json, item_features_json,
                 trend_json, rejected_json, volume_per_day, sell_estimate_json,
                 market_context_json, decision_support_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("auction_uuid"),
                row.get("decision"),
                row.get("suggested_price"),
                row.get("expected_profit"),
                row.get("confidence"),
                row.get("comparable_count"),
                row.get("comparable_prices_json"),
                row.get("reasons_json"),
                row.get("item_features_json"),
                row.get("trend_json"),
                row.get("rejected_json"),
                row.get("volume_per_day"),
                row.get("sell_estimate_json"),
                row.get("market_context_json"),
                row.get("decision_support_json"),
                utcnow(),
            ),
        )
        return int(cur.lastrowid)


def latest_analysis(uuid: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM auction_analysis WHERE auction_uuid = ? ORDER BY id DESC LIMIT 1",
            (uuid,),
        ).fetchone()


def latest_analyses_map() -> Dict[str, sqlite3.Row]:
    """Return the most recent analysis for each auction in one pass."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.* FROM auction_analysis a
            JOIN (
                SELECT auction_uuid, MAX(id) AS max_id
                FROM auction_analysis GROUP BY auction_uuid
            ) latest ON a.id = latest.max_id
            """
        ).fetchall()
    return {row["auction_uuid"]: row for row in rows}


def analysis_history(uuid: str, limit: int = 10) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return list(
            conn.execute(
                "SELECT * FROM auction_analysis WHERE auction_uuid = ? ORDER BY id DESC LIMIT ?",
                (uuid, limit),
            ).fetchall()
        )


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

def record_notification(uuid: str, ntype: str, decision: Optional[str], message_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notifications (auction_uuid, notification_type, decision, message_hash, sent_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (uuid, ntype, decision, message_hash, utcnow()),
        )


def recent_notification_exists(uuid: str, decision: Optional[str], since_iso: str) -> bool:
    with get_conn() as conn:
        if decision is None:
            row = conn.execute(
                "SELECT 1 FROM notifications WHERE auction_uuid = ? AND sent_at >= ? LIMIT 1",
                (uuid, since_iso),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM notifications WHERE auction_uuid = ? AND decision = ? AND sent_at >= ? LIMIT 1",
                (uuid, decision, since_iso),
            ).fetchone()
        return row is not None


def message_hash_exists(message_hash: str, since_iso: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM notifications WHERE message_hash = ? AND sent_at >= ? LIMIT 1",
            (message_hash, since_iso),
        ).fetchone()
        return row is not None


def notification_history(uuid: str, limit: int = 20) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return list(
            conn.execute(
                "SELECT * FROM notifications WHERE auction_uuid = ? ORDER BY id DESC LIMIT ?",
                (uuid, limit),
            ).fetchall()
        )


# --------------------------------------------------------------------------
# relist links / carry-buy-cost
# --------------------------------------------------------------------------

def get_carry_source_candidates(
    item_tag: str,
    exclude_uuid: str,
    since_iso: str,
    new_first_seen: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Recent same-tag auctions with a saved buy cost that may be the previous listing."""
    with get_conn() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM tracked_auctions
                 WHERE item_tag = ? AND auction_uuid != ?
                   AND buy_cost IS NOT NULL
                   AND COALESCE(ignored, 0) = 0
                   AND COALESCE(sold_at, ends_at, last_seen, updated_at) >= ?
                   AND (
                        status IN ('SOLD', 'EXPIRED', 'STALE')
                        OR (status = 'ACTIVE' AND COALESCE(missed_syncs, 0) >= 1)
                        OR (status = 'ACTIVE' AND ? IS NOT NULL AND last_seen IS NOT NULL AND last_seen < ?)
                   )
                 ORDER BY COALESCE(sold_at, ends_at, updated_at, last_seen) DESC
                """,
                (item_tag, exclude_uuid, since_iso, new_first_seen, new_first_seen),
            ).fetchall()
        )


def get_carry_candidates(new_auction_uuid: str, lookback_days: int) -> List[sqlite3.Row]:
    """Recent carry-source candidates for a new auction, before feature scoring."""
    row = get_auction(new_auction_uuid)
    if row is None or not row["item_tag"]:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    return get_carry_source_candidates(
        row["item_tag"], new_auction_uuid, since, row["first_seen"]
    )


def upsert_relist_suggestion(old_uuid: str, new_uuid: str, confidence: int, reason: str) -> bool:
    """Create/update a pending carry suggestion.

    Ignored or accepted links stay retired so dismissed suggestions do not reappear.
    Returns True when a new pending link was inserted.
    """
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT accepted, ignored
              FROM relist_links
             WHERE old_auction_uuid = ? AND new_auction_uuid = ?
            """,
            (old_uuid, new_uuid),
        ).fetchone()
        if existing is not None:
            if existing["accepted"] or existing["ignored"]:
                return False
            conn.execute(
                """
                UPDATE relist_links
                   SET confidence = ?, reason = ?
                 WHERE old_auction_uuid = ? AND new_auction_uuid = ?
                """,
                (int(confidence), reason, old_uuid, new_uuid),
            )
            return False

        conn.execute(
            """
            INSERT INTO relist_links
                (old_auction_uuid, new_auction_uuid, confidence, reason, accepted, ignored, created_at)
            VALUES (?, ?, ?, ?, 0, 0, ?)
            """,
            (old_uuid, new_uuid, int(confidence), reason, utcnow()),
        )
        return True


def insert_relist_link(old_uuid: str, new_uuid: str, confidence: int, reason: str) -> bool:
    """Backward-compatible wrapper for creating/updating a pending suggestion."""
    return upsert_relist_suggestion(old_uuid, new_uuid, confidence, reason)


_PENDING_LINK_SELECT = """
    SELECT rl.id, rl.old_auction_uuid, rl.new_auction_uuid, rl.confidence, rl.reason,
           rl.created_at,
           t.item_name AS old_item_name, t.buy_cost AS old_buy_cost,
           t.min_profit AS old_min_profit, t.target_sell_price AS old_target_sell_price,
           t.notes AS old_notes,
           t.status AS old_status, t.missed_syncs AS old_missed_syncs,
           t.sold_at AS old_sold_at, t.ends_at AS old_ends_at,
           t.last_seen AS old_last_seen, t.updated_at AS old_updated_at
      FROM relist_links rl
      JOIN tracked_auctions t ON t.auction_uuid = rl.old_auction_uuid
      JOIN tracked_auctions n ON n.auction_uuid = rl.new_auction_uuid
     WHERE rl.accepted = 0 AND rl.ignored = 0
       AND t.buy_cost IS NOT NULL
       AND COALESCE(t.ignored, 0) = 0
       AND n.buy_cost IS NULL
       AND n.carry_suggestion_ignored = 0
       AND COALESCE(n.status, 'ACTIVE') = 'ACTIVE'
"""


def get_pending_carry_links(new_uuid: str) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return list(
            conn.execute(
                _PENDING_LINK_SELECT + " AND rl.new_auction_uuid = ? ORDER BY rl.confidence DESC",
                (new_uuid,),
            ).fetchall()
        )


def get_carry_suggestions(new_auction_uuid: str) -> List[sqlite3.Row]:
    """Pending stored carry suggestions for a new auction."""
    return get_pending_carry_links(new_auction_uuid)


def pending_carry_links_map() -> Dict[str, List[sqlite3.Row]]:
    """All pending carry suggestions grouped by new auction uuid (for the dashboard)."""
    out: Dict[str, List[sqlite3.Row]] = {}
    with get_conn() as conn:
        rows = conn.execute(_PENDING_LINK_SELECT + " ORDER BY rl.confidence DESC").fetchall()
    for r in rows:
        out.setdefault(r["new_auction_uuid"], []).append(r)
    return out


def carry_user_fields(new_uuid: str, old_uuid: str) -> bool:
    """Copy user-owned fields from an old auction onto the new one.

    Carries: buy_cost, min_profit, target_sell_price, notes. Never carries
    ignored or sold. Returns False if the old auction has no buy cost.
    """
    with get_conn() as conn:
        old = conn.execute(
            "SELECT buy_cost, min_profit, target_sell_price, notes FROM tracked_auctions WHERE auction_uuid = ?",
            (old_uuid,),
        ).fetchone()
        if old is None or old["buy_cost"] is None:
            return False
        cur = conn.execute(
            """
            UPDATE tracked_auctions SET
                buy_cost = ?,
                min_profit = COALESCE(?, min_profit),
                target_sell_price = ?,
                notes = COALESCE(?, notes),
                carried_from_uuid = ?,
                carry_suggestion_ignored = 0,
                updated_at = ?
             WHERE auction_uuid = ? AND buy_cost IS NULL
            """,
            (
                old["buy_cost"], old["min_profit"], old["target_sell_price"],
                old["notes"], old_uuid, utcnow(), new_uuid,
            ),
        )
        return cur.rowcount > 0


def copy_user_fields_to_relisted_auction(new_uuid: str, old_uuid: str) -> bool:
    """Copy buy_cost/min_profit/target_sell_price/notes from old -> new."""
    return carry_user_fields(new_uuid, old_uuid)


def accept_relist_link(new_uuid: str, old_uuid: str) -> None:
    """Mark the chosen link accepted and retire the other pending suggestions."""
    now = utcnow()
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT 1 FROM relist_links
             WHERE new_auction_uuid = ? AND old_auction_uuid = ?
            """,
            (new_uuid, old_uuid),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO relist_links
                    (old_auction_uuid, new_auction_uuid, confidence, reason, accepted, ignored, created_at)
                VALUES (?, ?, 0, 'Accepted manual carry.', 0, 0, ?)
                """,
                (old_uuid, new_uuid, now),
            )
        conn.execute(
            """
            UPDATE relist_links
               SET accepted = 1, ignored = 0, accepted_at = ?
             WHERE new_auction_uuid = ? AND old_auction_uuid = ?
            """,
            (now, new_uuid, old_uuid),
        )
        conn.execute(
            "UPDATE relist_links SET ignored = 1 WHERE new_auction_uuid = ? AND old_auction_uuid != ? AND accepted = 0",
            (new_uuid, old_uuid),
        )


def accept_carry_suggestion(new_uuid: str, old_uuid: str) -> None:
    """Accepted carry link helper with the requested public name."""
    accept_relist_link(new_uuid, old_uuid)


def ignore_carry_suggestions(new_uuid: str) -> None:
    """User dismissed the suggestion(s): never offer them for this auction again."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE tracked_auctions SET carry_suggestion_ignored = 1, updated_at = ? WHERE auction_uuid = ?",
            (utcnow(), new_uuid),
        )
        conn.execute(
            "UPDATE relist_links SET ignored = 1 WHERE new_auction_uuid = ? AND accepted = 0",
            (new_uuid,),
        )


def has_any_relist_link(new_uuid: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM relist_links WHERE new_auction_uuid = ? LIMIT 1", (new_uuid,)
        ).fetchone()
        return row is not None


def get_accepted_carry_link(new_uuid: str) -> Optional[sqlite3.Row]:
    """Accepted carry link details for the auction detail page."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT rl.*, t.item_name AS old_item_name, t.buy_cost AS old_buy_cost
              FROM relist_links rl
              LEFT JOIN tracked_auctions t ON t.auction_uuid = rl.old_auction_uuid
             WHERE rl.new_auction_uuid = ? AND rl.accepted = 1
             ORDER BY rl.accepted_at DESC, rl.id DESC
             LIMIT 1
            """,
            (new_uuid,),
        ).fetchone()


# --------------------------------------------------------------------------
# undercut alerts
# --------------------------------------------------------------------------

def record_undercut_alert(
    *,
    auction_uuid: str,
    candidate_uuid: Optional[str],
    item_tag: Optional[str],
    my_price: int,
    candidate_price: int,
    gap_coins: int,
    gap_percent: float,
    confidence: int,
    candidate_item_name: Optional[str],
    reason: str,
    notification_hash: str,
    notified: bool = False,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO undercut_alerts
                (auction_uuid, candidate_uuid, item_tag, my_price, candidate_price,
                 gap_coins, gap_percent, confidence, candidate_item_name, reason,
                 created_at, notified, notification_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                auction_uuid, candidate_uuid, item_tag, my_price, candidate_price,
                gap_coins, gap_percent, confidence, candidate_item_name, reason,
                utcnow(), 1 if notified else 0, notification_hash,
            ),
        )
        return int(cur.lastrowid)


def mark_undercut_alert_notified(alert_id: int, notification_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE undercut_alerts SET notified = 1, notification_hash = ? WHERE id = ?",
            (notification_hash, alert_id),
        )


def latest_undercut_for_auction(uuid: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM undercut_alerts
             WHERE auction_uuid = ?
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """,
            (uuid,),
        ).fetchone()


def latest_undercuts_map() -> Dict[str, sqlite3.Row]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.* FROM undercut_alerts u
            JOIN (
                SELECT auction_uuid, MAX(id) AS max_id
                FROM undercut_alerts GROUP BY auction_uuid
            ) latest ON u.id = latest.max_id
            """
        ).fetchall()
    return {row["auction_uuid"]: row for row in rows}


def undercut_history(uuid: str, limit: int = 20) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM undercut_alerts
                 WHERE auction_uuid = ?
                 ORDER BY created_at DESC, id DESC
                 LIMIT ?
                """,
                (uuid, limit),
            ).fetchall()
        )


def recent_undercut_alert_exists(
    uuid: str,
    candidate_uuid: Optional[str],
    notification_hash: Optional[str],
    cooldown_minutes: int,
    *,
    notified_only: bool = True,
) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)).isoformat()
    clauses = ["auction_uuid = ?", "created_at >= ?"]
    params: List[Any] = [uuid, since]
    if notified_only:
        clauses.append("notified = 1")
    if notification_hash:
        clauses.append("notification_hash = ?")
        params.append(notification_hash)
    elif candidate_uuid:
        clauses.append("candidate_uuid = ?")
        params.append(candidate_uuid)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT 1 FROM undercut_alerts WHERE {' AND '.join(clauses)} LIMIT 1",
            tuple(params),
        ).fetchone()
        return row is not None
