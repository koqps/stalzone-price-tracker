"""
combined.py — Run the Discord bot + dashboard in a single process.

This is the easiest way to host everything on one free server (Render, Railway, etc.).
The dashboard runs in the main thread (required by hosting platforms), and the
bot runs in a background thread.

Usage:
    python combined.py

Environment variables (all in .env):
    DISCORD_TOKEN=your_bot_token
    EXBO_CLIENT_ID=your_exbo_client_id
    EXBO_CLIENT_SECRET=your_exbo_client_secret
    REGION=na
    LIVE_MARKET_DATA=true
"""
import os
import threading
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

def run_bot():
    """Run the Discord bot in a background thread."""
    from bot_integrated_legacy import bot
    token = os.getenv("DISCORD_TOKEN", "")
    if not token:
        logging.error("DISCORD_TOKEN not set — bot will not start")
        return
    asyncio.run(bot.start(token))

def main():
    # Start the bot in a background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    logging.info("Bot thread started")

    # Run the dashboard in the main thread (hosting platforms need this)
    import uvicorn
    from dashboard.server import app

    port = int(os.getenv("PORT", 8420))
    host = os.getenv("HOST", "0.0.0.0")
    logging.info(f"Starting dashboard on {host}:{port}")
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    main()
