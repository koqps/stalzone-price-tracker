"""
server.py — FastAPI backend for the StalZone price-tracker dashboard.

Serves valuation reports, historical price trends, community signals,
and deviation alerts from the MarketDB SQLite store. Includes a
`/api/seed` endpoint that injects realistic demo data so the dashboard
renders meaningfully on first launch before the bot's scan loop has
populated real observations.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from market_db import MarketDB, bonus_bucket

app = FastAPI(title="StalZone Price Tracker")
db = MarketDB()

# ---------------------------------------------------------------------
# Sample data seeding (for demo / first-launch)
# ---------------------------------------------------------------------
SAMPLE_ITEMS = [
    {"item_id": "firebird", "item_name": "Firebird", "qlt": 3, "base": 850_000, "vol": 0.12},
    {"item_id": "timber_uc", "item_name": "Timber", "qlt": 1, "base": 520_000, "vol": 0.08},
    {"item_id": "polyhedron", "item_name": "Polyhedron", "qlt": 4, "base": 3_200_000, "vol": 0.18},
    {"item_id": "shrimp_r", "item_name": "Shrimp", "qlt": 3, "base": 680_000, "vol": 0.10},
    {"item_id": "shell_com", "item_name": "Shell", "qlt": 0, "base": 95_000, "vol": 0.06},
    {"item_id": "compass_exc", "item_name": "Compass", "qlt": 4, "base": 2_800_000, "vol": 0.15},
    {"item_id": "junk_sp", "item_name": "Junk", "qlt": 2, "base": 310_000, "vol": 0.09},
    {"item_id": "drift_leg", "item_name": "Drift", "qlt": 5, "base": 8_500_000, "vol": 0.22},
    {"item_id": "crystal_r", "item_name": "Crystal", "qlt": 3, "base": 720_000, "vol": 0.11},
    {"item_id": "wrench_uc", "item_name": "Wrench", "qlt": 1, "base": 440_000, "vol": 0.07},
]
SAMPLE_COMMUNITY = [
    {"item_name": "Firebird", "source": "reddit", "source_name": "r/Stalcraft",
     "sentiment": "bullish", "sentiment_score": 0.4, "claimed_price": 920_000,
     "url": "https://reddit.com/r/Stalcraft", "excerpt": "Firebird prices climbing after patch"},
    {"item_name": "Timber", "source": "discord_trade", "source_name": "NA Trade #1",
     "sentiment": "bearish", "sentiment_score": -0.3, "claimed_price": 480_000,
     "url": "https://discord.gg/stalcraft-eng", "excerpt": "Timber oversupplied, dropping fast"},
    {"item_name": "Polyhedron", "source": "staldata", "source_name": "staldata.org",
     "sentiment": "bullish", "sentiment_score": 0.25, "claimed_price": 3_500_000,
     "url": "https://staldata.org", "excerpt": "Median up 8% week-over-week"},
    {"item_name": "Shrimp", "source": "youtube", "source_name": "StalkerPriceGuides",
     "sentiment": "neutral", "sentiment_score": 0.05, "claimed_price": None,
     "url": "https://youtube.com/watch?v=demo", "excerpt": "Shrimp market overview — stable demand"},
    {"item_name": "Drift", "source": "reddit", "source_name": "r/Stalcraft",
     "sentiment": "bullish", "sentiment_score": 0.6, "claimed_price": 9_200_000,
     "url": "https://reddit.com/r/Stalcraft", "excerpt": "Legendary Drift is BiS now, huge demand"},
    {"item_name": "Compass", "source": "forum", "source_name": "EXBO Forum",
     "sentiment": "bearish", "sentiment_score": -0.2, "claimed_price": 2_400_000,
     "url": "https://forum-stalcraft.net", "excerpt": "Compass nerf incoming, prices softening"},
]


def seed_sample_data() -> None:
    """Inject 30 days of realistic valuation history + community signals + alerts."""
    now = time.time()
    rng = random.Random(42)

    # Clear old sample rows (keep manual_prices)
    with db._conn() as conn:
        for t in ("auction_snapshot", "sale_observation", "community_signal",
                  "valuation_report", "price_deviation_alert"):
            conn.execute(f"DELETE FROM {t}")

    for item in SAMPLE_ITEMS:
        item_id = item["item_id"]
        name = item["item_name"]
        qlt = item["qlt"]
        base = item["base"]
        vol = item["vol"]
        bucket = bonus_bucket(0.0)

        # 30 days of valuation history with a gentle trend + noise
        trend = rng.uniform(-0.15, 0.25)  # overall drift over the month
        prev_fair = base
        for day in range(30, -1, -1):
            ts = now - day * 86400
            daily_move = rng.gauss(0, vol / 3)
            seasonal = 0.05 * math.sin(day / 4.0)  # weekly oscillation
            fair = base * (1 + trend * (30 - day) / 30 + daily_move + seasonal)
            fair = max(base * 0.5, fair)
            floor_v = fair * 0.92
            quick = fair * 0.97
            stretch = fair * 1.10
            conf = max(30, min(95, int(70 + rng.gauss(0, 12) +
                                       (10 if day < 14 else -5))))
            live_n = rng.randint(2, 9)
            conf_sales = rng.randint(3, 18)
            inf_sales = rng.randint(5, 30)
            comm_n = rng.randint(0, 6)
            comm_bias = rng.uniform(-0.4, 0.4)

            db.record_valuation(
                item_id=item_id, item_name=name, region="na", qlt=qlt,
                bonus_bucket=bucket, floor_price=round(floor_v, 2),
                quick_sale_price=round(quick, 2),
                fair_value_price=round(fair, 2),
                stretch_price=round(stretch, 2), confidence=conf,
                live_comparables=live_n, confirmed_sales=conf_sales,
                inferred_sales=inf_sales, community_signals=comm_n,
                community_bias=round(comm_bias, 3),
                evidence_summary=f"per-tier n={conf_sales+inf_sales}; live n={live_n}",
                computed_at=ts,
            )
            # also seed some sale observations
            for _ in range(rng.randint(1, 4)):
                db.record_sale(
                    item_id=item_id, item_name=name, region="na", qlt=qlt,
                    bonus_bucket=bucket,
                    unit_price=round(fair * rng.uniform(0.85, 1.15), 2),
                    amount=1,
                    source=rng.choice(["confirmed_bid_sale", "inferred_buyout_sale", "official_history"]),
                    confidence=rng.choice([1.0, 0.6, 0.35]),
                    observed_at=ts + rng.uniform(0, 86400),
                )
            prev_fair = fair

        # Seed a few auction snapshots for the most recent day
        for _ in range(rng.randint(3, 8)):
            db.record_snapshot(
                item_id=item_id, item_name=name, region="na", qlt=qlt,
                bonus=0.0, bonus_bucket=bucket, amount=1,
                buyout_price=round(base * rng.uniform(0.8, 1.2), 2),
                unit_price=round(base * rng.uniform(0.8, 1.2), 2),
                lot_key=f"{item_id}_{rng.randint(1000,9999)}",
                observed_at=now - rng.uniform(0, 86400),
            )

    # Community signals (last 7 days)
    for sig in SAMPLE_COMMUNITY:
        for day in range(7, 0, -1):
            if rng.random() < 0.4:
                db.record_community_signal(
                    item_name=sig["item_name"], region="na",
                    claimed_price=sig["claimed_price"],
                    sentiment=sig["sentiment"],
                    sentiment_score=sig["sentiment_score"] + rng.gauss(0, 0.1),
                    source=sig["source"], source_name=sig["source_name"],
                    url=sig["url"], excerpt=sig["excerpt"],
                    confidence=0.15 if sig["source"] != "staldata" else 0.20,
                    collected_at=now - day * 86400 + rng.uniform(0, 86400),
                )

    # Seed a few deviation alerts
    alert_templates = [
        ("volatility_spike", "critical", "Firebird", 3,
         "Firebird fair value moved up 24.3% (850,000 -> 1,057,000 RUB)."),
        ("history_vs_live_gap", "high", "Timber", 1,
         "Timber historical sales (520,000 RUB) are 48% above the cheapest live comparable (350,000 RUB). Estimate may be stale."),
        ("community_vs_market_gap", "watch", "Drift", 5,
         "Legendary Drift community claim (9,200,000 RUB via reddit) is 62% off market fair value (5,670,000 RUB)."),
        ("snipe_opportunity", "high", "Compass", 4,
         "Compass listing at 1,900,000 RUB is 32% below fair value (2,800,000 RUB). Potential snipe."),
    ]
    for atype, sev, name, qlt, msg in alert_templates:
        db.record_alert(
            item_id=name.lower().replace(" ", "_"), item_name=name,
            region="na", qlt=qlt, bonus_bucket=0, alert_type=atype,
            baseline_price=2_800_000, observed_price=1_900_000,
            deviation_pct=32.0, severity=sev, message=msg,
            created_at=now - rng.uniform(0, 86400 * 3),
        )


import math  # noqa: E402  (used by seed_sample_data)


# ---------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------
@app.get("/api/seed")
def api_seed() -> dict:
    seed_sample_data()
    return {"ok": True, "message": "Sample data seeded"}


@app.get("/api/valuations")
def api_valuations(region: str = "na") -> list[dict]:
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
            "ORDER BY collected_at DESC LIMIT 200",
            (region, since),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/alerts")
def api_alerts(region: str = "na", days: int = 7) -> list[dict]:
    return [dict(r) for r in db.recent_alerts(region, days)]


@app.get("/api/summary")
def api_summary(region: str = "na") -> dict:
    with db._conn() as conn:
        total_items = conn.execute(
            "SELECT COUNT(DISTINCT item_id) FROM valuation_report WHERE region=?",
            (region,)).fetchone()[0]
        total_alerts = conn.execute(
            "SELECT COUNT(*) FROM price_deviation_alert WHERE region=? AND created_at>=?",
            (region, time.time() - 7 * 86400)).fetchone()[0]
        critical = conn.execute(
            "SELECT COUNT(*) FROM price_deviation_alert WHERE region=? AND severity='critical' "
            "AND created_at>=?", (region, time.time() - 7 * 86400)).fetchone()[0]
        avg_conf = conn.execute(
            "SELECT AVG(confidence) FROM valuation_report WHERE region=? "
            "AND computed_at>=?", (region, time.time() - 2 * 86400)).fetchone()[0] or 0
        total_signals = conn.execute(
            "SELECT COUNT(*) FROM community_signal WHERE region=? AND collected_at>=?",
            (region, time.time() - 7 * 86400)).fetchone()[0]
        # Quality tier breakdown
        tier_rows = conn.execute(
            "SELECT qlt, COUNT(DISTINCT item_id) as cnt FROM valuation_report "
            "WHERE region=? GROUP BY qlt ORDER BY qlt", (region,)).fetchall()
        tier_breakdown = {str(r["qlt"]): r["cnt"] for r in tier_rows}
        # Snapshot and sale counts
        snap_count = conn.execute(
            "SELECT COUNT(*) FROM auction_snapshot WHERE region=?", (region,)).fetchone()[0]
        sale_count = conn.execute(
            "SELECT COUNT(*) FROM sale_observation WHERE region=?", (region,)).fetchone()[0]
    return {
        "total_items": total_items,
        "total_alerts": total_alerts,
        "critical_alerts": critical,
        "avg_confidence": round(avg_conf, 1),
        "community_signals": total_signals,
        "tier_breakdown": tier_breakdown,
        "snapshots": snap_count,
        "sales": sale_count,
        "data_source": "live" if db.get_meta("live_data_ingested") == "true" else "sample",
        "last_ingestion": db.get_meta("last_ingestion", ""),
    }


# ---------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
def health() -> dict:
    return {"ok": True}
