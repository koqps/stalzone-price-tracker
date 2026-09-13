"""Helpers for artifact quality and exact enhancement level.

STALZONE artifact prices are comparable only within the same artifact, rarity,
and explicit enhancement level. +0, +1, ... +15 are all separate markets.
Unknown enhancement levels are excluded rather than guessed.
"""
from __future__ import annotations

from typing import Any


def extract_quality(additional: dict[str, Any] | None) -> int | None:
    if not additional or not isinstance(additional, dict):
        return None
    value = additional.get("qlt", additional.get("quality"))
    try:
        qlt = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(5, qlt))


def extract_upgrade_level(additional: dict[str, Any] | None) -> int | None:
    """Return the explicit artifact enhancement level (0..15).

    A missing level with no upgrade bonus is safely treated as +0. If the API
    supplies non-zero enhancement data but omits the explicit level, return None
    so that row cannot contaminate a different enhancement market.
    """
    if not additional or not isinstance(additional, dict):
        return None
    raw = additional.get("upgrade_level")
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("level", raw.get("amount")))
    if raw is not None:
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return None
        return level if 0 <= level <= 15 else None
    bonus = additional.get("upgrade_bonus")
    if isinstance(bonus, dict):
        bonus = bonus.get("value", bonus.get("amount"))
    if bonus in (None, "", 0, 0.0, "0", "0.0"):
        return 0
    return None


def variant_key(level: int | None) -> float | None:
    """Compatibility value for older bot call signatures; not a DB identity."""
    return None if level is None else level / 100.0


def extract_variant(additional: dict[str, Any] | None) -> tuple[int | None, int | None, float | None]:
    qlt = extract_quality(additional)
    level = extract_upgrade_level(additional)
    return qlt, level, variant_key(level)


def bucket_to_upgrade_level(bucket: int | float | None) -> int | None:
    """Decode legacy level*10 compatibility buckets only."""
    if bucket is None:
        return None
    try:
        value = int(bucket)
    except (TypeError, ValueError):
        return None
    return value // 10 if 0 <= value <= 150 and value % 10 == 0 else None


def upgrade_label(level: int | None) -> str:
    return f"+{level}" if level is not None else "+?"
