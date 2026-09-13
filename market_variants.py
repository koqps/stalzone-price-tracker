"""Market-variant helpers for artifact quality and enhancement level.

STALZONE artifact prices must be compared within the same rarity *and* the same
upgrade level.  A +15 artifact is not a valid comparable for a +0 artifact.
The official auction payload exposes ``upgrade_level`` in ``additional``.

The existing model already keys evidence by ``bonus_bucket``.  To keep the
current model/database interfaces stable while making enhancement level
explicit, production code encodes level N as N/100 before passing it through
``bonus_bucket``.  With the current 2.5% bucket step this produces stable keys
0, 10, 20 ... 150 for +0 ... +15 respectively.
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

    If ``upgrade_level`` is omitted and ``upgrade_bonus`` is also absent/zero,
    treat the listing as +0.  If a non-zero bonus exists but the explicit level
    is missing, return None instead of guessing; this prevents mixed-level price
    contamination.
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
        if 0 <= level <= 15:
            return level
        return None

    bonus = additional.get("upgrade_bonus")
    if isinstance(bonus, dict):
        bonus = bonus.get("value", bonus.get("amount"))
    if bonus in (None, "", 0, 0.0, "0", "0.0"):
        return 0
    return None


def variant_key(level: int | None) -> float | None:
    """Encode +N as a stable fractional key used by the existing model."""
    if level is None:
        return None
    return level / 100.0


def extract_variant(additional: dict[str, Any] | None) -> tuple[int | None, int | None, float | None]:
    qlt = extract_quality(additional)
    level = extract_upgrade_level(additional)
    return qlt, level, variant_key(level)


def bucket_to_upgrade_level(bucket: int | float | None) -> int | None:
    """Decode a production bonus_bucket back to +N.

    New clean data uses bucket = level * 10.  Values outside that shape are
    treated as legacy/unknown and never silently labeled as an upgrade level.
    """
    if bucket is None:
        return None
    try:
        value = int(bucket)
    except (TypeError, ValueError):
        return None
    if 0 <= value <= 150 and value % 10 == 0:
        return value // 10
    return None


def upgrade_label(level: int | None) -> str:
    return f"+{level}" if level is not None else "+?"
