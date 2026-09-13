"""Helpers for artifact quality and exact enhancement level.

STALZONE artifact prices are comparable only within the same artifact, rarity,
and explicit enhancement level. +0, +1, ... +15 are all separate markets.
The official auction API represents this enhancement level as ``additional.ptn``.
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
    """Return the official artifact enhancement/potential level (0..15).

    Official auction payloads use ``ptn``. ``upgrade_level`` and ``potential``
    are accepted only as compatibility aliases for alternate wrappers.
    Missing level is +0 only when there is no contradictory enhancement data.
    """
    if not additional or not isinstance(additional, dict):
        return None

    raw = additional.get("ptn")
    if raw is None:
        raw = additional.get("upgrade_level")
    if raw is None:
        raw = additional.get("potential")
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("level", raw.get("amount")))
    if raw is not None:
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return None
        return level if 0 <= level <= 15 else None

    # The official API omits ptn for +0 on some records. Only infer +0 when no
    # non-zero enhancement indicator is present; otherwise exclude the row.
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
