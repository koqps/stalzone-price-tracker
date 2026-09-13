"""Build calculator routes and source-truthed calculator data.

Artifact stat ranges are refreshed from the current EXBO-normalized dataset.
The older normalized dataset is used only for optional-trait/unit metadata where
it is still useful, while live prices remain sourced from this tracker's NA
market observations.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse

from artifact_catalog import load_artifact_catalog

router = APIRouter()
STATIC_DIR = Path(__file__).parent / "static"
CACHE_TTL = 6 * 60 * 60
CURRENT_ARTIFACTS_URL = "https://raw.githubusercontent.com/will-bot2026/stalcraft_v1/main/data/normalized-exbo/artifacts.json"
LEGACY_ARTIFACTS_URL = "https://raw.githubusercontent.com/will-bot2026/stalcraft_v1/main/data/normalized/artifacts.json"
CONTAINERS_URL = "https://raw.githubusercontent.com/will-bot2026/stalcraft_v1/main/data/normalized-exbo/containers.json"
LEGACY_CONTAINERS_URL = "https://raw.githubusercontent.com/will-bot2026/stalcraft_v1/main/data/normalized/containers.json"
EXBO_RAW_BASE = "https://raw.githubusercontent.com/EXBO-Studio/stalzone-database/main/global"
_cache: dict[str, tuple[float, Any]] = {}


def _download_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "stalzone-price-tracker/2.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def _cached_json(key: str, url: str) -> Any:
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    data = await asyncio.to_thread(_download_json, url)
    _cache[key] = (time.time(), data)
    return data


def _stat_defs(rows: list[dict], containers: list[dict]) -> dict[str, dict]:
    defs: dict[str, dict] = {}
    for row in [*rows, *containers]:
        for stat in [*(row.get("stats") or []), *(row.get("additionalStats") or [])]:
            key = str(stat.get("key") or "")
            if key and key not in defs:
                defs[key] = stat
    return defs


def _fallback_positive(key: str, minimum: float, maximum: float) -> bool:
    # Accumulation values are directional: negative reduces load (beneficial),
    # positive adds load (harmful). Do not inherit this classification globally
    # because the same stat key legitimately appears with both signs.
    if key.endswith("_accumulation"):
        return max(minimum, maximum) <= 0
    return True


def _level_zero_row(rows: list[dict]) -> dict:
    """Pick the +0 definition from the EXBO-normalized 16-level row group.

    The upstream export currently contains one row per +0..+15 level but does
    not retain the level field. +level increases beneficial magnitudes by 2%
    each step, so +0 is the row with the smallest beneficial magnitude. Harmful
    endpoints are level-invariant, making any tied row equivalent.
    """
    if not rows:
        return {}

    def score(row: dict) -> tuple[float, float]:
        positives = [s for s in row.get("stats") or [] if s.get("isPositive")]
        if positives:
            return (
                sum(abs(float(s.get("max") or 0)) for s in positives),
                sum(abs(float(s.get("min") or 0)) for s in positives),
            )
        all_stats = row.get("stats") or []
        return (sum(abs(float(s.get("max") or 0)) for s in all_stats), 0.0)

    return min(rows, key=score)


def _merge_stat_metadata(stats: list[dict], defs: dict[str, dict]) -> list[dict]:
    out = []
    for stat in stats:
        row = dict(stat)
        key = str(row.get("key") or "")
        known = defs.get(key) or {}
        if known:
            # Current EXBO values and sign classification win; legacy metadata
            # is used only for display names/units missing from newer exports.
            row["name"] = known.get("name") or row.get("name")
            row["isPercentage"] = bool(known.get("isPercentage", row.get("isPercentage", False)))
        row["origin"] = "artefact"
        out.append(row)
    return out


def _official_stats_by_name(official: dict) -> dict[str, dict]:
    return {
        str(row.get("name") or "").strip().lower(): row
        for row in official.get("stats") or []
        if row.get("name")
    }


def _fallback_artifact(item_id: str, official: dict, stat_defs: dict[str, dict]) -> dict:
    path = str(official.get("data_path") or "").strip("/")
    raw = _download_json(f"{EXBO_RAW_BASE}/{path}") if path else {}
    official_stats = _official_stats_by_name(official)
    stats = []
    for block in raw.get("infoBlocks") or []:
        for element in block.get("elements") or []:
            if element.get("type") != "range":
                continue
            name_obj = element.get("name") or {}
            key = str(name_obj.get("key") or "") if isinstance(name_obj, dict) else ""
            if "artefact_properties.factor" not in key:
                continue
            minimum = float(element.get("min") or 0)
            maximum = float(element.get("max") or 0)
            known = stat_defs.get(key) or {}
            lines = name_obj.get("lines") if isinstance(name_obj, dict) else None
            name = str((lines or {}).get("en") or known.get("name") or key.rsplit(".", 1)[-1].replace("_", " ").title())
            official_stat = official_stats.get(name.strip().lower()) or {}

            # Accumulation sign is artifact-specific and must always come from
            # the actual values. For other families prefer the current official
            # tooltip color classification, then fall back to normalized metadata.
            if key.endswith("_accumulation"):
                is_positive = _fallback_positive(key, minimum, maximum)
            elif official_stat:
                is_positive = not bool(official_stat.get("harmful", False))
            else:
                is_positive = bool(known.get("isPositive", _fallback_positive(key, minimum, maximum)))

            display = str(official_stat.get("display") or "")
            is_percentage = "%" in display if display else bool(known.get("isPercentage", False))
            stats.append({
                "key": key,
                "name": name,
                "min": minimum,
                "max": maximum,
                "isPositive": is_positive,
                "isPercentage": is_percentage,
                "origin": "artefact",
            })
    return {
        "id": item_id,
        "name": official.get("item_name") or item_id,
        "category": f"artefact/{str(official.get('artifact_class') or 'other').lower().replace(' ', '_')}",
        "rarity": "rarity.ordinary",
        "level": 0,
        "quality": 100,
        "stats": stats,
        "additionalStats": [],
        "icon_url": official.get("icon_url"),
        "artifact_class": official.get("artifact_class") or "Artifact",
        "description": official.get("description") or "",
        "calculator_source": "current official EXBO item",
    }


@router.get("/api/build-calculator-data")
async def build_calculator_data():
    current_rows, legacy_rows, containers, legacy_containers = await asyncio.gather(
        _cached_json("current-artifacts", CURRENT_ARTIFACTS_URL),
        _cached_json("legacy-artifacts", LEGACY_ARTIFACTS_URL),
        _cached_json("current-containers", CONTAINERS_URL),
        _cached_json("legacy-containers", LEGACY_CONTAINERS_URL),
    )
    catalog = await load_artifact_catalog()
    legacy = {str(row.get("id") or ""): row for row in legacy_rows if row.get("id")}
    stat_defs = _stat_defs(legacy_rows, legacy_containers)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in current_rows:
        item_id = str(row.get("id") or "")
        if item_id:
            grouped[item_id].append(row)

    artifacts = []
    for item_id, official in catalog.items():
        source_rows = grouped.get(item_id) or []
        if source_rows:
            current = _level_zero_row(source_rows)
            old = legacy.get(item_id) or {}
            artifacts.append({
                "id": item_id,
                "name": official.get("item_name") or current.get("name") or item_id,
                "category": current.get("category") or old.get("category") or "artefact/other_arts",
                "rarity": old.get("rarity") or "rarity.ordinary",
                "level": 0,
                "quality": 100,
                "stats": _merge_stat_metadata(current.get("stats") or [], stat_defs),
                "additionalStats": old.get("additionalStats") or [],
                "icon_url": official.get("icon_url"),
                "artifact_class": official.get("artifact_class") or str(current.get("category") or "").split("/")[-1].replace("_", " ").title(),
                "description": official.get("description") or "",
                "calculator_source": "current EXBO-normalized +0 ranges",
            })
        else:
            try:
                fallback = await asyncio.to_thread(_fallback_artifact, item_id, official, stat_defs)
                old = legacy.get(item_id) or {}
                fallback["additionalStats"] = old.get("additionalStats") or []
                if not fallback["stats"] and fallback["additionalStats"]:
                    fallback["calculator_source"] = "current official item + rolled trait catalog"
                artifacts.append(fallback)
            except Exception:
                artifacts.append({
                    "id": item_id,
                    "name": official.get("item_name") or item_id,
                    "category": "artefact/other_arts",
                    "rarity": "rarity.ordinary",
                    "level": 0,
                    "quality": 100,
                    "stats": [],
                    "additionalStats": [],
                    "icon_url": official.get("icon_url"),
                    "artifact_class": official.get("artifact_class") or "Artifact",
                    "description": official.get("description") or "",
                    "calculator_source": "official catalog fallback",
                })

    artifacts.sort(key=lambda r: str(r.get("name") or r.get("id") or "").lower())
    containers = sorted(
        containers,
        key=lambda r: (-int(r.get("capacity") or 0), str(r.get("name") or "").lower()),
    )
    return {
        "artifacts": artifacts,
        "containers": containers,
        "source": "current EXBO-normalized artifact ranges + official catalog metadata",
        "license": "MIT",
        "updated_at": time.time(),
    }


@router.get("/build-calculator")
def build_calculator_page():
    return FileResponse(str(STATIC_DIR / "build-calculator.html"))


@router.get("/calculator")
def calculator_alias():
    return FileResponse(str(STATIC_DIR / "build-calculator.html"))