from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from funko_deal_bot.models import DealAlert, Listing


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    item_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    price REAL,
                    currency TEXT,
                    first_seen TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS denied_notices (
                    user_id TEXT PRIMARY KEY,
                    notified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_queue (
                    item_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    enqueued_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_scan_queue_status_time
                    ON scan_queue(status, enqueued_at);
                """
            )
        self._dedupe_alerts()
        self._migrate_owner_dm_from_chats()

    def known_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT item_id FROM listings").fetchall()
            queued = conn.execute("SELECT item_id FROM scan_queue WHERE status IN ('pending','processing','done')").fetchall()
        return {row["item_id"] for row in rows} | {row["item_id"] for row in queued}

    def add_listing(self, listing: Listing) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO listings(item_id, title, url, price, currency, first_seen, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing.item_id,
                    listing.title,
                    listing.url,
                    listing.price,
                    listing.currency,
                    _utc_now(),
                    json.dumps(listing.to_dict()),
                ),
            )


    def enqueue_listings(self, listings: list[Listing]) -> int:
        """Persist newly discovered eBay listings before any slow Vision work."""
        added = 0
        now = _utc_now()
        with self._connect() as conn:
            for listing in listings:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO scan_queue
                       (item_id, payload, status, attempts, enqueued_at)
                       VALUES (?, ?, 'pending', 0, ?)""",
                    (listing.item_id, json.dumps(listing.to_dict()), now),
                )
                if cur.rowcount:
                    added += 1
                    continue
                # A failed item is deliberately not part of known_ids(), so a later
                # eBay poll can discover it again. Re-open that queue row instead of
                # letting the old PRIMARY KEY silently discard the retry.
                retry = conn.execute(
                    """UPDATE scan_queue SET
                           payload=?, status='pending', attempts=0, enqueued_at=?,
                           started_at=NULL, finished_at=NULL, last_error=NULL
                       WHERE item_id=? AND status='failed'""",
                    (json.dumps(listing.to_dict()), now, listing.item_id),
                )
                added += int(retry.rowcount or 0)
        return added

    def requeue_stale(self, max_age_seconds: int = 1800) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE scan_queue SET status='pending', last_error='requeued after stale worker', started_at=NULL
                   WHERE status='processing' AND started_at IS NOT NULL
                     AND (julianday(?) - julianday(started_at))*86400 > ?""",
                (_utc_now(), int(max_age_seconds)),
            )
        return int(cur.rowcount or 0)

    def claim_next_queue_item(self) -> Listing | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT item_id, payload FROM scan_queue WHERE status='pending' ORDER BY enqueued_at, item_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "UPDATE scan_queue SET status='processing', attempts=attempts+1, started_at=? WHERE item_id=? AND status='pending'",
                (_utc_now(), row['item_id']),
            )
            if cur.rowcount != 1:
                return None
            return Listing.from_dict(json.loads(row['payload']))

    def finish_queue_item(self, item_id: str, listing: Listing, error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE scan_queue SET status=?, finished_at=?, last_error=? WHERE item_id=?",
                ('failed' if error else 'done', _utc_now(), error[:500], item_id),
            )

    def upsert_listing(self, listing: Listing) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO listings(item_id, title, url, price, currency, first_seen, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                     title=excluded.title, url=excluded.url, price=excluded.price,
                     currency=excluded.currency, payload=excluded.payload""",
                (listing.item_id, listing.title, listing.url, listing.price, listing.currency, _utc_now(), json.dumps(listing.to_dict())),
            )

    def queue_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) n FROM scan_queue GROUP BY status").fetchall()
        out = {'pending':0,'processing':0,'done':0,'failed':0}
        for row in rows:
            out[str(row['status'])] = int(row['n'])
        return out

    def catalog(self) -> list[Listing]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM listings ORDER BY first_seen DESC LIMIT 400"
            ).fetchall()
        items: list[Listing] = []
        for row in rows:
            data = json.loads(row["payload"])
            items.append(Listing.from_dict(data))
        return items

    def has_alert(self, item_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alerts WHERE item_id=? LIMIT 1",
                (item_id,),
            ).fetchone()
        return row is not None

    def save_alert(self, alert: DealAlert) -> bool:
        """Persist an alert once per item_id. Returns False if already alerted."""
        item_id = alert.listing.item_id
        if self.has_alert(item_id):
            return False
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO alerts(item_id, kind, created_at, payload) VALUES (?, ?, ?, ?)",
                    (
                        item_id,
                        alert.kind,
                        _utc_now(),
                        json.dumps(alert.to_dict()),
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def _dedupe_alerts(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM alerts
                WHERE id NOT IN (SELECT MIN(id) FROM alerts GROUP BY item_id)
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_item_id ON alerts(item_id)"
            )

    def recent_alerts(self, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload, created_at FROM alerts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["payload"])
            payload["created_at"] = row["created_at"]
            out.append(payload)
        return out

    def add_chat(self, chat_id: int | str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO chats(chat_id, added_at) VALUES (?, ?)",
                (str(chat_id), _utc_now()),
            )

    def chat_ids(self) -> list[str]:
        return self.alert_chat_ids()

    def _migrate_owner_dm_from_chats(self) -> None:
        """Keep a /start личка from older builds after ACL started using meta keys."""
        if self.get_owner_dm():
            return
        with self._connect() as conn:
            rows = conn.execute("SELECT chat_id FROM chats").fetchall()
        for row in rows:
            cid = str(row["chat_id"]).strip()
            if cid and not cid.startswith("-"):
                self.set_owner_dm(cid)
                return

    def set_owner_dm(self, chat_id: int | str) -> None:
        cid = str(chat_id).strip()
        if not cid or cid.startswith("-"):
            return
        self.add_chat(cid)
        primary = self.get_owner_dm()
        if not primary:
            self.set_meta("owner_dm_chat_id", cid)
            return
        if cid == primary:
            return
        extras = self._extra_dm_ids()
        if cid not in extras:
            extras.append(cid)
            self.set_meta("extra_dm_chat_ids", ",".join(extras))

    def get_owner_dm(self) -> str:
        return self.get_meta("owner_dm_chat_id")

    def _extra_dm_ids(self) -> list[str]:
        raw = self.get_meta("extra_dm_chat_ids")
        ids: list[str] = []
        for part in raw.split(","):
            cid = part.strip()
            if cid and cid not in ids:
                ids.append(cid)
        return ids

    def dm_chat_ids(self) -> list[str]:
        ids: list[str] = []
        primary = self.get_owner_dm().strip()
        if primary:
            ids.append(primary)
        for cid in self._extra_dm_ids():
            if cid not in ids:
                ids.append(cid)
        return ids

    def set_bound_group(self, chat_id: int | str) -> None:
        cid = str(chat_id).strip()
        if not cid:
            return
        self.set_meta("bound_group_chat_id", cid)
        self.add_chat(cid)

    def get_bound_group(self) -> str:
        return self.get_meta("bound_group_chat_id")

    def clear_bound_group(self) -> str:
        previous = self.get_bound_group()
        if previous:
            with self._connect() as conn:
                conn.execute("DELETE FROM chats WHERE chat_id=?", (previous,))
        self.set_meta("bound_group_chat_id", "")
        return previous

    def alert_chat_ids(self) -> list[str]:
        ids = self.dm_chat_ids()
        bound = self.get_bound_group().strip()
        if bound and bound not in ids:
            ids.append(bound)
        return ids

    def first_denied_notice(self, user_id: int | str) -> bool:
        """Return True the first time we should tell this user they have no access."""
        uid = str(user_id)
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO denied_notices(user_id, notified_at) VALUES (?, ?)",
                    (uid, _utc_now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def increment_meta_int(self, key: str, amount: int = 1) -> int:
        """Atomically increment an integer meta counter and return the new value."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            current = int(row["value"]) if row and str(row["value"]).strip().lstrip("-").isdigit() else 0
            new_value = current + int(amount)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(new_value)),
            )
        return new_value

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def stats(self) -> dict:
        with self._connect() as conn:
            listings = conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()["n"]
            alerts = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
            chats = conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]
        dests = self.alert_chat_ids()
        return {
            "listings": listings,
            "alerts": alerts,
            "chats": len(dests) if dests else chats,
            "last_scan": self.get_meta("last_scan"),
            "last_error": self.get_meta("last_error"),
            "last_mode": self.get_meta("last_mode"),
            "last_fetched": self.get_meta("last_fetched"),
            "last_new_count": self.get_meta("last_new_count"),
            "last_processed_count": self.get_meta("last_processed_count", "0"),
            "last_failed_count": self.get_meta("last_failed_count", "0"),
            "total_scans": self.get_meta("total_scans", "0"),
            "total_fetched": self.get_meta("total_fetched", "0"),
            "total_new": self.get_meta("total_new", "0"),
            "total_processed": self.get_meta("total_processed", "0"),
            "total_alerts": max(int(self.get_meta("total_alerts", "0") or 0), int(alerts)),
            "last_new_item_id": self.get_meta("last_new_item_id"),
            "last_new_title": self.get_meta("last_new_title"),
            "last_new_url": self.get_meta("last_new_url"),
            **{f"queue_{k}": v for k, v in self.queue_stats().items()},
        }
