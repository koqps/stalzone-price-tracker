"""Helpers for artifact rarity, pattern/point value, and derived quality.

STALZONE artifact prices are comparable only within the same artifact, rarity,
and explicit ptn value. The API's ``additional.ptn`` is the displayed +0..+15
point/upgrade value; combined with qlt it also identifies the quality value
inside the rarity bracket.
"""
from __future__ import annotations

from typing import Any

QUALITY_BRACKETS = {
    0: (85, 100),
    1: (100, 115),
    2: (115, 130),
    3: (130, 145),
    4: (145, 160),
    5: (160, 175),
    6: (175, 190),
}


def extract_quality(additional: dict[str, Any] | None) -> int | None:
    """Return API qlt rarity bracket 0..6 (Common through Unique)."""
    if not additional or not isinstance(additional, dict):
        return None
    value = additional.get("qlt", additional.get("quality"))
    try:
        qlt = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(6, qlt))


def extract_upgrade_level(additional: dict[str, Any] | None) -> int | None:
    """Return official artifact ptn/displayed +level (0..15)."""
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

    # Some +0 records omit ptn. Infer +0 only when there is no contradictory
    # enhancement information.
    bonus = additional.get("upgrade_bonus")
    if isinstance(bonus, dict):
        bonus = bonus.get("value", bonus.get("amount"))
    if bonus in (None, "", 0, 0.0, "0", "0.0"):
        return 0
    return None


def derived_quality(qlt: int | None, ptn: int | None) -> int | None:
    """Map qlt + ptn to the optimizer/display quality (e.g. 4+15 => 160)."""
    if qlt not in QUALITY_BRACKETS or ptn is None or not 0 <= int(ptn) <= 15:
        return None
    return QUALITY_BRACKETS[int(qlt)][0] + int(ptn)


def variant_key(level: int | None) -> float | None:
    """Compatibility value for older bot call signatures; not a DB identity."""
    return None if level is None else level / 100.0


def extract_variant(additional: dict[str, Any] | None) -> tuple[int | None, int | None, float | None]:
    qlt = extract_quality(additional)
    level = extract_upgrade_level(additional)
    return qlt, level, variant_key(level)


def bucket_to_upgrade_level(bucket: int | float | None) -> int | None:
    if bucket is None:
        return None
    try:
        value = int(bucket)
    except (TypeError, ValueError):
        return None
    return value // 10 if 0 <= value <= 150 and value % 10 == 0 else None


def upgrade_label(level: int | None) -> str:
    return f"+{level}" if level is not None else "+?"
