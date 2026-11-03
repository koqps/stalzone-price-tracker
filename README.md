# StalZone Discord Artifact Tracker

Discord-first artifact market alerts for StalZone. The website/dashboard is no longer part of the runtime. The small HTTP endpoint in `bot_only.py` exists only for Render health checks.

## Commands

- `/watch` — select an artifact, exact quality, exact +0 to +15 level, and maximum RUB per item.
- `/watches` — list your active watches.
- `/unwatch` — remove one of your watches.
- `/scan` — trigger an immediate market scan.
- `/status` — show bot/catalog/watch status.
- `/reload_catalog` — refresh the item catalog.

Each watch belongs to the Discord user who created it. Alerts are sent to the channel where `/watch` was run and mention that user.

## Environment

Required:
- `DISCORD_TOKEN`
- `EXBO_CLIENT_ID`
- `EXBO_CLIENT_SECRET`

Optional:
- `REGION=na`
- `REALM=global`
- `SCAN_INTERVAL=300`
- `LOT_LIMIT=100`
- `BOT_DB_PATH=data/tracker.sqlite3`
- `PORT=10000` (provided by Render)

## Run locally

```bash
python -m pip install -r requirements.txt
python bot_only.py
```

## Variant matching

The watcher matches quality (`qlt`) and enhancement/potential (`ptn`, 0–15) independently. It does not substitute a missing value with Common or +0. If the API omits variant metadata, the lot is skipped rather than producing a misleading alert.

This is important because the official API issue tracker has documented auction responses where artifact `additional` metadata is empty, leaving quality and + level unavailable. See [EXBO API issue #113](https://github.com/EXBO-Studio/stalzone-api/issues/113).

## Render

The Blueprint runs `python bot_only.py` as a web service with a minimal `/health` endpoint. It is not a website. Render's free service can sleep and its local filesystem is ephemeral; use a durable database or persistent disk before relying on it for long-term watchlist storage. A periodic HTTP ping may reduce sleeping but is not a guarantee of uninterrupted operation.
