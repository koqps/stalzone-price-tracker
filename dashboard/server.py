"""FastAPI dashboard backend for exact-level NA artifact market intelligence."""
from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from artifact_catalog import QUALITY_NAMES, get_artifact_metadata, load_artifact_catalog
from market_db import MarketDB

app = FastAPI(title="StalZone Price Tracker")
db = MarketDB()

QUALITY_COLORS = {0:"#8b8f86",1:"#79b84b",2:"#4f98d1",3:"#9a63d8",4:"#e05252",5:"#e6a33c"}


def _median(values):
    vals=[float(v) for v in values if v is not None and float(v)>0]
    return round(statistics.median(vals),2) if vals else None


def _mean(values):
    vals=[float(v) for v in values if v is not None and float(v)>0]
    return round(statistics.fmean(vals),2) if vals else None


def _targets(live_floor, live_median, live_count, sale_median, sale_count, *, exact_level: bool=True):
    if not exact_level:
        return {"sell_quick":None,"sell_recommended":None,"sell_high_margin":None,"sell_confidence":"unknown","sell_evidence":"Upgrade level is unknown; excluded from exact-level prediction."}
    observed=[x for x in (live_floor,live_median,sale_median) if x and x>0]
    if not observed:
        return {"sell_quick":None,"sell_recommended":None,"sell_high_margin":None,"sell_confidence":"none","sell_evidence":"No observed NA evidence for this exact rarity and upgrade level."}
    if live_floor and sale_median: quick=min(live_floor*.99,sale_median*.97)
    elif live_floor: quick=live_floor*.99
    else: quick=sale_median*.95
    rec=max(quick,statistics.median(observed))
    high=None
    if sale_count>=5 and (live_count>=2 or sale_count>=15):
        candidates=[rec*1.06]
        if sale_median: candidates.append(sale_median*1.08)
        if live_median: candidates.append(live_median*1.05)
        high=max(candidates)
    conf="high" if sale_count>=20 and live_count>=3 else "medium" if sale_count>=5 or live_count>=3 else "low"
    bits=[]
    if live_count: bits.append(f"{live_count} current +level listings")
    if sale_count: bits.append(f"{sale_count} exact-level official sales in 7d")
    return {"sell_quick":round(quick,2),"sell_recommended":round(rec,2),"sell_high_margin":round(high,2) if high else None,"sell_confidence":conf,"sell_evidence":"; ".join(bits)}


@app.get("/api/quality-tiers")
def quality_tiers():
    return [{"qlt":q,"name":name,"color":QUALITY_COLORS[q]} for q,name in QUALITY_NAMES.items()]


@app.get("/api/market")
def market(region: str="na", live_minutes: int=20, sale_days: int=7):
    now=time.time(); live_since=now-max(5,live_minutes)*60; sale_since=now-max(1,sale_days)*86400
    with db._conn() as conn:
        snapshots=conn.execute("SELECT item_id,item_name,qlt,upgrade_level,unit_price,observed_at FROM auction_snapshot WHERE region=? AND observed_at>=? AND unit_price>0",(region,live_since)).fetchall()
        sales=conn.execute("SELECT item_id,item_name,qlt,upgrade_level,unit_price,observed_at FROM sale_observation WHERE region=? AND observed_at>=? AND unit_price>0 AND source='official_history'",(region,sale_since)).fetchall()
        valuations=conn.execute("SELECT v.* FROM valuation_report v JOIN (SELECT item_id,qlt,upgrade_level,MAX(computed_at) mx FROM valuation_report WHERE region=? GROUP BY item_id,qlt,upgrade_level) x ON v.item_id=x.item_id AND v.qlt=x.qlt AND v.upgrade_level=x.upgrade_level AND v.computed_at=x.mx WHERE v.region=?",(region,region)).fetchall()
    grouped={}
    def ensure(item_id,item_name,qlt,level):
        key=(item_id,int(qlt),int(level if level is not None else -1))
        if key not in grouped:
            lev=key[2]
            grouped[key]={"item_id":item_id,"item_name":item_name or item_id,"qlt":key[1],"qlt_name":QUALITY_NAMES.get(key[1],f"Q{key[1]}"),"tier_color":QUALITY_COLORS.get(key[1],"#888"),"upgrade_level":lev,"upgrade_label":f"+{lev}" if lev>=0 else "+?","_live":[],"_sales":[],"latest_observation":0.0,"latest_sale":0.0}
        return grouped[key]
    for r in snapshots:
        g=ensure(r["item_id"],r["item_name"],r["qlt"],r["upgrade_level"]); g["_live"].append(r["unit_price"]); g["latest_observation"]=max(g["latest_observation"],r["observed_at"] or 0)
    for r in sales:
        g=ensure(r["item_id"],r["item_name"],r["qlt"],r["upgrade_level"]); g["_sales"].append(r["unit_price"]); g["latest_sale"]=max(g["latest_sale"],r["observed_at"] or 0)
    for v in valuations:
        g=ensure(v["item_id"],v["item_name"],v["qlt"],v["upgrade_level"]); g["_valuation"]=v
    out=[]
    for g in grouped.values():
        live=g.pop("_live"); sales7=g.pop("_sales"); val=g.pop("_valuation",None)
        floor=round(min(live),2) if live else None; live_med=_median(live); sale_med=_median(sales7)
        targets=_targets(floor,live_med,len(live),sale_med,len(sales7),exact_level=g["upgrade_level"]>=0)
        out.append({**g,"live_floor":floor,"live_median":live_med,"live_listings":len(live),"sale_median":sale_med,"sale_average":_mean(sales7),"sale_count":len(sales7),"model_fair_value":val["fair_value_price"] if val else None,"model_quick_sale":val["quick_sale_price"] if val else None,"model_stretch":val["stretch_price"] if val else None,"model_confidence":val["confidence"] if val else None,"patch_risk":val["patch_risk"] if val and "patch_risk" in val.keys() else 0,"price_source":"official_na_auction",**targets})
    return sorted(out,key=lambda r:(r["item_name"].lower(),r["qlt"],r["upgrade_level"]))


@app.get("/api/artifacts")
async def artifacts():
    catalog=await load_artifact_catalog()
    return sorted(catalog.values(),key=lambda x:x["item_name"].lower())


@app.get("/api/artifacts/{item_id}")
async def artifact(item_id: str):
    item=await get_artifact_metadata(item_id)
    if not item: raise HTTPException(status_code=404,detail="Artifact not found")
    return item


@app.get("/api/alerts")
def alerts(region: str="na", days: int=7):
    return [dict(r) for r in db.recent_alerts(region,days)]


@app.get("/api/patch-signals")
def patch_signals(days: int=30):
    return [dict(r) for r in db.recent_patch_signals(days)]


@app.get("/api/summary")
def summary(region: str="na"):
    with db._conn() as conn:
        snaps=conn.execute("SELECT COUNT(*) FROM auction_snapshot WHERE region=?",(region,)).fetchone()[0]
        sales=conn.execute("SELECT COUNT(*) FROM sale_observation WHERE region=? AND source='official_history'",(region,)).fetchone()[0]
        items=conn.execute("SELECT COUNT(DISTINCT item_id) FROM sale_observation WHERE region=?",(region,)).fetchone()[0]
        levels=conn.execute("SELECT COUNT(DISTINCT upgrade_level) FROM sale_observation WHERE region=? AND upgrade_level>=0",(region,)).fetchone()[0]
        alerts_count=conn.execute("SELECT COUNT(*) FROM price_deviation_alert WHERE region=? AND created_at>=?",(region,time.time()-7*86400)).fetchone()[0]
    return {"total_items":items,"snapshots":snaps,"sales":sales,"upgrade_levels":levels,"alerts":alerts_count,"data_source":"official_na_live" if db.get_meta("live_data_ingested")=="true" else "waiting","last_ingestion":db.get_meta("last_ingestion","")}


@app.api_route("/api/seed",methods=["GET","POST"])
def seed():
    raise HTTPException(status_code=403,detail="Sample seeding is disabled")

STATIC_DIR=Path(__file__).parent/"static"
app.mount("/static",StaticFiles(directory=str(STATIC_DIR)),name="static")

@app.get("/")
def index(): return FileResponse(str(STATIC_DIR/"index.html"))

@app.get("/app.js")
def app_js(): return FileResponse(str(STATIC_DIR/"app.js"),media_type="application/javascript")

@app.get("/health")
def health(): return {"ok":True,"source":"official_na_auction","variant_key":"artifact+rarity+exact_upgrade_level","persistent":"supabase" if supabase_enabled() else "local"}
