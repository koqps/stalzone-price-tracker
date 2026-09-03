"""
market_db.py — Market Intelligence Database for the StalZone NA Artifact Sniper
================================================================================

Extends the bot's existing per-tier price database (item_id, qlt sale
tracking) into a full market-intelligence layer that separates evidence by
*source* and *confidence*, so the price model (see price_model.py) can
combine official auction data with community sentiment without letting
noisy sources set the price directly.

Design principles (per prior valuation-bug fixes in this project):
  * Live comparable auction listings are the hard ceiling/floor — never
    overridden by history or community claims.
  * Observations are bucketed by (item_id, region, qlt, bonus_bucket), not
    just (item_id, qlt), so enhancement tiers don't get mixed together.
  * Every observation carries a `source` and a `confidence` weight so the
    resolver can rank evidence instead of averaging blindly.
  * Community data (Reddit, Discord trade channels, staldata.org, YouTube,
    manually verified price-guide entries) is stored as `community_signal`
    rows — informative, never authoritative until a human promotes it to
    `manual_prices`.

Tables
------
auction_snapshot     Point-in-time active-lot observations (for deviation
                     detection and live-comparable-cap calculations).
sale_observation     Confirmed or inferred completed sales, tagged by
                     source and confidence (mirrors the bot's existing
                     per-tier sale log, now with region/bucket columns).
community_signal      Raw community-sourced price mentions/valuations from
                     forums, Discord trade channels, staldata.org, YouTube,
                     with sentiment and a source URL for auditability.
valuation_report      Daily computed valuation snapshot per (item, region,
                     qlt, bonus_bucket): floor / quick_sale / fair_value /
                     stretch + confidence + evidence summary. This is what
                     the dashboard renders as "historical price trends".
price_deviation_alert Rows logged whenever the daily monitor finds a lot or
                     a community claim that deviates significantly from the
                     rolling valuation_report baseline.
manual_prices         Human-approved override ranges (owner-maintained).
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(__file__).parent / "cache" / "market.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS auction_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    item_name TEXT,
    region TEXT NOT NULL,
    qlt INTEGER NOT NULL,
    bonus REAL NOT NULL DEFAULT 0,
    bonus_bucket INTEGER NOT NULL DEFAULT 0,
    amount INTEGER NOT NULL DEFAULT 1,
    buyout_price REAL,
    unit_price REAL,
    lot_key TEXT,
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_lookup
    ON auction_snapshot(item_id, region, qlt, bonus_bucket, observed_at);

CREATE TABLE IF NOT EXISTS sale_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    item_name TEXT,
    region TEXT NOT NULL,
    qlt INTEGER NOT NULL,
    bonus_bucket INTEGER NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL,
    amount INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,           -- confirmed_bid_sale | inferred_buyout_sale | official_history
    confidence REAL NOT NULL,
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sale_lookup
    ON sale_observation(item_id, region, qlt, bonus_bucket, observed_at);

CREATE TABLE IF NOT EXISTS community_signal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT,
    item_name TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT 'na',
    qlt INTEGER,
    bonus_bucket INTEGER,
    claimed_price REAL,
    sentiment TEXT NOT NULL DEFAULT 'neutral',   -- bullish | bearish | neutral
    sentiment_score REAL NOT NULL DEFAULT 0,     -- -1..+1
    source TEXT NOT NULL,            -- reddit | discord_trade | staldata | youtube | forum | manual_verified
    source_name TEXT,                -- e.g. subreddit, channel, video title
    url TEXT,
    excerpt TEXT,
    confidence REAL NOT NULL DEFAULT 0.15,
    collected_at REAL NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_community_lookup
    ON community_signal(item_name, region, collected_at);

CREATE TABLE IF NOT EXISTS valuation_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    region TEXT NOT NULL,
    qlt INTEGER NOT NULL,
    bonus_bucket INTEGER NOT NULL DEFAULT 0,
    floor_price REAL,
    quick_sale_price REAL,
    fair_value_price REAL,
    stretch_price REAL,
    confidence INTEGER NOT NULL,
    live_comparables INTEGER NOT NULL DEFAULT 0,
    confirmed_sales INTEGER NOT NULL DEFAULT 0,
    inferred_sales INTEGER NOT NULL DEFAULT 0,
    community_signals INTEGER NOT NULL DEFAULT 0,
    community_bias REAL NOT NULL DEFAULT 0,
    evidence_summary TEXT,
    computed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_valuation_lookup
    ON valuation_report(item_id, region, qlt, bonus_bucket, computed_at);

CREATE TABLE IF NOT EXISTS price_deviation_alert (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    region TEXT NOT NULL,
    qlt INTEGER NOT NULL,
    bonus_bucket INTEGER NOT NULL DEFAULT 0,
    alert_type TEXT NOT NULL,        -- snipe_opportunity | overpriced_listing | history_vs_live_gap
                                      -- | community_vs_market_gap | volatility_spike
    baseline_price REAL,
    observed_price REAL,
    deviation_pct REAL,
    severity TEXT NOT NULL,          -- info | watch | high | critical
    message TEXT,
    created_at REAL NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alert_lookup
    ON price_deviation_alert(created_at, severity);

CREATE TABLE IF NOT EXISTS manual_prices (
    item_id TEXT NOT NULL,
    region TEXT NOT NULL,
    qlt INTEGER NOT NULL,
    bonus_min REAL NOT NULL DEFAULT 0,
    bonus_max REAL NOT NULL DEFAULT 1,
    floor_per_unit REAL NOT NULL,
    target_per_unit REAL NOT NULL,
    source_url TEXT,
    note TEXT,
    updated_at REAL NOT NULL,
    expires_at REAL,
    PRIMARY KEY (item_id, region, qlt, bonus_min, bonus_max)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL
);
"""


def bonus_bucket(bonus: float, step_pct: float = 2.5) -> int:
    """Bucket a bonus fraction (0.098 = +9.8%) into basis points, step 2.5%."""
    bonus_pct = max(0.0, bonus or 0.0) * 100
    return int(round(bonus_pct / step_pct) * step_pct * 10)


class MarketDB:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- writers ---------------------------------------------------------

    def record_snapshot(self, **fields: Any) -> None:
        fields.setdefault("observed_at", time.time())
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO auction_snapshot ({','.join(fields)}) "
                f"VALUES ({','.join('?' for _ in fields)})",
                list(fields.values()),
            )

    def record_sale(self, **fields: Any) -> None:
        fields.setdefault("observed_at", time.time())
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO sale_observation ({','.join(fields)}) "
                f"VALUES ({','.join('?' for _ in fields)})",
                list(fields.values()),
            )

    def record_community_signal(self, **fields: Any) -> None:
        fields.setdefault("collected_at", time.time())
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO community_signal ({','.join(fields)}) "
                f"VALUES ({','.join('?' for _ in fields)})",
                list(fields.values()),
            )

    def record_valuation(self, **fields: Any) -> None:
        fields.setdefault("computed_at", time.time())
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO valuation_report ({','.join(fields)}) "
                f"VALUES ({','.join('?' for _ in fields)})",
                list(fields.values()),
            )

    def record_alert(self, **fields: Any) -> None:
        fields.setdefault("created_at", time.time())
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO price_deviation_alert ({','.join(fields)}) "
                f"VALUES ({','.join('?' for _ in fields)})",
                list(fields.values()),
            )

    # -- readers ----------------------------------------------------------

    def recent_sales(
        self, item_id: str, region: str, qlt: int, bonus_bucket_val: int,
        since: float, limit: int = 200,
    ) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM sale_observation WHERE item_id=? AND region=? "
                "AND qlt=? AND bonus_bucket=? AND observed_at>=? "
                "ORDER BY observed_at DESC LIMIT ?",
                (item_id, region, qlt, bonus_bucket_val, since, limit),
            ).fetchall()

    def recent_community_signals(
        self, item_name: str, region: str, since: float, limit: int = 100,
    ) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM community_signal WHERE item_name=? AND region=? "
                "AND collected_at>=? ORDER BY collected_at DESC LIMIT ?",
                (item_name, region, since, limit),
            ).fetchall()

    def latest_snapshot(
        self, item_id: str, region: str, since: float,
    ) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM auction_snapshot WHERE item_id=? AND region=? "
                "AND observed_at>=? ORDER BY observed_at DESC",
                (item_id, region, since),
            ).fetchall()

    def valuation_history(
        self, item_id: str, region: str, qlt: int, bonus_bucket_val: int,
        days: int = 30,
    ) -> list[sqlite3.Row]:
        since = time.time() - days * 86400
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM valuation_report WHERE item_id=? AND region=? "
                "AND qlt=? AND bonus_bucket=? AND computed_at>=? "
                "ORDER BY computed_at ASC",
                (item_id, region, qlt, bonus_bucket_val, since),
            ).fetchall()

    def latest_valuations(self, region: str, limit: int = 500) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT v.* FROM valuation_report v "
                "JOIN (SELECT item_id, qlt, bonus_bucket, MAX(computed_at) mx "
                "      FROM valuation_report WHERE region=? "
                "      GROUP BY item_id, qlt, bonus_bucket) latest "
                "ON v.item_id=latest.item_id AND v.qlt=latest.qlt "
                "AND v.bonus_bucket=latest.bonus_bucket AND v.computed_at=latest.mx "
                "WHERE v.region=? ORDER BY v.computed_at DESC LIMIT ?",
                (region, region, limit),
            ).fetchall()

    def recent_alerts(self, region: str, days: int = 7, limit: int = 200) -> list[sqlite3.Row]:
        since = time.time() - days * 86400
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM price_deviation_alert WHERE region=? AND created_at>=? "
                "ORDER BY created_at DESC LIMIT ?",
                (region, since, limit),
            ).fetchall()

    def manual_price(self, item_id: str, region: str, qlt: int, bonus: float) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM manual_prices WHERE item_id=? AND region=? AND qlt=? "
                "AND bonus_min<=? AND bonus_max>=? AND (expires_at IS NULL OR expires_at>?) "
                "ORDER BY updated_at DESC LIMIT 1",
                (item_id, region, qlt, bonus, bonus, time.time()),
            ).fetchone()

    def set_manual_price(self, **fields: Any) -> None:
        fields.setdefault("updated_at", time.time())
        cols = ",".join(fields)
        placeholders = ",".join("?" for _ in fields)
        updates = ",".join(f"{k}=excluded.{k}" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO manual_prices ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(item_id, region, qlt, bonus_min, bonus_max) "
                f"DO UPDATE SET {updates}",
                list(fields.values()),
            )

    # -- meta -----------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """Store a metadata key-value pair (e.g. 'live_data_ingested' -> 'true')."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        """Retrieve a metadata value, or default if not found."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else default
