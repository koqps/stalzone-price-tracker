"""
live_ingestion.py — Live auction data ingestion via the StalCraft API (scapi).

Pulls active auction lots and completed-sale price history for tracked
artifacts and writes them into the MarketDB (auction_snapshot and
sale_observation tables).  This is the bridge between the official in-game
economy and the price-tracking model.

Can be used in three modes:
  1. Standalone script:  python live_ingestion.py
  2. Imported and called:  from live_ingestion import ingest_live_data
  3. Per-item from the bot's scan loop:  ingest_item_lots(api_client, db, item_id, item_name)

Requires EXBO_CLIENT_ID and EXBO_CLIENT_SECRET environment variables
(or a .env file with python-dotenv installed).
"""
from __future__ import annotations

import asyncio
import os
import time
import logging
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from market_db import MarketDB, bonus_bucket

log = logging.getLogger("live_ingestion")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

# ─── Configuration ───────────────────────────────────────────────────────────
REGION = os.getenv("REGION", "na").lower()
EXBO_CLIENT_ID = os.getenv("EXBO_CLIENT_ID", "")
EXBO_CLIENT_SECRET = os.getenv("EXBO_CLIENT_SECRET", "")
# How many lots to fetch per item (higher = catches expensive Rare/Exclusive/Legendary)
LOT_LIMIT = int(os.getenv("LOT_LIMIT", "100"))
# How many price-history records to fetch per item
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
# Sleep between API calls to respect rate limits (seconds)
API_SLEEP = float(os.getenv("API_SLEEP", "1.5"))
# Realm for the stalcraft-database (global = international/NA)
REALM = os.getenv("REALM", "global").lower()

# ─── scapi lazy initialization ───────────────────────────────────────────────
_api_client = None
_db_lookup = None


def get_api_client():
    """Initialise and cache the scapi AppClient."""
    global _api_client
    if _api_client is not None:
        return _api_client
    from scapi import AppClient
    if not EXBO_CLIENT_ID or not EXBO_CLIENT_SECRET:
        raise RuntimeError(
            "EXBO_CLIENT_ID and EXBO_CLIENT_SECRET must be set. "
            "Get them at https://eapi.stalcraft.net (OAuth2 credentials)."
        )
    _api_client = AppClient(
        client_id=EXBO_CLIENT_ID,
        client_secret=EXBO_CLIENT_SECRET,
    )
    log.info("scapi AppClient initialised (region=%s)", REGION)
    return _api_client


def get_db_lookup():
    """Initialise and cache the DatabaseLookup for tradeable items."""
    global _db_lookup
    if _db_lookup is not None:
        return _db_lookup
    from scapi import DatabaseLookup
    _db_lookup = DatabaseLookup()
    log.info("scapi DatabaseLookup initialised (realm=%s)", REALM)
    return _db_lookup


# ─── Tradeable artifact discovery ────────────────────────────────────────────

async def load_tradeable_artifacts() -> dict[str, str]:
    """Load all tradeable artifacts from the stalcraft-database.

    Returns a dict mapping item_id -> item_name.
    """
    lookup = get_db_lookup()
    all_items = await lookup.get_all(realm=REALM)
    artifacts: dict[str, str] = {}

    for item_id, data in all_items.items():
        # Items in the database have a "category" field.
        # Artifacts are under "artefact" or "artifact" category.
        category = (data.get("category") or "").lower()
        sub_category = (data.get("sub_category") or "").lower()
        name = data.get("name", {}).get("en", data.get("name", "")) if isinstance(data.get("name"), dict) else data.get("name", item_id)

        # Check if it's an artifact (various possible category names)
        is_artifact = any(k in category or k in sub_category
                          for k in ("artefact", "artifact", "art"))

        # Check if tradeable (some items have a tradeable flag, others are implicitly tradeable)
        tradeable = data.get("tradeable", True)

        if is_artifact and tradeable:
            artifacts[item_id] = str(name)

    log.info("Loaded %d tradeable artifacts from stalcraft-database", len(artifacts))
    return artifacts


# ─── Quality extraction from additional dict ────────────────────────────────

QUALITY_NAMES = {
    0: "Common",
    1: "Uncommon",
    2: "Special",
    3: "Rare",
    4: "Exclusive",
    5: "Legendary",
}


def extract_quality(additional: dict | None) -> tuple[int | None, float | None]:
    """Extract quality tier and upgrade bonus from a lot's additional dict.

    Returns (qlt, upgrade_bonus).  Returns (None, None) if quality cannot
    be determined — this is the critical fix for Rare/Exclusive/Legendary
    artifacts being silently treated as Common.

    The scapi additional dict structure for auction lots:
      {
        "qlt": 3,              # quality tier 0-5
        "upgrade_bonus": 0.08, # bonus fraction
        "upgrade_level": 2,    # upgrade level
        "bound": false,
        ...
      }
    """
    if not additional or not isinstance(additional, dict):
        return None, None

    qlt = additional.get("qlt")
    if qlt is None:
        # Some lots use "quality" instead of "qlt"
        qlt = additional.get("quality")

    if qlt is not None:
        try:
            qlt = int(qlt)
            if not 0 <= qlt <= 5:
                log.warning("Unexpected quality tier %d — clamping", qlt)
                qlt = max(0, min(5, qlt))
        except (ValueError, TypeError):
            qlt = None

    # Upgrade bonus can be a float or a dict
    upgrade_bonus = additional.get("upgrade_bonus")
    if isinstance(upgrade_bonus, dict):
        # Some formats: {"value": 0.08, ...}
        upgrade_bonus = upgrade_bonus.get("value", upgrade_bonus.get("amount"))
    if upgrade_bonus is not None:
        try:
            upgrade_bonus = float(upgrade_bonus)
        except (ValueError, TypeError):
            upgrade_bonus = None

    return qlt, upgrade_bonus


# ─── Live lot ingestion ──────────────────────────────────────────────────────

def ingest_item_lots(
    api_client,
    db: MarketDB,
    item_id: str,
    item_name: str,
    region: str = None,
    limit: int = None,
) -> int:
    """Fetch active auction lots for one item and record them as snapshots.

    Returns the number of lots recorded.
    """
    region = region or REGION
    limit = limit or LOT_LIMIT

    try:
        from scapi.enums import SortAuction, Order
        listing = api_client.auction(item_id).lots(
            limit=limit,
            sort=SortAuction.BUYOUT_PRICE,
            order=Order.ASC,
            additional=True,
            region=region,
        )
    except Exception as e:
        log.error("Failed to fetch lots for %s (%s): %s", item_name, item_id, e)
        return 0

    recorded = 0
    for lot in listing:
        qlt, upgrade_bonus = extract_quality(getattr(lot, "additional", None))

        # Skip lots where quality can't be determined — this prevents
        # the old bug where unknown-quality lots were silently treated as Common.
        if qlt is None:
            continue

        amount = getattr(lot, "amount", 1) or 1
        buyout_price = getattr(lot, "buyout_price", 0) or 0
        if buyout_price <= 0:
            continue

        unit_price = buyout_price / max(amount, 1)

        # Generate a unique lot key using item_id + start_time + buyout_price
        # This is more reliable than the old _lot_key which could collide.
        start_time = getattr(lot, "start_time", None)
        lot_key = f"{item_id}_{start_time}_{buyout_price}_{amount}"

        db.record_snapshot(
            item_id=item_id,
            item_name=item_name,
            region=region,
            qlt=qlt,
            bonus=upgrade_bonus or 0.0,
            bonus_bucket=bonus_bucket(upgrade_bonus),
            amount=amount,
            buyout_price=buyout_price,
            unit_price=unit_price,
            lot_key=lot_key,
        )
        recorded += 1

    log.info("  %s: %d lots recorded (of %d fetched)", item_name, recorded, len(listing))
    return recorded


def ingest_item_history(
    api_client,
    db: MarketDB,
    item_id: str,
    item_name: str,
    region: str = None,
    limit: int = None,
) -> int:
    """Fetch completed-sale price history for one item and record as observations.

    Returns the number of sales recorded.
    """
    region = region or REGION
    limit = limit or HISTORY_LIMIT

    try:
        listing = api_client.auction(item_id).price_history(
            limit=limit,
            additional=True,
            region=region,
        )
    except Exception as e:
        log.error("Failed to fetch price history for %s (%s): %s", item_name, item_id, e)
        return 0

    recorded = 0
    for price in listing:
        qlt, upgrade_bonus = extract_quality(getattr(price, "additional", None))

        if qlt is None:
            continue

        amount = getattr(price, "amount", 1) or 1
        sale_price = getattr(price, "price", 0) or 0
        if sale_price <= 0:
            continue

        unit_price = sale_price / max(amount, 1)
        sale_time = getattr(price, "time", None)

        db.record_sale(
            item_id=item_id,
            item_name=item_name,
            region=region,
            qlt=qlt,
            bonus_bucket=bonus_bucket(upgrade_bonus),
            unit_price=unit_price,
            amount=amount,
            source="official_price_history",
            confidence=0.80,  # Official API data is high-confidence
            observed_at=sale_time.isoformat() if hasattr(sale_time, "isoformat") else None,
        )
        recorded += 1

    log.info("  %s: %d sales recorded (of %d fetched)", item_name, recorded, len(listing))
    return recorded


def ingest_live_data(
    db: MarketDB | None = None,
    tracked_items: dict[str, str] | None = None,
    region: str = None,
    fetch_lots: bool = True,
    fetch_history: bool = True,
) -> dict[str, int]:
    """Full live data ingestion: fetch lots + price history for all tracked artifacts.

    Args:
        db: MarketDB instance (created if None)
        tracked_items: dict of item_id -> item_name (loaded from DB if None)
        region: StalCraft region (defaults to env REGION)
        fetch_lots: whether to fetch active auction lots
        fetch_history: whether to fetch price history

    Returns:
        dict with 'lots_recorded', 'sales_recorded', 'items_processed', 'errors'
    """
    region = region or REGION
    db = db or MarketDB()
    api = get_api_client()

    if tracked_items is None:
        try:
            tracked_items = asyncio.run(load_tradeable_artifacts())
        except Exception as e:
            log.error("Failed to load tradeable artifacts: %s", e)
            tracked_items = {}

    if not tracked_items:
        log.warning("No tracked items to ingest")
        return {"lots_recorded": 0, "sales_recorded": 0, "items_processed": 0, "errors": 0}

    lots_total = 0
    sales_total = 0
    errors = 0

    log.info("Starting live ingestion for %d items (region=%s)", len(tracked_items), region)

    for i, (item_id, item_name) in enumerate(tracked_items.items(), 1):
        log.info("[%d/%d] Processing %s (%s)", i, len(tracked_items), item_name, item_id)

        try:
            if fetch_lots:
                n = ingest_item_lots(api, db, item_id, item_name, region)
                lots_total += n
                time.sleep(API_SLEEP)

            if fetch_history:
                n = ingest_item_history(api, db, item_id, item_name, region)
                sales_total += n
                time.sleep(API_SLEEP)
        except Exception as e:
            log.error("Error processing %s: %s", item_name, e)
            errors += 1
            time.sleep(API_SLEEP * 2)

    log.info(
        "Live ingestion complete: %d lots, %d sales, %d items, %d errors",
        lots_total, sales_total, len(tracked_items), errors,
    )

    # Mark that we now have live data
    db.set_meta("live_data_ingested", "true")
    db.set_meta("last_ingestion", str(int(time.time())))

    return {
        "lots_recorded": lots_total,
        "sales_recorded": sales_total,
        "items_processed": len(tracked_items),
        "errors": errors,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Live auction data ingestion")
    parser.add_argument("--lots-only", action="store_true", help="Only fetch active lots")
    parser.add_argument("--history-only", action="store_true", help="Only fetch price history")
    parser.add_argument("--region", default=None, help="StalCraft region (default: from env)")
    args = parser.parse_args()

    result = ingest_live_data(
        region=args.region,
        fetch_lots=not args.history_only,
        fetch_history=not args.lots_only,
    )
    print(f"\nResult: {result}")
