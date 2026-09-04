"""
combined.py — Runs the Discord bot and the FastAPI dashboard together in a
single process, so they can share the same SQLite market.db file and both
run inside one Render Web Service (Render's free tier does not give every
account a free Background Worker, and even when it does, a separate worker
gets its own filesystem — which would leave the bot writing to a *different*
market.db than the one the dashboard reads from).

Start command on Render:
    python combined.py

Required env vars (see HOSTING.md):
    DISCORD_TOKEN, EXBO_CLIENT_ID, EXBO_CLIENT_SECRET, REGION,
    LIVE_MARKET_DATA, LOT_LIMIT, SCAN_INTERVAL
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import uvicorn

from dashboard.server import app  # FastAPI app (serves API + static frontend)
from bot import bot, DISCORD_TOKEN  # Discord bot instance + its token

log = logging.getLogger("combined")


def run_bot() -> None:
    """Run the Discord bot in a background thread with its own event loop."""
    if not DISCORD_TOKEN:
        log.warning(
            "DISCORD_TOKEN is not set — skipping bot startup. "
            "The dashboard will still run, but no live data will be ingested."
        )
        return
    try:
        # bot.run() blocks and manages its own asyncio event loop, which is
        # fine to do inside a plain (non-async) daemon thread.
        bot.run(DISCORD_TOKEN)
    except Exception:
        log.exception("Discord bot crashed")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    threading.Thread(target=run_bot, daemon=True, name="discord-bot").start()

    port = int(os.getenv("PORT", "8420"))
    # Runs in the main thread, as Render's health checks expect a process
    # bound to $PORT.
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
