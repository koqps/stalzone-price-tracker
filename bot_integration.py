"""
bot_integration.py — Bridge between the existing StalZone Discord bot and the
new price-tracking model.

This module provides drop-in replacements for the bot's broken functions:

  get_lot_quality()     — FIXED: returns None for unknown quality instead of
                          silently defaulting to Common (which was hiding
                          Rare, Exclusive, and Legendary artifacts)

  lot_key()             — FIXED: uses a more unique key to avoid lot identity
                          collisions that were inflating resale volume

  has_cheaper_comparable_listing()  — NEW: checks live auction comparables
                                      before the bot claims "big profit"

  live_resale_cap_per_unit()        — NEW: returns the cheapest live comparable
                                      for a given (item, qlt, bonus_bucket)

  record_observations()  — NEW: writes snapshots + sales from the bot's scan
                           loop into MarketDB so the price model has live data

  evaluate_lot_with_model() — NEW: uses the confidence-weighted price model
                               instead of static multipliers

Integration into your existing bot:

    from bot_integration import (
        get_lot_quality,
        lot_key,
        has_cheaper_comparable_listing,
        live_resale_cap_per_unit,
        record_observations,
        evaluate_lot_with_model,
        MarketDB,
    )

    # In your scan_item() function, after fetching lots:
    pricedb = MarketDB()  # or pass an existing instance

    for lot in lots:
        qlt, bonus = get_lot_quality(lot)
        if qlt is None:
            continue  # skip lots where quality can't be determined

        # Check live comparables BEFORE claiming profit
        if has_cheaper_comparable_listing(lots, item_id, qlt, bonus, lot):
            continue  # a cheaper comparable exists — not a real flip opportunity

        cap, _ = live_resale_cap_per_unit(lots, item_id, qlt, bonus, lot)
        if cap is not None and cap < unit_price:
            continue  # this lot is more expensive than the cheapest comparable

        # Use the price model for valuation
        result = evaluate_lot_with_model(pricedb, item_id, item_name, qlt, bonus)
        # result has .fair_value, .quick_sale, .stretch, .confidence
"""
from __future__ import annotations

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
from price_model import compute_valuation, ValuationResult

log = logging.getLogger("bot_integration")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

REGION = os.getenv("REGION", "na").lower()

# ─── FIXED: Quality detection ───────────────────────────────────────────────

QUALITY_NAMES = {
    0: "Common",
    1: "Uncommon",
    2: "Special",
    3: "Rare",
    4: "Exclusive",
    5: "Legendary",
}


def get_lot_quality(lot) -> tuple[int | None, float | None]:
    """Return (quality_tier, upgrade_bonus) for a lot, or (None, None) if
    the quality cannot be determined.

    CRITICAL FIX: The old version silently returned qlt=0 (Common) whenever it
    couldn't parse the metadata.  That caused every Rare, Exclusive, and
    Legendary artifact to be recorded as "Common" in the database, making the
    per-tier valuation model completely blind to high-tier items.

    Now it returns None for unknown quality, and the scan loop must skip those
    lots instead of treating them as Common.
    """
    additional = getattr(lot, "additional", None)
    if not additional or not isinstance(additional, dict):
        return None, None

    qlt = additional.get("qlt")
    if qlt is None:
        qlt = additional.get("quality")

    if qlt is not None:
        try:
            qlt = int(qlt)
            if not 0 <= qlt <= 5:
                log.warning("Unexpected quality tier %d — clamping to 0-5", qlt)
                qlt = max(0, min(5, qlt))
        except (ValueError, TypeError):
            qlt = None
    else:
        # If additional has no quality info at all, return None — do NOT
        # default to Common.  This is the core fix for Rare/Exclusive/Legendary.
        return None, None

    upgrade_bonus = additional.get("upgrade_bonus")
    if isinstance(upgrade_bonus, dict):
        upgrade_bonus = upgrade_bonus.get("value", upgrade_bonus.get("amount"))
    if upgrade_bonus is not None:
        try:
            upgrade_bonus = float(upgrade_bonus)
        except (ValueError, TypeError):
            upgrade_bonus = None
    else:
        upgrade_bonus = 0.0

    return qlt, upgrade_bonus


# ─── FIXED: Lot identity key ────────────────────────────────────────────────

def lot_key(item_id: str, lot) -> str:
    """Generate a unique lot key.

    FIX: The old _lot_key only used item_id + buyout_price + amount, which
    collided when two identical-price lots existed simultaneously.  Now uses
    start_time for uniqueness.
    """
    buyout_price = getattr(lot, "buyout_price", 0) or 0
    amount = getattr(lot, "amount", 1) or 1
    start_time = getattr(lot, "start_time", None)
    start_str = start_time.isoformat() if hasattr(start_time, "isoformat") else str(start_time)
    return f"{item_id}_{start_str}_{buyout_price}_{amount}"


# ─── NEW: Live comparable listing checks ────────────────────────────────────

def _matching_comparables(
    lots: list,
    item_id: str,
    qlt: int,
    upgrade_bonus: float | None,
    lot_to_skip=None,
) -> list[tuple[float, float]]:
    """Find live comparable listings for the same (item, qlt, bonus_bucket).

    Returns a list of (unit_price, amount) tuples for lots that match the
    same quality tier and bonus bucket, sorted by unit_price ascending.
    Excludes lot_to_skip (the lot being evaluated).
    """
    target_bucket = bonus_bucket(upgrade_bonus)
    skip_key = id(lot_to_skip) if lot_to_skip is not None else None
    comparables: list[tuple[float, float]] = []

    for lot in lots:
        if id(lot) == skip_key:
            continue
        lot_qlt, lot_bonus = get_lot_quality(lot)
        if lot_qlt != qlt:
            continue
        if bonus_bucket(lot_bonus) != target_bucket:
            continue
        buyout = getattr(lot, "buyout_price", 0) or 0
        amount = getattr(lot, "amount", 1) or 1
        if buyout <= 0:
            continue
        unit_price = buyout / max(amount, 1)
        comparables.append((unit_price, amount))

    comparables.sort(key=lambda x: x[0])
    return comparables


def has_cheaper_comparable_listing(
    lots: list,
    item_id: str,
    qlt: int,
    upgrade_bonus: float | None,
    current_lot,
) -> bool:
    """True only if another comparable lot is cheaper than the candidate.

    Returns False when there are no comparables (can't prove it's cheaper).
    This is the guard that prevents the bot from claiming "huge profit" on an
    item when other players are listing the exact same artifact for less.
    """
    comparables = _matching_comparables(lots, item_id, qlt, upgrade_bonus, current_lot)
    if not comparables:
        return False  # No comparables — can't prove cheaper exists

    buyout = getattr(current_lot, "buyout_price", 0) or 0
    amount = getattr(current_lot, "amount", 1) or 1
    current_unit = buyout / max(amount, 1)

    cheapest = comparables[0][0]
    return cheapest < current_unit


def live_resale_cap_per_unit(
    lots: list,
    item_id: str,
    qlt: int,
    upgrade_bonus: float | None,
    current_lot=None,
) -> tuple[float | None, int]:
    """Return (cheapest comparable unit price, count) excluding the candidate.

    This is the hard ceiling: the bot should never claim a resale value above
    this number, because a buyer can always get the same artifact cheaper
    from another listing.  Returns (None, 0) when no comparables exist.
    """
    comparables = _matching_comparables(lots, item_id, qlt, upgrade_bonus, current_lot)
    if not comparables:
        return None, 0
    return comparables[0][0], len(comparables)


# ─── NEW: Record observations to MarketDB ───────────────────────────────────

def record_observations(
    db: MarketDB,
    item_id: str,
    item_name: str,
    lots: list,
    region: str = None,
) -> int:
    """Record active lot snapshots from the bot's scan loop into MarketDB.

    Call this in your scan_item() function after fetching lots:

        record_observations(pricedb, item_id, item_name, lots)

    Returns the number of lots recorded.
    """
    region = region or REGION
    recorded = 0
    for lot in lots:
        qlt, upgrade_bonus = get_lot_quality(lot)
        if qlt is None:
            continue

        buyout = getattr(lot, "buyout_price", 0) or 0
        amount = getattr(lot, "amount", 1) or 1
        if buyout <= 0:
            continue

        unit_price = buyout / max(amount, 1)
        key = lot_key(item_id, lot)

        db.record_snapshot(
            item_id=item_id,
            item_name=item_name,
            region=region,
            qlt=qlt,
            bonus=upgrade_bonus or 0.0,
            bonus_bucket=bonus_bucket(upgrade_bonus),
            amount=amount,
            buyout_price=buyout,
            unit_price=unit_price,
            lot_key=key,
        )
        recorded += 1

    return recorded


def record_sale(
    db: MarketDB,
    item_id: str,
    item_name: str,
    qlt: int,
    upgrade_bonus: float | None,
    unit_price: float,
    amount: int = 1,
    region: str = None,
    source: str = "inferred_buyout_sale",
    confidence: float = 0.60,
):
    """Record a completed sale (e.g., when a lot disappears between scans).

    Call this from your background task that detects disappeared lots:

        record_sale(pricedb, item_id, item_name, qlt, bonus, unit_price, amount)
    """
    region = region or REGION
    db.record_sale(
        item_id=item_id,
        item_name=item_name,
        region=region,
        qlt=qlt,
        bonus_bucket=bonus_bucket(upgrade_bonus),
        unit_price=unit_price,
        amount=amount,
        source=source,
        confidence=confidence,
    )


# ─── NEW: Price model integration ───────────────────────────────────────────

def evaluate_lot_with_model(
    db: MarketDB,
    item_id: str,
    item_name: str,
    qlt: int,
    upgrade_bonus: float | None,
    region: str = None,
) -> ValuationResult:
    """Use the confidence-weighted price model to evaluate an artifact.

    Returns a ValuationResult with:
      .fair_value       — market-consensus price per unit
      .quick_sale       — realistic fast-resale price
      .stretch          — bullish ceiling
      .floor            — conservative minimum
      .confidence       — 0-100 confidence score
      .confidence_label — "High"/"Medium"/"Low"/"Very Low"
      .sources_used     — list of source types that contributed
    """
    region = region or REGION
    return compute_valuation(
        db=db,
        item_id=item_id,
        item_name=item_name,
        qlt=qlt,
        upgrade_bonus=upgrade_bonus or 0.0,
        region=region,
    )


def should_alert_lot(
    db: MarketDB,
    item_id: str,
    item_name: str,
    qlt: int,
    upgrade_bonus: float | None,
    lots: list,
    current_lot,
    min_confidence: int = 65,
    min_margin_pct: float = 15.0,
) -> tuple[bool, ValuationResult | None, str]:
    """Decide whether to send a Discord alert for a lot.

    Returns (should_alert, valuation_result, reason).

    This replaces the old static-multiplier logic with:
    1. Live comparable check (skip if a cheaper comparable exists)
    2. Confidence-weighted price model
    3. Confidence-gated alerts (minimum 65/100)
    4. Live resale cap as hard ceiling
    """
    # Step 1: Skip if there's a cheaper comparable listing — this lot
    # is not a flip opportunity if the same artifact is listed for less.
    if has_cheaper_comparable_listing(lots, item_id, qlt, upgrade_bonus, current_lot):
        return False, None, "Cheaper comparable listing exists — not a flip opportunity"

    # Step 2: Get the live resale cap (excludes the candidate)
    cap, comparable_count = live_resale_cap_per_unit(lots, item_id, qlt, upgrade_bonus, current_lot)

    # Step 3: Evaluate with the price model
    result = evaluate_lot_with_model(db, item_id, item_name, qlt, upgrade_bonus)

    # Step 4: Confidence gate
    if result.confidence < min_confidence:
        return False, result, f"Confidence {result.confidence} below threshold {min_confidence}"

    # Step 5: Check margin — the lot's unit price should be below fair value
    buyout = getattr(current_lot, "buyout_price", 0) or 0
    amount = getattr(current_lot, "amount", 1) or 1
    unit_price = buyout / max(amount, 1)

    if result.fair_value <= 0:
        return False, result, "Fair value is zero"

    margin_pct = ((result.fair_value - unit_price) / result.fair_value) * 100
    if margin_pct < min_margin_pct:
        return False, result, f"Margin {margin_pct:.1f}% below threshold {min_margin_pct}%"

    # Step 6: If there's a live cap, make sure the lot is below it
    if cap is not None and unit_price >= cap:
        return False, result, f"Unit price {unit_price:,.0f} >= live cap {cap:,.0f}"

    return True, result, f"Profitable: {margin_pct:.1f}% margin, confidence {result.confidence}, {comparable_count} comparables"
