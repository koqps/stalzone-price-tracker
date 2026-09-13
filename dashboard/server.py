"""FastAPI backend for the NA STALZONE artifact market dashboard.

The dashboard distinguishes observed official market measurements from model
estimates. Artifact metadata (images, classes and stat ranges) is sourced from
EXBO-Studio/stalzone-database.
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from artifact_catalog import QUALITY_NAMES, get_artifact_metadata, load_artifact_catalog
from market_db import MarketDB

app = FastAPI(title="StalZone Price Tracker")
db = MarketDB()

QUALITY_COLORS = {
    0: "#8b8f86",
    1: "#79b84b",
    2: "#4f98d1",
    3: "#9a63d8",
    4: "#e6a33c",
    5: "#e05252",
}


def _median(values: list[float]) -> float | None:
    values = [float(v) for v in values if v is not None and float(v) > 0]
    return round(statistics.median(values), 2) if values else None


def _mean(values: list[float]) -> float | None:
    values = [float(v) for v in values if v is not None and float(v) > 0]
    return round(statistics.fmean(values), 2) if values else None


@app.get("/api/quality-tiers")
def api_quality_tiers() -> list[dict]:
    """All auction quality tiers, including Legendary (qlt=5)."""
    return [
        {"qlt": qlt, "name": name, "color": QUALITY_COLORS[qlt]}
        for qlt, name in QUALITY_NAMES.items()
    ]


@app.get("/api/market")
def api_market(region: str = "na", live_minutes: int = 20, sale_days: int = 7) -> list[dict]:
    """Return observed market facts grouped by artifact and quality tier.

    live_* fields come directly from recently observed official auction lots.
    sale_* fields come directly from official completed-sale history. Model
    estimates are returned separately and clearly named model_*.
    """
    now = time.time()
    live_since = now - max(5, live_minutes) * 60
    sale_since = now - max(1, sale_days) * 86400

    with db._conn() as conn:
        snapshots = conn.execute(
            "SELECT item_id,item_name,qlt,unit_price,observed_at FROM auction_snapshot "
            "WHERE region=? AND observed_at>=? AND unit_price>0",
            (region, live_since),
        ).fetchall()
        sales = conn.execute(
            "SELECT item_id,item_name,qlt,unit_price,observed_at FROM sale_observation "
            "WHERE region=? AND observed_at>=? AND unit_price>0 AND source='official_history'",
            (region, sale_since),
        ).fetchall()
        valuations = conn.execute(
            "SELECT v.* FROM valuation_report v JOIN ("
            " SELECT item_id,qlt,bonus_bucket,MAX(computed_at) mx FROM valuation_report "
            " WHERE region=? GROUP BY item_id,qlt,bonus_bucket"
            ") x ON v.item_id=x.item_id AND v.qlt=x.qlt "
            "AND v.bonus_bucket=x.bonus_bucket AND v.computed_at=x.mx "
            "WHERE v.region=?",
            (region, region),
        ).fetchall()

    grouped: dict[tuple[str, int], dict] = {}

    def ensure(item_id: str, item_name: str, qlt: int) -> dict:
        key = (item_id, int(qlt))
        if key not in grouped:
            grouped[key] = {
                "item_id": item_id,
                "item_name": item_name or item_id,
                "qlt": int(qlt),
                "qlt_name": QUALITY_NAMES.get(int(qlt), f"Q{qlt}"),
                "tier_color": QUALITY_COLORS.get(int(qlt), "#8b8f86"),
                "_live": [],
                "_sales": [],
                "latest_observation": 0.0,
                "latest_sale": 0.0,
            }
        return grouped[key]

    for r in snapshots:
        g = ensure(r["item_id"], r["item_name"], r["qlt"])
        g["_live"].append(r["unit_price"])
        g["latest_observation"] = max(g["latest_observation"], r["observed_at"] or 0)

    for r in sales:
        g = ensure(r["item_id"], r["item_name"], r["qlt"])
        g["_sales"].append(r["unit_price"])
        g["latest_sale"] = max(g["latest_sale"], r["observed_at"] or 0)

    # Keep model-only rows visible, but never present them as observed prices.
    for v in valuations:
        g = ensure(v["item_id"], v["item_name"], v["qlt"])
        current = g.get("_valuation")
        if current is None or (v["confidence"] or 0) > (current["confidence"] or 0):
            g["_valuation"] = v

    result: list[dict] = []
    for g in grouped.values():
        live = g.pop("_live")
        sales7 = g.pop("_sales")
        valuation = g.pop("_valuation", None)
        row = {
            **g,
            "live_floor": round(min(live), 2) if live else None,
            "live_median": _median(live),
            "live_listings": len(live),
            "sale_median": _median(sales7),
            "sale_average": _mean(sales7),
            "sale_count": len(sales7),
            "model_fair_value": valuation["fair_value_price"] if valuation else None,
            "model_quick_sale": valuation["quick_sale_price"] if valuation else None,
            "model_stretch": valuation["stretch_price"] if valuation else None,
            "model_confidence": valuation["confidence"] if valuation else None,
            "price_source": "official_na_auction",
        }
        result.append(row)

    result.sort(key=lambda r: (r["item_name"].lower(), r["qlt"]))
    return result


@app.get("/api/artifacts")
async def api_artifacts() -> list[dict]:
    catalog = await load_artifact_catalog()
    return sorted(catalog.values(), key=lambda x: x["item_name"].lower())


@app.get("/api/artifacts/{item_id}")
async def api_artifact(item_id: str) -> dict:
    item = await get_artifact_metadata(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return item


@app.get("/api/valuations")
def api_valuations(region: str = "na") -> list[dict]:
    """Legacy model-estimate endpoint kept for chart/history compatibility."""
    return [dict(r) for r in db.latest_valuations(region)]


@app.get("/api/history/{item_id}")
def api_history(item_id: str, region: str = "na", qlt: int = 3,
                bucket: int = 0, days: int = 30) -> list[dict]:
    return [dict(r) for r in db.valuation_history(item_id, region, qlt, bucket, days)]


@app.get("/api/community")
def api_community(region: str = "na", days: int = 7) -> list[dict]:
    since = time.time() - days * 86400
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM community_signal WHERE region=? AND collected_at>=? "
            "ORDER BY collected_at DESC LIMIT 200", (region, since),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/alerts")
def api_alerts(region: str = "na", days: int = 7) -> list[dict]:
    return [dict(r) for r in db.recent_alerts(region, days)]


@app.get("/api/summary")
def api_summary(region: str = "na") -> dict:
    with db._conn() as conn:
        snap_count = conn.execute(
            "SELECT COUNT(*) FROM auction_snapshot WHERE region=?", (region,)
        ).fetchone()[0]
        sale_count = conn.execute(
            "SELECT COUNT(*) FROM sale_observation WHERE region=? AND source='official_history'", (region,)
        ).fetchone()[0]
        total_items = conn.execute(
            "SELECT COUNT(DISTINCT item_id) FROM ("
            " SELECT item_id FROM auction_snapshot WHERE region=? UNION "
            " SELECT item_id FROM sale_observation WHERE region=? AND source='official_history'"
            ")", (region, region),
        ).fetchone()[0]
        total_alerts = conn.execute(
            "SELECT COUNT(*) FROM price_deviation_alert WHERE region=? AND created_at>=?",
            (region, time.time() - 7 * 86400),
        ).fetchone()[0]
        avg_conf = conn.execute(
            "SELECT AVG(confidence) FROM valuation_report WHERE region=? AND computed_at>=?",
            (region, time.time() - 2 * 86400),
        ).fetchone()[0] or 0
        tier_rows = conn.execute(
            "SELECT qlt,COUNT(DISTINCT item_id) cnt FROM ("
            " SELECT item_id,qlt FROM auction_snapshot WHERE region=? UNION "
            " SELECT item_id,qlt FROM sale_observation WHERE region=? AND source='official_history'"
            ") GROUP BY qlt ORDER BY qlt", (region, region),
        ).fetchall()

    return {
        "total_items": total_items,
        "total_alerts": total_alerts,
        "avg_confidence": round(avg_conf, 1),
        "tier_breakdown": {str(r["qlt"]): r["cnt"] for r in tier_rows},
        "snapshots": snap_count,
        "sales": sale_count,
        "data_source": "official_na_live" if db.get_meta("live_data_ingested") == "true" else "waiting",
        "last_ingestion": db.get_meta("last_ingestion", ""),
    }


@app.api_route("/api/seed", methods=["GET", "POST"])
def api_seed() -> dict:
    """Production safety: never replace live observations with invented demo data."""
    if os.getenv("ALLOW_SAMPLE_SEED", "false").lower() != "true":
        raise HTTPException(status_code=403, detail="Sample seeding is disabled")
    raise HTTPException(status_code=410, detail="Sample seeding was removed from the live tracker")


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/app.js")
def legacy_app_js() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "app.js"), media_type="application/javascript")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "source": "official_na_auction"}
