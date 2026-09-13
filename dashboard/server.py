"""FastAPI dashboard backend for exact-level NA artifact market intelligence."""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from artifact_catalog import QUALITY_NAMES, get_artifact_metadata, load_artifact_catalog
from market_db import MarketDB, supabase_enabled

app = FastAPI(title="StalZone Price Tracker")
db = MarketDB()

QUALITY_COLORS = {
    0: "#8b8f86",
    1: "#79b84b",
    2: "#4f98d1",
    3: "#9a63d8",
    4: "#e05252",  # Exclusive
    5: "#e6a33c",  # Legendary
}


def _median(values):
    vals = [float(v) for v in values if v is not None and float(v) > 0]
    return round(statistics.median(vals), 2) if vals else None


def _mean(values):
    vals = [float(v) for v in values if v is not None and float(v) > 0]
    return round(statistics.fmean(vals), 2) if vals else None


def _patch_context(item_name: str, patches: list) -> dict | None:
    needle = (item_name or "").casefold().strip()
    if not needle:
        return None
    for row in patches:
        hay = f"{row['title'] or ''} {row['summary'] or ''}".casefold()
        if needle in hay:
            return {
                "title": row["title"],
                "direction": row["impact_direction"],
                "confidence": row["confidence"],
                "published_at": row["published_at"],
                "url": row["source_url"],
            }
    return None


def _targets(live_floor, live_median, live_count, sale_median, sale_count, patch: dict | None = None):
    observed = [x for x in (live_floor, live_median, sale_median) if x and x > 0]
    if not observed:
        return {
            "sell_quick": None,
            "sell_recommended": None,
            "sell_high_margin": None,
            "sell_confidence": "none",
            "sell_evidence": "No observed NA evidence for this exact rarity and +level.",
        }

    if live_floor and sale_median:
        quick = min(live_floor * .99, sale_median * .97)
    elif live_floor:
        quick = live_floor * .99
    else:
        quick = sale_median * .95

    recommended = max(quick, statistics.median(observed))
    high = None
    if sale_count >= 5 and (live_count >= 2 or sale_count >= 15):
        candidates = [recommended * 1.06]
        if sale_median:
            candidates.append(sale_median * 1.08)
        if live_median:
            candidates.append(live_median * 1.05)
        high = max(candidates)

    if sale_count >= 20 and live_count >= 3:
        confidence = "high"
    elif sale_count >= 5 or live_count >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    bits = []
    if live_count:
        bits.append(f"{live_count} current exact-level listings")
    if sale_count:
        bits.append(f"{sale_count} exact-level official sales in 7d")

    # Patch notes are context, never a made-up price multiplier. While a market
    # is plausibly repricing, suppress the aggressive target until fresh sales
    # accumulate and lower confidence by one step.
    if patch:
        direction = str(patch.get("direction") or "uncertain").replace("_", " ")
        bits.append(f"official patch signal: {direction}")
        if confidence == "high":
            confidence = "medium"
        elif confidence == "medium":
            confidence = "low"
        if sale_count < 10:
            high = None

    return {
        "sell_quick": round(quick, 2),
        "sell_recommended": round(recommended, 2),
        "sell_high_margin": round(high, 2) if high else None,
        "sell_confidence": confidence,
        "sell_evidence": "; ".join(bits) or "Observed NA market data",
    }


@app.get("/api/quality-tiers")
def quality_tiers():
    return [{"qlt": q, "name": name, "color": QUALITY_COLORS[q]} for q, name in QUALITY_NAMES.items()]


@app.get("/api/market")
def market(region: str = "na", live_minutes: int = 20, sale_days: int = 7):
    now = time.time()
    live_since = now - max(5, live_minutes) * 60
    sale_since = now - max(1, sale_days) * 86400

    with db._conn() as conn:
        snapshots = conn.execute(
            "SELECT id,item_id,item_name,qlt,upgrade_level,unit_price,lot_key,observed_at "
            "FROM auction_snapshot WHERE region=? AND observed_at>=? AND unit_price>0 "
            "AND upgrade_level BETWEEN 0 AND 15",
            (region, live_since),
        ).fetchall()
        sales = conn.execute(
            "SELECT item_id,item_name,qlt,upgrade_level,unit_price,observed_at "
            "FROM sale_observation WHERE region=? AND observed_at>=? AND unit_price>0 "
            "AND source='official_history' AND upgrade_level BETWEEN 0 AND 15",
            (region, sale_since),
        ).fetchall()
        valuations = conn.execute(
            "SELECT v.* FROM valuation_report v JOIN ("
            " SELECT item_id,qlt,upgrade_level,MAX(computed_at) mx FROM valuation_report "
            " WHERE region=? AND upgrade_level BETWEEN 0 AND 15 GROUP BY item_id,qlt,upgrade_level"
            ") x ON v.item_id=x.item_id AND v.qlt=x.qlt AND v.upgrade_level=x.upgrade_level "
            "AND v.computed_at=x.mx WHERE v.region=?",
            (region, region),
        ).fetchall()
        patches = conn.execute(
            "SELECT * FROM patch_signal WHERE published_at>=? ORDER BY published_at DESC LIMIT 100",
            (now - 30 * 86400,),
        ).fetchall()

    # A bot scan records the same active lot repeatedly. Only the newest snapshot
    # of each lot key is a current listing; counting every scan would fake liquidity.
    latest_lots = {}
    for r in snapshots:
        key = r["lot_key"] or f"row:{r['id']}"
        prev = latest_lots.get(key)
        if prev is None or (r["observed_at"] or 0) > (prev["observed_at"] or 0):
            latest_lots[key] = r
    snapshots = list(latest_lots.values())

    grouped = {}

    def ensure(item_id, item_name, qlt, level):
        key = (item_id, int(qlt), int(level))
        if key not in grouped:
            grouped[key] = {
                "item_id": item_id,
                "item_name": item_name or item_id,
                "qlt": int(qlt),
                "qlt_name": QUALITY_NAMES.get(int(qlt), f"Q{qlt}"),
                "tier_color": QUALITY_COLORS.get(int(qlt), "#888"),
                "upgrade_level": int(level),
                "upgrade_label": f"+{int(level)}",
                "_live": [],
                "_sales": [],
                "latest_observation": 0.0,
                "latest_sale": 0.0,
            }
        return grouped[key]

    for r in snapshots:
        g = ensure(r["item_id"], r["item_name"], r["qlt"], r["upgrade_level"])
        g["_live"].append(r["unit_price"])
        g["latest_observation"] = max(g["latest_observation"], r["observed_at"] or 0)

    for r in sales:
        g = ensure(r["item_id"], r["item_name"], r["qlt"], r["upgrade_level"])
        g["_sales"].append(r["unit_price"])
        g["latest_sale"] = max(g["latest_sale"], r["observed_at"] or 0)

    for v in valuations:
        g = ensure(v["item_id"], v["item_name"], v["qlt"], v["upgrade_level"])
        g["_valuation"] = v

    out = []
    for g in grouped.values():
        live = g.pop("_live")
        sales7 = g.pop("_sales")
        val = g.pop("_valuation", None)
        floor = round(min(live), 2) if live else None
        live_med = _median(live)
        sale_med = _median(sales7)
        patch = _patch_context(g["item_name"], patches)
        targets = _targets(floor, live_med, len(live), sale_med, len(sales7), patch)
        out.append({
            **g,
            "live_floor": floor,
            "live_median": live_med,
            "live_listings": len(live),
            "sale_median": sale_med,
            "sale_average": _mean(sales7),
            "sale_count": len(sales7),
            "model_fair_value": val["fair_value_price"] if val else None,
            "model_quick_sale": val["quick_sale_price"] if val else None,
            "model_stretch": val["stretch_price"] if val else None,
            "model_confidence": val["confidence"] if val else None,
            "patch_risk": 1 if patch else 0,
            "patch_context": patch,
            "price_source": "official_na_auction",
            **targets,
        })

    return sorted(out, key=lambda r: (r["item_name"].lower(), r["qlt"], r["upgrade_level"]))


@app.get("/api/artifacts")
async def artifacts():
    catalog = await load_artifact_catalog()
    return sorted(catalog.values(), key=lambda x: x["item_name"].lower())


@app.get("/api/artifacts/{item_id}")
async def artifact(item_id: str):
    item = await get_artifact_metadata(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return item


@app.get("/api/alerts")
def alerts(region: str = "na", days: int = 7):
    return [dict(r) for r in db.recent_alerts(region, days)]


@app.get("/api/patch-signals")
def patch_signals(days: int = 30):
    return [dict(r) for r in db.recent_patch_signals(days)]


@app.get("/api/summary")
def summary(region: str = "na"):
    with db._conn() as conn:
        snaps = conn.execute("SELECT COUNT(*) FROM auction_snapshot WHERE region=?", (region,)).fetchone()[0]
        sales = conn.execute(
            "SELECT COUNT(*) FROM sale_observation WHERE region=? AND source='official_history'",
            (region,),
        ).fetchone()[0]
        items = conn.execute(
            "SELECT COUNT(DISTINCT item_id) FROM ("
            "SELECT item_id FROM auction_snapshot WHERE region=? UNION "
            "SELECT item_id FROM sale_observation WHERE region=? AND source='official_history')",
            (region, region),
        ).fetchone()[0]
        levels = conn.execute(
            "SELECT COUNT(DISTINCT upgrade_level) FROM sale_observation WHERE region=? AND upgrade_level BETWEEN 0 AND 15",
            (region,),
        ).fetchone()[0]
        alerts_count = conn.execute(
            "SELECT COUNT(*) FROM price_deviation_alert WHERE region=? AND created_at>=?",
            (region, time.time() - 7 * 86400),
        ).fetchone()[0]

    return {
        "total_items": items,
        "snapshots": snaps,
        "sales": sales,
        "upgrade_levels": levels,
        "alerts": alerts_count,
        "data_source": "official_na_live" if db.get_meta("live_data_ingested") == "true" else "waiting",
        "last_ingestion": db.get_meta("last_ingestion", ""),
        "persistent": "supabase" if supabase_enabled() else "local_cache_only",
    }


@app.api_route("/api/seed", methods=["GET", "POST"])
def seed():
    raise HTTPException(status_code=403, detail="Sample seeding is disabled")


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/app.js")
def app_js():
    return FileResponse(str(STATIC_DIR / "app.js"), media_type="application/javascript")


@app.get("/health")
def health():
    return {
        "ok": True,
        "source": "official_na_auction",
        "variant_key": "artifact+rarity+exact_upgrade_level",
        "persistent": "supabase" if supabase_enabled() else "local_cache_only",
    }
