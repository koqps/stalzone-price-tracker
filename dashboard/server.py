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
    4: "#e05252",
    5: "#e6a33c",
    6: "#ef78c8",
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
    """Return the recent exact-variant market without scanning lifetime valuation history."""
    now = time.time()
    live_since = now - max(5, live_minutes) * 60
    sale_since = now - max(1, sale_days) * 86400
    valuation_since = now - 6 * 3600

    with db._conn() as conn:
        snapshots = conn.execute(
            "SELECT id,item_id,item_name,qlt,ptn,upgrade_level,unit_price,lot_key,observed_at "
            "FROM auction_snapshot WHERE region=? AND observed_at>=? AND unit_price>0 "
            "AND upgrade_level BETWEEN 0 AND 15",
            (region, live_since),
        ).fetchall()
        sales = conn.execute(
            "SELECT item_id,item_name,qlt,ptn,upgrade_level,unit_price,observed_at "
            "FROM sale_observation WHERE region=? AND observed_at>=? AND unit_price>0 "
            "AND source='official_history' AND upgrade_level BETWEEN 0 AND 15",
            (region, sale_since),
        ).fetchall()
        valuations = conn.execute(
            "SELECT v.* FROM valuation_report v JOIN ("
            " SELECT item_id,qlt,upgrade_level,MAX(computed_at) mx FROM valuation_report "
            " WHERE region=? AND computed_at>=? AND upgrade_level BETWEEN 0 AND 15 "
            " GROUP BY item_id,qlt,upgrade_level"
            ") x ON v.item_id=x.item_id AND v.qlt=x.qlt AND v.upgrade_level=x.upgrade_level "
            "AND v.computed_at=x.mx WHERE v.region=? AND v.computed_at>=?",
            (region, valuation_since, region, valuation_since),
        ).fetchall()
        patches = conn.execute(
            "SELECT * FROM patch_signal WHERE published_at>=? ORDER BY published_at DESC LIMIT 100",
            (now - 30 * 86400,),
        ).fetchall()

    latest_lots = {}
    for r in snapshots:
        key = r["lot_key"] or f"row:{r['id']}"
        prev = latest_lots.get(key)
        if prev is None or (r["observed_at"] or 0) > (prev["observed_at"] or 0):
            latest_lots[key] = r
    snapshots = list(latest_lots.values())

    grouped = {}

    def ensure(item_id, item_name, qlt, ptn, level):
        key = (item_id, int(qlt), int(ptn), int(level))
        if key not in grouped:
            grouped[key] = {
                "item_id": item_id,
                "item_name": item_name or item_id,
                "qlt": int(qlt),
                "qlt_name": QUALITY_NAMES.get(int(qlt), f"Q{qlt}"),
                "ptn": int(ptn),
                "pattern_label": f"+{int(ptn)}",
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
        g = ensure(r["item_id"], r["item_name"], r["qlt"], r["ptn"], r["upgrade_level"])
        g["_live"].append(r["unit_price"])
        g["latest_observation"] = max(g["latest_observation"], r["observed_at"] or 0)

    for r in sales:
        g = ensure(r["item_id"], r["item_name"], r["qlt"], r["ptn"], r["upgrade_level"])
        g["_sales"].append(r["unit_price"])
        g["latest_sale"] = max(g["latest_sale"], r["observed_at"] or 0)

    valuation_map = {
        (str(v["item_id"]), int(v["qlt"]), int(v["upgrade_level"])): v
        for v in valuations
    }
    for key, g in grouped.items():
        item_id, qlt, _ptn, level = key
        v = valuation_map.get((str(item_id), int(qlt), int(level)))
        if v is not None:
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

    return sorted(out, key=lambda r: (r["item_name"].lower(), r["qlt"], r["ptn"], r["upgrade_level"]))


@app.get("/api/price-history")
def price_history(item_id: str, region: str = "na", hours: int = 24, qlt: int | None = None, upgrade_level: int | None = None):
    """Return bounded official completed-sale history for the chart UI."""
    hours = max(1, min(int(hours), 24 * 90))
    since = time.time() - hours * 3600
    sql = (
        "SELECT item_id,item_name,qlt,upgrade_level,unit_price,amount,observed_at "
        "FROM sale_observation WHERE region=? AND item_id=? AND observed_at>=? "
        "AND source='official_history' AND unit_price>0 AND upgrade_level BETWEEN 0 AND 15"
    )
    args: list[object] = [region, item_id, since]
    if qlt is not None:
        sql += " AND qlt=?"
        args.append(int(qlt))
    if upgrade_level is not None:
        sql += " AND upgrade_level=?"
        args.append(int(upgrade_level))
    sql += " ORDER BY observed_at ASC LIMIT 5000"
    with db._conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
    return {"item_id": item_id, "region": region, "hours": hours, "rows": rows}


@app.get("/api/artifacts")
async def artifacts():
    catalog = await load_artifact_catalog()
    return sorted(catalog.values(), key=lambda x: x["item_name"].lower())


@app.get("/api/artifacts/{item_id}")
async def artifact(item_id: str, upgrade_level: int = 0):
    if upgrade_level < 0 or upgrade_level > 15:
        raise HTTPException(status_code=422, detail="upgrade_level must be between 0 and 15")
    item = await get_artifact_metadata(item_id, upgrade_level=upgrade_level)
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
    """Fast dashboard counters; avoid lifetime COUNT(*) scans on growing tables."""
    now = time.time()
    with db._conn() as conn:
        seq = {
            str(r["name"]): int(r["seq"] or 0)
            for r in conn.execute(
                "SELECT name,seq FROM sqlite_sequence WHERE name IN ('auction_snapshot','sale_observation')"
            ).fetchall()
        }
        items = conn.execute(
            "SELECT COUNT(DISTINCT item_id) FROM auction_snapshot WHERE region=? AND observed_at>=?",
            (region, now - 24 * 3600),
        ).fetchone()[0]
        levels = conn.execute(
            "SELECT COUNT(DISTINCT upgrade_level) FROM sale_observation "
            "WHERE region=? AND observed_at>=? AND upgrade_level BETWEEN 0 AND 15",
            (region, now - 7 * 86400),
        ).fetchone()[0]
        alerts_count = conn.execute(
            "SELECT COUNT(*) FROM price_deviation_alert WHERE region=? AND created_at>=?",
            (region, now - 7 * 86400),
        ).fetchone()[0]
        meta_rows = conn.execute(
            "SELECT key,value FROM meta WHERE key IN ('live_data_ingested','last_ingestion')"
        ).fetchall()

    meta = {str(r["key"]): str(r["value"] or "") for r in meta_rows}
    return {
        "total_items": items,
        "snapshots": seq.get("auction_snapshot", 0),
        "sales": seq.get("sale_observation", 0),
        "upgrade_levels": levels,
        "alerts": alerts_count,
        "data_source": "official_na_live" if meta.get("live_data_ingested") == "true" else "waiting",
        "last_ingestion": meta.get("last_ingestion", ""),
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


@app.get("/login")
def login_page():
    return FileResponse(str(STATIC_DIR / "login.html"))


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
