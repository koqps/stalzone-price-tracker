"""Official STALZONE artifact metadata helpers.

This module reads artifact names, classes, icons, descriptions and stat ranges
from EXBO-Studio/stalzone-database through scapi's DatabaseLookup. It does not
invent artifact stats or prices.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from typing import Any

from live_ingestion import REALM, get_db_lookup

CATALOG_TTL_SECONDS = 6 * 60 * 60
_ICON_BASE = "https://raw.githubusercontent.com/EXBO-Studio/stalzone-database/main"

_catalog_cache: dict[str, dict[str, Any]] = {}
_catalog_loaded_at = 0.0
_catalog_lock = asyncio.Lock()


def _download_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "stalzone-price-tracker/2.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))

QUALITY_NAMES = {
    0: "Common",
    1: "Uncommon",
    2: "Special",
    3: "Rare",
    4: "Exclusive",
    5: "Legendary",
}

_CLASS_LABELS = {
    "biochemical": "Biochemical",
    "electrophysical": "Electrophysical",
    "gravity": "Gravity",
    "thermal": "Thermal",
    "other_arts": "Other",
}


def _line(value: Any, language: str = "en") -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    lines = value.get("lines")
    if isinstance(lines, dict):
        return str(lines.get(language) or lines.get("en") or lines.get("ru") or "")
    if value.get("type") == "text":
        return str(value.get("text") or "")
    return ""


def _artifact_class(data_path: str) -> str:
    parts = [p for p in data_path.strip("/").split("/") if p]
    try:
        idx = parts.index("artefact")
        key = parts[idx + 1]
    except (ValueError, IndexError):
        key = "other_arts"
    return _CLASS_LABELS.get(key, key.replace("_", " ").title())


def _icon_path(data_path: str) -> str:
    clean = data_path.strip("/")
    if clean.startswith("items/"):
        clean = "icons/" + clean[len("items/"):]
    if clean.endswith(".json"):
        clean = clean[:-5] + ".png"
    return clean


def _stat_group(name: str, harmful: bool = False) -> str:
    n = name.lower()
    if any(k in n for k in ("movement", "running", "speed", "sprint")):
        return "Mobility"
    if "carry" in n or "weight" in n:
        return "Carry"
    if any(k in n for k in ("vitality", "health regeneration", "healing", "periodic healing")):
        return "Survival"
    if "stamina" in n:
        return "Stamina"
    if any(k in n for k in ("recoil", "sway", "stability")):
        return "Weapon Handling"
    if any(k in n for k in ("bullet", "explosion", "laceration", "bleeding protection")):
        return "Combat Protection"
    if any(k in n for k in (
        "protection", "resistance", "reaction", "radiation", "psy", "biological",
        "bioinfection", "thermal", "electricity", "fire",
    )):
        return "Anomaly Protection" if not harmful else "Anomaly Load"
    if any(k in n for k in ("temperature", "burning", "frost", "infection", "bleeding")):
        return "Anomaly Load"
    if any(k in n for k in ("reload", "charge required", "triggers when", "reduces damage")):
        return "Special Mechanics"
    return "Utility"


def _extract_stats(item: dict[str, Any]) -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    excluded = {"weight", "base selling price", "charge", "max. charge", "freshness"}

    for block in item.get("infoBlocks") or []:
        for element in block.get("elements") or []:
            kind = element.get("type")
            if kind not in {"range", "numeric"}:
                continue
            name = _line(element.get("name"))
            if not name or name.lower() in excluded:
                continue

            formatted = element.get("formatted") or {}
            display = _line(formatted.get("value"))
            color = str(formatted.get("valueColor") or formatted.get("nameColor") or "").upper()
            harmful = color.startswith(("C1", "C2", "D", "E"))

            # Artifact gameplay stats live under stalker artifact-property keys.
            key_obj = element.get("name") or {}
            key = str(key_obj.get("key") or "") if isinstance(key_obj, dict) else ""
            is_artifact_stat = "artefact" in key or "artifact" in key
            if kind == "numeric" and not is_artifact_stat:
                continue

            row: dict[str, Any] = {
                "key": key,
                "name": name,
                "display": display,
                "group": _stat_group(name, harmful),
                "harmful": harmful,
                "kind": kind,
            }
            if kind == "range":
                row["min"] = element.get("min")
                row["max"] = element.get("max")
            else:
                row["value"] = element.get("value")
            stats.append(row)

    # De-duplicate while preserving official order.
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for stat in stats:
        key = (stat["name"], stat.get("display") or "")
        if key not in seen:
            seen.add(key)
            unique.append(stat)
    return unique


def _description(item: dict[str, Any]) -> str:
    for block in item.get("infoBlocks") or []:
        if block.get("type") != "text":
            continue
        text = _line(block.get("text"))
        if text:
            return text.replace("\\n", "\n")
    return ""


async def load_artifact_catalog(force: bool = False) -> dict[str, dict[str, Any]]:
    """Return metadata for every official global artifact.

    Metadata comes from EXBO-Studio/stalzone-database. Results are cached in
    memory because full detail requires one official JSON asset per artifact.
    """
    global _catalog_cache, _catalog_loaded_at
    now = time.time()
    if _catalog_cache and not force and now - _catalog_loaded_at < CATALOG_TTL_SECONDS:
        return _catalog_cache

    async with _catalog_lock:
        now = time.time()
        if _catalog_cache and not force and now - _catalog_loaded_at < CATALOG_TTL_SECONDS:
            return _catalog_cache

        lookup = None
        listing: list[tuple[str, dict[str, Any]]] = []
        try:
            lookup = get_db_lookup()
            all_items = await lookup.get_all(realm=REALM)
            for item_id, entry in all_items.items():
                data_path = str(entry.get("data") or "")
                if "/items/artefact/" in data_path.lower():
                    listing.append((item_id, entry))
        except Exception:
            # scapi can start before its local database index is hydrated. The
            # canonical repository listing is a safe read-only fallback.
            raw_listing = await asyncio.to_thread(
                _download_json, f"{_ICON_BASE}/{REALM}/listing.json"
            )
            for entry in raw_listing if isinstance(raw_listing, list) else []:
                data_path = str(entry.get("data") or "")
                if "/items/artefact/" not in data_path.lower():
                    continue
                item_id = data_path.rsplit("/", 1)[-1].removesuffix(".json")
                listing.append((item_id, entry))

        semaphore = asyncio.Semaphore(10)

        async def load_one(item_id: str, entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            data_path = str(entry.get("data") or "").strip("/")
            name_obj = entry.get("name") or {}
            name = _line(name_obj) or item_id
            item: dict[str, Any] = {}
            async with semaphore:
                if lookup is not None:
                    try:
                        item = await lookup.item_info(path=data_path, realm=REALM)
                    except Exception:
                        item = {}
                if not item and data_path:
                    try:
                        item = await asyncio.to_thread(
                            _download_json, f"{_ICON_BASE}/{REAL]}/{data_path}"
                        )
                    except Exception:
                        item = {}

            icon_path = str(entry.get("icon") or "").strip("/") or _icon_path(data_path)
            stats = _extract_stats(item)
            groups = sorted({s["group"] for s in stats})
            return item_id, {
                "item_id": item_id,
                "item_name": _line(item.get("name")) or name,
                "artifact_class": _artifact_class(data_path),
                "data_path": data_path,
                "icon_path": icon_path,
                "icon_url": f"{_ICON_BASE}/{REALM}/{icon_path}",
                "wiki_url": f"https://stalzone.wiki/en/items/artefacts/{item_id}",
                "description": _description(item),
                "stats": stats,
                "stat_groups": groups,
                "source": "EXBO-Studio/stalzone-database",
            }

        rows = await asyncio.gather(*(load_one(item_id, entry) for item_id, entry in listing))
        _catalog_cache = dict(rows)
        _catalog_loaded_at = time.time()
        return _catalog_cache


async def get_artifact_metadata(item_id: str) -> dict[str, Any] | None:
    catalog = await load_artifact_catalog()
    return catalog.get(item_id)
