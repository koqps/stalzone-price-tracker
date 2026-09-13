"""Run the Discord bot, dashboard, persistence, and patch monitor together."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse, JSONResponse
from scapi.config import Config

Config.REALM = os.getenv("REALM", "global").lower()

from dashboard.server import app, db
from dashboard.opportunities import router as opportunities_router
from dashboard.build_calculator import router as build_calculator_router
import bot as bot_module
from bot_catalog_commands import register_catalog_commands
from patch_monitor import collect_official_patch_signals

app.include_router(opportunities_router)
app.include_router(build_calculator_router)

_INDEX_PATH = Path(__file__).parent / "dashboard" / "static" / "index.html"
_NORMALIZED_ARTIFACTS_URL = "https://raw.githubusercontent.com/will-bot2026/stalcraft_v1/main/data/normalized-exbo/artifacts.json"
_NORMALIZED_CACHE_PATH = Path(__file__).parent / ".artifact-market-metadata.json"
_normalized_fallback_cache: tuple[float, dict[str, dict]] | None = None

_CLASS_LABELS = {
    "biochemical": "Biochemical",
    "electrophysical": "Electrophysical",
    "gravity": "Gravity",
    "thermal": "Thermal",
    "other_arts": "Other",
}


def _read_persistent_metadata() -> dict[str, dict]:
    """Read the last successful normalized metadata snapshot without network I/O."""
    try:
        if not _NORMALIZED_CACHE_PATH.exists():
            return {}
        data = json.loads(_NORMALIZED_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.getLogger("combined").exception("Could not read artifact metadata cache")
        return {}


def _cached_normalized_metadata() -> dict[str, dict]:
    """Return metadata already in memory/on disk. Never performs network I/O."""
    global _normalized_fallback_cache
    if _normalized_fallback_cache:
        return _normalized_fallback_cache[1]
    disk = _read_persistent_metadata()
    if disk:
        _normalized_fallback_cache = (time.time(), disk)
    return disk


def _normalized_artifact_metadata() -> dict[str, dict]:
    """Refresh class/stat metadata in the background; never called by request handlers."""
    global _normalized_fallback_cache
    now = time.time()
    if _normalized_fallback_cache and now - _normalized_fallback_cache[0] < 6 * 3600:
        return _normalized_fallback_cache[1]
    try:
        req = urllib.request.Request(
            _NORMALIZED_ARTIFACTS_URL,
            headers={"User-Agent": "stalzone-price-tracker/2.0"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            item_id = str(row.get("id") or "")
            if item_id:
                grouped.setdefault(item_id, []).append(row)

        def score(row: dict) -> tuple[float, float]:
            positives = [s for s in row.get("stats") or [] if s.get("isPositive")]
            if positives:
                return (
                    sum(abs(float(s.get("max") or 0)) for s in positives),
                    sum(abs(float(s.get("min") or 0)) for s in positives),
                )
            stats = row.get("stats") or []
            return (sum(abs(float(s.get("max") or 0)) for s in stats), 0.0)

        out: dict[str, dict] = {}
        for item_id, variants in grouped.items():
            row = min(variants, key=score)
            category = str(row.get("category") or "artefact/other_arts")
            key = category.split("/")[-1]
            stats = []
            for st in row.get("stats") or []:
                minimum = st.get("min")
                maximum = st.get("max")
                pct = bool(st.get("isPercentage"))
                if minimum == maximum:
                    display = f"{minimum}{'%' if pct else ''}"
                else:
                    display = f"{minimum}–{maximum}{'%' if pct else ''}"
                stats.append({
                    "name": st.get("name") or str(st.get("key") or "").rsplit(".", 1)[-1].replace("_", " ").title(),
                    "display": display,
                    "min": minimum,
                    "max": maximum,
                    "harmful": not bool(st.get("isPositive", True)),
                    "kind": "range",
                })
            out[item_id] = {
                "item_name": row.get("name") or item_id,
                "artifact_class": _CLASS_LABELS.get(key, key.replace("_", " ").title()),
                "stats": stats,
                "stat_groups": [],
            }
        _normalized_fallback_cache = (time.time(), out)
        try:
            _NORMALIZED_CACHE_PATH.write_text(
                json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception:
            logging.getLogger("combined").exception("Could not persist artifact metadata cache")
        return out
    except Exception:
        logging.getLogger("combined").exception("Background artifact metadata refresh failed")
        return _cached_normalized_metadata()


def _observed_artifact_fallback() -> list[dict]:
    """Return the market artifact list immediately using local DB + cached metadata only."""
    meta = _cached_normalized_metadata()
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT item_id, MAX(item_name) item_name FROM ("
            " SELECT item_id,item_name FROM auction_snapshot WHERE item_id IS NOT NULL "
            " UNION ALL "
            " SELECT item_id,item_name FROM sale_observation WHERE item_id IS NOT NULL"
            ") GROUP BY item_id"
        ).fetchall()
    result = []
    for r in rows:
        item_id = str(r["item_id"])
        m = meta.get(item_id) or {}
        enriched = bool(m)
        result.append({
            "item_id": item_id,
            "item_name": str(m.get("item_name") or r["item_name"] or item_id),
            "artifact_class": m.get("artifact_class") or "Artifact",
            "icon_url": None,
            "description": "",
            "stats": m.get("stats") or [],
            "stat_groups": m.get("stat_groups") or [],
            "source": "normalized_exbo_cached_plus_observed_market" if enriched else "observed_tracker_database_fast",
            "metadata_degraded": not enriched,
        })
    return sorted(result, key=lambda x: x["item_name"].lower())


@app.middleware("http")
async def keep_artifact_api_available(request, call_next):
    """Serve /api/artifacts without any upstream network dependency."""
    if request.method == "GET" and request.url.path == "/api/artifacts":
        rows = _observed_artifact_fallback()
        degraded = any(bool(row.get("metadata_degraded")) for row in rows)
        return JSONResponse(
            rows,
            headers={"X-Stalzone-Metadata": "warming" if degraded else "cached-normalized"},
        )
    return await call_next(request)


@app.middleware("http")
async def inject_build_calculator_tab(request, call_next):
    """Keep the main dashboard static while exposing the calculator as a tab."""
    if request.method == "GET" and request.url.path == "/":
        html = _INDEX_PATH.read_text(encoding="utf-8")
        tab = '<a class="build-tab-link" href="/build-calculator">🧪 Build Calculator</a>'
        if tab not in html:
            html = html.replace("</nav>", tab + "</nav>", 1)
            html = html.replace(
                "</style>",
                ".nav .build-tab-link{color:var(--muted);border-radius:10px;padding:9px 14px;font-weight:800;font-size:12px;text-decoration:none}.nav .build-tab-link:hover{color:#fff;background:color-mix(in srgb,var(--accent) 18%,var(--panel2))}@media(max-width:700px){.nav .build-tab-link{flex:1;text-align:center;padding:9px 7px}}\n</style>",
                1,
            )
            html = html.replace(
                "</body>",
                "<script src=\"/static/market-ui-resilience.js?v=1\"></script><script>(()=>{const v=new URLSearchParams(location.search).get('view');if(v){const b=document.querySelector(`.nav button[data-view=\"${v}\"]`);if(b)setTimeout(()=>b.click(),0)}})();</script></body>",
                1,
            )
        return HTMLResponse(html)
    return await call_next(request)


bot = bot_module.bot
DISCORD_TOKEN = bot_module.DISCORD_TOKEN
REGION = bot_module.REGION
log = logging.getLogger("combined")

COLLECTOR_ENABLED = os.getenv("COLLECTOR_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

bot_module.QUALITY_COLORS = {
    0: 0x8B8F86,
    1: 0x79B84B,
    2: 0x4F98D1,
    3: 0x9A63D8,
    4: 0xE05252,
    5: 0xE6A33C,
}

_original_send_discord_alerts = bot_module.send_discord_alerts
_sent_lots: dict[str, float] = {}


async def _deduped_send_discord_alerts(alerts: list[dict]):
    now = time.time()
    fresh = []
    for alert in alerts:
        key = str(alert.get("lot_key") or "")
        if key and now - _sent_lots.get(key, 0) < 300:
            continue
        if key:
            _sent_lots[key] = now
        fresh.append(alert)
    if fresh:
        await _original_send_discord_alerts(fresh)


bot_module.send_discord_alerts = _deduped_send_discord_alerts
register_catalog_commands(bot, region=REGION)


def run_bot() -> None:
    if not DISCORD_TOKEN:
        log.warning("DISCORD_TOKEN is not set — dashboard will run without live ingestion")
        return
    try:
        bot.run(DISCORD_TOKEN)
    except Exception:
        log.exception("Discord bot crashed")


def run_patch_monitor() -> None:
    interval = max(3600, int(os.getenv("PATCH_MONITOR_INTERVAL", "21600")))
    while True:
        try:
            collect_official_patch_signals()
        except Exception:
            log.exception("Official patch monitor failed")
        time.sleep(interval)


def warm_artifact_metadata() -> None:
    """Refresh rich market metadata after startup without delaying HTTP readiness."""
    try:
        rows = _normalized_artifact_metadata()
        log.info("Artifact market metadata warmup ready: %s items", len(rows))
    except Exception:
        log.exception("Artifact market metadata warmup failed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )
    threading.Thread(target=warm_artifact_metadata, daemon=True, name="artifact-metadata").start()
    if COLLECTOR_ENABLED:
        log.info("Collector enabled: starting Discord bot, ingestion, and patch monitor")
        threading.Thread(target=run_bot, daemon=True, name="discord-bot").start()
        threading.Thread(target=run_patch_monitor, daemon=True, name="patch-monitor").start()
    else:
        log.info("Collector disabled: dashboard-only mode")
    port = int(os.getenv("PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
