"""
combined.py — Runs the Discord bot and the FastAPI dashboard together in a
single process so both share the same SQLite market database.
"""
from __future__ import annotations

import logging
import os
import threading

import uvicorn
from scapi.config import Config

# DatabaseLookup uses Config.REALM for its initial sync. Keep it aligned with
# the international/global database used by the NA market tracker.
Config.REALM = os.getenv("REALM", "global").lower()

from dashboard.server import app
from bot import bot, DISCORD_TOKEN, REGION
from bot_catalog_commands import register_catalog_commands

log = logging.getLogger("combined")

# Register the official metadata + observed market command before Discord sync.
register_catalog_commands(bot, region=REGION)


def run_bot() -> None:
    """Run the Discord bot in a background thread with its own event loop."""
    if not DISCORD_TOKEN:
        log.warning(
            "DISCORD_TOKEN is not set — skipping bot startup. "
            "The dashboard will still run, but no live data will be ingested."
        )
        return
    try:
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
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
