"""
bot.py — StalZone NA Artifact Sniper Bot (Integrated Edition)

Discord bot + live STALZONE auction ingestion for the dashboard/model.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import discord
from discord import app_commands
from discord.ext import commands, tasks

from scapi import AppClient, DatabaseLookup
from scapi.enums import Order, SortAuction
from scapi.exceptions import NotFoundError

from market_db import MarketDB
from bot_integration import (
    get_lot_quality,
    lot_key,
    live_resale_cap_per_unit,
    record_observations,
    evaluate_lot_with_model,
    should_alert_lot,
)
from live_ingestion import load_tradeable_artifacts, ingest_live_data
import community_sources

# ─── Configuration ───────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
EXBO_CLIENT_ID = os.getenv("EXBO_CLIENT_ID", "")
EXBO_CLIENT_SECRET = os.getenv("EXBO_CLIENT_SECRET", "")
TARGET_GUILD_IDS = [int(x) for x in os.getenv("TARGET_GUILD_IDS", "").split(",") if x.strip()]
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0") or 0)
REGION = os.getenv("REGION", "na").lower()
LOT_LIMIT = int(os.getenv("LOT_LIMIT", os.getenv("AUCTION_LOT_LIMIT", "100")))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
DISCORD_ALERT_WEBHOOK = os.getenv("DISCORD_ALERT_WEBHOOK", "")
LIVE_MARKET_DATA = os.getenv("LIVE_MARKET_DATA", "false").lower() == "true"
MIN_ALERT_CONFIDENCE = int(os.getenv("MIN_ALERT_CONFIDENCE", "65"))
MIN_MARGIN_PCT = float(os.getenv("MIN_MARGIN_PCT", "15"))

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

QUALITY_NAMES = {0: "Common", 1: "Uncommon", 2: "Special", 3: "Rare", 4: "Exclusive", 5: "Legendary"}
QUALITY_COLORS = {
    0: 0x7a7f72,
    1: 0x8fbc3f,
    2: 0x5591c7,
    3: 0xa86fdf,
    4: 0xe8a317,
    5: 0xc63838,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("stalzone_bot")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

api_client: AppClient | None = None
db_lookup: DatabaseLookup | None = None
market_db: MarketDB | None = None
tracked_items: dict[str, str] = {}
recent_lots: dict[str, set[str]] = {}
last_sale_alerts: dict[str, float] = {}


def confidence_label(score: int) -> str:
    if score >= 70:
        return "High"
    if score >= 45:
        return "Medium"
    return "Low"


async def init_api():
    """Initialize the scapi client and discover tradeable artifacts."""
    global api_client, db_lookup, tracked_items

    if not EXBO_CLIENT_ID or not EXBO_CLIENT_SECRET:
        log.error("EXBO_CLIENT_ID and EXBO_CLIENT_SECRET must be set")
        return False

    api_client = AppClient(
        client_id=EXBO_CLIENT_ID,
        client_secret=EXBO_CLIENT_SECRET,
    )
    log.info("scapi AppClient initialised (region=%s)", REGION)

    db_lookup = DatabaseLookup()
    log.info("scapi DatabaseLookup initialised")

    try:
        tracked_items = await load_tradeable_artifacts()
        log.info("Loaded %d tradeable artifacts", len(tracked_items))
    except Exception as e:
        log.exception("Failed to load tradeable artifacts: %s", e)
        tracked_items = {}

    return True


def init_db():
    global market_db
    market_db = MarketDB()
    log.info("MarketDB initialised at %s", market_db.path)
    return market_db


async def scan_item(item_id: str, item_name: str) -> list[dict]:
    """Scan auction lots for one artifact and persist observations/valuations."""
    alerts: list[dict] = []

    try:
        listing = await api_client.auction(item_id).lots(
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
    log.info("  %s: %d lots fetched", item_name, len(lots))

    if market_db:
        await record_observations(market_db, item_id, item_name, lots, REGION)

    current_keys: set[str] = set()

    for lot in lots:
        qlt, upgrade_bonus = get_lot_quality(lot)
        if qlt is None:
            continue

        key = lot_key(item_id, lot)
        current_keys.add(key)

        should_alert, valuation, reason = await should_alert_lot(
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

        if should_alert and valuation and valuation.fair_value:
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
                "confidence_label": confidence_label(valuation.confidence),
                "live_cap": cap,
                "margin_pct": ((valuation.fair_value - unit_price) / valuation.fair_value * 100),
                "lot_key": key,
            })

    prev_keys = recent_lots.get(item_id, set())
    for dkey in prev_keys - current_keys:
        log.debug("  %s: lot disappeared (possible sale): %s", item_name, dkey)

    recent_lots[item_id] = current_keys
    return alerts


async def scan_all_items() -> list[dict]:
    if not tracked_items:
        log.warning("No tracked items to scan")
        return []

    log.info("=== Starting scan of %d items ===", len(tracked_items))
    all_alerts: list[dict] = []

    for i, (item_id, item_name) in enumerate(tracked_items.items(), 1):
        log.info("[%d/%d] Scanning %s", i, len(tracked_items), item_name)
        all_alerts.extend(await scan_item(item_id, item_name))
        await asyncio.sleep(1.0)

    log.info("=== Scan complete: %d alerts found ===", len(all_alerts))

    if all_alerts and LIVE_MARKET_DATA:
        await send_discord_alerts(all_alerts)
    elif all_alerts:
        log.info("Alerts found but LIVE_MARKET_DATA=false — not sending Discord alerts")

    return all_alerts


async def send_discord_alerts(alerts: list[dict]):
    """Send alerts only when LIVE_MARKET_DATA has enabled dispatch."""
    if not alerts:
        return

    for alert in alerts[:10]:
        embed = discord.Embed(
            title=f"💰 {alert['qlt_name']} {alert['item_name']}",
            description=f"Profitable listing detected on the {REGION.upper()} auction",
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
                f"Quick Sale: {(alert['quick_sale'] or 0):,.0f}\n"
                f"Stretch: {(alert['stretch'] or 0):,.0f}"
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
        embed.add_field(name="Margin", value=f"{alert['margin_pct']:.1f}% below fair value", inline=False)
        embed.set_footer(text="StalZone Price Tracker — confidence-weighted model")

        if DISCORD_ALERT_WEBHOOK:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    webhook = discord.Webhook.from_url(DISCORD_ALERT_WEBHOOK, session=session)
                    await webhook.send(embed=embed)
            except Exception as e:
                log.error("Failed to send webhook alert: %s", e)
        elif CHANNEL_ID:
            channel = bot.get_channel(CHANNEL_ID)
            if channel is None:
                log.error("Configured CHANNEL_ID %s is not visible to the bot", CHANNEL_ID)
            else:
                try:
                    await channel.send(embed=embed)
                except discord.Forbidden:
                    log.error("Bot lacks permission to send to CHANNEL_ID %s", CHANNEL_ID)
                except Exception as e:
                    log.error("Failed to send to CHANNEL_ID %s: %s", CHANNEL_ID, e)
        else:
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

        await asyncio.sleep(0.5)


@tasks.loop(seconds=SCAN_INTERVAL)
async def scan_loop():
    try:
        await scan_all_items()
    except Exception as e:
        log.exception("Scan loop error: %s", e)


@tasks.loop(hours=6)
async def history_ingest_loop():
    try:
        log.info("=== Starting price history ingestion ===")
        result = await ingest_live_data(
            db=market_db,
            tracked_items=tracked_items,
            region=REGION,
            fetch_lots=False,
            fetch_history=True,
        )
        log.info("History ingestion complete: %s", result)
    except Exception as e:
        log.exception("History ingestion error: %s", e)


@tasks.loop(hours=24)
async def community_collection_loop():
    try:
        log.info("=== Starting community signal collection ===")
        signals = await asyncio.to_thread(
            community_sources.collect_all_community_signals,
            item_filter=set(list(tracked_items.values())[:20]),
        )
        if market_db and signals:
            for s in signals:
                market_db.record_community_signal(
                    item_name=s.get("item_name", ""),
                    region=s.get("region", REGION),
                    claimed_price=s.get("claimed_price"),
                    sentiment=s.get("sentiment", "neutral"),
                    sentiment_score=s.get("sentiment_score", 0),
                    source=s.get("source", "unknown"),
                    source_name=s.get("source_name", ""),
                    url=s.get("url", ""),
                    excerpt=s.get("excerpt", ""),
                    confidence=s.get("confidence", 0.15),
                    collected_at=s.get("collected_at", time.time()),
                )
            log.info("Community collection complete: %d signals", len(signals))
        else:
            log.info("Community collection complete: 0 signals")
    except Exception as e:
        log.error("Community collection error: %s", e)


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %d)", bot.user, bot.user.id)
    log.info("Connected to %d guild(s)", len(bot.guilds))

    await init_api()
    init_db()

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands", len(synced))
    except Exception as e:
        log.error("Failed to sync commands: %s", e)

    if not scan_loop.is_running():
        scan_loop.start()
    if not history_ingest_loop.is_running():
        history_ingest_loop.start()
    if not community_collection_loop.is_running():
        community_collection_loop.start()

    log.info("Bot ready! Scan interval: %ds, Lot limit: %d", SCAN_INTERVAL, LOT_LIMIT)


@bot.tree.command(name="status", description="Show bot status and tracked items")
async def cmd_status(interaction: discord.Interaction):
    embed = discord.Embed(title="StalZone Bot Status", color=0x8fbc3f)
    embed.add_field(name="Tracked Items", value=str(len(tracked_items)), inline=True)
    embed.add_field(name="Region", value=REGION.upper(), inline=True)
    embed.add_field(name="Lot Limit", value=str(LOT_LIMIT), inline=True)
    embed.add_field(name="Scan Interval", value=f"{SCAN_INTERVAL}s", inline=True)
    embed.add_field(name="Live Market Data", value="✅ Yes" if LIVE_MARKET_DATA else "❌ No (verification mode)", inline=True)
    embed.add_field(name="Min Confidence", value=str(MIN_ALERT_CONFIDENCE), inline=True)

    if market_db:
        try:
            with market_db._conn() as conn:
                snap_count = conn.execute(
                    "SELECT COUNT(*) FROM auction_snapshot WHERE region=?", (REGION,)
                ).fetchone()[0]
                sale_count = conn.execute(
                    "SELECT COUNT(*) FROM sale_observation WHERE region=?", (REGION,)
                ).fetchone()[0]
            embed.add_field(name="Snapshots", value=str(snap_count), inline=True)
            embed.add_field(name="Sales", value=str(sale_count), inline=True)
        except Exception:
            pass

    embed.set_footer(text="StalZone Price Tracker — Integrated Edition")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="scan", description="Manually trigger a scan of all tracked items")
async def cmd_scan(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 Starting manual scan...")
    alerts = await scan_all_items()
    await interaction.followup.send(f"✅ Scan complete: {len(alerts)} profitable lots found.")


@bot.tree.command(name="price", description="Get price valuation for an artifact")
@app_commands.describe(item_name="Name of the artifact")
async def cmd_price(interaction: discord.Interaction, item_name: str):
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

    try:
        listing = await api_client.auction(item_id).lots(
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

    by_tier: dict[int, list] = {}
    for lot in lots:
        qlt, bonus = get_lot_quality(lot)
        if qlt is not None:
            by_tier.setdefault(qlt, []).append(lot)

    if not by_tier:
        await interaction.response.send_message(f"❌ Could not determine quality for any {actual_name} listings.")
        return

    embed = discord.Embed(
        title=f"💰 {actual_name} — Price Valuation",
        description=f"{REGION.upper()} Auction — {len(lots)} active listings",
        color=0x8fbc3f,
    )

    for qlt in sorted(by_tier):
        tier_lots = by_tier[qlt]
        first_lot = tier_lots[0]
        _, bonus = get_lot_quality(first_lot)
        result = await evaluate_lot_with_model(market_db, item_id, actual_name, qlt, bonus, REGION)

        buyout = getattr(first_lot, "buyout_price", 0) or 0
        amount = getattr(first_lot, "amount", 1) or 1
        unit_price = buyout / max(amount, 1)
        cap, _ = live_resale_cap_per_unit(lots, item_id, qlt, bonus, first_lot)

        tier_name = QUALITY_NAMES.get(qlt, f"Q{qlt}")
        embed.add_field(
            name=f"{tier_name} ({len(tier_lots)} listings)",
            value=(
                f"Cheapest: **{unit_price:,.0f}**/unit\n"
                f"Fair Value: {(result.fair_value or 0):,.0f}\n"
                f"Quick Sale: {(result.quick_sale or 0):,.0f}\n"
                f"Stretch: {(result.stretch or 0):,.0f}\n"
                f"Confidence: {result.confidence}/100 ({confidence_label(result.confidence)})"
                + (f"\nLive Cap: {cap:,.0f}/unit" if cap else "")
            ),
            inline=True,
        )

    embed.set_footer(text="StalZone Price Tracker — confidence-weighted model")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="items", description="List all tracked artifacts")
async def cmd_items(interaction: discord.Interaction):
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
    if mode not in ("lots", "history", "both"):
        await interaction.response.send_message("❌ Mode must be: lots, history, or both")
        return

    await interaction.response.send_message(f"📥 Starting live data ingestion ({mode})...")
    try:
        result = await ingest_live_data(
            market_db,
            tracked_items,
            REGION,
            mode in ("lots", "both"),
            mode in ("history", "both"),
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
    await interaction.response.send_message("🔄 Reloading artifacts...")
    try:
        global tracked_items
        tracked_items = await load_tradeable_artifacts()
        await interaction.followup.send(f"✅ Reloaded {len(tracked_items)} artifacts.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to reload: {e}")


@bot.tree.command(name="ping", description="Check bot latency")
async def cmd_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latency: {round(bot.latency * 1000)}ms")


def main():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN must be set")
        return

    if not EXBO_CLIENT_ID or not EXBO_CLIENT_SECRET:
        print("ERROR: EXBO_CLIENT_ID and EXBO_CLIENT_SECRET must be set")
        return

    print("=" * 60)
    print("  StalZone NA Artifact Sniper — Integrated Edition")
    print("=" * 60)
    print(f"  Region:        {REGION.upper()}")
    print(f"  Lot Limit:     {LOT_LIMIT}")
    print(f"  Scan Interval: {SCAN_INTERVAL}s")
    print(f"  Live Market:   {'Yes' if LIVE_MARKET_DATA else 'No (verification mode)'}")
    print(f"  Channel ID:    {'configured' if CHANNEL_ID else 'not configured'}")
    print("=" * 60)

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
