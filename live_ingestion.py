"""Live STALCRAFT NA auction ingestion with exact artifact upgrade levels."""
from __future__ import annotations

import asyncio
import logging
import os
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from market_db import MarketDB, bonus_bucket
from market_variants import extract_upgrade_level

log = logging.getLogger("live_ingestion")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

REGION = os.getenv("REGION", "na").lower()
EXBO_CLIENT_ID = os.getenv("EXBO_CLIENT_ID", "")
EXBO_CLIENT_SECRET = os.getenv("EXBO_CLIENT_SECRET", "")
LOT_LIMIT = int(os.getenv("LOT_LIMIT", os.getenv("AUCTION_LOT_LIMIT", "100")))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
API_SLEEP = float(os.getenv("API_SLEEP", "1.5"))
REALM = os.getenv("REALM", "global").lower()

_api_client = None
_db_lookup = None

QUALITY_NAMES = {0:"Common",1:"Uncommon",2:"Special",3:"Rare",4:"Exclusive",5:"Legendary"}


def get_api_client():
    global _api_client
    if _api_client is not None:
        return _api_client
    from scapi import AppClient
    if not EXBO_CLIENT_ID or not EXBO_CLIENT_SECRET:
        raise RuntimeError("EXBO_CLIENT_ID and EXBO_CLIENT_SECRET must be set")
    _api_client = AppClient(client_id=EXBO_CLIENT_ID, client_secret=EXBO_CLIENT_SECRET)
    log.info("scapi AppClient initialised (region=%s)", REGION)
    return _api_client


def get_db_lookup():
    global _db_lookup
    if _db_lookup is not None:
        return _db_lookup
    from scapi import DatabaseLookup
    from scapi.database.github import GitHubClient
    _db_lookup = DatabaseLookup(github=GitHubClient(owner="EXBO-Studio", repository="stalzone-database", branch="main"))
    log.info("scapi DatabaseLookup initialised (realm=%s)", REALM)
    return _db_lookup


async def load_tradeable_artifacts() -> dict[str, str]:
    lookup = get_db_lookup()
    all_items = await lookup.get_all(realm=REALM)
    artifacts: dict[str, str] = {}
    for item_id, data in all_items.items():
        if "/items/artefact/" not in str(data.get("data") or "").lower():
            continue
        name_data = data.get("name") or {}
        if isinstance(name_data, dict):
            lines = name_data.get("lines") or {}
            name = lines.get("en") or lines.get("ru") or item_id if isinstance(lines, dict) else item_id
        else:
            name = name_data or item_id
        artifacts[item_id] = str(name)
    log.info("Loaded %d artifacts from stalzone-database", len(artifacts))
    return artifacts


def extract_quality(additional: dict | None) -> tuple[int | None, float | None]:
    """Backward-compatible (quality, bonus) extraction."""
    if not additional or not isinstance(additional, dict):
        return None, None
    raw = additional.get("qlt", additional.get("quality"))
    try:
        qlt = int(raw)
        qlt = max(0, min(5, qlt))
    except (TypeError, ValueError):
        qlt = None
    bonus = additional.get("upgrade_bonus")
    if isinstance(bonus, dict):
        bonus = bonus.get("value", bonus.get("amount"))
    try:
        bonus = float(bonus) if bonus is not None else 0.0
    except (TypeError, ValueError):
        bonus = 0.0
    return qlt, bonus


def extract_market_variant(additional: dict | None) -> tuple[int | None, float, int]:
    """Return (quality, raw bonus, exact +level). Unknown levels are -1."""
    qlt, bonus = extract_quality(additional)
    level = extract_upgrade_level(additional)
    return qlt, float(bonus or 0.0), -1 if level is None else int(level)


async def ingest_item_lots(api_client, db: MarketDB, item_id: str, item_name: str, region: str = None, limit: int = None) -> int:
    region = region or REGION
    limit = limit or LOT_LIMIT
    try:
        from scapi.enums import SortAuction, Order
        listing = await api_client.auction(item_id).lots(limit=limit, sort=SortAuction.BUYOUT_PRICE, order=Order.ASC, additional=True, region=region)
    except Exception as exc:
        log.error("Failed to fetch lots for %s (%s): %s", item_name, item_id, exc)
        return 0

    recorded = 0
    lots = list(listing)
    for lot in lots:
        qlt, bonus, level = extract_market_variant(getattr(lot, "additional", None))
        if qlt is None:
            continue
        amount = getattr(lot, "amount", 1) or 1
        buyout_price = getattr(lot, "buyout_price", 0) or 0
        if buyout_price <= 0:
            continue
        unit_price = buyout_price / max(amount, 1)
        start_time = getattr(lot, "start_time", None)
        lot_key = f"{item_id}_{start_time}_{buyout_price}_{amount}"
        db.record_snapshot(
            item_id=item_id, item_name=item_name, region=region, qlt=qlt,
            upgrade_level=level, bonus=bonus, bonus_bucket=bonus_bucket(bonus),
            amount=amount, buyout_price=buyout_price, unit_price=unit_price, lot_key=lot_key,
        )
        recorded += 1
    log.info("  %s: %d lots recorded (of %d fetched)", item_name, recorded, len(lots))
    return recorded


async def ingest_item_history(api_client, db: MarketDB, item_id: str, item_name: str, region: str = None, limit: int = None) -> int:
    region = region or REGION
    limit = limit or HISTORY_LIMIT
    try:
        listing = await api_client.auction(item_id).price_history(limit=limit, additional=True, region=region)
    except Exception as exc:
        log.error("Failed to fetch price history for %s (%s): %s", item_name, item_id, exc)
        return 0

    recorded = 0
    history = list(listing)
    for price in history:
        qlt, bonus, level = extract_market_variant(getattr(price, "additional", None))
        if qlt is None:
            continue
        amount = getattr(price, "amount", 1) or 1
        sale_price = getattr(price, "price", 0) or 0
        if sale_price <= 0:
            continue
        sale_time = getattr(price, "time", None)
        observed_at = sale_time.timestamp() if hasattr(sale_time, "timestamp") else time.time()
        db.record_sale(
            item_id=item_id, item_name=item_name, region=region, qlt=qlt,
            upgrade_level=level, bonus_bucket=bonus_bucket(bonus),
            unit_price=sale_price / max(amount, 1), amount=amount,
            source="official_history", confidence=0.80, observed_at=observed_at,
        )
        recorded += 1
    log.info("  %s: %d sales recorded (of %d fetched)", item_name, recorded, len(history))
    return recorded


async def ingest_live_data(db: MarketDB | None = None, tracked_items: dict[str,str] | None = None, region: str = None, fetch_lots: bool = True, fetch_history: bool = True) -> dict[str,int]:
    region = region or REGION
    db = db or MarketDB()
    api = get_api_client()
    if tracked_items is None:
        try:
            tracked_items = await load_tradeable_artifacts()
        except Exception as exc:
            log.error("Failed to load tradeable artifacts: %s", exc)
            tracked_items = {}
    if not tracked_items:
        return {"lots_recorded":0,"sales_recorded":0,"items_processed":0,"errors":0}

    lots_total = sales_total = errors = 0
    log.info("Starting live ingestion for %d items (region=%s)", len(tracked_items), region)
    for i, (item_id, item_name) in enumerate(tracked_items.items(), 1):
        log.info("[%d/%d] Processing %s (%s)", i, len(tracked_items), item_name, item_id)
        try:
            if fetch_lots:
                lots_total += await ingest_item_lots(api, db, item_id, item_name, region)
                await asyncio.sleep(API_SLEEP)
            if fetch_history:
                sales_total += await ingest_item_history(api, db, item_id, item_name, region)
                await asyncio.sleep(API_SLEEP)
        except Exception as exc:
            log.error("Error processing %s: %s", item_name, exc)
            errors += 1
            await asyncio.sleep(API_SLEEP * 2)

    db.set_meta("live_data_ingested", "true")
    db.set_meta("last_ingestion", str(int(time.time())))
    return {"lots_recorded":lots_total,"sales_recorded":sales_total,"items_processed":len(tracked_items),"errors":errors}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--lots-only", action="store_true")
    parser.add_argument("--history-only", action="store_true")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()
    print(asyncio.run(ingest_live_data(region=args.region, fetch_lots=not args.history_only, fetch_history=not args.lots_only)))
