import os
import sys
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Ensure project root is in path to import market_db, price_model, etc.
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    import market_db
    import price_model
    import live_ingestion
except ImportError as e:
    logging.warning(f"Core module import warning in server.py: {e}")

app = FastAPI(title="StalZone Market Dashboard")

# Set up relative directory paths
DASHBOARD_DIR = Path(__file__).resolve().parent
STATIC_DIR = DASHBOARD_DIR / "static"

# Mount static files correctly
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def read_root():
    """Serves the main index.html dashboard file."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return JSONResponse(status_code=404, content={"error": "index.html not found"})

@app.get("/app.js")
async def read_app_js():
    """Fallback route so /app.js resolves cleanly if requested at root."""
    js_file = STATIC_DIR / "app.js"
    if js_file.exists():
        return FileResponse(str(js_file), media_type="application/javascript")
    raise HTTPException(status_code=404, detail="app.js not found")

@app.get("/health")
async def health_check():
    """Health check endpoint for UptimeRobot keeping Render awake."""
    return {"status": "ok", "service": "stalzone-tracker"}

@app.get("/api/dbstats")
async def get_db_stats():
    """Fetches database statistics with proper await handling."""
    try:
        if hasattr(market_db, "get_db_stats"):
            # Properly awaiting async db stats call
            if asyncio.iscoroutinefunction(market_db.get_db_stats):
                stats = await market_db.get_db_stats()
            else:
                stats = market_db.get_db_stats()
            return stats
        return {"status": "active", "tracked_items": 0, "sales": 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/items")
async def get_items():
    """Returns all tracked artifact items."""
    try:
        if hasattr(market_db, "get_tracked_items"):
            if asyncio.iscoroutinefunction(market_db.get_tracked_items):
                items = await market_db.get_tracked_items()
            else:
                items = market_db.get_tracked_items()
            return {"items": items}
        return {"items": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/valuation/{item_name}")
async def get_valuation(item_name: str):
    """Calculates confidence-weighted price model per quality tier for an artifact."""
    try:
        if hasattr(price_model, "calculate_valuation"):
            if asyncio.iscoroutinefunction(price_model.calculate_valuation):
                result = await price_model.calculate_valuation(item_name)
            else:
                result = price_model.calculate_valuation(item_name)
            return result
        return {"item": item_name, "error": "Valuation engine unavailable"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/ingest")
async def trigger_ingestion(background_tasks: BackgroundTasks):
    """Triggers live ingestion in background task to avoid HTTP timeout."""
    try:
        if hasattr(live_ingestion, "run_ingestion"):
            if asyncio.iscoroutinefunction(live_ingestion.run_ingestion):
                background_tasks.add_task(live_ingestion.run_ingestion, "both")
            else:
                background_tasks.add_task(live_ingestion.run_ingestion, "both")
            return {"status": "success", "message": "Live ingestion started in background"}
        return {"status": "error", "message": "Live ingestion module unavailable"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
