from __future__ import annotations
import asyncio, logging, os
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
import discord
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI
import uvicorn

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stalzone")
TOKEN = os.getenv("DISCORD_TOKEN", "")
CLIENT_ID = os.getenv("EXBO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("EXBO_CLIENT_SECRET", "")
REGION = os.getenv("REGION", "na").lower()
REALM = os.getenv("REALM", "global").lower()
INTERVAL = max(60, int(os.getenv("SCAN_INTERVAL", "300")))
LIMIT = min(200, max(1, int(os.getenv("LOT_LIMIT", "100"))))
DB_URL = os.getenv("SUPABASE_DB_URL", "")
QUALITIES = {0:"Common", 1:"Uncommon", 2:"Special", 3:"Rare", 4:"Exclusive", 5:"Legendary", 6:"Unique"}
Q_CHOICES = [app_commands.Choice(name=n, value=i) for i,n in QUALITIES.items()]
L_CHOICES = [app_commands.Choice(name=f"+{i}", value=i) for i in range(16)]
api: Any = None
catalog: dict[str,str] = {}
bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
app = FastAPI()

@app.get("/")
@app.get("/health")
async def health():
    return {"ok": True, "discord_ready": bot.is_ready(), "artifacts": len(catalog)}

@contextmanager
def dbconn():
    if not DB_URL:
        raise RuntimeError("SUPABASE_DB_URL is missing. Add the Supabase Transaction Pooler connection string in Render.")
    conn = psycopg.connect(DB_URL, row_factory=dict_row, connect_timeout=15)
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def init_db():
    # Tables are provisioned by the Supabase migration; never create local/ephemeral storage.
    with dbconn() as c:
        c.execute("SELECT 1 FROM public.discord_artifact_watches LIMIT 1")
        c.execute("SELECT 1 FROM public.discord_sent_alerts LIMIT 1")

async def load_catalog():
    from scapi import DatabaseLookup
    # scapi.DatabaseLookup.get_all is asynchronous; await it before iterating.
    raw = await DatabaseLookup().get_all(realm=REALM)
    result = {}
    for item_id, item in raw.items():
        if not isinstance(item, dict): continue
        cats = " ".join(str(item.get(k) or "") for k in ("category","sub_category","subcategory","path", "type")).lower()
        if "artifact" not in cats and "artefact" not in cats and "/items/artefact/" not in str(item_id).lower():
            continue
        if item.get("tradeable") is False: continue
        name = item.get("name") or item.get("title") or item_id
        if isinstance(name, dict): name = name.get("en") or name.get("EN") or next(iter(name.values()), item_id)
        result[str(item_id)] = str(name)
    if not result:
        raise RuntimeError("Artifact catalog returned zero entries; refusing to run with an empty catalog.")
    return dict(sorted(result.items(), key=lambda x:x[1].casefold()))

def extra(lot):
    x = getattr(lot, "additional", None)
    return x if isinstance(x, dict) else {}

def quality_of(x):
    try: q = int(x.get("qlt", x.get("quality")))
    except (TypeError, ValueError): return None
    return q if q in QUALITIES else None

def level_of(x):
    # STALZONE auction terminology uses ptn for the +0..+15 sharpening/potential.
    # Some wrappers may expose a normalized upgrade_level alias instead.
    for key in ("ptn", "upgrade_level", "upgradeLevel", "upgrade_lvl"):
        if key in x:
            try: n = int(x[key])
            except (TypeError, ValueError): return None
            return n if 0 <= n <= 15 else None
    return None

def alert_once(key, watch_id):
    with dbconn() as c:
        row = c.execute(
            "INSERT INTO public.discord_sent_alerts(alert_key,watch_id) VALUES (%s,%s) "
            "ON CONFLICT(alert_key) DO NOTHING RETURNING alert_key",
            (key, watch_id),
        ).fetchone()
        return row is not None

def was_alerted(key):
    with dbconn() as c:
        return c.execute(
            "SELECT 1 FROM public.discord_sent_alerts WHERE alert_key=%s", (key,)
        ).fetchone() is not None

def watches():
    with dbconn() as c:
        return c.execute(
            "SELECT * FROM public.discord_artifact_watches WHERE enabled=true"
        ).fetchall()

async def fetch_lots(item_id):
    from scapi.enums import Order, SortAuction
    result = await asyncio.to_thread(api.auction(item_id).lots, limit=LIMIT,
        sort=SortAuction.BUYOUT_PRICE, order=Order.ASC, additional=True, region=REGION)
    return list(result)

async def scan_market():
    if api is None: return (0,0)
    grouped = {}
    for w in watches(): grouped.setdefault(w["item_id"], []).append(w)
    checked = sent = 0
    for item_id, item_watches in grouped.items():
        try: lots = await fetch_lots(item_id)
        except Exception:
            log.exception("Auction request failed for %s", item_id)
            continue
        for lot in lots:
            x = extra(lot); q = quality_of(x); level = level_of(x)
            if q is None or level is None: continue
            amount = max(1, int(getattr(lot,"amount",1) or 1))
            total = float(getattr(lot,"buyout_price",0) or 0)
            if total <= 0: continue
            price = total / amount; checked += 1
            started = getattr(lot,"start_time",None)
            if hasattr(started,"isoformat"): started = started.isoformat()
            lot_id = f"{item_id}:{started}:{total}:{amount}"
            for w in item_watches:
                if q != w["quality"] or level != w["level"] or price > w["max_price"]: continue
                alert_key = f"{lot_id}:{w['id']}"
                if was_alerted(alert_key): continue
                channel = bot.get_channel(int(w["channel_id"]))
                if channel is None:
                    try: channel = await bot.fetch_channel(int(w["channel_id"]))
                    except Exception:
                        log.warning("Unable to access alert channel %s", w["channel_id"]); continue
                embed = discord.Embed(title=f"Market alert: {w['item_name']}",
                    description=f"{QUALITIES[q]} +{level} is at or below your price limit.",
                    color=0x8FBC3F)
                embed.add_field(name="Price", value=f"{price:,.0f} RUB each")
                embed.add_field(name="Quantity", value=str(amount))
                embed.add_field(name="Your limit", value=f"{w['max_price']:,.0f} RUB")
                embed.add_field(name="Region", value=REGION.upper())
                try:
                    await channel.send(content=f"<@{w['user_id']}>", embed=embed)
                    alert_once(alert_key, w["id"])
                    sent += 1
                except Exception:
                    log.exception("Failed to send alert")
        await asyncio.sleep(0.8)
    log.info("Scan complete: %d listings checked, %d alerts sent", checked, sent)
    return checked, sent

async def scanner():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try: await scan_market()
        except Exception: log.exception("Scanner error")
        await asyncio.sleep(INTERVAL)

async def autocomplete_artifact(interaction, current):
    q = current.casefold()
    return [app_commands.Choice(name=n[:100], value=i)
        for i,n in catalog.items() if q in n.casefold() or q in i.casefold()][:25]

@bot.event
async def on_ready():
    global api, catalog
    log.info("Connected to Discord as %s", bot.user)
    if api is None:
        if not CLIENT_ID or not CLIENT_SECRET: raise RuntimeError("EXBO OAuth credentials are missing.")
        from scapi import AppClient
        api = AppClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
        catalog = await load_catalog()
        log.info("Loaded %d tradeable artifacts", len(catalog))
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands", len(synced))
        asyncio.create_task(scanner())

@bot.tree.command(name="watch", description="Watch an exact artifact rarity and enhancement")
@app_commands.describe(artifact="Artifact", quality="Rarity", level="Enhancement level", max_price="Maximum RUB per artifact")
@app_commands.choices(quality=Q_CHOICES, level=L_CHOICES)
@app_commands.autocomplete(artifact=autocomplete_artifact)
async def watch_cmd(interaction: discord.Interaction, artifact: str,
    quality: app_commands.Choice[int], level: app_commands.Choice[int],
    max_price: app_commands.Range[int,1,2000000000]):
    if interaction.guild_id is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True); return
    name = catalog.get(artifact)
    if not name:
        await interaction.response.send_message("Artifact not found. Try /reload_catalog.", ephemeral=True); return
    with dbconn() as c:
        c.execute("""INSERT INTO public.discord_artifact_watches
        (guild_id,user_id,channel_id,item_id,item_name,quality,level,max_price)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(guild_id,user_id,item_id,quality,level)
        DO UPDATE SET channel_id=excluded.channel_id,max_price=excluded.max_price,
                      enabled=true,updated_at=now()""",
        (str(interaction.guild_id),str(interaction.user.id),str(interaction.channel_id),
         artifact,name,quality.value,level.value,max_price))
    await interaction.response.send_message(
        f"Watching {name} — {quality.name} +{level.value}; alert at {max_price:,} RUB or less per item.",
        ephemeral=True)

@bot.tree.command(name="watches", description="List your active artifact alerts")
async def watches_cmd(interaction: discord.Interaction):
    with dbconn() as c:
        rows = c.execute("SELECT * FROM public.discord_artifact_watches WHERE guild_id=%s AND user_id=%s AND enabled=true",
            (str(interaction.guild_id),str(interaction.user.id))).fetchall()
    if not rows:
        await interaction.response.send_message("No active watches. Use /watch.",ephemeral=True); return
    lines = [f"• {w['item_name']} — {QUALITIES[w['quality']]} +{w['level']} ≤ {w['max_price']:,.0f} RUB" for w in rows[:30]]
    await interaction.response.send_message("\\n".join(lines),ephemeral=True)

@bot.tree.command(name="unwatch", description="Remove one of your artifact alerts")
@app_commands.describe(artifact="Artifact ID from the catalog", quality="Rarity", level="Enhancement level")
@app_commands.choices(quality=Q_CHOICES, level=L_CHOICES)
@app_commands.autocomplete(artifact=autocomplete_artifact)
async def unwatch_cmd(interaction: discord.Interaction, artifact: str,
    quality: app_commands.Choice[int], level: app_commands.Choice[int]):
    with dbconn() as c:
        cur = c.execute("DELETE FROM public.discord_artifact_watches WHERE guild_id=%s AND user_id=%s AND item_id=%s AND quality=%s AND level=%s",
            (str(interaction.guild_id),str(interaction.user.id),artifact,quality.value,level.value))
    await interaction.response.send_message("Watch removed." if cur.rowcount else "No matching watch.",ephemeral=True)

@bot.tree.command(name="scan", description="Run a market scan immediately")
async def scan_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True,thinking=True)
    checked,sent = await scan_market()
    await interaction.followup.send(f"Scan complete: {checked} listings checked; {sent} alerts sent.",ephemeral=True)

@bot.tree.command(name="status", description="Show tracker status")
async def status_cmd(interaction: discord.Interaction):
    with dbconn() as c:
        count = c.execute("SELECT COUNT(*) AS count FROM public.discord_artifact_watches WHERE enabled=true").fetchone()["count"]
    await interaction.response.send_message(
        f"Bot: {'online' if bot.is_ready() else 'connecting'} | Region: {REGION.upper()} | "
        f"Artifacts: {len(catalog)} | Watches: {count} | Scan: {INTERVAL}s", ephemeral=True)

@bot.tree.command(name="reload_catalog", description="Refresh artifact catalog")
async def reload_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True,thinking=True)
    try:
        global catalog
        catalog = await load_catalog()
        await interaction.followup.send(f"Catalog refreshed: {len(catalog)} artifacts.")
    except Exception as e:
        log.exception("Catalog refresh failed")
        await interaction.followup.send(f"Catalog refresh failed: {type(e).__name__}: {e}")

async def health_server():
    server = uvicorn.Server(uvicorn.Config(app,host="0.0.0.0",
        port=int(os.getenv("PORT","10000")),log_level="warning"))
    await server.serve()

async def main():
    if not TOKEN: raise RuntimeError("DISCORD_TOKEN is missing.")
    if not CLIENT_ID or not CLIENT_SECRET: raise RuntimeError("EXBO_CLIENT_ID and EXBO_CLIENT_SECRET are missing.")
    init_db()
    await asyncio.gather(bot.start(TOKEN),health_server())

if __name__ == "__main__":
    asyncio.run(main())
