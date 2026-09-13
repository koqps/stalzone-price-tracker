"""Run the Discord bot, dashboard, persistence, and patch monitor together."""
from __future__ import annotations

import logging
import os
import threading
import time
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


def _observed_artifact_fallback() -> list[dict]:
    """Return real observed artifact identities when metadata lookup is unavailable."""
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT item_id, MAX(item_name) item_name FROM ("
            " SELECT item_id,item_name FROM auction_snapshot WHERE item_id IS NOT NULL "
            " UNION ALL "
            " SELECT item_id,item_name FROM sale_observation WHERE item_id IS NOT NULL"
            ") GROUP BY item_id"
        ).fetchall()
    return sorted(
        [
            {
                "item_id": str(r["item_id"]),
                "item_name": str(r["item_name"] or r["item_id"]),
                "artifact_class": "Artifact",
                "icon_url": None,
                "description": "",
                "stats": [],
                "stat_groups": [],
                "source": "observed_tracker_database_fallback",
                "metadata_degraded": True,
            }
            for r in rows
        ],
        key=lambda x: x["item_name"].lower(),
    )


@app.middleware("http")
async def keep_artifact_api_available(request, call_next):
    """Do not let a temporary upstream catalog outage blank the market UI."""
    try:
        return await call_next(request)
    except Exception:
        if request.method == "GET" and request.url.path == "/api/artifacts":
            logging.getLogger("combined").exception(
                "Official artifact catalog failed; serving observed database fallback"
            )
            return JSONResponse(_observed_artifact_fallback(), headers={"X-Stalzone-Metadata": "degraded"})
        raise


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
                "<script>(()=>{const v=new URLSearchParams(location.search).get('view');if(v){const b=document.querySelector(`.nav button[data-view=\"${v}\"]`);if(b)setTimeout(()=>b.click(),0)}})();</script></body>",
                1,
            )
        return HTMLResponse(html)
    return await call_next(request)


bot = bot_module.bot
DISCORD_TOKEN = bot_module.DISCORD_TOKEN
REGION = bot_module.REGION
log = logging.getLogger("combined")

# One host should own Discord + live ingestion. Other deployments can remain
# online as dashboard-only fallbacks without duplicating scans or bot sessions.
COLLECTOR_ENABLED = os.getenv("COLLECTOR_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

# Correct quality colors everywhere, including the older bot.py embed paths.
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )
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
