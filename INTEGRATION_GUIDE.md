# Integration Guide — Connecting Your Existing Bot to the Price Tracker

This guide shows exactly what to add and change in your existing `bot.py`
to connect it to the new price-tracking system. You keep your existing bot
structure, commands, and PriceDB — you just add the new modules alongside them.

## Step 0: Copy the tracker files

Copy these files from the StalZone Price Tracker zip into the same
folder as your existing `bot.py`:

```
your-bot-folder/
├── bot.py              ← your existing bot (modify this)
├── market_db.py        ← NEW (from the tracker)
├── price_model.py      ← NEW
├── bot_integration.py  ← NEW
├── live_ingestion.py   ← NEW
├── community_sources.py ← NEW
├── daily_monitor.py    ← NEW
├── cache/
│   ├── prices.db       ← your existing PriceDB (unchanged)
│   └── market.db       ← NEW (auto-created by MarketDB)
└── .env
```

## Step 1: Add imports (top of bot.py)

Find your existing imports (around line 55-61 in your bot):

```python
from scapi import AppClient, DatabaseLookup
from scapi.enums import Order, Region, Realm, SortAuction
from scapi.exceptions import NotFoundError, RateLimitError, RequestError
```

Add these right below them:

```python
# --- Price tracker integration ---
from market_db import MarketDB, bonus_bucket
from bot_integration import (
    get_lot_quality as get_lot_quality_fixed,
    lot_key as lot_key_fixed,
    has_cheaper_comparable_listing,
    live_resale_cap_per_unit,
    record_observations,
    record_sale,
    evaluate_lot_with_model,
    should_alert_lot,
)
from live_ingestion import ingest_live_data, load_tradeable_artifacts as load_tradeable_artifacts_tracker
```

## Step 2: Initialize MarketDB (in your bot startup)

Find where you initialize `pricedb` (your PriceDB). Add this right after it:

```python
# --- Price tracker DB (for dashboard + price model) ---
market_db = MarketDB()  # creates cache/market.db automatically
```

Add `market_db` to your global variables section, near where you define
`pricedb`, `api_client`, `TRACKED_ITEMS`, etc.

## Step 3: Replace get_lot_quality() with the fixed version

Find your existing `get_lot_quality()` function and replace it with the
fixed version that returns `None` for unknown quality instead of defaulting
to Common:

```python
def get_lot_quality(lot) -> tuple[int, float] | None:
    """Return (quality_tier, upgrade_bonus) for a lot, or None if unknown.

    FIXED: No longer defaults to Common when quality can't be parsed.
    This was hiding Rare, Exclusive, and Legendary artifacts.
    """
    additional = getattr(lot, "additional", None)
    if not additional or not isinstance(additional, dict):
        return None

    qlt = additional.get("qlt")
    if qlt is None:
        qlt = additional.get("quality")
    if qlt is None:
        return None  # CRITICAL: return None, not 0 (Common)

    try:
        qlt = int(qlt)
        if not 0 <= qlt <= 5:
            return None
    except (ValueError, TypeError):
        return None

    upgrade_bonus = additional.get("upgrade_bonus")
    if isinstance(upgrade_bonus, dict):
        upgrade_bonus = upgrade_bonus.get("value", upgrade_bonus.get("amount"))
    if upgrade_bonus is not None:
        try:
            upgrade_bonus = float(upgrade_bonus)
            if upgrade_bonus > 1.0:
                upgrade_bonus /= 100.0
        except (ValueError, TypeError):
            upgrade_bonus = 0.0
    else:
        upgrade_bonus = 0.0

    return qlt, max(0.0, upgrade_bonus)
```

## Step 4: Fix the lot key (in your PriceDB class)

Find `_lot_key()` in your PriceDB class and update it to use `start_time`
for uniqueness:

```python
@staticmethod
def _lot_key(item_id: str, lot: Any) -> str:
    """Generate a unique lot key using start_time to prevent collisions."""
    buyout_price = getattr(lot, "buyout_price", 0) or 0
    amount = getattr(lot, "amount", 1) or 1
    start_time = getattr(lot, "start_time", None)
    start_str = start_time.isoformat() if hasattr(start_time, "isoformat") else str(start_time)
    return f"{item_id}_{start_str}_{buyout_price}_{amount}"
```

## Step 5: Add live comparable checks to scan_item()

In your `scan_item()` function, find where you process each lot:

```python
quality = get_lot_quality(lot)
if quality is None:
    continue

qlt, bonus = quality
```

Add these two lines right after `qlt, bonus = quality`:

```python
        # --- NEW: Live comparable listing check ---
        # Skip this lot if there's a cheaper comparable listing of the
        # same quality tier and bonus bucket. This prevents the bot from
        # alerting "huge profit" when the same artifact is listed cheaper.
        if has_cheaper_comparable_listing(list(lots), item_id, qlt, bonus, lot):
            continue

        # --- NEW: Get the live resale cap (hard ceiling) ---
        live_cap = live_resale_cap_per_unit(list(lots), item_id, qlt, bonus)
```

Then find where you calculate `unit_est` (your estimated resale value).
After that calculation, add:

```python
        # --- NEW: Cap the estimate at the live market ceiling ---
        if live_cap is not None:
            # Never let history inflate above what the live market supports
            unit_est = min(unit_est, live_cap)
            # If the candidate is already at or above the live cap, skip it
            candidate_unit = buyout / amount
            if candidate_unit >= live_cap:
                continue
```

## Step 6: Record observations to MarketDB

In your `scan_item()` function, right after you fetch lots successfully
(after `lots = await api_client.auction(item_id).lots(...)`), add:

```python
    # --- NEW: Record all lots as snapshots in the market tracker DB ---
    # This feeds the dashboard and the price model with live data.
    record_observations(market_db, item_id, item_name, list(lots), str(REGION).lower())
```

And in your sale-detection code (where you detect a lot disappeared),
add a call to record the sale in MarketDB:

```python
    # --- NEW: Record the sale in the market tracker DB ---
    record_sale(
        db=market_db,
        item_id=item_id,
        item_name=item_name,
        qlt=qlt,
        upgrade_bonus=bonus,
        unit_price=buyout / amount,
        amount=amount,
        region=str(REGION).lower(),
        source="inferred_buyout_sale",
        confidence=0.60,
    )
```

## Step 7: Add the !ingest command

Add this new command to your bot (anywhere near your other commands):

```python
@bot.command()
async def ingest(ctx: commands.Context, mode: str = "both") -> None:
    """Pull live auction data + price history into the tracker.

    Usage:
      !ingest lots     - fetch active auction lots only
      !ingest history  - fetch price history only
      !ingest both     - fetch both (default)
    """
    if mode not in ("lots", "history", "both"):
        await ctx.send("Usage: !ingest lots|history|both")
        return

    await ctx.send(f"📥 Starting live data ingestion ({mode})...")

    fetch_lots = mode in ("lots", "both")
    fetch_history = mode in ("history", "both")

    # Build tracked_items dict for the ingestion
    items_dict = {item["id"]: item["name"] for item in TRACKED_ITEMS}

    try:
        result = ingest_live_data(
            db=market_db,
            tracked_items=items_dict,
            region=str(REGION).lower(),
            fetch_lots=fetch_lots,
            fetch_history=fetch_history,
        )
        await ctx.send(
            f"✅ Ingestion complete!\n"
            f"   Lots: {result['lots_recorded']}\n"
            f"   Sales: {result['sales_recorded']}\n"
            f"   Items: {result['items_processed']}\n"
            f"   Errors: {result['errors']}"
        )
    except Exception as e:
        await ctx.send(f"❌ Ingestion failed: {e}")
```

## Step 8: Add the !valuation command (optional but recommended)

This command uses the confidence-weighted price model instead of static
multipliers:

```python
@bot.command()
async def valuation(ctx: commands.Context, *, name: str) -> None:
    """Show the price-model valuation for an artifact (all quality tiers).

    Uses the confidence-weighted multi-source model instead of static
    multipliers. Shows fair value, quick sale, stretch, and confidence.
    """
    match = next((i for i in TRACKED_ITEMS if i["name"].lower() == name.lower()), None)
    if not match:
        match = next((i for i in TRACKED_ITEMS if name.lower() in i["name"].lower()), None)
    if not match:
        await ctx.send(f"No tracked artefact matching '{name}'.")
        return

    item_id = match["id"]
    item_name = match["name"]

    # Fetch lots to find which quality tiers are available
    try:
        lots = await api_client.auction(item_id).lots(
            limit=100, sort=SortAuction.BUYOUT_PRICE, order=Order.ASCENDING,
            additional=True, region=REGION,
        )
    except Exception as e:
        await ctx.send(f"Error fetching lots: {e}")
        return

    # Group by quality tier
    by_tier: dict[int, list] = {}
    for lot in lots:
        q = get_lot_quality(lot)
        if q is None:
            continue
        by_tier.setdefault(q[0], []).append(lot)

    if not by_tier:
        await ctx.send(f"No quality data available for **{item_name}**.")
        return

    lines = [f"**{item_name}** — Price Model Valuation:"]
    for qlt in sorted(by_tier.keys()):
        tier_lots = by_tier[qlt]
        _, bonus = get_lot_quality(tier_lots[0])

        # Use the price model
        result = evaluate_lot_with_model(
            market_db, item_id, item_name, qlt, bonus, str(REGION).lower()
        )

        buyout = getattr(tier_lots[0], "buyout_price", 0) or 0
        amount = getattr(tier_lots[0], "amount", 1) or 1
        unit_price = buyout / max(amount, 1)
        cap = live_resale_cap_per_unit(list(lots), item_id, qlt, bonus)

        tier_name = QUALITY_NAMES.get(qlt, f"Q{qlt}")
        lines.append(
            f"• {tier_name} ({len(tier_lots)} listings): "
            f"Cheapest {fmt_rub(int(unit_price))} | "
            f"Fair {fmt_rub(int(result.fair_value))} | "
            f"Quick {fmt_rub(int(result.quick_sale))} | "
            f"Stretch {fmt_rub(int(result.stretch))} | "
            f"Conf {result.confidence}/100 ({result.confidence_label})"
            + (f" | Cap {fmt_rub(int(cap))}" if cap else "")
        )

    await ctx.send("\n".join(lines))
```

## Step 9: Update send_alert() to show live cap and confidence

Find your `send_alert()` function and add these parameters:

```python
async def send_alert(
    channel, item_name, item_id, lot, buyout, amount, qlt, bonus,
    est_val, net_yield, net_profit, pct,
    source: str = "",
    live_cap_per_unit: float | None = None,
    comparable_count: int = 0,
    confidence: int = 0,
) -> None:
```

Add these fields to the embed (before the profit field):

```python
    if live_cap_per_unit is not None:
        embed.add_field(
            name="Live Comparable Floor",
            value=f"{fmt_rub(int(live_cap_per_unit))} RUB/unit\n{comparable_count} comparable listing(s)",
            inline=True,
        )
    else:
        embed.add_field(
            name="Live Comparable Floor",
            value="No comparable current listings",
            inline=True,
        )

    if confidence > 0:
        conf_label = "High" if confidence >= 70 else "Medium" if confidence >= 45 else "Low"
        embed.add_field(
            name="Confidence",
            value=f"{confidence}/100 ({conf_label})",
            inline=True,
        )
```

Then update the call in `scan_item()` to pass the new args:

```python
    await send_alert(
        channel, item_name, item_id, lot, buyout, amount, qlt, bonus,
        est_val, net_yield, net_profit, net_profit_pct,
        source, live_cap, comparable_count, confidence,
    )
```

## Step 10: Add .env variables

Add these to your existing `.env` file:

```env
# --- Price tracker ---
LIVE_MARKET_DATA=false
LOT_LIMIT=100
MIN_ALERT_CONFIDENCE=65
MIN_MARGIN_PCT=15

# --- Community data (optional) ---
# YOUTUBE_API_KEY=your_youtube_api_key
# YOUTUBE_RESEARCH_ENABLED=false
# DISCORD_ALERT_WEBHOOK=https://discord.com/api/webhooks/...
```

Set `LIVE_MARKET_DATA=true` once you've verified the bot is recording
data correctly. This enables Discord alert dispatch from the daily monitor.

## Step 11: Increase AUCTION_LOT_LIMIT

In your configuration section, change:

```python
AUCTION_LOT_LIMIT = int(os.getenv("AUCTION_LOT_LIMIT", "100"))
```

And use `AUCTION_LOT_LIMIT` instead of `40` in your `scan_item()` lots
fetch call.

## Quick Summary of Changes

| What | Where | Change |
|------|-------|--------|
| Imports | Top of bot.py | Add market_db, bot_integration, live_ingestion imports |
| MarketDB init | Near pricedb init | Add `market_db = MarketDB()` |
| get_lot_quality() | Replace function | Return None for unknown quality |
| _lot_key() | In PriceDB class | Use start_time for uniqueness |
| scan_item() | After quality check | Add has_cheaper_comparable_listing check |
| scan_item() | After estimate | Cap at live_resale_cap_per_unit |
| scan_item() | After lots fetch | Add record_observations() call |
| scan_item() | Sale detection | Add record_sale() call |
| send_alert() | Function signature | Add live_cap, confidence params |
| New: !ingest | New command | Pull live data via scapi |
| New: !valuation | New command | Price model valuation per tier |
| .env | Config file | Add LIVE_MARKET_DATA, LOT_LIMIT, etc. |
| AUCTION_LOT_LIMIT | Config | Change from 40 to 100 |

## After Making Changes

1. Restart your bot: `python bot.py`
2. Run `!clearprices` to clear old contaminated sale data
3. Run `!ingest both` to pull live auction data + price history
4. Run `!dbstats` to verify sales are being recorded
5. Run `!valuation Firebird` to see the price model in action
6. Start the dashboard: `uvicorn dashboard.server:app --port 8420`
7. Open `http://localhost:8420` — it should show LIVE DATA

The dashboard will automatically switch from "SAMPLE DATA" to "LIVE DATA"
once your bot writes real observations to MarketDB.
