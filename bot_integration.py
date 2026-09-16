"""Bridge between the Discord bot and exact-level market intelligence.

The public bot.py API is kept stable, but the second value returned by
``get_lot_quality`` is an exact ptn/+level compatibility key (+N -> N/100).
Internally every comparison is made with the explicit integer ptn/+level, so
+0..+15 never share evidence.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from market_db import MarketDB
from market_variants import extract_variant
from price_model import compute_valuation, ValuationResult

log = logging.getLogger("bot_integration")
REGION = os.getenv("REGION", "na").lower()
SALE_RECENCY_DAYS = int(os.getenv("SALE_RECENCY_DAYS", "14"))
ALERT_DEDUPE_MINUTES = int(os.getenv("ALERT_DEDUPE_MINUTES", "30"))
LIVE_SNAPSHOT_MINUTES = int(os.getenv("LIVE_SNAPSHOT_MINUTES", "20"))
_valuation_cache: dict[tuple[str, str, int, int], tuple[float, ValuationResult]] = {}


def _level_from_key(value: float | int | None) -> int | None:
    if value is None:
        return None
    try:
        level = int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None
    return level if 0 <= level <= 15 else None


def _level_bucket(level: int | None) -> int:
    return int(level) * 10 if level is not None else -1


def _raw_bonus(additional: dict | None) -> float:
    if not isinstance(additional, dict):
        return 0.0
    raw = additional.get("upgrade_bonus")
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("amount"))
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def get_lot_quality(lot) -> tuple[int | None, float | None]:
    qlt, level, key = extract_variant(getattr(lot, "additional", None))
    if qlt is None or level is None:
        return qlt, None
    return qlt, key


def lot_key(item_id: str, lot) -> str:
    buyout_price = getattr(lot, "buyout_price", 0) or 0
    amount = getattr(lot, "amount", 1) or 1
    start_time = getattr(lot, "start_time", None)
    start_str = start_time.isoformat() if hasattr(start_time, "isoformat") else str(start_time)
    return f"{item_id}_{start_str}_{buyout_price}_{amount}"


def _matching_comparables(lots: list, item_id: str, qlt: int, upgrade_bonus: float | None, lot_to_skip=None) -> list[tuple[float, float]]:
    target_level = _level_from_key(upgrade_bonus)
    if target_level is None:
        return []
    skip_key = id(lot_to_skip) if lot_to_skip is not None else None
    comparables: list[tuple[float, float]] = []
    for lot in lots:
        if id(lot) == skip_key:
            continue
        lot_qlt, lot_key_value = get_lot_quality(lot)
        if lot_qlt != qlt or _level_from_key(lot_key_value) != target_level:
            continue
        buyout = getattr(lot, "buyout_price", 0) or 0
        amount = getattr(lot, "amount", 1) or 1
        if buyout > 0:
            comparables.append((buyout / max(amount, 1), amount))
    comparables.sort(key=lambda x: x[0])
    return comparables


def has_cheaper_comparable_listing(lots, item_id, qlt, upgrade_bonus, current_lot) -> bool:
    comparables = _matching_comparables(lots, item_id, qlt, upgrade_bonus, current_lot)
    if not comparables:
        return False
    buyout = getattr(current_lot, "buyout_price", 0) or 0
    amount = getattr(current_lot, "amount", 1) or 1
    return comparables[0][0] < buyout / max(amount, 1)


def live_resale_cap_per_unit(lots, item_id, qlt, upgrade_bonus, current_lot=None) -> tuple[float | None, int]:
    comparables = _matching_comparables(lots, item_id, qlt, upgrade_bonus, current_lot)
    return (comparables[0][0], len(comparables)) if comparables else (None, 0)


async def record_observations(db: MarketDB, item_id: str, item_name: str, lots: list, region: str = None) -> int:
    region = region or REGION
    with db._conn() as conn:
        conn.execute("DELETE FROM auction_snapshot WHERE item_id=? AND region=?", (item_id, region))
    recorded = 0
    for lot in lots:
        qlt, level_key = get_lot_quality(lot)
        level = _level_from_key(level_key)
        if qlt is None or level is None:
            continue
        buyout = getattr(lot, "buyout_price", 0) or 0
        amount = getattr(lot, "amount", 1) or 1
        if buyout <= 0:
            continue
        additional = getattr(lot, "additional", None)
        db.record_snapshot(
            item_id=item_id,
            item_name=item_name,
            region=region,
            qlt=qlt,
            ptn=level,
            upgrade_level=level,
            bonus=_raw_bonus(additional),
            bonus_bucket=_level_bucket(level),
            amount=amount,
            buyout_price=buyout,
            unit_price=buyout / max(amount, 1),
            lot_key=lot_key(item_id, lot),
        )
        recorded += 1
    return recorded


async def record_sale(db: MarketDB, item_id: str, item_name: str, qlt: int, upgrade_bonus: float | None, unit_price: float, amount: int = 1, region: str = None, source: str = "inferred_buyout_sale", confidence: float = 0.60):
    region = region or REGION
    level = _level_from_key(upgrade_bonus)
    if level is None:
        return
    db.record_sale(
        item_id=item_id,
        item_name=item_name,
        region=region,
        qlt=qlt,
        ptn=level,
        upgrade_level=level,
        bonus_bucket=_level_bucket(level),
        unit_price=unit_price,
        amount=amount,
        source=source,
        confidence=confidence,
    )


def _recent_patch_context(db: MarketDB, item_id: str, item_name: str, days: int = 14) -> tuple[float, str | None]:
    try:
        rows = db.recent_patch_signals(days=days, limit=100)
    except Exception:
        return 0.0, None
    name = (item_name or "").casefold()
    matches = []
    for row in rows:
        summary = str(row["summary"] or "")
        same_id = bool(row["item_id"] and row["item_id"] == item_id)
        mentioned = bool(name and name in summary.casefold())
        if same_id or mentioned:
            matches.append(row)
    if not matches:
        return 0.0, None
    newest = matches[0]
    direction = str(newest["impact_direction"] or "uncertain")
    title = str(newest["title"] or "Official patch")
    return 1.0, f"{title}: {direction.replace('_', ' ')}; market may still be repricing"


async def evaluate_lot_with_model(db: MarketDB, item_id: str, item_name: str, qlt: int, upgrade_bonus: float | None, region: str = None) -> ValuationResult:
    region = region or REGION
    level = _level_from_key(upgrade_bonus)
    if level is None:
        return compute_valuation(item_id, item_name, region, qlt, -1)

    now = time.time()
    cache_key = (item_id, region, int(qlt), int(level))
    cached = _valuation_cache.get(cache_key)
    if cached and now - cached[0] < 90:
        return cached[1]

    since = now - SALE_RECENCY_DAYS * 86400
    live_since = now - max(5, LIVE_SNAPSHOT_MINUTES) * 60
    live_rows = db.latest_snapshot(item_id, region, live_since, qlt=qlt, upgrade_level=level)
    live_prices = [r["unit_price"] for r in live_rows if r["unit_price"]]
    sale_rows = db.recent_sales(item_id, region, qlt, _level_bucket(level), since, upgrade_level=level)
    confirmed = [r["unit_price"] for r in sale_rows if r["source"] == "confirmed_bid_sale"]
    inferred = [r["unit_price"] for r in sale_rows if r["source"] == "inferred_buyout_sale"]
    official = [r["unit_price"] for r in sale_rows if r["source"] == "official_history"]
    community_rows = db.recent_community_signals(item_name, region, since)
    community_signals = [{"sentiment_score": r["sentiment_score"], "confidence": r["confidence"]} for r in community_rows]
    manual = db.manual_price(item_id, region, qlt, float(level) / 100.0, upgrade_level=level)

    result = compute_valuation(
        item_id=item_id,
        item_name=item_name,
        region=region,
        qlt=qlt,
        bonus_bucket=_level_bucket(level),
        live_comparable_unit_prices=live_prices,
        confirmed_sale_prices=confirmed,
        inferred_sale_prices=inferred,
        official_history_prices=official,
        quality_multiplier=1.0,
        community_signals=community_signals,
        manual_override=dict(manual) if manual else None,
    )

    patch_risk, patch_context = _recent_patch_context(db, item_id, item_name)
    if patch_risk:
        result.confidence = max(0, result.confidence - 10)
        if patch_context:
            result.evidence.append(patch_context)

    row = result.as_row()
    row["upgrade_level"] = level
    row["bonus_bucket"] = _level_bucket(level)
    row["patch_risk"] = patch_risk
    db.record_valuation(**row)
    _valuation_cache[cache_key] = (now, result)
    if len(_valuation_cache) > 5000:
        cutoff = now - 300
        for key, (ts, _) in list(_valuation_cache.items()):
            if ts < cutoff:
                _valuation_cache.pop(key, None)
    return result


def _alert_recently_recorded(db: MarketDB, item_id: str, region: str, qlt: int, level: int, observed_price: float) -> bool:
    cutoff = time.time() - max(1, ALERT_DEDUPE_MINUTES) * 60
    with db._conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM price_deviation_alert WHERE item_id=? AND region=? AND qlt=? AND upgrade_level=? AND observed_price=? AND created_at>=? LIMIT 1",
            (item_id, region, qlt, level, observed_price, cutoff),
        ).fetchone()
    return bool(row)


async def should_alert_lot(db: MarketDB, item_id: str, item_name: str, qlt: int, upgrade_bonus: float | None, lots: list, current_lot, min_confidence: int = 65, min_margin_pct: float = 15.0) -> tuple[bool, ValuationResult | None, str]:
    level = _level_from_key(upgrade_bonus)
    if level is None:
        return False, None, "Exact enhancement level unavailable"
    if has_cheaper_comparable_listing(lots, item_id, qlt, upgrade_bonus, current_lot):
        return False, None, "Cheaper exact-level comparable listing exists"

    cap, comparable_count = live_resale_cap_per_unit(lots, item_id, qlt, upgrade_bonus, current_lot)
    result = await evaluate_lot_with_model(db, item_id, item_name, qlt, upgrade_bonus, region=REGION)
    if result.confidence < min_confidence:
        return False, result, f"Confidence {result.confidence} below threshold {min_confidence}"

    buyout = getattr(current_lot, "buyout_price", 0) or 0
    amount = getattr(current_lot, "amount", 1) or 1
    unit_price = buyout / max(amount, 1)
    if not result.fair_value or result.fair_value <= 0:
        return False, result, "Fair value unavailable"

    margin_pct = ((result.fair_value - unit_price) / result.fair_value) * 100
    if margin_pct < min_margin_pct:
        return False, result, f"Margin {margin_pct:.1f}% below threshold {min_margin_pct}%"
    if cap is not None and unit_price >= cap:
        return False, result, f"Unit price {unit_price:,.0f} >= exact-level live cap {cap:,.0f}"
    if _alert_recently_recorded(db, item_id, REGION, qlt, level, unit_price):
        return False, result, "Same profitable listing was already alerted recently"

    db.record_alert(
        item_id=item_id,
        item_name=item_name,
        region=REGION,
        qlt=qlt,
        upgrade_level=level,
        bonus_bucket=_level_bucket(level),
        alert_type="profitable_listing",
        baseline_price=result.fair_value,
        observed_price=unit_price,
        deviation_pct=margin_pct,
        severity="high" if margin_pct >= 30 else "watch",
        message=(f"{item_name} +{level}: {unit_price:,.0f} vs fair {result.fair_value:,.0f}; {margin_pct:.1f}% modeled margin, confidence {result.confidence}"),
    )
    return True, result, f"Profitable: {margin_pct:.1f}% margin, confidence {result.confidence}, {comparable_count} exact-level comparables"
