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


@router.get("/api/build-calculator-data")
async def build_calculator_data():
    artifacts_raw, containers = await asyncio.gather(
        _cached_json("artifacts", ARTIFACTS_URL),
        _cached_json("containers", CONTAINERS_URL),
    )
    catalog = await load_artifact_catalog()
    artifacts = []
    for row in artifacts_raw:
        item_id = str(row.get("id") or "")
        if not item_id:
            continue
        official = catalog.get(item_id) or {}
        artifacts.append({
            **row,
            "icon_url": official.get("icon_url"),
            "artifact_class": official.get("artifact_class") or str(row.get("category") or "").split("/")[-1].replace("_", " ").title(),
            "description": official.get("description") or "",
        })
    artifacts.sort(key=lambda r: str(r.get("name") or r.get("id") or "").lower())
    containers = sorted(
        containers,
        key=lambda r: (-int(r.get("capacity") or 0), str(r.get("name") or "").lower()),
    )
    return {
        "artifacts": artifacts,
        "containers": containers,
        "source": "UltimateBuild normalized STALCRAFT data + EXBO catalog icons",
        "license": "MIT",
        "updated_at": time.time(),
    }


@router.get("/build-calculator")
def build_calculator_page():
    return FileResponse(str(STATIC_DIR / "build-calculator.html"))


@router.get("/calculator")
def calculator_alias():
    return FileResponse(str(STATIC_DIR / "build-calculator.html"))
