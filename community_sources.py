"""
community_sources.py — Collectors for community price sentiment
================================================================

Each collector returns a uniform list of "signal" dicts shaped like:
    {
        "item_name": "Firebird",
        "item_id": "0n9q",
        "region": "na",
        "qlt": 3,
        "bonus_bucket": 25,
        "claimed_price": 1_200_000,    # may be None for pure sentiment posts
        "sentiment": "bullish",       # bullish | bearish | neutral
        "sentiment_score": 0.4,       # -1..+1
        "source": "reddit",           # matches SOURCE_CONFIDENCE in price_model
        "source_name": "r/Stalcraft",
        "url": "...",
        "excerpt": "...",
        "confidence": 0.12,
        "collected_at": time.time(),
    }

Design: the collectors are decoupled from the network — they call
`_fetch_json(url)` / `_fetch_text(url)` helpers so the bot can later
swap in aiohttp, the SDK, or even a cached fixture for testing.
Community signals NEVER set the price directly (see price_model.py) —
they only adjust confidence and label sentiment, and they surface
"manual review needed" flags for the dashboard.

Available collectors:
  * reddit_stalcraft   — r/Stalcraft posts/comments mentioning a price.
  * discord_trade      — pull trade-channel message history (requires
                         a bot token + channel IDs in env; optional).
  * staldata_market    — public staldata.org auction stats snapshot.
  * youtube_guides     — recent "price guide" video metadata via YouTube
                         Data API (quota-limited; off by default).
  * forum_threads      — generic RSS/feed reader for trade-discussion
                         threads (EXBO forum, fan sites).

In production: call the heavier Reddit/Discord/YouTube collectors on a
slow cadence (e.g. a daily cron via daily_monitor.py), NOT inside the
hot auction-scan loop — those loops run continuously and community
data has a short half-life of relevance.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------
# Shared fetch helpers — keep these simple so they can be monkeypatched
# in tests. Swap for aiohttp in the Discord bot context if preferred.
# ---------------------------------------------------------------------
def _fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> Any:
    req = Request(url, headers={"User-Agent": "stalzone-monitor/1.0", **(headers or {})})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_text(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> str:
    req = Request(url, headers={"User-Agent": "stalzone-monitor/1.0", **(headers or {})})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------
# Sentiment / price extraction
# ---------------------------------------------------------------------
_PRICE_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*(k|K|m|M|rub|RUB|r|R)?\b")
_ITEMS_HINT_RE = re.compile(
    r"\b(firebird|timber|polyhedron|shrimp|junk|crab|shell|antimatter|"
    r"compass|watch|drift|crystal|sphere|wrench|medallion)\b",
    re.IGNORECASE,
)
_BULLISH = re.compile(r"\b(bullish|rising|up|demand|hot|rare drop|buffed|meta|BiS)\b", re.I)
_BEARISH = re.compile(r"\b(bearish|falling|dump|crash|oversupplied|nerf|dead|nobody buys|cheap)\b", re.I)


def _score_sentiment(text: str) -> tuple[str, float]:
    bull = len(_BULLISH.findall(text))
    bear = len(_BEARISH.findall(text))
    if bull > bear:
        return "bullish", min(1.0, (bull - bear) / max(bull, 1) * 0.6 + 0.2)
    if bear > bull:
        return "bearish", max(-1.0, -((bear - bull) / max(bear, 1) * 0.6 + 0.2))
    return "neutral", 0.0


def _extract_prices(text: str) -> list[float]:
    """Extract numeric prices from text, handling k/m suffixes and comma-formatted RUB.

    Examples:
      "500k"   -> 500000
      "1.2m"   -> 1200000
      "850,000" -> 850000
      "500 rub" -> 500
    """
    prices = []
    for m in _PRICE_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        suffix = (m.group(2) or "").lower()
        try:
            val = float(raw)
        except ValueError:
            continue
        if suffix in ("k",):
            val *= 1_000
        elif suffix in ("m",):
            val *= 1_000_000
        # rub/r/R suffix means the number is already in RUB units
        prices.append(val)
    return prices


def _extract_item_mention(text: str) -> str | None:
    m = _ITEMS_HINT_RE.search(text)
    return m.group(1).capitalize() if m else None


# ---------------------------------------------------------------------
# Reddit — r/Stalcraft
# ---------------------------------------------------------------------
def reddit_stalcraft(
    query: str | None = None, limit: int = 50, item_filter: set[str] | None = None,
) -> list[dict]:
    """Fetch recent r/Stalcraft posts via the public JSON search endpoint.

    No API key required for read-only search. Rate-limit politely (max
    ~1 request per minute). Returns decoded signals; callers should
    filter by `item_filter` if they only care about specific artifacts.
    """
    base = "https://www.reddit.com/r/Stalcraft/search.json"
    q = query or "price"
    params = f"?q={query or 'price'}&sort=new&restrict_sr=on&limit={limit}"
    try:
        payload = _fetch_json(base + params, headers={"Accept": "application/json"})
    except Exception:
        return []

    signals: list[dict] = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        text = f"{d.get('title', '')} {d.get('selftext', '')}"
        item = _extract_item_mention(text)
        if not item:
            continue
        if item_filter and item.lower() not in {i.lower() for i in item_filter}:
            continue
        sentiment, score = _score_sentiment(text)
        prices = _extract_prices(text)
        claimed = prices[0] if prices else None
        signals.append({
            "item_name": item,
            "region": "na",
            "claimed_price": claimed,
            "sentiment": sentiment,
            "sentiment_score": score,
            "source": "reddit",
            "source_name": "r/Stalcraft",
            "url": f"https://reddit.com{d.get('permalink', '')}",
            "excerpt": d.get("title", "")[:200],
            "confidence": 0.12,
            "collected_at": time.time(),
        })
    return signals


# ---------------------------------------------------------------------
# Discord trade channel history — requires a bot token + channel IDs
# ---------------------------------------------------------------------
def discord_trade(
    item_filter: set[str] | None = None, channel_ids: list[int] | None = None,
) -> list[dict]:
    """Pull trade-channel message history via the Discord REST API.

    Requires DISCORD_TOKEN (bot) and a list of CHANNEL_IDs in env. This is
    OFF by default — enable only if you already run a bot in those trade
    channels. Returns the same signal shape as the other collectors.
    """
    token = os.getenv("DISCORD_TOKEN", "")
    channel_ids = channel_ids or [int(x) for x in os.getenv("TRADE_CHANNEL_IDS", "").split(",") if x]
    if not token or not channel_ids:
        return []

    headers = {"Authorization": f"Bot {token}"}
    signals: list[dict] = []
    for cid in channel_ids:
        try:
            payload = _fetch_json(
                f"https://discord.com/api/v10/channels/{cid}/messages?limit=100",
                headers=headers,
            )
        except Exception:
            continue
        for msg in payload:
            text = msg.get("content", "")
            item = _extract_item_mention(text)
            if not item:
                continue
            if item_filter and item.lower() not in {i.lower() for i in item_filter}:
                continue
            sentiment, score = _score_sentiment(text)
            prices = _extract_prices(text)
            claimed = prices[0] if prices else None
            signals.append({
                "item_name": item,
                "region": "na",
                "claimed_price": claimed,
                "sentiment": sentiment,
                "sentiment_score": score,
                "source": "discord_trade",
                "source_name": f"Discord channel {cid}",
                "url": f"https://discord.com/channels/@me/{cid}/{msg.get('id','')}",
                "excerpt": text[:200],
                "confidence": 0.15,
                "collected_at": time.time(),
            })
    return signals


# ---------------------------------------------------------------------
# staldata.org — public market-data snapshot (no auth; community site)
# ---------------------------------------------------------------------
def staldata_market(item_filter: set[str] | None = None) -> list[dict]:
    """Pull the staldata.org market snapshot (public, no API key).

    staldata.org caches public auction stats; it does NOT call the
    official StalCraft API from the browser, so it's safe to reference
    as a community aggregate even when your own scapi client is
    rate-limited. Signals here are treated as a secondary cross-check,
    not as a price authority.
    """
    try:
        payload = _fetch_json("https://staldata.org/api/market/snapshot")
    except Exception:
        return []

    signals: list[dict] = []
    for row in payload.get("items", []):
        name = row.get("name", "")
        if item_filter and name.lower() not in {i.lower() for i in item_filter}:
            continue
        price = row.get("median_price") or row.get("price")
        trend = row.get("trend", 0)  # -1..+1 from their own calc
        sentiment = "bullish" if trend > 0.1 else "bearish" if trend < -0.1 else "neutral"
        signals.append({
            "item_name": name,
            "region": row.get("region", "na"),
            "claimed_price": float(price) if price else None,
            "sentiment": sentiment,
            "sentiment_score": float(trend or 0),
            "source": "staldata",
            "source_name": "staldata.org",
            "url": f"https://staldata.org/item/{row.get('id','')}",
            "excerpt": f"staldata median {price} ({row.get('change_pct', '?')}%)",
            "confidence": 0.20,
            "collected_at": time.time(),
        })
    return signals


# ---------------------------------------------------------------------
# YouTube price-guide videos (optional, quota-limited)
# ---------------------------------------------------------------------
def youtube_guides(
    item_filter: set[str] | None = None, max_results: int = 5,
) -> list[dict]:
    """Search for recent StalCraft price-guide / market videos.

    Requires YOUTUBE_API_KEY. YouTube search has a quota cost — this is
    intentionally light (a few searches per run). Videos are used for
    manual-review context only; their claimed prices never feed the
    valuation model directly. Set YOUTUBE_RESEARCH_ENABLED=true to enable.
    """
    enabled = os.getenv("YOUTUBE_RESEARCH_ENABLED", "false").lower() == "true"
    api_key = os.getenv("YOUTUBE_API_KEY", "")
    if not enabled or not api_key:
        return []

    query = "Stalzone artifact price guide NA"
    url = (
        "https://www.googleapis.com/youtube/v3/search?part=snippet"
        f"&q={query}&type=video&maxResults={max_results}&order=date&key={api_key}"
    )
    try:
        payload = _fetch_json(url)
    except Exception:
        return []

    signals: list[dict] = []
    for row in payload.get("items", []):
        vid = (row.get("id") or {}).get("videoId")
        if not vid:
            continue
        snippet = row.get("snippet", {})
        title = snippet.get("title", "")
        item = _extract_item_mention(title)
        if item_filter and (not item or item.lower() not in {i.lower() for i in item_filter}):
            # Still surface the video for manual review even if no item match.
            item = item or "general"
        sentiment, score = _score_sentiment(title)
        signals.append({
            "item_name": item or "general",
            "region": "na",
            "claimed_price": None,
            "sentiment": sentiment,
            "sentiment_score": score,
            "source": "youtube",
            "source_name": snippet.get("channelTitle", "Unknown"),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "excerpt": title,
            "confidence": 0.10,
            "collected_at": time.time(),
        })
    return signals


# ---------------------------------------------------------------------
# Generic forum/feed reader — for EXBO forum trade threads, fan sites
# ---------------------------------------------------------------------
def forum_threads(feed_urls: list[str] | None = None) -> list[dict]:
    """Pull trade-discussion threads from configured RSS/JSON feeds.

    Default feeds: none — configure via env TRADE_FEED_URLS. This avoids
    hard-coding fragile scrape URLs and lets the owner vet sources first.
    """
    urls = feed_urls or [
        u for u in os.getenv("TRADE_FEED_URLS", "").split(",") if u
    ]
    if not urls:
        return []

    signals: list[dict] = []
    for url in urls:
        try:
            text = _fetch_text(url)
        except Exception:
            continue
        item = _extract_item_mention(text)
        if not item:
            continue
        sentiment, score = _score_sentiment(text)
        prices = _extract_prices(text)
        signals.append({
            "item_name": item,
            "region": "na",
            "claimed_price": prices[0] if prices else None,
            "sentiment": sentiment,
            "sentiment_score": score,
            "source": "forum",
            "source_name": url.split("/")[2] if "/" in url else url,
            "url": url,
            "excerpt": text[:200],
            "confidence": 0.12,
            "collected_at": time.time(),
        })
    return signals


# ---------------------------------------------------------------------
# Orchestrator — call this from the daily monitor, not the hot scan loop
# ---------------------------------------------------------------------
def collect_all_community_signals(
    item_filter: set[str] | None = None,
) -> list[dict]:
    """Run every enabled collector and merge results.

    Collectors that require keys (Discord, YouTube) silently no-op if the
    key isn't configured, so this is always safe to call.
    """
    out: list[dict] = []
    out.extend(reddit_stalcraft(item_filter=item_filter))
    out.extend(staldata_market(item_filter=item_filter))
    out.extend(discord_trade(item_filter=item_filter))
    out.extend(youtube_guides(item_filter=item_filter))
    out.extend(forum_threads())
    return out
