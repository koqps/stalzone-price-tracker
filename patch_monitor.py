"""Collect official STALZONE patch/news context for market predictions.

Patch notes are never allowed to invent a price. They are stored as a secondary
risk/context signal so the dashboard/model can warn that an artifact market may
be repricing while observed NA auction data remains authoritative.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from market_db import MarketDB

log = logging.getLogger("patch_monitor")
STEAM_NEWS_RSS = "https://store.steampowered.com/feeds/news/app/1818450/?l=english&cc=US&format=rss"
USER_AGENT = "StalZone-Price-Tracker/1.0"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value or ""))
    return _WS_RE.sub(" ", text).strip()


def _classify(title: str, text: str) -> tuple[str, float]:
    hay = f"{title} {text}".casefold()
    supply_down = (
        "reduced the chances" in hay
        or "reduced chances" in hay
        or "reduced drop" in hay
        or "reduced spawn" in hay
        or "spawn rate reduced" in hay
        or "number of artifacts spawned" in hay and "reduced" in hay
    )
    supply_up = (
        "increased the chances" in hay
        or "increased chances" in hay
        or "increased drop" in hay
        or "increased spawn" in hay
        or "more artifacts" in hay
    )
    if supply_down and not supply_up:
        return "supply_down", 0.90
    if supply_up and not supply_down:
        return "supply_up", 0.90
    if "artifact" in hay or "artefact" in hay or "anomal" in hay:
        return "artifact_market_change", 0.70
    return "uncertain", 0.45


def collect_official_patch_signals(db: MarketDB | None = None, max_items: int = 30) -> int:
    db = db or MarketDB()
    req = urllib.request.Request(STEAM_NEWS_RSS, headers={"user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            payload = response.read()
    except Exception as exc:
        log.warning("Official patch feed unavailable: %s", exc)
        return 0

    try:
        root = ET.fromstring(payload)
    except Exception as exc:
        log.warning("Could not parse official patch feed: %s", exc)
        return 0

    recorded = 0
    for item in root.findall(".//item")[:max_items]:
        title = _clean(item.findtext("title") or "")
        link = _clean(item.findtext("link") or "")
        description = _clean(item.findtext("description") or "")
        published_raw = _clean(item.findtext("pubDate") or "")
        combined = f"{title} {description}".casefold()

        # Keep only update/news entries plausibly relevant to the market.
        if not any(k in combined for k in ("patch", "hotfix", "artifact", "artefact", "electrostorm", "game changes", "update")):
            continue

        try:
            published = parsedate_to_datetime(published_raw).timestamp()
        except Exception:
            published = time.time()
        # Ignore stale news; recent patches are what can invalidate price priors.
        if published < time.time() - 45 * 86400:
            continue

        direction, confidence = _classify(title, description)
        raw_id = f"{link}|{title}|{published_raw}".encode("utf-8")
        patch_id = hashlib.sha1(raw_id).hexdigest()[:20]
        db.record_patch_signal(
            patch_id=patch_id,
            published_at=published,
            source_url=link or STEAM_NEWS_RSS,
            title=title or "Official STALZONE update",
            item_id="",  # generic patch row; artifact names are matched from summary text
            artifact_class="",
            signal_type="official_patch",
            impact_direction=direction,
            confidence=confidence,
            summary=description[:7000],
        )
        recorded += 1

    log.info("Official patch monitor processed %d relevant news items", recorded)
    return recorded
