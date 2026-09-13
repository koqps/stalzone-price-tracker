"""Live exact-level auction opportunities for the dashboard.

Every candidate is compared only with the same artifact, rarity, and exact
upgrade level. Expected profit includes the configured auction sale tax.
"""
from __future__ import annotations

import os
import statistics
import time
from collections import defaultdict

from fastapi import APIRouter, Query

from market_db import MarketDB

router = APIRouter()
db = MarketDB()
TAX_RATE = max(0.0, min(0.25, float(os.getenv("AUCTION_TAX_RATE", "0.05"))))


def _median(values):
    vals = [float(v) for v in values if v is not None and float(v) > 0]
    return statistics.median(vals) if vals else None


def _confidence(sale_count: int, comparable_count: int, patch_risk: bool) -> str:
    if sale_count >= 15 and comparable_count >= 2:
        value = "high"
    elif sale_count >= 5 or comparable_count >= 2:
        value = "medium"
    else:
        value = "low"
    if patch_risk:
        return {"high": "medium", "medium": "low", "low": "low"}[value]
    return value


@router.get("/api/opportunities")
def opportunities(
    region: str = "na",
    sale_days: int = Query(7, ge=1, le=30),
    min_profit: float = Query(0, ge=0),
    min_roi: float = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1500),
):
    now = time.time()
    # Twenty minutes is only the candidate pool. Per exact market we then keep
    # rows from its most recent scan window, so old lots are not called current.
    snapshot_since = now - 20 * 60
    sale_since = now - sale_days * 86400
    with db._conn() as conn:
        snapshots = conn.execute(
            "SELECT id,item_id,item_name,qlt,upgrade_level,amount,buyout_price,unit_price,lot_key,observed_at "
            "FROM auction_snapshot WHERE region=? AND observed_at>=? AND unit_price>0 "
            "AND upgrade_level BETWEEN 0 AND 15",
            (region, snapshot_since),
        ).fetchall()
        sales = conn.execute(
            "SELECT item_id,qlt,upgrade_level,unit_price FROM sale_observation "
            "WHERE region=? AND observed_at>=? AND unit_price>0 AND source='official_history' "
            "AND upgrade_level BETWEEN 0 AND 15",
            (region, sale_since),
        ).fetchall()
        patches = conn.execute(
            "SELECT item_id,title,published_at FROM patch_signal WHERE published_at>=? ORDER BY published_at DESC",
            (now - 14 * 86400,),
        ).fetchall()

    by_market = defaultdict(list)
    for row in snapshots:
        by_market[(row["item_id"], int(row["qlt"]), int(row["upgrade_level"]))].append(row)

    # Keep only the most recent scan slice for each exact market and dedupe the
    # same lot_key. This is a better approximation of lots that are still live.
    current_markets = {}
    for key, rows in by_market.items():
        latest_ts = max(float(r["observed_at"] or 0) for r in rows)
        recent = [r for r in rows if float(r["observed_at"] or 0) >= latest_ts - 150]
        deduped = {}
        for r in recent:
            lot_key = r["lot_key"] or f"row:{r['id']}"
            old = deduped.get(lot_key)
            if old is None or float(r["observed_at"] or 0) > float(old["observed_at"] or 0):
                deduped[lot_key] = r
        current_markets[key] = list(deduped.values())

    sale_groups = defaultdict(list)
    for row in sales:
        sale_groups[(row["item_id"], int(row["qlt"]), int(row["upgrade_level"]))].append(float(row["unit_price"]))

    patch_items = {str(r["item_id"]) for r in patches if r["item_id"]}
    results = []
    for key, lots in current_markets.items():
        item_id, qlt, level = key
        sold = sale_groups.get(key, [])
        sale_median = _median(sold)
        sale_count = len(sold)
        ordered = sorted(lots, key=lambda r: float(r["unit_price"] or 0))

        for candidate in ordered:
            buy = float(candidate["unit_price"] or 0)
            if buy <= 0:
                continue
            other_prices = [float(r["unit_price"]) for r in ordered if r is not candidate and float(r["unit_price"] or 0) > 0]
            next_floor = min(other_prices) if other_prices else None

            anchors = []
            evidence = []
            if sale_median and sale_count >= 2:
                # Slight haircut makes the resale estimate more conservative
                # than blindly assuming the historical median.
                anchors.append(sale_median * (0.97 if sale_count >= 5 else 0.94))
                evidence.append(f"{sale_count} official {sale_days}d sales")
            if next_floor:
                anchors.append(next_floor * 0.995)
                evidence.append(f"{len(other_prices)} current comparables")
            if not anchors:
                continue

            resale_target = min(anchors)
            net_proceeds = resale_target * (1.0 - TAX_RATE)
            profit = net_proceeds - buy
            roi = (profit / buy * 100.0) if buy else 0.0
            if profit <= 0 or profit < min_profit or roi < min_roi:
                continue

            patch_risk = item_id in patch_items
            confidence = _confidence(sale_count, len(other_prices), patch_risk)
            if patch_risk:
                evidence.append("recent official patch signal")

            observed_at = float(candidate["observed_at"] or 0)
            results.append({
                "lot_key": candidate["lot_key"],
                "item_id": item_id,
                "item_name": candidate["item_name"] or item_id,
                "qlt": qlt,
                "upgrade_level": level,
                "amount": int(candidate["amount"] or 1),
                "buy_price": round(buy, 2),
                "resale_target": round(resale_target, 2),
                "net_proceeds": round(net_proceeds, 2),
                "estimated_profit": round(profit, 2),
                "roi_pct": round(roi, 2),
                "sale_median": round(sale_median, 2) if sale_median else None,
                "sale_count": sale_count,
                "next_live_price": round(next_floor, 2) if next_floor else None,
                "live_comparables": len(other_prices),
                "confidence": confidence,
                "patch_risk": patch_risk,
                "tax_rate": TAX_RATE,
                "observed_at": observed_at,
                "age_seconds": max(0, round(now - observed_at)),
                "evidence": "; ".join(evidence),
                "source": "official_na_auction",
            })

    rank = {"high": 3, "medium": 2, "low": 1}
    results.sort(key=lambda r: (rank.get(r["confidence"], 0), r["estimated_profit"], r["roi_pct"]), reverse=True)
    for i, row in enumerate(results[:limit], start=1):
        row["rank"] = i
    return results[:limit]
