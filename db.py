"""SQLite storage layer.

A new connection is opened per operation (SQLite handles this well in WAL mode)
which keeps things simple across the request handlers and the background loop.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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

            CREATE INDEX IF NOT EXISTS idx_analysis_uuid ON auction_analysis(auction_uuid);
            CREATE INDEX IF NOT EXISTS idx_notif_uuid ON notifications(auction_uuid);
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
        acols = {row["name"] for row in conn.execute("PRAGMA table_info(auction_analysis)")}
        if "rejected_json" not in acols:
            conn.execute("ALTER TABLE auction_analysis ADD COLUMN rejected_json TEXT")


# --------------------------------------------------------------------------
# tracked_auctions
# --------------------------------------------------------------------------

def upsert_auction(data: Dict[str, Any]) -> None:
    """Insert or update an auction from a sync. Preserves user-entered fields."""
    now = utcnow()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT auction_uuid, first_seen FROM tracked_auctions WHERE auction_uuid = ?",
            (data["auction_uuid"],),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE tracked_auctions
                   SET item_tag = ?, item_name = ?, skycofl_url = ?, listing_price = ?,
                       active = ?, ends_at = COALESCE(?, ends_at), last_seen = ?, updated_at = ?
                 WHERE auction_uuid = ?
                """,
                (
                    data.get("item_tag"),
                    data.get("item_name"),
                    data.get("skycofl_url"),
                    data.get("listing_price"),
                    int(data.get("active", 1)),
                    data.get("ends_at"),
                    now,
                    now,
                    data["auction_uuid"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO tracked_auctions
                    (auction_uuid, item_tag, item_name, skycofl_url, listing_price,
                     buy_cost, min_profit, ignored, active, sold, ends_at,
                     first_seen, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, 0, ?, 0, ?, ?, ?, ?)
                """,
                (
                    data["auction_uuid"],
                    data.get("item_tag"),
                    data.get("item_name"),
                    data.get("skycofl_url"),
                    data.get("listing_price"),
                    settings.relist_min_profit_after_tax,
                    int(data.get("active", 1)),
                    data.get("ends_at"),
                    now,
                    now,
                    now,
                ),
            )


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
) -> None:
    """Insert/update an auction observed during a sync.

    Preserves user-entered fields (buy_cost, min_profit, notes, ignored).
    notification_eligible / sold_notified are sticky: once 1 they never drop to 0.
    """
    now = utcnow()
    active = 1 if status == "ACTIVE" else 0
    sold = 1 if status == "SOLD" else 0
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT auction_uuid FROM tracked_auctions WHERE auction_uuid = ?", (uuid,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE tracked_auctions SET
                    item_tag = COALESCE(?, item_tag),
                    item_name = COALESCE(?, item_name),
                    skycofl_url = COALESCE(?, skycofl_url),
                    listing_price = COALESCE(?, listing_price),
                    sold_price = COALESCE(?, sold_price),
                    ends_at = COALESCE(?, ends_at),
                    status = ?, active = ?, sold = ?,
                    last_sync_seen = ?, missed_syncs = 0,
                    notification_eligible = CASE WHEN ? = 1 THEN 1 ELSE notification_eligible END,
                    sold_notified = CASE WHEN ? = 1 THEN 1 ELSE sold_notified END,
                    last_seen = ?, updated_at = ?
                 WHERE auction_uuid = ?
                """,
                (
                    item_tag, item_name, skycofl_url, listing_price, sold_price, ends_at,
                    status, active, sold, sync_id,
                    notification_eligible, sold_notified, now, now, uuid,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO tracked_auctions
                    (auction_uuid, item_tag, item_name, skycofl_url, listing_price,
                     buy_cost, min_profit, ignored, active, sold, sold_price, ends_at,
                     status, last_sync_seen, missed_syncs, notification_eligible, sold_notified,
                     first_seen, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?, 0, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    uuid, item_tag, item_name, skycofl_url, listing_price,
                    settings.relist_min_profit_after_tax, active, sold, sold_price, ends_at,
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
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE tracked_auctions
               SET sold = 1, active = 0, status = 'SOLD', sold_notified = 1,
                   sold_price = COALESCE(?, sold_price), updated_at = ?
             WHERE auction_uuid = ?
            """,
            (sold_price, utcnow(), uuid),
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
                 trend_json, rejected_json, volume_per_day, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
