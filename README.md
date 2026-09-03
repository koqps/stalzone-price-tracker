# StalZone Price Tracker — Market Intelligence System

A price-tracking model for StalCraft/StalZone NA artifacts that integrates
official in-game auction data with community sentiment from trade forums and
official community hubs. Includes a daily deviation monitor, a web dashboard,
and a fully integrated Discord bot.

## Architecture

```
stalzone-price-tracker/
├── bot.py                    # Integrated Discord bot with all fixes
├── bot_integration.py        # Bridge: fixed get_lot_quality(), live cap, price model
├── live_ingestion.py          # Live auction data ingestion via scapi (lots + price history)
├── market_db.py              # SQLite market-intelligence database
├── price_model.py            # Confidence-weighted multi-source valuation model
├── community_sources.py     # Collectors: Reddit, Discord, staldata, YouTube, forums
├── daily_monitor.py          # Daily deviation detector + Discord alert dispatcher
├── dashboard/
│   ├── server.py             # FastAPI backend (serves API + static frontend)
│   └── static/
│       ├── index.html        # StalCraft-themed dark dashboard UI
│       └── app.js            # Frontend logic + Chart.js price trends
├── requirements.txt
├── README.md
└── HOSTING.md                # Free hosting guide
```

## What Changed: Rare/Exclusive/Legendary Fix

The previous bot silently treated unknown-quality artifacts as Common (qlt=0),
which meant Rare, Exclusive, and Legendary artifacts were never detected or
valued correctly. This is now fixed:

1. `get_lot_quality()` returns `None` for unknown quality instead of defaulting to Common
2. The scan loop skips lots where quality can't be determined (instead of recording them as Common)
3. `AUCTION_LOT_LIMIT` raised from 40 to 100 — catches expensive high-tier listings
4. Lot identity key uses `start_time` for uniqueness — prevents collisions
5. Live comparable listing check — never claims "big profit" when cheaper listings exist
6. Confidence-weighted price model replaces static multipliers

## Quick Start

### 1. Install dependencies

```bash
cd stalzone-price-tracker
pip install -r requirements.txt
```

### 2. Create a .env file

```bash
DISCORD_TOKEN=your_discord_bot_token
EXBO_CLIENT_ID=your_exbo_client_id
EXBO_CLIENT_SECRET=your_exbo_client_secret
REGION=na
LIVE_MARKET_DATA=true
LOT_LIMIT=100
SCAN_INTERVAL=300
```

### 3. Run the bot

```bash
python bot.py
```

### 4. Run the dashboard (separate terminal)

```bash
uvicorn dashboard.server:app --host 0.0.0.0 --port 8420
```

Open `http://localhost:8420` in your browser.

### 5. Pull live data

In Discord, use the `/ingest both` command to pull live auction lots and
price history into the tracker. The dashboard will switch from "SAMPLE DATA"
to "LIVE DATA" automatically.

## Discord Commands

| Command | Description |
|---------|-------------|
| `/status` | Show bot status, tracked items, and database stats |
| `/scan` | Manually trigger a scan of all tracked items |
| `/price <item_name>` | Get price valuation for an artifact (all quality tiers) |
| `/items` | List all tracked artifacts |
| `/ingest <mode>` | Pull live auction data (lots, history, or both) |
| `/reload` | Reload tradeable artifacts from the stalcraft-database |
| `/ping` | Check bot latency |

## Price Model — Evidence Hierarchy

| Priority | Source | Role |
|----------|--------|------|
| 1 | Live comparable auction listings | Hard ceiling/floor — never overridden |
| 2 | Confirmed completed sales | Strongest historical evidence |
| 3 | Inferred (disappearance) sales | Weaker, conservative percentile |
| 4 | Official mixed-quality price history | Cold-start fallback only |
| 5 | Community signals (Reddit/Discord/forums/staldata/YouTube) | Confidence adjustment ONLY |
| 6 | Manual human-verified overrides | Can widen range, still capped by live market |

## Community Data Sources

| Source | Method | Auth Required | Confidence Weight |
|--------|--------|---------------|-------------------|
| Reddit r/Stalcraft | Public JSON search | None | 0.12 |
| Discord trade channels | REST API | Bot token | 0.15 |
| staldata.org | Public API | None | 0.20 |
| YouTube guides | YouTube Data API | API key | 0.10 |
| EXBO forum / fan sites | RSS/feed reader | None | 0.12 |

## Daily Monitor — Deviation Detection

| Alert Type | Trigger | Default Threshold |
|------------|---------|-------------------|
| `snipe_opportunity` | Listing well below fair value | 15% deviation |
| `overpriced_listing` | Listing far above live cap | 25% deviation |
| `history_vs_live_gap` | Stale history dragging estimate | 40% gap |
| `community_vs_market_gap` | Community claim wildly off market | 60% deviation |
| `volatility_spike` | Day-over-day fair value movement | 20% change |

## Dashboard Features

- **Overview**: KPI cards, data source banner (LIVE/SAMPLE), price trend chart, tier breakdown, recent alerts, community pulse
- **Valuations**: Sortable table of all tracked artifacts with price ranges and confidence scores
- **Alerts**: Full list of deviation alerts with severity badges
- **Community**: Community valuation reports with sentiment analysis

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/seed` | Seed sample data (demo mode) |
| `GET /api/valuations?region=na` | Latest valuations for all tracked items |
| `GET /api/history/{item_id}?region=na&qlt=3&bucket=0&days=30` | Historical valuation trend |
| `GET /api/community?region=na&days=7` | Recent community signals |
| `GET /api/alerts?region=na&days=7` | Recent deviation alerts |
| `GET /api/summary?region=na` | KPI summary with tier breakdown and data source |
| `GET /health` | Health check |

## Hosting

See [HOSTING.md](HOSTING.md) for free hosting options including Render.com,
Railway, Fly.io, and self-hosting with Cloudflare Tunnel.
