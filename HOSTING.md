# Free Hosting Guide — StalZone Price Tracker

This guide explains how to host the bot and dashboard online for free.

## Architecture Overview

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Discord Bot   │────▶│  MarketDB (SQLite)│◀────│  Dashboard API  │
│  (bot.py)       │     │  market.db        │     │  (server.py)    │
│                 │     │                   │     │                 │
│  • Scans lots   │     │  • Snapshots      │     │  • Serves API   │
│  • Ingests data │     │  • Sales          │     │  • Serves HTML  │
│  • Sends alerts │     │  • Valuations     │     │  • Chart.js     │
└─────────────────┘     └──────────────────┘     └─────────────────┘
        │                                               │
        ▼                                               ▼
┌─────────────────┐                          ┌─────────────────┐
│  StalCraft API  │                          │  Web Browser    │
│  (eapi.stalcraft)│                         │  (you)          │
└─────────────────┘                          └─────────────────┘
```

The bot and dashboard share the same SQLite database file (`cache/market.db`).
They must run on the same machine (or share the file via a network mount).

## Option 1: Render.com (Recommended — Free Tier)

Render offers a free tier that can host both the bot and dashboard.

### Step 1: Push to GitHub

```bash
cd stalzone-price-tracker
git init
git add .
git commit -m "StalZone Price Tracker"
git remote add origin https://github.com/YOUR_USERNAME/stalzone-price-tracker.git
git push -u origin main
```

### Step 2: Host the Dashboard (Free Web Service)

1. Go to [render.com](https://render.com) and sign up
2. Click **New** → **Web Service**
3. Connect your GitHub repo
4. Configure:
   - **Name:** stalzone-dashboard
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn dashboard.server:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Free
5. Add environment variables:
   - `REGION` = `na`
6. Deploy

The dashboard will be available at `https://stalzone-dashboard.onrender.com`

### Step 3: Host the Bot (Free Background Worker)

1. On Render, click **New** → **Background Worker**
2. Connect the same GitHub repo
3. Configure:
   - **Name:** stalzone-bot
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance Type:** Free
4. Add environment variables:
   - `DISCORD_TOKEN` = your Discord bot token
   - `EXBO_CLIENT_ID` = your EXBO OAuth client ID
   - `EXBO_CLIENT_SECRET` = your EXBO OAuth client secret
   - `REGION` = `na`
   - `LIVE_MARKET_DATA` = `true`
   - `LOT_LIMIT` = `100`
   - `SCAN_INTERVAL` = `300`
5. Deploy

**Important:** The free tier sleeps after 15 minutes of inactivity. The bot
will restart automatically when it receives a Discord message. To keep it
awake, you can use a free uptime monitor like [UptimeRobot](https://uptimerobot.com)
to ping the dashboard URL every 5 minutes (which keeps the dashboard warm,
though the bot worker runs independently).

### Shared Database Note

On Render, each service gets its own filesystem. To share the SQLite database
between the bot and dashboard, you have two options:

**Option A: Run bot + dashboard in one service (simpler)**

Merge both into a single web service that runs the FastAPI dashboard and
launches the bot as a background task:

```python
# combined.py
import asyncio
import threading
from dashboard.server import app
from bot import bot

def run_bot():
    asyncio.run(bot.start(os.getenv("DISCORD_TOKEN")))

# Start bot in background thread
threading.Thread(target=run_bot, daemon=True).start()

# Run the dashboard in the main thread (required by Render)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8420)))
```

Use this as the start command: `python combined.py`

**Option B: Use a free PostgreSQL database**

Use Render's free PostgreSQL (90 days) or [Supabase](https://supabase.com)
(free tier, permanent) instead of SQLite. This requires modifying `market_db.py`
to use `psycopg2` instead of `sqlite3`.

## Option 2: Railway.app (Free Trial + Cheap)

[Railway](https://railway.app) gives you $5 free credit and is very easy:

1. Go to [railway.app](https://railway.app)
2. **New Project** → **Deploy from GitHub repo**
3. Configure start command: `python bot.py`
4. Add all environment variables
5. For the dashboard, create a second service in the same project with
   start command: `uvicorn dashboard.server:app --host 0.0.0.0 --port $PORT`

Railway doesn't sleep, so the bot stays online 24/7. The $5 credit lasts
about 1-2 months for a small bot.

## Option 3: Fly.io (Free Allowance)

[Fly.io](https://fly.io) offers a free allowance that can host small apps:

1. Install the Fly CLI: `curl -L https://fly.io/install.sh | sh`
2. Create a `fly.toml` in your project:

```toml
app = "stalzone-tracker"
primary_region = "lax"

[build]
  builder = "paketobuildpacks/builder:base"

[http_service]
  internal_port = 8420
  force_https = true

[vm]
  memory = "512mb"
  cpu_kind = "shared"
  cpus = 1
```

3. Deploy: `fly deploy`
4. Set secrets:
```bash
fly secrets set DISCORD_TOKEN=your_token
fly secrets set EXBO_CLIENT_ID=your_id
fly secrets set EXBO_CLIENT_SECRET=your_secret
fly secrets set LIVE_MARKET_DATA=true
```

## Option 4: Your Own Machine (Always Free)

If you have a computer that's always on (or a Raspberry Pi):

1. Install Python 3.11+
2. Clone the repo
3. `pip install -r requirements.txt`
4. Create a `.env` file with your credentials
5. Run both services:

```bash
# Terminal 1 — Dashboard
uvicorn dashboard.server:app --host 0.0.0.0 --port 8420

# Terminal 2 — Bot
python bot.py
```

To make the dashboard accessible from the internet, use a free tunnel:
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (free, permanent)
- [ngrok](https://ngrok.com) (free tier, temporary URL)
- [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) (free, permanent)

Example with Cloudflare Tunnel:
```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Create a tunnel
cloudflared tunnel create stalzone
cloudflared tunnel route dns stalzone stalzone.yourdomain.com

# Run the tunnel (points your domain to localhost:8420)
cloudflared tunnel run stalzone
```

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_TOKEN` | Yes (bot) | — | Discord bot token |
| `EXBO_CLIENT_ID` | Yes | — | EXBO OAuth2 client ID |
| `EXBO_CLIENT_SECRET` | Yes | — | EXBO OAuth2 client secret |
| `REGION` | No | `na` | StalCraft region |
| `LOT_LIMIT` | No | `100` | Lots to fetch per scan |
| `HISTORY_LIMIT` | No | `50` | Price history records per item |
| `SCAN_INTERVAL` | No | `300` | Seconds between scans |
| `DISCORD_ALERT_WEBHOOK` | No | — | Webhook URL for price alerts |
| `LIVE_MARKET_DATA` | No | `false` | Enable Discord alert dispatch |
| `MIN_ALERT_CONFIDENCE` | No | `65` | Min confidence to alert |
| `MIN_MARGIN_PCT` | No | `15` | Min profit margin % to alert |
| `YOUTUBE_API_KEY` | No | — | YouTube Data API key |
| `TRADE_CHANNEL_IDS` | No | — | Discord trade channel IDs (comma-separated) |
| `TRADE_FEED_URLS` | No | — | RSS feed URLs (comma-separated) |

## Getting EXBO API Credentials

1. Go to [eapi.stalcraft.net](https://eapi.stalcraft.net)
2. Log in with your StalCraft account
3. Go to **OAuth2** → **Create Application**
4. Set the redirect URI to `https://stalcraft.net` (or your dashboard URL)
5. Copy the **Client ID** and **Client Secret**
6. Put them in your `.env` file:

```
EXBO_CLIENT_ID=your_client_id_here
EXBO_CLIENT_SECRET=your_client_secret_here
```

## Getting a Discord Bot Token

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it "StalZone Tracker"
3. Go to **Bot** → **Add Bot**
4. Copy the **Token**
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**
6. Invite the bot to your server using the OAuth2 URL Generator (scope: `bot`,
   permissions: `Send Messages`, `Embed Links`, `Read Message History`)
7. Put the token in your `.env`:

```
DISCORD_TOKEN=your_discord_token_here
```

## Quick Start Checklist

- [ ] Create a Discord application and get the bot token
- [ ] Get EXBO API credentials from eapi.stalcraft.net
- [ ] Create a `.env` file with all required variables
- [ ] `pip install -r requirements.txt`
- [ ] Test locally: `python bot.py`
- [ ] Test dashboard locally: `uvicorn dashboard.server:app --port 8420`
- [ ] Push to GitHub
- [ ] Deploy to Render (or your chosen host)
- [ ] Set environment variables on the hosting platform
- [ ] Verify the bot is online in Discord
- [ ] Verify the dashboard loads in your browser
- [ ] Use `/ingest both` in Discord to pull live data
- [ ] Set `LIVE_MARKET_DATA=true` to enable Discord alerts
