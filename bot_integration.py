"""
bot_integration.py — Bridge between the existing StalZone Discord bot and the
new price-tracking model.
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
    """Generate a unique lot key."""
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
    """Find live comparable listings for the same (item, qlt, bonus_bucket)."""
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
    """True only if another comparable lot is cheaper than the candidate."""
    comparables = _matching_comparables(lots, item_id, qlt, upgrade_bonus, current_lot)
    if not comparables:
        return False

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
    """Return (cheapest comparable unit price, count) excluding the candidate."""
    comparables = _matching_comparables(lots, item_id, qlt, upgrade_bonus, current_lot)
    if not comparables:
        return None, 0
    return comparables[0][0], len(comparables)


# ─── NEW: Record observations to MarketDB ───────────────────────────────────

async def record_observations(
    db: MarketDB,
    item_id: str,
    item_name: str,
    lots: list,
    region: str = None,
) -> int:
    """Record active lot snapshots into MarketDB using async database execution."""
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

        if hasattr(db, "record_snapshot") and callable(getattr(db, "record_snapshot")):
            res = db.record_snapshot(
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
            if hasattr(res, "__await__"):
                await res
        recorded += 1

    return recorded


async def record_sale(
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
    """Record a completed sale into MarketDB asynchronously."""
    region = region or REGION
    if hasattr(db, "record_sale") and callable(getattr(db, "record_sale")):
        res = db.record_sale(
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
        if hasattr(res, "__await__"):
            await res


# ─── NEW: Price model integration ───────────────────────────────────────────

async def evaluate_lot_with_model(
    db: MarketDB,
    item_id: str,
    item_name: str,
    qlt: int,
    upgrade_bonus: float | None,
    region: str = None,
) -> ValuationResult:
    """Use the confidence-weighted price model to evaluate an artifact."""
    region = region or REGION
    res = compute_valuation(
        db=db,
        item_id=item_id,
        item_name=item_name,
        qlt=qlt,
        upgrade_bonus=upgrade_bonus or 0.0,
        region=region,
    )
    if hasattr(res, "__await__"):
        res = await res
    return res


async def should_alert_lot(
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
    """Decide whether to send a Discord alert for a lot asynchronously."""
    if has_cheaper_comparable_listing(lots, item_id, qlt, upgrade_bonus, current_lot):
        return False, None, "Cheaper comparable listing exists — not a flip opportunity"

    cap, comparable_count = live_resale_cap_per_unit(lots, item_id, qlt, upgrade_bonus, current_lot)

    result = await evaluate_lot_with_model(db, item_id, item_name, qlt, upgrade_bonus, region=REGION)

    if result.confidence < min_confidence:
        return False, result, f"Confidence {result.confidence} below threshold {min_confidence}"

    buyout = getattr(current_lot, "buyout_price", 0) or 0
    amount = getattr(current_lot, "amount", 1) or 1
    unit_price = buyout / max(amount, 1)

    if result.fair_value <= 0:
        return False, result, "Fair value is zero"

    margin_pct = ((result.fair_value - unit_price) / result.fair_value) * 100
    if margin_pct < min_margin_pct:
        return False, result, f"Margin {margin_pct:.1f}% below threshold {min_margin_pct}%"

    if cap is not None and unit_price >= cap:
        return False, result, f"Unit price {unit_price:,.0f} >= live cap {cap:,.0f}"

    return True, result, f"Profitable: {margin_pct:.1f}% margin, confidence {result.confidence}, {comparable_count} comparables"
