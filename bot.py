"""
bot.py — StalZone NA Artifact Sniper Bot (Integrated Edition)

Complete Discord bot that integrates the price-tracking model with live
auction data ingestion.  Includes all fixes from the prior session:

  ✓ get_lot_quality() no longer silently defaults to Common — returns None
    for unknown quality, so Rare/Exclusive/Legendary artifacts are detected
  ✓ AUCTION_LOT_LIMIT raised from 40 to 100 — catches expensive high-tier lots
  ✓ lot_key() uses start_time for uniqueness — prevents identity collisions
  ✓ Live comparable listing check — never alerts "big profit" when cheaper
    listings of the same artifact exist
  ✓ Confidence-weighted price model — replaces static multipliers
  ✓ MarketDB integration — records snapshots + sales for the dashboard
  ✓ Price history ingestion — pulls official sale data from the API
  ✓ Community sentiment — optional integration via daily_monitor.py

Environment variables (.env):
  DISCORD_TOKEN=your_discord_bot_token
  EXBO_CLIENT_ID=your_exbo_oauth_client_id
  EXBO_CLIENT_SECRET=your_exbo_oauth_client_secret
  TARGET_GUILD_IDS=123456789,987654321  (optional: restrict to specific servers)
  REGION=na                               (default region)
  LOT_LIMIT=100                           (lots to fetch per scan)
  HISTORY_LIMIT=50                        (price history records per item)
  SCAN_INTERVAL=300                       (seconds between scans)
  DISCORD_ALERT_WEBHOOK=https://...      (optional: webhook for price alerts)
  LIVE_MARKET_DATA=true                   (enable Discord alert dispatch)
  MIN_ALERT_CONFIDENCE=65                 (minimum confidence to alert)
  MIN_MARGIN_PCT=15                       (minimum profit margin % to alert)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Discord ─────────────────────────────────────────────────────────────────
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ─── scapi (StalCraft API) ──────────────────────────────────────────────────
from scapi import AppClient, DatabaseLookup, Listing
from scapi.enums import Order, Region, SortAuction
from scapi.exceptions import NotFoundError

# ─── Price tracker integration ──────────────────────────────────────────────
from market_db import MarketDB, bonus_bucket
from price_model import compute_valuation, ValuationResult
from bot_integration import (
    get_lot_quality,
    lot_key,
    has_cheaper_comparable_listing,
    live_resale_cap_per_unit,
    record_observations,
    record_sale,
    evaluate_lot_with_model,
    should_alert_lot,
)
from live_ingestion import (
    get_api_client,
    load_tradeable_artifacts,
    ingest_live_data,
    ingest_item_lots,
    ingest_item_history,
    extract_quality,
)
import community_sources

# ─── Configuration ───────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
EXBO_CLIENT_ID = os.getenv("EXBO_CLIENT_ID", "")
EXBO_CLIENT_SECRET = os.getenv("EXBO_CLIENT_SECRET", "")
TARGET_GUILD_IDS = [int(x) for x in os.getenv("TARGET_GUILD_IDS", "").split(",") if x.strip()]
REGION = os.getenv("REGION", "na").lower()
LOT_LIMIT = int(os.getenv("LOT_LIMIT", "100"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
DISCORD_ALERT_WEBHOOK = os.getenv("DISCORD_ALERT_WEBHOOK", "")
LIVE_MARKET_DATA = os.getenv("LIVE_MARKET_DATA", "false").lower() == "true"
MIN_ALERT_CONFIDENCE = int(os.getenv("MIN_ALERT_CONFIDENCE", "65"))
MIN_MARGIN_PCT = float(os.getenv("MIN_MARGIN_PCT", "15"))

# Database paths
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# StalCraft quality names for display
QUALITY_NAMES = {0: "Common", 1: "Uncommon", 2: "Special", 3: "Rare", 4: "Exclusive", 5: "Legendary"}
QUALITY_COLORS = {
    0: 0x7a7f72,  # Common — gray
    1: 0x8fbc3f,  # Uncommon — green
    2: 0x5591c7,  # Special — blue
    3: 0xa86fdf,  # Rare — purple
    4: 0xe8a317,  # Exclusive — amber
    5: 0xc63838,  # Legendary — red
}

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("stalzone_bot")

# ─── Bot setup ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── Global state ────────────────────────────────────────────────────────────
api_client: AppClient | None = None
db_lookup: DatabaseLookup | None = None
market_db: MarketDB | None = None
tracked_items: dict[str, str] = {}  # item_id -> item_name
recent_lots: dict[str, set[str]] = {}  # item_id -> set of lot_keys (for sale detection)
last_sale_alerts: dict[str, float] = {}  # dedup key -> timestamp


# ─── Initialization ─────────────────────────────────────────────────────────

async def init_api():
    """Initialize the scapi AppClient and DatabaseLookup."""
    global api_client, db_lookup, tracked_items

    if not EXBO_CLIENT_ID or not EXBO_CLIENT_SECRET:
        log.error("EXBO_CLIENT_ID and EXBO_CLIENT_SECRET must be set in .env")
        return False

    api_client = AppClient(
        client_id=EXBO_CLIENT_ID,
        client_secret=EXBO_CLIENT_SECRET,
    )
    log.info("scapi AppClient initialised (region=%s)", REGION)

    db_lookup = DatabaseLookup()
    log.info("scapi DatabaseLookup initialised")

    # Load tradeable artifacts from the stalcraft-database
    try:
        tracked_items = await load_tradeable_artifacts()
        log.info("Loaded %d tradeable artifacts", len(tracked_items))
    except Exception as e:
        log.error("Failed to load tradeable artifacts: %s", e)
        log.info("Falling back to manual item list")
        # Fallback: a few known artifact IDs (user can expand)
        tracked_items = {}

    return True


def init_db():
    """Initialize the MarketDB."""
    global market_db
    market_db = MarketDB()
    log.info("MarketDB initialised at %s", market_db.path)
    return market_db


# ─── Item scanning ──────────────────────────────────────────────────────────

async def scan_item(item_id: str, item_name: str) -> list[dict]:
    """Scan auction lots for a single item.

    Returns a list of alert dicts for profitable lots.

    Uses the fixed get_lot_quality(), live comparable checks, and the
    confidence-weighted price model.
    """
    alerts = []

    try:
        listing = await asyncio.to_thread(
            api_client.auction(item_id).lots,
            limit=LOT_LIMIT,
            sort=SortAuction.BUYOUT_PRICE,
            order=Order.ASC,
            additional=True,
            region=REGION,
        )
    except NotFoundError:
        return alerts
    except Exception as e:
        log.error("Failed to fetch lots for %s: %s", item_name, e)
        return alerts

    lots = list(listing)
    total_lots = len(lots)
    log.info("  %s: %d lots fetched", item_name, total_lots)

    # Record all lots as snapshots in MarketDB
    if market_db:
        record_observations(market_db, item_id, item_name, lots, REGION)

    # Track lot keys for sale detection
    current_keys: set[str] = set()

    for lot in lots:
        qlt, upgrade_bonus = get_lot_quality(lot)

        # CRITICAL FIX: Skip lots where quality can't be determined.
        # The old code defaulted to Common (qlt=0), which hid all
        # Rare, Exclusive, and Legendary artifacts.
        if qlt is None:
            continue

        key = lot_key(item_id, lot)
        current_keys.add(key)

        # Check if this lot appeared since last scan (new listing)
        was_seen = key in recent_lots.get(item_id, set())

        # Evaluate the lot with the price model
        should_alert, valuation, reason = should_alert_lot(
            db=market_db,
            item_id=item_id,
            item_name=item_name,
            qlt=qlt,
            upgrade_bonus=upgrade_bonus,
            lots=lots,
            current_lot=lot,
            min_confidence=MIN_ALERT_CONFIDENCE,
            min_margin_pct=MIN_MARGIN_PCT,
        )

        if should_alert and valuation:
            buyout = getattr(lot, "buyout_price", 0) or 0
            amount = getattr(lot, "amount", 1) or 1
            unit_price = buyout / max(amount, 1)

            cap, _ = live_resale_cap_per_unit(lots, item_id, qlt, upgrade_bonus, lot)

            alerts.append({
                "item_id": item_id,
                "item_name": item_name,
                "qlt": qlt,
                "qlt_name": QUALITY_NAMES.get(qlt, f"Q{qlt}"),
                "upgrade_bonus": upgrade_bonus or 0.0,
                "buyout_price": buyout,
                "amount": amount,
                "unit_price": unit_price,
                "fair_value": valuation.fair_value,
                "quick_sale": valuation.quick_sale,
                "stretch": valuation.stretch,
                "confidence": valuation.confidence,
                "confidence_label": valuation.confidence_label,
                "live_cap": cap,
                "margin_pct": ((valuation.fair_value - unit_price) / valuation.fair_value * 100) if valuation.fair_value > 0 else 0,
                "lot_key": key,
            })

    # Detect disappeared lots (inferred sales)
    prev_keys = recent_lots.get(item_id, set())
    disappeared = prev_keys - current_keys

    for dkey in disappeared:
        # In a real implementation, you'd look up the lot's last known price
        # and record it as a sale. For now, just log it.
        log.debug("  %s: lot disappeared (possible sale): %s", item_name, dkey)

    recent_lots[item_id] = current_keys
    return alerts


async def scan_all_items():
    """Scan all tracked items for profitable lots."""
    if not tracked_items:
        log.warning("No tracked items to scan")
        return

    log.info("=== Starting scan of %d items ===", len(tracked_items))
    all_alerts = []

    for i, (item_id, item_name) in enumerate(tracked_items.items(), 1):
        log.info("[%d/%d] Scanning %s", i, len(tracked_items), item_name)
        alerts = await scan_item(item_id, item_name)
        all_alerts.extend(alerts)

        # Rate limit: small delay between items
        await asyncio.sleep(1.0)

    log.info("=== Scan complete: %d alerts found ===", len(all_alerts))

    # Send Discord alerts
    if all_alerts and LIVE_MARKET_DATA:
        await send_discord_alerts(all_alerts)
    elif all_alerts:
        log.info("Alerts found but LIVE_MARKET_DATA=false — not sending Discord alerts")
        for a in all_alerts:
            log.info("  ALERT: %s %s — %,.0f RUB (fair: %,.0f, conf: %d)",
                     a["qlt_name"], a["item_name"], a["unit_price"],
                     a["fair_value"], a["confidence"])


# ─── Discord alerting ───────────────────────────────────────────────────────

async def send_discord_alerts(alerts: list[dict]):
    """Send profit alerts to the configured Discord webhook or channel."""
    if not alerts:
        return

    # Group by item to avoid spam
    for alert in alerts[:10]:  # cap at 10 alerts per scan
        embed = discord.Embed(
            title=f"💰 {alert['qlt_name']} {alert['item_name']}",
            description=f"Profitable listing detected on the NA auction",
            color=QUALITY_COLORS.get(alert["qlt"], 0x8fbc3f),
        )
        embed.add_field(
            name="Listing Price",
            value=f"{alert['buyout_price']:,.0f} RUB ({alert['amount']}x → {alert['unit_price']:,.0f}/unit)",
            inline=False,
        )
        embed.add_field(
            name="Price Model",
            value=(
                f"Fair Value: **{alert['fair_value']:,.0f}**\n"
                f"Quick Sale: {alert['quick_sale']:,.0f}\n"
                f"Stretch: {alert['stretch']:,.0f}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Confidence",
            value=f"{alert['confidence']}/100 ({alert['confidence_label']})",
            inline=True,
        )
        if alert["live_cap"]:
            embed.add_field(
                name="Live Cap",
                value=f"{alert['live_cap']:,.0f}/unit (cheapest comparable)",
                inline=True,
            )
        embed.add_field(
            name="Margin",
            value=f"{alert['margin_pct']:.1f}% below fair value",
            inline=False,
        )
        embed.set_footer(text="StalZone Price Tracker — confidence-weighted model")

        # Try webhook first
        if DISCORD_ALERT_WEBHOOK:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    webhook = discord.Webhook.from_url(DISCORD_ALERT_WEBHOOK, session=session)
                    await webhook.send(embed=embed)
            except Exception as e:
                log.error("Failed to send webhook alert: %s", e)
        else:
            # Send to first available guild channel
            for guild in bot.guilds:
                if TARGET_GUILD_IDS and guild.id not in TARGET_GUILD_IDS:
                    continue
                for channel in guild.text_channels:
                    try:
                        await channel.send(embed=embed)
                        break
                    except discord.Forbidden:
                        continue
                    except Exception:
                        continue
                break

        # Dedup: wait before sending next alert
        await asyncio.sleep(0.5)


# ─── Background tasks ───────────────────────────────────────────────────────

@tasks.loop(seconds=SCAN_INTERVAL)
async def scan_loop():
    """Periodic scan loop — runs every SCAN_INTERVAL seconds."""
    try:
        await scan_all_items()
    except Exception as e:
        log.error("Scan loop error: %s", e)


@tasks.loop(hours=6)
async def history_ingest_loop():
    """Periodic price history ingestion — runs every 6 hours."""
    try:
        log.info("=== Starting price history ingestion ===")
        result = await asyncio.to_thread(
            ingest_live_data,
            db=market_db,
            tracked_items=tracked_items,
            region=REGION,
            fetch_lots=False,
            fetch_history=True,
        )
        log.info("History ingestion complete: %s", result)
    except Exception as e:
        log.error("History ingestion error: %s", e)


@tasks.loop(hours=24)
async def community_collection_loop():
    """Daily community signal collection — runs every 24 hours."""
    try:
        log.info("=== Starting community signal collection ===")
        signals = community_sources.collect_all_community_signals(
            item_list=list(tracked_items.values())[:20],  # cap at 20 items
            region=REGION,
        )
        if market_db and signals:
            for s in signals:
                market_db.record_community_signal(
                    item_name=s.get("item_name", ""),
                    source=s.get("source", "unknown"),
                    signal_type=s.get("signal_type", "price_claim"),
                    claimed_value=s.get("claimed_value"),
                    text=s.get("text", ""),
                    url=s.get("url", ""),
                    sentiment=s.get("sentiment", "neutral"),
                    region=REGION,
                )
            log.info("Community collection complete: %d signals", len(signals))
        else:
            log.info("Community collection complete: 0 signals")
    except Exception as e:
        log.error("Community collection error: %s", e)


# ─── Bot events ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %d)", bot.user, bot.user.id)
    log.info("Connected to %d guild(s)", len(bot.guilds))

    # Initialize API and DB
    await init_api()
    init_db()

    # Sync commands
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands", len(synced))
    except Exception as e:
        log.error("Failed to sync commands: %s", e)

    # Start background tasks
    if not scan_loop.is_running():
        scan_loop.start()
    if not history_ingest_loop.is_running():
        history_ingest_loop.start()
    if not community_collection_loop.is_running():
        community_collection_loop.start()

    log.info("Bot ready! Scan interval: %ds, Lot limit: %d", SCAN_INTERVAL, LOT_LIMIT)


# ─── Slash commands ─────────────────────────────────────────────────────────

@bot.tree.command(name="status", description="Show bot status and tracked items")
async def cmd_status(interaction: discord.Interaction):
    """Show bot status."""
    embed = discord.Embed(title="StalZone Bot Status", color=0x8fbc3f)
    embed.add_field(name="Tracked Items", value=str(len(tracked_items)), inline=True)
    embed.add_field(name="Region", value=REGION.upper(), inline=True)
    embed.add_field(name="Lot Limit", value=str(LOT_LIMIT), inline=True)
    embed.add_field(name="Scan Interval", value=f"{SCAN_INTERVAL}s", inline=True)
    embed.add_field(name="Live Market Data", value="✅ Yes" if LIVE_MARKET_DATA else "❌ No (demo mode)", inline=True)
    embed.add_field(name="Min Confidence", value=str(MIN_ALERT_CONFIDENCE), inline=True)

    # Count observations in MarketDB
    if market_db:
        try:
            rows = market_db.get_rows(
                "SELECT COUNT(*) as cnt FROM auction_snapshot WHERE region=?",
                [REGION],
            )
            snap_count = rows[0]["cnt"] if rows else 0
            rows = market_db.get_rows(
                "SELECT COUNT(*) as cnt FROM sale_observation WHERE region=?",
                [REGION],
            )
            sale_count = rows[0]["cnt"] if rows else 0
            embed.add_field(name="Snapshots", value=str(snap_count), inline=True)
            embed.add_field(name="Sales", value=str(sale_count), inline=True)
        except Exception:
            pass

    embed.set_footer(text="StalZone Price Tracker — Integrated Edition")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="scan", description="Manually trigger a scan of all tracked items")
async def cmd_scan(interaction: discord.Interaction):
    """Manually trigger a scan."""
    await interaction.response.send_message("🔍 Starting manual scan...")
    alerts = await scan_all_items()
    if alerts:
        await interaction.followup.send(f"✅ Scan complete: {len(alerts)} profitable lots found!")
        if LIVE_MARKET_DATA:
            await send_discord_alerts(alerts)
    else:
        await interaction.followup.send("✅ Scan complete: no profitable lots found.")


@bot.tree.command(name="price", description="Get price valuation for an artifact")
@app_commands.describe(item_name="Name of the artifact")
async def cmd_price(interaction: discord.Interaction, item_name: str):
    """Get a price valuation for an artifact using the price model."""
    # Search for the item in tracked_items
    item_id = None
    actual_name = item_name
    for iid, name in tracked_items.items():
        if item_name.lower() in name.lower():
            item_id = iid
            actual_name = name
            break

    if not item_id:
        await interaction.response.send_message(f"❌ Artifact '{item_name}' not found in tracked items.")
        return

    # Fetch lots to get quality info
    try:
        listing = await asyncio.to_thread(
            api_client.auction(item_id).lots,
            limit=LOT_LIMIT,
            sort=SortAuction.BUYOUT_PRICE,
            order=Order.ASC,
            additional=True,
            region=REGION,
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to fetch auction data: {e}")
        return

    lots = list(listing)
    if not lots:
        await interaction.response.send_message(f"❌ No active listings for {actual_name}.")
        return

    # Group by quality tier
    by_tier: dict[int, list] = {}
    for lot in lots:
        qlt, bonus = get_lot_quality(lot)
        if qlt is None:
            continue
        by_tier.setdefault(qlt, []).append(lot)

    if not by_tier:
        await interaction.response.send_message(f"❌ Could not determine quality for any {actual_name} listings.")
        return

    embed = discord.Embed(
        title=f"💰 {actual_name} — Price Valuation",
        description=f"NA Auction — {len(lots)} active listings",
        color=0x8fbc3f,
    )

    for qlt in sorted(by_tier.keys()):
        tier_lots = by_tier[qlt]
        # Get valuation from price model
        first_lot = tier_lots[0]
        _, bonus = get_lot_quality(first_lot)

        result = evaluate_lot_with_model(market_db, item_id, actual_name, qlt, bonus)

        buyout = getattr(first_lot, "buyout_price", 0) or 0
        amount = getattr(first_lot, "amount", 1) or 1
        unit_price = buyout / max(amount, 1)
        cap, _ = live_resale_cap_per_unit(lots, item_id, qlt, bonus, first_lot)

        tier_name = QUALITY_NAMES.get(qlt, f"Q{qlt}")
        embed.add_field(
            name=f"{tier_name} ({len(tier_lots)} listings)",
            value=(
                f"Cheapest: **{unit_price:,.0f}**/unit\n"
                f"Fair Value: {result.fair_value:,.0f}\n"
                f"Quick Sale: {result.quick_sale:,.0f}\n"
                f"Stretch: {result.stretch:,.0f}\n"
                f"Confidence: {result.confidence}/100 ({result.confidence_label})"
                + (f"\nLive Cap: {cap:,.0f}/unit" if cap else "")
            ),
            inline=True,
        )

    embed.set_footer(text="StalZone Price Tracker — confidence-weighted model")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="items", description="List all tracked artifacts")
async def cmd_items(interaction: discord.Interaction):
    """List all tracked artifacts."""
    if not tracked_items:
        await interaction.response.send_message("No tracked items loaded.")
        return

    embed = discord.Embed(title="Tracked Artifacts", color=0x8fbc3f)
    items_text = "\n".join(f"• {name}" for name in list(tracked_items.values())[:25])
    if len(tracked_items) > 25:
        items_text += f"\n... and {len(tracked_items) - 25} more"
    embed.description = items_text
    embed.set_footer(text=f"Total: {len(tracked_items)} artifacts")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ingest", description="Pull live auction data + price history into the tracker")
@app_commands.describe(mode="lots, history, or both")
async def cmd_ingest(interaction: discord.Interaction, mode: str = "both"):
    """Manually trigger live data ingestion."""
    if mode not in ("lots", "history", "both"):
        await interaction.response.send_message("❌ Mode must be: lots, history, or both")
        return

    await interaction.response.send_message(f"📥 Starting live data ingestion ({mode})...")

    fetch_lots = mode in ("lots", "both")
    fetch_history = mode in ("history", "both")

    try:
        result = await asyncio.to_thread(
            ingest_live_data,
            market_db,
            tracked_items,
            REGION,
            fetch_lots,
            fetch_history,
        )
        await interaction.followup.send(
            f"✅ Ingestion complete!\n"
            f"   Lots: {result['lots_recorded']}\n"
            f"   Sales: {result['sales_recorded']}\n"
            f"   Items: {result['items_processed']}\n"
            f"   Errors: {result['errors']}"
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Ingestion failed: {e}")


@bot.tree.command(name="reload", description="Reload tradeable artifacts from the database")
async def cmd_reload(interaction: discord.Interaction):
    """Reload the tradeable artifacts list."""
    await interaction.response.send_message("🔄 Reloading artifacts...")
    try:
        global tracked_items
        tracked_items = await load_tradeable_artifacts()
        await interaction.followup.send(f"✅ Reloaded {len(tracked_items)} artifacts.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to reload: {e}")


@bot.tree.command(name="ping", description="Check bot latency")
async def cmd_ping(interaction: discord.Interaction):
    """Check bot latency."""
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latency: {latency_ms}ms")


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN must be set in .env")
        return

    if not EXBO_CLIENT_ID or not EXBO_CLIENT_SECRET:
        print("ERROR: EXBO_CLIENT_ID and EXBO_CLIENT_SECRET must be set in .env")
        print("Get them at https://eapi.stalcraft.net (OAuth2 credentials)")
        return

    print("=" * 60)
    print("  StalZone NA Artifact Sniper — Integrated Edition")
    print("=" * 60)
    print(f"  Region:       {REGION.upper()}")
    print(f"  Lot Limit:    {LOT_LIMIT}")
    print(f"  Scan Interval: {SCAN_INTERVAL}s")
    print(f"  Live Market:  {'Yes' if LIVE_MARKET_DATA else 'No (demo mode)'}")
    print(f"  Min Confidence: {MIN_ALERT_CONFIDENCE}")
    print("=" * 60)

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
