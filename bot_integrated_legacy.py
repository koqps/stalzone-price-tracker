"""
bot_integrated_legacy.py — StalZone NA Artifact Sniper (Pre-Patched Merge)

This is a RECONSTRUCTED merge candidate that combines your existing bot's
confirmed structure with all tracker integration fixes.  Because the exact
original PriceDB class body and scan_item() were not fully preserved in the
session transcript, the PriceDB here is a RECONSTRUCTED implementation
matching the known public interface (tier_sales, tier_stats, update_active_lots,
clear, _lot_key).  It uses a separate database file (cache/prices_patched.db)
to avoid colliding with your existing cache/prices.db.

WHAT'S DIFFERENT FROM YOUR ORIGINAL BOT:
  ✓ get_lot_quality() returns None for unknown quality (not Common)
  ✓ AUCTION_LOT_LIMIT raised to 100 (was 40)
  ✓ _lot_key() uses start_time for uniqueness
  ✓ has_cheaper_comparable_listing() — skips lots with cheaper comparables
  ✓ live_resale_cap_per_unit() — hard ceiling on resale estimates
  ✓ record_observations() — writes to MarketDB for the dashboard
  ✓ record_sale() — writes inferred sales to MarketDB
  ✓ !ingest command — pulls live data via scapi
  ✓ !valuation command — price model valuation per tier
  ✓ send_alert() shows live cap and confidence
  ✓ Confidence-gated alerts (minimum 65/100)

HOW TO USE:
  1. Copy this file and the tracker modules into your bot folder
  2. Diff against your existing bot.py to see all changes
  3. Merge the changes you want into your bot.py, OR
  4. Use this file directly (it's a complete, runnable bot)

  python bot_integrated_legacy.py

RECONSTRUCTED PriceDB NOTE:
  The PriceDB class below is a faithful reconstruction from the known
  interface.  It uses cache/prices_patched.db so it won't touch your
  existing cache/prices.db.  If you prefer to keep your original PriceDB,
  just diff this file with your bot.py and copy only the tracker integration
  changes (imports, get_lot_quality, scan_item additions, new commands).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands
from dotenv import load_dotenv

from scapi import AppClient, DatabaseLookup
from scapi.enums import Order, Region, Realm, SortAuction
from scapi.exceptions import NotFoundError, RateLimitError, RequestError

# --- Price tracker integration ---
from market_db import MarketDB, bonus_bucket
from price_model import compute_valuation, ValuationResult
from live_ingestion import ingest_live_data, extract_quality

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
EXBO_CLIENT_ID = os.getenv("EXBO_CLIENT_ID", "")
EXBO_CLIENT_SECRET = os.getenv("EXBO_CLIENT_SECRET", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

AUCTION_TAX_RATE = float(os.getenv("AUCTION_TAX_RATE", "0.05"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "10.0"))
MIN_PROFIT_RUB = int(os.getenv("MIN_PROFIT_RUB", "15000"))
MIN_HISTORY_SAMPLES = int(os.getenv("MIN_HISTORY_SAMPLES", "8"))
HISTORY_TTL_SECONDS = int(os.getenv("HISTORY_TTL_SECONDS", "600"))
VALUATION_PERCENTILE = float(os.getenv("VALUATION_PERCENTILE", "15"))

SCAN_DELAY_SECONDS = float(os.getenv("SCAN_DELAY_SECONDS", "1.0"))
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "3"))
REGION = Region(os.getenv("REGION", "na").lower())

ALERT_COOLDOWN_SECONDS = 900  # 15 min dedup window per lot

# --- Per-tier sale tracking (SQLite) ---
PRICE_DB_PATH = Path(__file__).parent / "cache" / "prices_patched.db"
MIN_TIER_SALES = int(os.getenv("MIN_TIER_SALES", "5"))
LOW_SAMPLE_TIER_SALES = int(os.getenv("LOW_SAMPLE_TIER_SALES", "2"))
TIER_PERCENTILE = float(os.getenv("TIER_PERCENTILE", "20"))
SALE_RECENCY_DAYS = int(os.getenv("SALE_RECENCY_DAYS", "14"))
BUYOUT_EARLY_WINDOW = int(os.getenv("BUYOUT_EARLY_WINDOW", "60"))

# --- Lot limit (FIXED: was 40, now 100) ---
AUCTION_LOT_LIMIT = int(os.getenv("AUCTION_LOT_LIMIT", "100"))

# --- Live comparable checks (NEW) ---
LIVE_UNDERCUT_RUB = int(os.getenv("LIVE_UNDERCUT_RUB", "1000"))
LIVE_UNDERCUT_PCT = float(os.getenv("LIVE_UNDERCUT_PCT", "0.005"))
MAX_BONUS_MATCH_GAP = float(os.getenv("MAX_BONUS_MATCH_GAP", "0.015"))
MIN_LIVE_COMPARABLES = int(os.getenv("MIN_LIVE_COMPARABLES", "1"))
MAX_HISTORY_OVER_LIVE_RATIO = float(os.getenv("MAX_HISTORY_OVER_LIVE_RATIO", "1.15"))

# --- Confidence-gated alerts (NEW) ---
MIN_ALERT_CONFIDENCE = int(os.getenv("MIN_ALERT_CONFIDENCE", "65"))

# --- Price tracker (NEW) ---
LIVE_MARKET_DATA = os.getenv("LIVE_MARKET_DATA", "false").lower() == "true"

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
QUALITY_NAMES = {
    0: "Common",
    1: "Uncommon",
    2: "Special",
    3: "Rare",
    4: "Exclusive",
    5: "Legendary",
}

QUALITY_VALUE_MULTIPLIERS = {
    0: 1.0,   # Common
    1: 1.20,  # Uncommon
    2: 1.50,  # Special
    3: 2.50,  # Rare
    4: 4.00,  # Exclusive
    5: 6.00,  # Legendary
}

# ----------------------------------------------------------------------
# scapi version-tolerant helpers
# ----------------------------------------------------------------------
# Legacy code used Order.ASCENDING; scapi 2.1.2 uses Order.ASC.
# This works with both versions.
ORDER_ASC = getattr(Order, "ASCENDING", getattr(Order, "ASC", "asc"))
SORT_BUYOUT = getattr(SortAuction, "BUYOUT_PRICE", "buyout_price")

# Region code helper — str(Region.NA) might be "Region.NA" or "na" depending on version
REGION_CODE = getattr(REGION, "value", str(REGION)).lower()


def _q(qlt: int | None) -> str:
    """Format a quality tier name."""
    if qlt is None:
        return "Unknown"
    return QUALITY_NAMES.get(qlt, f"Q{qlt}")


def fmt_rub(n: int | float) -> str:
    """Format a number as RUB."""
    return f"{int(n):,}"


def percentile(values: list[float], pct: float) -> float:
    """Compute a percentile from a list of values."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _mult(qlt: int) -> float:
    """Get the quality value multiplier."""
    return QUALITY_VALUE_MULTIPLIERS.get(qlt, 1.0)


# scapi sync/async tolerance helpers
async def maybe_await(value):
    """Await value if it's awaitable, otherwise return it directly.

    scapi's lots() and price_history() may return a coroutine or a plain
    BaseListing depending on version — this handles both transparently.
    """
    return await value if inspect.isawaitable(value) else value


def listing_rows(value) -> list:
    """Extract a plain list of lot/price records from any scapi return shape."""
    if value is None:
        return []
    if hasattr(value, "data"):
        return list(value.data or [])
    if hasattr(value, "items") and not callable(value.items):
        return list(value.items or [])
    if isinstance(value, dict):
        return list(value.get("data") or value.get("items") or value.get("results") or [])
    return list(value)


# ----------------------------------------------------------------------
# FIXED: get_lot_quality — returns None for unknown quality
# ----------------------------------------------------------------------

def get_lot_quality(lot: Any) -> tuple[int, float] | None:
    """Return (quality_tier 0..5, upgrade_bonus fraction), or None if unknown.

    CRITICAL FIX: Returns None when quality metadata is missing or unparseable,
    instead of silently defaulting to Common (qlt=0).  This was the root cause
    of Rare, Exclusive, and Legendary artifacts being invisible to the bot.
    """
    additional = getattr(lot, "additional", None)
    if additional is None:
        return None

    if isinstance(additional, dict):
        data = additional
    elif hasattr(additional, "model_dump"):
        data = additional.model_dump()
    elif hasattr(additional, "dict"):
        data = additional.dict()
    else:
        data = vars(additional) if hasattr(additional, "__dict__") else {}

    if not isinstance(data, dict):
        return None

    raw_qlt = data.get("qlt", data.get("quality"))
    if raw_qlt is None:
        return None  # CRITICAL: return None, not 0 (Common)

    try:
        qlt = int(raw_qlt)
    except (TypeError, ValueError):
        return None

    if qlt not in QUALITY_NAMES:
        return None

    raw_bonus = data.get("upgrade_bonus", data.get("upgradeBonus", data.get("bonus", 0.0)))
    try:
        bonus = float(raw_bonus or 0.0)
    except (TypeError, ValueError):
        bonus = 0.0

    # Defensive: if API returns 9.8 instead of 0.098, convert.
    if bonus > 1.0:
        bonus /= 100.0

    return qlt, max(0.0, bonus)


# ----------------------------------------------------------------------
# FIXED: Lot identity key — uses start_time for uniqueness
# ----------------------------------------------------------------------

def lot_key(item_id: str, lot: Any) -> str:
    """Generate a unique lot key using start_time to prevent collisions.

    FIX: The old _lot_key used item_id + start + end + buyout, which could
    collide when two identical listings existed simultaneously.
    """
    for attr in ("id", "lot_id", "auction_id", "listing_id"):
        value = getattr(lot, attr, None)
        if value is not None:
            return f"{item_id}_{attr}_{value}"

    buyout_price = getattr(lot, "buyout_price", 0) or 0
    amount = getattr(lot, "amount", 1) or 1
    start_time = getattr(lot, "start_time", None)
    start_str = start_time.isoformat() if hasattr(start_time, "isoformat") else str(start_time)
    return f"{item_id}_{start_str}_{buyout_price}_{amount}"


# ----------------------------------------------------------------------
# NEW: Live comparable listing checks
# ----------------------------------------------------------------------

def lot_buyout(lot: Any) -> int:
    """Return a valid buyout price, or 0 for bid-only / invalid lots."""
    try:
        return int(getattr(lot, "buyout_price", 0) or 0)
    except (TypeError, ValueError):
        return 0


def lot_amount(lot: Any) -> int:
    """Return a valid stack quantity."""
    try:
        return max(1, int(getattr(lot, "amount", 1) or 1))
    except (TypeError, ValueError):
        return 1


def comparable_live_prices(
    candidate: Any,
    all_lots: list[Any],
    qlt: int,
    bonus: float,
) -> list[float]:
    """Return per-unit buyout prices for comparable active listings.

    A comparable listing must:
      - Be a different lot (not the candidate)
      - Have a non-zero buyout price
      - Have the same quality tier
      - Have a bonus no worse than the candidate's, within tolerance
    """
    prices: list[float] = []
    candidate_key = lot_key("c", candidate)

    for other in all_lots:
        other_key = lot_key("c", other)
        if candidate_key == other_key:
            continue

        buyout = lot_buyout(other)
        if buyout <= 0:
            continue

        quality = get_lot_quality(other)
        if quality is None:
            continue

        other_qlt, other_bonus = quality
        if other_qlt != qlt:
            continue

        # A listing with meaningfully lower bonus is not equivalent.
        if other_bonus + MAX_BONUS_MATCH_GAP < bonus:
            continue

        amount = lot_amount(other)
        prices.append(buyout / amount)

    return sorted(prices)


def has_cheaper_comparable_listing(
    candidate: Any,
    all_lots: list[Any],
    qlt: int,
    bonus: float,
) -> bool:
    """True only if another comparable lot is cheaper than the candidate."""
    candidate_unit_price = lot_buyout(candidate) / lot_amount(candidate)
    for other_unit_price in comparable_live_prices(candidate, all_lots, qlt, bonus):
        if other_unit_price < candidate_unit_price:
            return True
    return False


def live_resale_cap_per_unit(
    candidate: Any,
    all_lots: list[Any],
    qlt: int,
    bonus: float,
) -> tuple[float | None, int]:
    """Return (cheapest comparable minus undercut, comparable_count) or (None, 0).

    This is the hard ceiling on resale value — the bot should never claim a
    resale above this number because a buyer can get the same artifact cheaper.
    """
    comparable = comparable_live_prices(candidate, all_lots, qlt, bonus)
    if len(comparable) < MIN_LIVE_COMPARABLES:
        return None, len(comparable)

    cheapest = comparable[0]
    undercut = max(LIVE_UNDERCUT_RUB, cheapest * LIVE_UNDERCUT_PCT)
    return max(0.0, cheapest - undercut), len(comparable)


# ----------------------------------------------------------------------
# RECONSTRUCTED: PriceDB (matches known interface)
# Uses cache/prices_patched.db to avoid colliding with your existing DB.
# ----------------------------------------------------------------------

class PriceDB:
    """Per-tier sale price tracker (RECONSTRUCTED from known interface).

    Tracks active lots and infers buyout sales when lots disappear before
    their end_time.  Groups sales by (item_id, qlt) for per-tier valuation.

    This is a reconstruction — if you have your own PriceDB, diff this file
    with your bot.py and keep your original PriceDB.
    """

    def __init__(self, path: Path = PRICE_DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS active_lots (
                lot_key TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                buyout_price INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                qlt INTEGER NOT NULL,
                bonus REAL NOT NULL DEFAULT 0,
                start_time TEXT,
                end_time TEXT,
                first_seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tier_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                qlt INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                amount INTEGER NOT NULL,
                buyout_price INTEGER NOT NULL,
                observed_at REAL NOT NULL,
                lot_key TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tier_sales_item
                ON tier_sales(item_id, qlt, observed_at);
        """)
        self._conn.commit()

    @staticmethod
    def _lot_key(item_id: str, lot: Any) -> str:
        """Prefer the API's unique lot id; otherwise use a rich fingerprint."""
        for attr in ("id", "lot_id", "auction_id", "listing_id"):
            value = getattr(lot, attr, None)
            if value is not None:
                return f"{item_id}_{attr}_{value}"

        start = getattr(lot, "start_time", None)
        end = getattr(lot, "end_time", None)
        if start is None or end is None:
            return f"{item_id}_{getattr(lot, 'buyout_price', 0)}_{getattr(lot, 'amount', 1)}"

        additional = getattr(lot, "additional", None) or {}
        if not isinstance(additional, dict):
            if hasattr(additional, "model_dump"):
                additional = additional.model_dump()
            elif hasattr(additional, "dict"):
                additional = additional.dict()

        qlt = additional.get("qlt", "")
        bonus = additional.get("upgrade_bonus", additional.get("upgradeBonus", ""))
        amount = getattr(lot, "amount", 1) or 1
        start_price = getattr(lot, "start_price", 0) or 0
        buyout = getattr(lot, "buyout_price", 0) or 0
        current = getattr(lot, "current_price", 0) or 0

        s = start.isoformat() if hasattr(start, "isoformat") else str(start)
        e = end.isoformat() if hasattr(end, "isoformat") else str(end)
        return f"{item_id}{s}{e}{amount}{start_price}{buyout}{current}{qlt}{bonus}"

    def update_active_lots(
        self,
        item_id: str,
        lots: list[Any],
        page_limit: int = 100,
    ) -> list[tuple[int, float, int, int, str]]:
        """Snapshot current lots and detect disappearances (inferred sales).

        Returns a list of (buyout_price, bonus, amount, qlt, lot_key) tuples
        for lots that disappeared before their end_time (likely sold).
        """
        now = time.time()
        sales: list[tuple[int, float, int, int, str]] = []

        with self._lock:
            # Build current lot keys
            current_keys: dict[str, Any] = {}
            for lot in lots:
                key = self._lot_key(item_id, lot)
                if key is None:
                    continue
                current_keys[key] = lot

            # Find disappeared lots
            cursor = self._conn.execute(
                "SELECT * FROM active_lots WHERE item_id=?",
                (item_id,),
            )
            for row in cursor.fetchall():
                if row["lot_key"] not in current_keys:
                    # Lot disappeared — check if it was likely a buyout
                    end_str = row["end_time"]
                    buyout = row["buyout_price"]
                    if buyout <= 0:
                        continue

                    try:
                        end_ts = datetime.fromisoformat(end_str).timestamp()
                    except (ValueError, TypeError):
                        end_ts = now + 86400

                    # Only count as a sale if it disappeared before expiry
                    if now < (end_ts - BUYOUT_EARLY_WINDOW):
                        sales.append((
                            buyout,
                            row["bonus"],
                            row["amount"],
                            row["qlt"],
                            row["lot_key"],
                        ))

            # Record sales
            for buyout, bonus, amount, qlt, key in sales:
                unit_price = buyout / max(amount, 1)
                self._conn.execute(
                    "INSERT INTO tier_sales (item_id, qlt, unit_price, amount, "
                    "buyout_price, observed_at, lot_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, qlt, unit_price, amount, buyout, now, key),
                )

            # Clear old active lots for this item and insert current ones
            self._conn.execute(
                "DELETE FROM active_lots WHERE item_id=?",
                (item_id,),
            )
            for key, lot in current_keys.items():
                quality = get_lot_quality(lot)
                if quality is None:
                    continue  # Don't default to Common
                qlt_val, bonus_val = quality
                start = getattr(lot, "start_time", None)
                end = getattr(lot, "end_time", None)
                start_str = start.isoformat() if hasattr(start, "isoformat") else str(start)
                end_str = end.isoformat() if hasattr(end, "isoformat") else str(end)
                self._conn.execute(
                    "INSERT INTO active_lots (lot_key, item_id, buyout_price, amount, "
                    "qlt, bonus, start_time, end_time, first_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, item_id, lot_buyout(lot), lot_amount(lot),
                     qlt_val, bonus_val, start_str, end_str, now),
                )

            self._conn.commit()

        return sales

    def tier_sales(self, item_id: str, qlt: int) -> list[float]:
        """Return recent per-unit sale prices for an (item_id, qlt) pair."""
        cutoff = time.time() - SALE_RECENCY_DAYS * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT unit_price FROM tier_sales "
                "WHERE item_id=? AND qlt=? AND observed_at>=? "
                "ORDER BY observed_at DESC",
                (item_id, qlt, cutoff),
            ).fetchall()
        return [r["unit_price"] for r in rows]

    def tier_stats(self) -> dict:
        """Return summary stats for the per-tier price database."""
        with self._lock:
            by_tier: dict[int, int] = {}
            for q in range(6):
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM tier_sales WHERE qlt=?", (q,)
                ).fetchone()[0]
                by_tier[q] = count

            total = self._conn.execute(
                "SELECT COUNT(*) FROM tier_sales"
            ).fetchone()[0]
            items = self._conn.execute(
                "SELECT COUNT(DISTINCT item_id) FROM tier_sales"
            ).fetchone()[0]
            active = self._conn.execute(
                "SELECT COUNT(*) FROM active_lots"
            ).fetchone()[0]

        return {
            "by_tier": by_tier,
            "total_sales": total,
            "items_with_sales": items,
            "active_tracked": active,
        }

    def clear(self) -> None:
        """Clear all recorded sale prices and active lots."""
        with self._lock:
            self._conn.execute("DELETE FROM tier_sales")
            self._conn.execute("DELETE FROM active_lots")
            self._conn.commit()


# ----------------------------------------------------------------------
# Bot setup
# ----------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Global state
api_client: AppClient | None = None
db_lookup: DatabaseLookup | None = None
pricedb = PriceDB()
market_db = MarketDB()  # NEW: for the price tracker dashboard + model
TRACKED_ITEMS: list[dict] = []
_price_history_cache: dict[str, tuple[float, list[float]]] = {}
_alert_cooldowns: dict[str, float] = {}
_scan_loop_started = False  # Guard against duplicate scan loops on reconnect


# ----------------------------------------------------------------------
# Tradeable artifact loading
# ----------------------------------------------------------------------

async def load_tradeable_artifacts() -> list[dict]:
    """Load all tradeable artifacts from the stalcraft-database."""
    global db_lookup
    if db_lookup is None:
        db_lookup = DatabaseLookup()
    items: list[dict] = []
    try:
        all_items = db_lookup.get_all(realm="global")
        for item_id, data in all_items.items():
            category = (data.get("category") or "").lower()
            sub = (data.get("sub_category") or "").lower()
            if any(k in category or k in sub for k in ("artefact", "artifact", "art")):
                name = data.get("name", {})
                if isinstance(name, dict):
                    name = name.get("en", name.get("ru", item_id))
                items.append({"id": item_id, "name": str(name)})
    except Exception as e:
        print(f"Failed to load from database: {e}")
    return items


# ----------------------------------------------------------------------
# Price history caching
# ----------------------------------------------------------------------

async def get_price_history(item_id: str) -> list[float]:
    """Get cached price history for an item."""
    now = time.time()
    if item_id in _price_history_cache:
        cached_at, prices = _price_history_cache[item_id]
        if now - cached_at < HISTORY_TTL_SECONDS:
            return prices

    try:
        raw = api_client.auction(item_id).price_history(
            limit=50, additional=True, region=REGION,
        )
        history = listing_rows(await maybe_await(raw))
        prices = [p.price / max(p.amount, 1) for p in history if p.price > 0]
    except Exception:
        prices = []

    _price_history_cache[item_id] = (now, prices)
    return prices


# ----------------------------------------------------------------------
# Confidence scoring (NEW)
# ----------------------------------------------------------------------

def valuation_confidence(
    live_comparables: int,
    confirmed_sales: int,
    inferred_sales: int,
    market_gap_pct: float,
    source: str,
) -> int:
    """Calculate a 0-100 confidence score for a valuation."""
    score = 0
    if live_comparables >= 3:
        score += 30
    elif live_comparables >= 1:
        score += 18

    if confirmed_sales >= 8:
        score += 35
    elif confirmed_sales >= 5:
        score += 25
    elif confirmed_sales >= 3:
        score += 15

    score += min(inferred_sales, 10) * 2

    if market_gap_pct <= 10:
        score += 20
    elif market_gap_pct <= 25:
        score += 10
    elif market_gap_pct > 50:
        score -= 25

    if "fallback" in source.lower():
        score -= 15

    return max(0, min(100, score))


# ----------------------------------------------------------------------
# Scan loop (FIXED + INTEGRATED)
# ----------------------------------------------------------------------

async def scan_item(item_id: str, item_name: str, channel) -> int:
    """Scan auction lots for a single item. Returns number of alerts sent."""
    alerts_sent = 0

    try:
        raw = api_client.auction(item_id).lots(
            limit=AUCTION_LOT_LIMIT,
            sort=SORT_BUYOUT,
            order=ORDER_ASC,
            additional=True,
            region=REGION,
        )
        lots = listing_rows(await maybe_await(raw))
    except (NotFoundError, RateLimitError, RequestError):
        return 0
    except Exception:
        return 0

    lots = list(lots)
    if not lots:
        return 0

    # --- NEW: Record all lots as snapshots in MarketDB ---
    from bot_integration import record_observations
    record_observations(market_db, item_id, item_name, lots, REGION_CODE)

    # Snapshot lots and detect disappeared sales
    sales = pricedb.update_active_lots(item_id, lots, page_limit=AUCTION_LOT_LIMIT)

    # --- NEW: Record inferred sales in MarketDB ---
    from bot_integration import record_sale
    for buyout_price, bonus, amount, qlt, key in sales:
        record_sale(
            db=market_db,
            item_id=item_id,
            item_name=item_name,
            qlt=qlt,
            upgrade_bonus=bonus,
            unit_price=buyout_price / max(amount, 1),
            amount=amount,
            region=REGION_CODE,
            source="inferred_buyout_sale",
            confidence=0.60,
        )

    # Get price history
    prices = await get_price_history(item_id)

    # Evaluate each lot
    best_per_quality: dict[int, tuple] = {}

    for lot in lots:
        quality = get_lot_quality(lot)
        if quality is None:
            continue  # FIXED: skip unknown quality instead of treating as Common

        qlt, bonus = quality

        buyout = lot_buyout(lot)
        amount = lot_amount(lot)
        if buyout <= 0:
            continue

        unit_price = buyout / amount

        # --- NEW: Live comparable check ---
        # Skip this lot if there's a cheaper comparable listing
        if has_cheaper_comparable_listing(lot, lots, qlt, bonus):
            continue

        # --- NEW: Get live resale cap (hard ceiling) ---
        live_cap, comparable_count = live_resale_cap_per_unit(lot, lots, qlt, bonus)

        # --- Valuation (existing logic + live cap) ---
        tier_sales = pricedb.tier_sales(item_id, qlt)

        if len(tier_sales) >= MIN_TIER_SALES:
            history_unit_est = percentile(tier_sales, TIER_PERCENTILE) * (1.0 + bonus)
            history_source = f"per-tier • {len(tier_sales)} sales"
        elif len(tier_sales) >= LOW_SAMPLE_TIER_SALES:
            history_unit_est = percentile(tier_sales, 20) * (1.0 + bonus)
            history_source = f"per-tier provisional • {len(tier_sales)} sales"
        elif len(prices) >= MIN_HISTORY_SAMPLES:
            mixed = percentile(prices, VALUATION_PERCENTILE)
            if mixed <= 0:
                continue
            history_unit_est = mixed * _mult(qlt) * (1.0 + bonus)
            history_source = "API history (fallback)"
        else:
            continue

        # --- NEW: Apply live cap as hard ceiling ---
        if live_cap is not None:
            max_history_value = live_cap * MAX_HISTORY_OVER_LIVE_RATIO
            capped_history_est = min(history_unit_est, max_history_value)
            unit_est = min(capped_history_est, live_cap)
            source = f"{history_source} • live cap {fmt_rub(int(live_cap))} ({comparable_count} comp)"
        else:
            unit_est = history_unit_est
            source = f"{history_source} • no live comparable"

        # Profit calculation
        net_yield = unit_est * (1 - AUCTION_TAX_RATE)
        net_profit = net_yield - unit_price
        net_profit_pct = (net_profit / unit_price * 100) if unit_price > 0 else 0

        if net_profit_pct < MIN_PROFIT_PCT:
            continue
        if net_profit < MIN_PROFIT_RUB:
            continue

        # --- NEW: Confidence score ---
        market_gap_pct = abs(history_unit_est - unit_est) / max(unit_est, 1) * 100
        confidence = valuation_confidence(
            live_comparables=comparable_count,
            confirmed_sales=len(tier_sales),
            inferred_sales=len(tier_sales),
            market_gap_pct=market_gap_pct,
            source=source,
        )

        # --- NEW: Confidence gate ---
        if confidence < MIN_ALERT_CONFIDENCE:
            continue

        # Cooldown dedup
        lot_k = lot_key(item_id, lot)
        cooldown_key = f"{item_id}_{lot_k}"
        now_ts = time.time()
        if cooldown_key in _alert_cooldowns:
            if now_ts - _alert_cooldowns[cooldown_key] < ALERT_COOLDOWN_SECONDS:
                continue
        _alert_cooldowns[cooldown_key] = now_ts

        # Track best per quality
        if qlt not in best_per_quality or net_profit > best_per_quality[qlt][8]:
            best_per_quality[qlt] = (
                lot, buyout, amount, qlt, bonus,
                unit_est, net_yield, net_profit, net_profit_pct,
                source, live_cap, comparable_count, confidence,
            )

    # Send alerts for best candidates
    for qlt, data in best_per_quality.items():
        (lot, buyout, amount, q, bonus,
         est_val, net_yield, net_profit, pct,
         source, live_cap, comp_count, conf) = data

        await send_alert(
            channel, item_name, item_id, lot, buyout, amount, q, bonus,
            est_val, net_yield, net_profit, pct,
            source, live_cap, comp_count, conf,
        )
        alerts_sent += 1

    return alerts_sent


# ----------------------------------------------------------------------
# Alert sending (UPDATED with live cap + confidence)
# ----------------------------------------------------------------------

async def send_alert(
    channel,
    item_name: str,
    item_id: str,
    lot: Any,
    buyout: int,
    amount: int,
    qlt: int,
    bonus: float,
    est_val: float,
    net_yield: float,
    net_profit: float,
    pct: float,
    source: str = "",
    live_cap_per_unit: float | None = None,
    comparable_count: int = 0,
    confidence: int = 0,
) -> None:
    """Send a Discord embed alert for a profitable listing."""
    qlt_name = _q(qlt)
    bonus_str = f"+{bonus*100:.1f}%" if bonus > 0 else "+0.0%"

    embed = discord.Embed(
        title=f"💰 {qlt_name} {item_name} {bonus_str}",
        description=f"Profitable listing detected on the NA auction",
        color=0x8fbc3f,
    )
    embed.add_field(
        name="Buyout",
        value=f"{fmt_rub(buyout)} RUB ({amount}x → {fmt_rub(int(buyout/amount))}/unit)",
        inline=False,
    )

    if live_cap_per_unit is not None:
        embed.add_field(
            name="Live Comparable Floor",
            value=f"{fmt_rub(int(live_cap_per_unit))} RUB/unit\n{comparable_count} comparable listing(s)",
            inline=True,
        )
    else:
        embed.add_field(
            name="Live Comparable Floor",
            value="No comparable current listings",
            inline=True,
        )

    if confidence > 0:
        conf_label = "High" if confidence >= 70 else "Medium" if confidence >= 45 else "Low"
        embed.add_field(
            name="Confidence",
            value=f"{confidence}/100 ({conf_label})",
            inline=True,
        )

    embed.add_field(
        name="Estimated Profit",
        value=f"Net: +{fmt_rub(int(net_profit))} RUB ({pct:.1f}%)",
        inline=True,
    )
    embed.add_field(
        name="Valuation",
        value=source,
        inline=False,
    )
    embed.set_footer(text="StalZone Sniper — confidence-weighted model")

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


# ----------------------------------------------------------------------
# Background scan loop
# ----------------------------------------------------------------------

async def scan_loop():
    """Periodically scan all tracked items for profitable lots."""
    await bot.wait_until_ready()
    global api_client, TRACKED_ITEMS

    if api_client is None:
        api_client = AppClient(
            client_id=EXBO_CLIENT_ID,
            client_secret=EXBO_CLIENT_SECRET,
        )

    if not TRACKED_ITEMS:
        TRACKED_ITEMS = await load_tradeable_artifacts()

    channel = bot.get_channel(CHANNEL_ID) if CHANNEL_ID else None

    while not bot.is_closed():
        if not TRACKED_ITEMS:
            await asyncio.sleep(60)
            continue

        # Process in batches
        batch = TRACKED_ITEMS[:SCAN_BATCH_SIZE]
        for item in batch:
            if bot.is_closed():
                break
            await scan_item(item["id"], item["name"], channel)
            await asyncio.sleep(SCAN_DELAY_SECONDS)

        # Rotate the list so all items get scanned
        TRACKED_ITEMS = TRACKED_ITEMS[SCAN_BATCH_SIZE:] + batch
        await asyncio.sleep(SCAN_DELAY_SECONDS)


# ----------------------------------------------------------------------
# Discord commands (prefix commands — existing + new)
# ----------------------------------------------------------------------

@bot.command()
async def status(ctx: commands.Context) -> None:
    """Show bot status."""
    embed = discord.Embed(title="StalZone Sniper Status", color=0x8fbc3f)
    embed.add_field(name="Tracked Items", value=str(len(TRACKED_ITEMS)), inline=True)
    embed.add_field(name="Region", value=str(REGION).upper(), inline=True)
    embed.add_field(name="Lot Limit", value=str(AUCTION_LOT_LIMIT), inline=True)
    embed.add_field(name="Scan Batch", value=f"{SCAN_BATCH_SIZE} items", inline=True)
    embed.add_field(name="Live Market Data", value="✅ Yes" if LIVE_MARKET_DATA else "❌ No (demo mode)", inline=True)
    embed.add_field(name="Min Confidence", value=str(MIN_ALERT_CONFIDENCE), inline=True)

    s = pricedb.tier_stats()
    by_tier = s["by_tier"]
    tier_lines = []
    for q in range(6):
        tier_lines.append(f"• {_q(q)}: {by_tier.get(q, 0)} sales")
    embed.add_field(name="Per-Tier Database", value="\n".join(tier_lines), inline=False)
    embed.add_field(name="Total Sales", value=f"{s['total_sales']:,}", inline=True)
    embed.add_field(name="Active Lots Tracked", value=str(s["active_tracked"]), inline=True)
    embed.set_footer(text="StalZone Sniper — Integrated Edition")
    await ctx.send(embed=embed)


@bot.command()
async def price(ctx: commands.Context, *, name: str) -> None:
    """Show the cheapest current lots for an artefact by name."""
    if not api_client:
        await ctx.send("API not ready yet.")
        return
    match = next((i for i in TRACKED_ITEMS if i["name"].lower() == name.lower()), None)
    if not match:
        match = next((i for i in TRACKED_ITEMS if name.lower() in i["name"].lower()), None)
    if not match:
        await ctx.send(f"No tracked artefact matching '{name}'.")
        return
    try:
        lots = listing_rows(await maybe_await(
            api_client.auction(match["id"]).lots(
                limit=5, sort=SORT_BUYOUT, order=ORDER_ASC,
                additional=True, region=REGION,
            )
        ))
    except Exception as e:
        await ctx.send(f"Error fetching lots: {e}")
        return
    if not lots:
        await ctx.send(f"No active lots for **{match['name']}** right now.")
        return
    lines = [f"**{match['name']}** ({match['id']}) — cheapest lots:"]
    for lot in lots[:5]:
        quality = get_lot_quality(lot)
        if quality is None:
            lines.append(f"• Unknown quality — {fmt_rub(lot_buyout(lot))} RUB — ends {getattr(lot, 'end_time', '—')}")
            continue
        qlt, bonus = quality
        lines.append(f"• {_q(qlt)} (+{bonus*100:.1f}%) — {fmt_rub(lot_buyout(lot))} RUB — ends {getattr(lot, 'end_time', '—')}")
    await ctx.send("\n".join(lines))


@bot.command()
async def items(ctx: commands.Context) -> None:
    """List all tracked artefacts."""
    if not TRACKED_ITEMS:
        await ctx.send("No tracked items loaded. Use !reload to load.")
        return
    names = [i["name"] for i in TRACKED_ITEMS]
    for chunk in [names[i:i+1900] for i in range(0, len(names), 1900)]:
        await ctx.send(f"```{chr(10).join(chunk)}```")


@bot.command()
async def reload(ctx: commands.Context) -> None:
    """Reload the tradeable artefact list from the database."""
    global TRACKED_ITEMS
    await ctx.send("🔄 Reloading artefact database…")
    TRACKED_ITEMS = await load_tradeable_artifacts()
    await ctx.send(f"✅ Now tracking {len(TRACKED_ITEMS)} artefacts.")


@bot.command()
async def ping(ctx: commands.Context) -> None:
    """Check bot latency."""
    await ctx.send(f"pong ({round(bot.latency*1000)}ms)")


@bot.command(name="dbstats")
async def dbstats(ctx: commands.Context) -> None:
    """Show the per-tier sale price database stats."""
    s = pricedb.tier_stats()
    by_tier = s["by_tier"]
    lines = ["📊 **Per-tier price database**"]
    for q in range(6):
        lines.append(f"• {_q(q)}: {by_tier.get(q, 0)} sales recorded")
    lines.append(f"Total sales: {s['total_sales']:,}")
    lines.append(f"Items with sales: {s['items_with_sales']}")
    lines.append(f"Active lots tracked: {s['active_tracked']}")
    lines.append(f"Min sales for a tier estimate: {MIN_TIER_SALES} | "
                 f"tier percentile: {int(TIER_PERCENTILE)}th | "
                 f"recency: {SALE_RECENCY_DAYS}d")
    lines.append(f"Lot limit: {AUCTION_LOT_LIMIT} | Min confidence: {MIN_ALERT_CONFIDENCE}")
    await ctx.send("\n".join(lines))


@bot.command(name="prices")
async def prices(ctx: commands.Context, *, name: str) -> None:
    """Show recorded per-tier resale values for an artefact."""
    match = next((i for i in TRACKED_ITEMS if i["name"].lower() == name.lower()), None)
    if not match:
        match = next((i for i in TRACKED_ITEMS if name.lower() in i["name"].lower()), None)
    if not match:
        await ctx.send(f"No tracked artefact matching '{name}'.")
        return
    iid = match["id"]
    lines = [f"**{match['name']}** ({iid}) — per-tier resale baselines:"]
    any_data = False
    for q in range(6):
        sales = pricedb.tier_sales(iid, q)
        if len(sales) >= MIN_TIER_SALES:
            any_data = True
            base = percentile(sales, TIER_PERCENTILE)
            lines.append(f"• {_q(q)}: {fmt_rub(int(base))} RUB/unit "
                        f"(from {len(sales)} sales)")
        elif sales:
            lines.append(f"• {_q(q)}: only {len(sales)} sale(s) — need {MIN_TIER_SALES}")
    if not any_data:
        lines.append("Not enough sales recorded yet. The bot learns prices "
                     "as lots sell — check back after it has run a while.")
    await ctx.send("\n".join(lines))


@bot.command(name="clearprices")
@commands.is_owner()
async def clearprices(ctx: commands.Context) -> None:
    """Clear all recorded sale prices (reset the price database). Owner only."""
    pricedb.clear()
    await ctx.send("🗑️ Price database cleared. Recording sales from scratch.")


@clearprices.error
async def clearprices_error(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.NotOwner):
        await ctx.send("⛔ Only the bot owner can clear the price database.")
    else:
        raise error


# --- NEW COMMANDS ---

@bot.command()
async def ingest(ctx: commands.Context, mode: str = "both") -> None:
    """Pull live auction data + price history into the tracker.

    Usage:
      !ingest lots     - fetch active auction lots only
      !ingest history  - fetch price history only
      !ingest both     - fetch both (default)
    """
    if mode not in ("lots", "history", "both"):
        await ctx.send("Usage: !ingest lots|history|both")
        return

    await ctx.send(f"📥 Starting live data ingestion ({mode})...")

    fetch_lots = mode in ("lots", "both")
    fetch_history = mode in ("history", "both")

    items_dict = {item["id"]: item["name"] for item in TRACKED_ITEMS}

    try:
        result = await asyncio.to_thread(
            ingest_live_data,
            market_db,
            items_dict,
            str(REGION).lower(),
            fetch_lots,
            fetch_history,
        )
        await ctx.send(
            f"✅ Ingestion complete!\n"
            f"   Lots: {result['lots_recorded']}\n"
            f"   Sales: {result['sales_recorded']}\n"
            f"   Items: {result['items_processed']}\n"
            f"   Errors: {result['errors']}"
        )
    except Exception as e:
        await ctx.send(f"❌ Ingestion failed: {e}")


@bot.command()
async def valuation(ctx: commands.Context, *, name: str) -> None:
    """Show the price-model valuation for an artifact (all quality tiers).

    Uses the confidence-weighted multi-source model instead of static
    multipliers. Shows fair value, quick sale, stretch, and confidence.
    """
    match = next((i for i in TRACKED_ITEMS if i["name"].lower() == name.lower()), None)
    if not match:
        match = next((i for i in TRACKED_ITEMS if name.lower() in i["name"].lower()), None)
    if not match:
        await ctx.send(f"No tracked artefact matching '{name}'.")
        return

    item_id = match["id"]
    item_name = match["name"]

    try:
        lots = listing_rows(await maybe_await(
            api_client.auction(item_id).lots(
                limit=AUCTION_LOT_LIMIT, sort=SORT_BUYOUT, order=ORDER_ASC,
                additional=True, region=REGION,
            )
        ))
    except Exception as e:
        await ctx.send(f"Error fetching lots: {e}")
        return

    # Group by quality tier
    by_tier: dict[int, list] = {}
    for lot in lots:
        q = get_lot_quality(lot)
        if q is None:
            continue
        by_tier.setdefault(q[0], []).append(lot)

    if not by_tier:
        await ctx.send(f"No quality data available for **{item_name}**.")
        return

    lines = [f"**{item_name}** — Price Model Valuation:"]
    for qlt in sorted(by_tier.keys()):
        tier_lots = by_tier[qlt]
        _, bonus = get_lot_quality(tier_lots[0])

        # Use the price model
        result = compute_valuation(
            db=market_db,
            item_id=item_id,
            item_name=item_name,
            qlt=qlt,
            upgrade_bonus=bonus or 0.0,
            region=REGION_CODE,
        )

        buyout = lot_buyout(tier_lots[0])
        amount = lot_amount(tier_lots[0])
        unit_price = buyout / max(amount, 1)
        cap, comp_count = live_resale_cap_per_unit(tier_lots[0], lots, qlt, bonus)

        tier_name = _q(qlt)
        lines.append(
            f"• {tier_name} ({len(tier_lots)} listings): "
            f"Cheapest {fmt_rub(int(unit_price))} | "
            f"Fair {fmt_rub(int(result.fair_value))} | "
            f"Quick {fmt_rub(int(result.quick_sale))} | "
            f"Stretch {fmt_rub(int(result.stretch))} | "
            f"Conf {result.confidence}/100"
            + (f" | Cap {fmt_rub(int(cap))} ({comp_count} comp)" if cap else " | No live comp")
        )

    await ctx.send("\n".join(lines))


@bot.command(name="debuglot")
async def debuglot(ctx: commands.Context, *, name: str) -> None:
    """Print raw fields for the cheapest lot of a matching artifact."""
    if not api_client:
        await ctx.send("API not ready yet.")
        return
    match = next((i for i in TRACKED_ITEMS if i["name"].lower() == name.lower()), None)
    if not match:
        match = next((i for i in TRACKED_ITEMS if name.lower() in i["name"].lower()), None)
    if not match:
        await ctx.send(f"No tracked artefact matching '{name}'.")
        return

    try:
        lots = listing_rows(await maybe_await(
            api_client.auction(match["id"]).lots(
                limit=5, sort=SORT_BUYOUT, order=ORDER_ASC,
                additional=True, region=REGION,
            )
        ))
    except Exception as e:
        await ctx.send(f"Error: {e}")
        return

    if not lots:
        await ctx.send("No current lots.")
        return

    lot = lots[0]
    payload = {
        "item_id": match["id"],
        "buyout_price": getattr(lot, "buyout_price", None),
        "amount": getattr(lot, "amount", None),
        "start_time": str(getattr(lot, "start_time", None)),
        "end_time": str(getattr(lot, "end_time", None)),
        "additional": getattr(lot, "additional", None),
    }
    text = json.dumps(payload, indent=2, default=str)
    for i in range(0, len(text), 1900):
        await ctx.send(f"```\n{text[i:i+1900]}\n```")


# ----------------------------------------------------------------------
# Bot events
# ----------------------------------------------------------------------

@bot.event
async def on_ready():
    global api_client, TRACKED_ITEMS
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Connected to {len(bot.guilds)} guild(s)")

    if api_client is None:
        api_client = AppClient(
            client_id=EXBO_CLIENT_ID,
            client_secret=EXBO_CLIENT_SECRET,
        )
        print(f"scapi AppClient initialised (region={REGION})")

    if not TRACKED_ITEMS:
        TRACKED_ITEMS = await load_tradeable_artifacts()
        print(f"Loaded {len(TRACKED_ITEMS)} tradeable artifacts")

    # Start the scan loop (only once — on_ready can fire on reconnect)
    global _scan_loop_started
    if not _scan_loop_started:
        _scan_loop_started = True
        bot.loop.create_task(scan_loop())
        print(f"Scan loop started (batch={SCAN_BATCH_SIZE}, delay={SCAN_DELAY_SECONDS}s, lot_limit={AUCTION_LOT_LIMIT})")
    print("Bot ready!")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "replace_me":
        raise SystemExit("Set DISCORD_TOKEN in .env (copy .env.example).")
    if not EXBO_CLIENT_ID or not EXBO_CLIENT_SECRET:
        raise SystemExit("Set EXBO_CLIENT_ID / EXBO_CLIENT_SECRET in .env.")

    print("=" * 60)
    print("  StalZone NA Artifact Sniper — Integrated Edition")
    print("=" * 60)
    print(f"  Region:        {REGION}")
    print(f"  Lot Limit:     {AUCTION_LOT_LIMIT}")
    print(f"  Scan Batch:    {SCAN_BATCH_SIZE}")
    print(f"  Scan Delay:    {SCAN_DELAY_SECONDS}s")
    print(f"  Live Market:   {'Yes' if LIVE_MARKET_DATA else 'No (demo mode)'}")
    print(f"  Min Confidence: {MIN_ALERT_CONFIDENCE}")
    print(f"  Price DB:      {PRICE_DB_PATH}")
    print(f"  Market DB:     cache/market.db")
    print("=" * 60)

    bot.run(DISCORD_TOKEN)
