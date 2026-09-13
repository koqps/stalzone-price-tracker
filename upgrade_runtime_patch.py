"""Runtime compatibility patch for explicit artifact enhancement levels.

This keeps the existing bot/model function signatures stable while ensuring the
second value historically called ``upgrade_bonus`` now represents the explicit
artifact enhancement level as a normalized market key (+N -> N/100).  All
existing bonus_bucket comparisons therefore separate +0, +1 ... +15 markets.
"""
from __future__ import annotations

import logging

import bot_integration
import live_ingestion
from market_variants import extract_variant

log = logging.getLogger("upgrade_variants")


def _extract_quality(additional):
    qlt, level, key = extract_variant(additional)
    if qlt is None:
        return None, None
    if level is None:
        # Do not guess when the API gives non-zero enhancement data without an
        # explicit level. Skipping is safer than contaminating +0/+10/+15.
        return qlt, None
    return qlt, key


def _get_lot_quality(lot):
    return _extract_quality(getattr(lot, "additional", None))


live_ingestion.extract_quality = _extract_quality
bot_integration.get_lot_quality = _get_lot_quality

log.info("Artifact market variants enabled: rarity + explicit upgrade level")
