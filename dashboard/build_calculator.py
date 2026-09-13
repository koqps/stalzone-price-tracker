"""Build calculator routes and normalized calculator data.

The calculator consumes normalized STALCRAFT artifact/container data from the
MIT-licensed UltimateBuild project, while prices still come from this tracker's
observed NA auction database. The remote normalized data is cached in memory.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse

from artifact_catalog import load_artifact_catalog

router = APIRouter()
STATIC_DIR = Path(__file__).parent / "static"
CACHE_TTL = 6 * 60 * 60
ARTIFACTS_URL = "https://raw.githubusercontent.com/will-bot2026/stalcraft_v1/main/data/normalized/artifacts.json"
CONTAINERS_URL = "https://raw.githubusercontent.com/will-bot2026/stalcraft_v1/main/data/normalized/containers.json"
EXBO_RAW_BASE = "https://raw.githubusercontent.com/EXBO-Studio/stalzone-database/main/global"
_cache: dict[str, tuple[float, Any]] = {}


def _download_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "stalzone-price-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
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
    # Positive accumulation is harmful; negative accumulation is beneficial.
    if key.endswith("_accumulation"):
        return max(minimum, maximum) <= 0
    return True


def _fallback_artifact(item_id: str, official: dict, stat_defs: dict[str, dict]) -> dict:
    path = str(official.get("data_path") or "").strip("/")
    raw = _download_json(f"{EXBO_RAW_BASE}/{path}") if path else {}
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
            stats.append({
                "key": key,
                "name": name,
                "min": minimum,
                "max": maximum,
                "isPositive": bool(known.get("isPositive", _fallback_positive(key, minimum, maximum))),
                "isPercentage": bool(known.get("isPercentage", False)),
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
        "calculator_source": "current EXBO fallback",
    }


@router.get("/api/build-calculator-data")
async def build_calculator_data():
    artifacts_raw, containers = await asyncio.gather(
        _cached_json("artifacts", ARTIFACTS_URL),
        _cached_json("containers", CONTAINERS_URL),
    )
    catalog = await load_artifact_catalog()
    normalized = {str(row.get("id") or ""): row for row in artifacts_raw if row.get("id")}
    stat_defs = _stat_defs(artifacts_raw, containers)

    artifacts = []
    for item_id, official in catalog.items():
        row = normalized.get(item_id)
        if row:
            artifacts.append({
                **row,
                "icon_url": official.get("icon_url"),
                "artifact_class": official.get("artifact_class") or str(row.get("category") or "").split("/")[-1].replace("_", " ").title(),
                "description": official.get("description") or "",
                "calculator_source": "UltimateBuild normalized",
            })
        else:
            try:
                artifacts.append(await asyncio.to_thread(_fallback_artifact, item_id, official, stat_defs))
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
        "source": "UltimateBuild normalized STALCRAFT data + current EXBO fallback",
        "license": "MIT",
        "updated_at": time.time(),
    }


@router.get("/build-calculator")
def build_calculator_page():
    return FileResponse(str(STATIC_DIR / "build-calculator.html"))


@router.get("/calculator")
def calculator_alias():
    return FileResponse(str(STATIC_DIR / "build-calculator.html"))
