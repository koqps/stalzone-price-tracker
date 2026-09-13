"""Run the Discord bot, dashboard, persistence, and patch monitor together."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse
from scapi.config import Config

Config.REALM = os.getenv("REALM", "global").lower()

from dashboard.server import app
from dashboard.opportunities import router as opportunities_router
from dashboard.build_calculator import router as build_calculator_router
import bot as bot_module
from bot_catalog_commands import register_catalog_commands
from patch_monitor import collect_official_patch_signals

app.include_router(opportunities_router)
app.include_router(build_calculator_router)

_INDEX_PATH = Path(__file__).parent / "dashboard" / "static" / "index.html"


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
    4: 0xE05252,  # Exclusive = red
    5: 0xE6A33C,  # Legendary = amber/gold
}

# bot.py's manual /scan calls the sender after scan_all_items already did. Wrap
# the sender so the same lot cannot be broadcast twice within five minutes.
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
