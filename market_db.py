"""SQLite working cache with durable Supabase mirroring.

Render's local filesystem is only a cache. Writes are mirrored to Supabase and
recent durable history is hydrated back into SQLite after a restart.

Startup is intentionally lightweight: the HTTP server must be able to bind
before expensive hydration/index maintenance begins.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from supabase_sync import hydrate_sqlite, mirror, enabled as supabase_enabled

log = logging.getLogger("market_db")
DB_PATH = Path(__file__).parent / "cache" / "market.db"

# Only tables plus correctness-critical unique indexes live on the synchronous
# startup path. The larger read-optimization indexes are built in background.
CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS auction_snapshot (
 id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,item_name TEXT,region TEXT NOT NULL,
 qlt INTEGER NOT NULL,upgrade_level INTEGER NOT NULL DEFAULT -1,bonus REAL NOT NULL DEFAULT 0,
 bonus_bucket INTEGER NOT NULL DEFAULT 0,amount INTEGER NOT NULL DEFAULT 1,buyout_price REAL,
 unit_price REAL,lot_key TEXT,observed_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS sale_observation (
 id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,item_name TEXT,region TEXT NOT NULL,
 qlt INTEGER NOT NULL,upgrade_level INTEGER NOT NULL DEFAULT -1,bonus_bucket INTEGER NOT NULL DEFAULT 0,
 unit_price REAL NOT NULL,amount INTEGER NOT NULL DEFAULT 1,source TEXT NOT NULL,confidence REAL NOT NULL,
 observed_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sale_dedupe ON sale_observation(item_id,region,qlt,upgrade_level,unit_price,amount,source,observed_at);

CREATE TABLE IF NOT EXISTS community_signal (
 id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT,item_name TEXT NOT NULL,region TEXT NOT NULL DEFAULT 'na',
 qlt INTEGER,upgrade_level INTEGER NOT NULL DEFAULT -1,bonus_bucket INTEGER,claimed_price REAL,
 sentiment TEXT NOT NULL DEFAULT 'neutral',sentiment_score REAL NOT NULL DEFAULT 0,source TEXT NOT NULL,
 source_name TEXT,url TEXT,excerpt TEXT,confidence REAL NOT NULL DEFAULT 0.15,collected_at REAL NOT NULL,
 reviewed INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS valuation_report (
 id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,item_name TEXT NOT NULL,region TEXT NOT NULL,
 qlt INTEGER NOT NULL,upgrade_level INTEGER NOT NULL DEFAULT -1,bonus_bucket INTEGER NOT NULL DEFAULT 0,
 floor_price REAL,quick_sale_price REAL,fair_value_price REAL,stretch_price REAL,confidence INTEGER NOT NULL,
 live_comparables INTEGER NOT NULL DEFAULT 0,confirmed_sales INTEGER NOT NULL DEFAULT 0,
 inferred_sales INTEGER NOT NULL DEFAULT 0,community_signals INTEGER NOT NULL DEFAULT 0,
 community_bias REAL NOT NULL DEFAULT 0,patch_risk REAL NOT NULL DEFAULT 0,evidence_summary TEXT,computed_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS price_deviation_alert (
 id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,item_name TEXT NOT NULL,region TEXT NOT NULL,
 qlt INTEGER NOT NULL,upgrade_level INTEGER NOT NULL DEFAULT -1,bonus_bucket INTEGER NOT NULL DEFAULT 0,
 alert_type TEXT NOT NULL,baseline_price REAL,observed_price REAL,deviation_pct REAL,severity TEXT NOT NULL,
 message TEXT,created_at REAL NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS patch_signal (
 id INTEGER PRIMARY KEY AUTOINCREMENT,patch_id TEXT NOT NULL,published_at REAL NOT NULL,source_url TEXT NOT NULL,
 title TEXT NOT NULL,item_id TEXT,artifact_class TEXT,signal_type TEXT NOT NULL,
 impact_direction TEXT NOT NULL DEFAULT 'uncertain',confidence REAL NOT NULL DEFAULT 0.25,
 summary TEXT,collected_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_patch_dedupe ON patch_signal(patch_id,item_id,signal_type);

CREATE TABLE IF NOT EXISTS manual_prices (
 item_id TEXT NOT NULL,region TEXT NOT NULL,qlt INTEGER NOT NULL,upgrade_level INTEGER NOT NULL DEFAULT -1,
 bonus_min REAL NOT NULL DEFAULT 0,bonus_max REAL NOT NULL DEFAULT 1,floor_per_unit REAL NOT NULL,
 target_per_unit REAL NOT NULL,source_url TEXT,note TEXT,updated_at REAL NOT NULL,expires_at REAL,
 PRIMARY KEY(item_id,region,qlt,upgrade_level,bonus_min,bonus_max));

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT,updated_at REAL);
"""

OPTIMIZATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_snapshot_lookup ON auction_snapshot(item_id,region,qlt,upgrade_level,observed_at);
CREATE INDEX IF NOT EXISTS idx_snapshot_region_time ON auction_snapshot(region,observed_at);
CREATE INDEX IF NOT EXISTS idx_sale_lookup ON sale_observation(item_id,region,qlt,upgrade_level,observed_at);
CREATE INDEX IF NOT EXISTS idx_sale_region_source_time ON sale_observation(region,source,observed_at);
CREATE INDEX IF NOT EXISTS idx_community_lookup ON community_signal(item_name,region,collected_at);
CREATE INDEX IF NOT EXISTS idx_valuation_lookup ON valuation_report(item_id,region,qlt,upgrade_level,computed_at);
CREATE INDEX IF NOT EXISTS idx_valuation_region_variant_time ON valuation_report(region,item_id,qlt,upgrade_level,computed_at);
CREATE INDEX IF NOT EXISTS idx_alert_lookup ON price_deviation_alert(created_at,severity);
CREATE INDEX IF NOT EXISTS idx_alert_region_time ON price_deviation_alert(region,created_at);
CREATE INDEX IF NOT EXISTS idx_patch_published ON patch_signal(published_at);
"""


def bonus_bucket(bonus: float | None, step_pct: float = 1.0) -> int:
    bonus_pct = max(0.0, float(bonus or 0.0)) * 100
    return int(round(bonus_pct / step_pct) * step_pct * 10)


class MarketDB:
    _hydrated_paths: set[str] = set()
    _maintenance_paths: set[str] = set()
    _maintenance_lock = threading.Lock()

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Keep this path short. combined.py imports multiple MarketDB users
        # before uvicorn.run(), so any expensive work here creates a full-site 502.
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(CORE_SCHEMA)
            self._upgrade_legacy_schema(conn)
        mirror.start()
        self._schedule_background_maintenance()

    def _schedule_background_maintenance(self) -> None:
        # The Google production VM is the live collector and has a persistent
        # SQLite cache. Running hydration/index rebuilds inside that same
        # process competes with collector writes and can lock every dashboard
        # endpoint. Render explicitly sets COLLECTOR_ENABLED=false, so it can
        # still hydrate its ephemeral cache in the background.
        collector_enabled = os.getenv("COLLECTOR_ENABLED", "true").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if collector_enabled:
            log.info("Skipping background database maintenance while collector is enabled")
            return
        key = str(self.path.resolve())
        with self._maintenance_lock:
            if key in self._maintenance_paths:
                return
            self._maintenance_paths.add(key)
        threading.Thread(
            target=self._background_maintenance,
            daemon=True,
            name="market-db-maintenance",
        ).start()

    def _background_maintenance(self) -> None:
        """Hydrate/optimize after HTTP startup instead of blocking uvicorn bind."""
        key = str(self.path.resolve())
        # Give combined.main() time to reach uvicorn.run() first.
        time.sleep(12)
        try:
            if supabase_enabled() and key not in self._hydrated_paths:
                with self._conn() as conn:
                    log.info("Starting background Supabase hydration")
                    hydrate_sqlite(conn)
                self._hydrated_paths.add(key)
                log.info("Background Supabase hydration complete")
            with self._conn() as conn:
                log.info("Starting background SQLite index maintenance")
                conn.executescript(OPTIMIZATION_INDEXES)
                conn.execute("ANALYZE")
                conn.execute("PRAGMA optimize")
            log.info("Background SQLite maintenance complete")
        except Exception:
            # Maintenance is best-effort. Never kill HTTP availability.
            log.exception("Background database maintenance failed")

    def _upgrade_legacy_schema(self, conn: sqlite3.Connection) -> None:
        for table in ("auction_snapshot","sale_observation","community_signal","valuation_report","price_deviation_alert","manual_prices"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "upgrade_level" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN upgrade_level INTEGER NOT NULL DEFAULT -1")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(valuation_report)").fetchall()}
        if "patch_risk" not in cols:
            conn.execute("ALTER TABLE valuation_report ADD COLUMN patch_risk REAL NOT NULL DEFAULT 0")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_rows(self, sql: str, params: list | tuple = ()) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(sql, params).fetchall()

    def _insert(self, table: str, fields: dict[str, Any], *, ignore: bool = False) -> None:
        cols = list(fields)
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        with self._conn() as conn:
            conn.execute(
                f"{verb} INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                [fields[c] for c in cols],
            )
        mirror.enqueue(table, fields)

    def record_snapshot(self, **fields: Any) -> None:
        fields.setdefault("upgrade_level", -1); fields.setdefault("observed_at", time.time())
        self._insert("auction_snapshot", fields)

    def record_sale(self, **fields: Any) -> None:
        fields.setdefault("upgrade_level", -1); fields.setdefault("observed_at", time.time())
        self._insert("sale_observation", fields, ignore=True)

    def record_community_signal(self, **fields: Any) -> None:
        fields.setdefault("upgrade_level", -1); fields.setdefault("collected_at", time.time())
        self._insert("community_signal", fields)

    def record_valuation(self, **fields: Any) -> None:
        fields.setdefault("upgrade_level", -1); fields.setdefault("patch_risk", 0.0); fields.setdefault("computed_at", time.time())
        # A scan evaluates many lots from the same exact variant. Persist at most
        # one valuation per variant every two minutes instead of one per lot.
        with self._conn() as conn:
            recent = conn.execute(
                "SELECT computed_at FROM valuation_report WHERE item_id=? AND region=? AND qlt=? "
                "AND upgrade_level=? ORDER BY computed_at DESC LIMIT 1",
                (fields["item_id"], fields["region"], fields["qlt"], fields["upgrade_level"]),
            ).fetchone()
        if recent and float(fields["computed_at"]) - float(recent["computed_at"] or 0) < 120:
            return
        self._insert("valuation_report", fields)

    def record_alert(self, **fields: Any) -> None:
        fields.setdefault("upgrade_level", -1); fields.setdefault("created_at", time.time())
        self._insert("price_deviation_alert", fields)

    def record_patch_signal(self, **fields: Any) -> None:
        fields.setdefault("collected_at", time.time())
        self._insert("patch_signal", fields, ignore=True)

    def recent_sales(self, item_id: str, region: str, qlt: int, bonus_bucket_val: int, since: float, limit: int = 200, upgrade_level: int | None = None) -> list[sqlite3.Row]:
        with self._conn() as conn:
            if upgrade_level is None:
                return conn.execute(
                    "SELECT * FROM sale_observation WHERE item_id=? AND region=? AND qlt=? AND bonus_bucket=? AND observed_at>=? ORDER BY observed_at DESC LIMIT ?",
                    (item_id,region,qlt,bonus_bucket_val,since,limit),
                ).fetchall()
            return conn.execute(
                "SELECT * FROM sale_observation WHERE item_id=? AND region=? AND qlt=? AND upgrade_level=? AND observed_at>=? ORDER BY observed_at DESC LIMIT ?",
                (item_id,region,qlt,upgrade_level,since,limit),
            ).fetchall()

    def recent_community_signals(self, item_name: str, region: str, since: float, limit: int = 100) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM community_signal WHERE item_name=? AND region=? AND collected_at>=? ORDER BY collected_at DESC LIMIT ?",
                (item_name,region,since,limit),
            ).fetchall()

    def latest_snapshot(self, item_id: str, region: str, since: float, qlt: int | None = None, upgrade_level: int | None = None) -> list[sqlite3.Row]:
        with self._conn() as conn:
            if qlt is not None and upgrade_level is not None:
                return conn.execute(
                    "SELECT * FROM auction_snapshot WHERE item_id=? AND region=? AND qlt=? AND upgrade_level=? "
                    "AND observed_at>=? ORDER BY observed_at DESC",
                    (item_id, region, qlt, upgrade_level, since),
                ).fetchall()
            return conn.execute(
                "SELECT * FROM auction_snapshot WHERE item_id=? AND region=? AND observed_at>=? ORDER BY observed_at DESC",
                (item_id,region,since),
            ).fetchall()

    def valuation_history(self, item_id: str, region: str, qlt: int, bonus_bucket_val: int, days: int = 30) -> list[sqlite3.Row]:
        since = time.time() - days * 86400
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM valuation_report WHERE item_id=? AND region=? AND qlt=? AND bonus_bucket=? AND computed_at>=? ORDER BY computed_at ASC",
                (item_id,region,qlt,bonus_bucket_val,since),
            ).fetchall()

    def latest_valuations(self, region: str, limit: int = 500) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT v.* FROM valuation_report v JOIN (SELECT item_id,qlt,upgrade_level,MAX(computed_at) mx FROM valuation_report WHERE region=? GROUP BY item_id,qlt,upgrade_level) x ON v.item_id=x.item_id AND v.qlt=x.qlt AND v.upgrade_level=x.upgrade_level AND v.computed_at=x.mx WHERE v.region=? ORDER BY v.computed_at DESC LIMIT ?",
                (region,region,limit),
            ).fetchall()

    def recent_alerts(self, region: str, days: int = 7, limit: int = 200) -> list[sqlite3.Row]:
        since = time.time() - days * 86400
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM price_deviation_alert WHERE region=? AND created_at>=? ORDER BY created_at DESC LIMIT ?",
                (region,since,limit),
            ).fetchall()

    def recent_patch_signals(self, days: int = 30, limit: int = 200) -> list[sqlite3.Row]:
        since = time.time() - days * 86400
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM patch_signal WHERE published_at>=? ORDER BY published_at DESC LIMIT ?",
                (since,limit),
            ).fetchall()

    def manual_price(self, item_id: str, region: str, qlt: int, bonus: float, upgrade_level: int = -1) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM manual_prices WHERE item_id=? AND region=? AND qlt=? AND upgrade_level IN (?, -1) AND bonus_min<=? AND bonus_max>=? AND (expires_at IS NULL OR expires_at>?) ORDER BY upgrade_level DESC,updated_at DESC LIMIT 1",
                (item_id,region,qlt,upgrade_level,bonus,bonus,time.time()),
            ).fetchone()

    def set_manual_price(self, **fields: Any) -> None:
        fields.setdefault("upgrade_level", -1); fields.setdefault("updated_at", time.time())
        cols = list(fields)
        updates = ",".join(f"{k}=excluded.{k}" for k in cols)
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO manual_prices ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) ON CONFLICT(item_id,region,qlt,upgrade_level,bonus_min,bonus_max) DO UPDATE SET {updates}",
                [fields[c] for c in cols],
            )
        mirror.enqueue("manual_prices", fields)

    def set_meta(self, key: str, value: str) -> None:
        row = {"key":key,"value":value,"updated_at":time.time()}
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (row["key"],row["value"],row["updated_at"]),
            )
        mirror.enqueue("meta", row)

    def get_meta(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default
