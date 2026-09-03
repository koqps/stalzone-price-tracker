"""
price_model.py — Confidence-weighted multi-source valuation model
===================================================================

Combines official StalZone/StalCraft auction data with community sentiment
to compute a defensible price RANGE (not a single number) for an artifact
at a given (item_id, region, qlt, bonus_bucket).

Evidence hierarchy (unchanged from the bot's existing hard-won rules):

    1. Live comparable auction listings   -> hard ceiling/floor
    2. Confirmed completed sales          -> strongest historical evidence
    3. Inferred (disappearance) sales     -> weaker, conservative percentile
    4. Official mixed-quality price history -> cold-start fallback only
    5. Community signals (Reddit/Discord/forums/staldata/YouTube)
       -> confidence & review-priority adjustment ONLY.
       Never allowed to move floor/quick_sale above the live cap.
    6. Manual human-verified overrides    -> can widen the trusted range
       but still bounded by the live comparable cap when one exists.

Output: a ValuationResult with floor / quick_sale / fair_value / stretch,
a 0-100 confidence score, and an evidence summary string for the dashboard
and Discord alerts.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Sequence

# ---------------------------------------------------------------------
# Tunable configuration (mirrors the bot's .env conventions)
# ---------------------------------------------------------------------
MIN_TIER_SALES = 5
LOW_SAMPLE_TIER_SALES = 2
TIER_PERCENTILE = 20            # conservative — lower end of observed sales
LIVE_UNDERCUT_PCT = 0.005
LIVE_UNDERCUT_RUB = 1000
MAX_HISTORY_OVER_LIVE_RATIO = 1.10

# Source credibility weights (0..1) — used for confidence scoring only.
SOURCE_CONFIDENCE = {
    "confirmed_bid_sale": 1.00,
    "live_listing": 0.40,
    "inferred_buyout_sale": 0.60,
    "official_history": 0.35,
    "manual_verified": 0.25,
    "staldata": 0.20,
    "reddit": 0.12,
    "discord_trade": 0.15,
    "forum": 0.12,
    "youtube": 0.10,
}

# Community sentiment can shift confidence at most +/- this many points.
MAX_COMMUNITY_CONFIDENCE_SWING = 12


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


@dataclass
class ValuationResult:
    item_id: str
    item_name: str
    region: str
    qlt: int
    bonus_bucket: int
    floor: float | None = None
    quick_sale: float | None = None
    fair_value: float | None = None
    stretch: float | None = None
    confidence: int = 0
    live_comparables: int = 0
    confirmed_sales: int = 0
    inferred_sales: int = 0
    community_signals: int = 0
    community_bias: float = 0.0     # -1 (bearish) .. +1 (bullish)
    evidence: list[str] = field(default_factory=list)

    def evidence_summary(self) -> str:
        return "; ".join(self.evidence) if self.evidence else "no evidence"

    def as_row(self) -> dict:
        return {
            "item_id": self.item_id,
            "item_name": self.item_name,
            "region": self.region,
            "qlt": self.qlt,
            "bonus_bucket": self.bonus_bucket,
            "floor_price": self.floor,
            "quick_sale_price": self.quick_sale,
            "fair_value_price": self.fair_value,
            "stretch_price": self.stretch,
            "confidence": self.confidence,
            "live_comparables": self.live_comparables,
            "confirmed_sales": self.confirmed_sales,
            "inferred_sales": self.inferred_sales,
            "community_signals": self.community_signals,
            "community_bias": self.community_bias,
            "evidence_summary": self.evidence_summary(),
        }


def _valuation_confidence(
    live_comparables: int,
    confirmed_sales: int,
    inferred_sales: int,
    market_gap_pct: float,
    used_fallback: bool,
    community_bias: float,
    community_signals: int,
) -> int:
    score = 0
    if live_comparables >= 3:
        score += 30
    elif live_comparables >= 1:
        score += 18

    if confirmed_sales >= 8:
        score += 35
    elif confirmed_sales >= 5:
        score += 25
    elif confirmed_sales >= 3:
        score += 15

    score += min(inferred_sales, 10) * 2

    if market_gap_pct <= 10:
        score += 20
    elif market_gap_pct <= 25:
        score += 10
    elif market_gap_pct > 50:
        score -= 25

    if used_fallback:
        score -= 15

    # Community signals nudge confidence slightly (never authoritative).
    # Strong agreement (many signals, consistent bias) adds a little trust;
    # strong disagreement (bias opposes the computed price) subtracts.
    if community_signals >= 3:
        nudge = community_bias * MAX_COMMUNITY_CONFIDENCE_SWING
        score += nudge

    return max(0, min(100, round(score)))


def compute_valuation(
    item_id: str,
    item_name: str,
    region: str,
    qlt: int,
    bonus_bucket: int,
    *,
    live_comparable_unit_prices: Sequence[float] = (),
    confirmed_sale_prices: Sequence[float] = (),
    inferred_sale_prices: Sequence[float] = (),
    official_history_prices: Sequence[float] = (),
    quality_multiplier: float = 1.0,
    community_signals: Sequence[dict] = (),
    manual_override: dict | None = None,
) -> ValuationResult:
    """Compute a bounded price range from all available evidence.

    `community_signals` is a list of dicts with at least
    {"sentiment_score": float in [-1,1], "confidence": float}. These only
    ever adjust confidence/labels — they can never raise floor/quick_sale
    above a live comparable cap.
    """
    result = ValuationResult(
        item_id=item_id, item_name=item_name, region=region,
        qlt=qlt, bonus_bucket=bonus_bucket,
    )
    result.live_comparables = len(live_comparable_unit_prices)
    result.confirmed_sales = len(confirmed_sale_prices)
    result.inferred_sales = len(inferred_sale_prices)
    result.community_signals = len(community_signals)

    # --- 1. Historical baseline (confirmed > inferred > official fallback)
    used_fallback = False
    history_est: float | None = None
    if len(confirmed_sale_prices) >= LOW_SAMPLE_TIER_SALES:
        history_est = percentile(list(confirmed_sale_prices), TIER_PERCENTILE)
        result.evidence.append(f"confirmed sales n={len(confirmed_sale_prices)}")
    elif len(inferred_sale_prices) >= MIN_TIER_SALES:
        history_est = percentile(list(inferred_sale_prices), TIER_PERCENTILE)
        result.evidence.append(f"inferred sales n={len(inferred_sale_prices)}")
    elif len(inferred_sale_prices) >= LOW_SAMPLE_TIER_SALES:
        history_est = percentile(list(inferred_sale_prices), 20)
        result.evidence.append(f"inferred sales (provisional) n={len(inferred_sale_prices)}")
    elif len(official_history_prices) >= 8:
        mixed = percentile(list(official_history_prices), 15)
        history_est = mixed * quality_multiplier
        used_fallback = True
        result.evidence.append("official mixed-quality history (fallback)")

    # --- 2. Live comparable cap — hard ceiling, never overridden.
    live_cap: float | None = None
    if live_comparable_unit_prices:
        cheapest = min(live_comparable_unit_prices)
        undercut = max(LIVE_UNDERCUT_RUB, cheapest * LIVE_UNDERCUT_PCT)
        live_cap = max(0.0, cheapest - undercut)
        result.evidence.append(
            f"live cap {live_cap:,.0f} from {len(live_comparable_unit_prices)} comparable(s)"
        )

    # --- 3. Combine bounded by live cap
    if history_est is not None and live_cap is not None:
        capped_history = min(history_est, live_cap * MAX_HISTORY_OVER_LIVE_RATIO)
        quick_sale = min(capped_history, live_cap)
        fair_value = capped_history
    elif live_cap is not None:
        quick_sale = live_cap
        fair_value = live_cap
    elif history_est is not None:
        quick_sale = history_est
        fair_value = history_est
    else:
        quick_sale = None
        fair_value = None

    # --- 4. Manual override widens range but is still capped by live market
    if manual_override:
        floor_o = manual_override.get("floor_per_unit")
        target_o = manual_override.get("target_per_unit")
        if floor_o is not None:
            quick_sale = min(quick_sale, floor_o) if quick_sale else floor_o
        if target_o is not None:
            fair_value = target_o if fair_value is None else max(fair_value, target_o)
        if live_cap is not None and fair_value is not None:
            fair_value = min(fair_value, live_cap * MAX_HISTORY_OVER_LIVE_RATIO)
        result.evidence.append("manual override applied")

    # --- 5. Community sentiment bias (label/confidence only, never price)
    community_bias = 0.0
    if community_signals:
        weighted = [
            (s.get("sentiment_score", 0.0) * s.get("confidence", 0.1))
            for s in community_signals
        ]
        total_w = sum(s.get("confidence", 0.1) for s in community_signals) or 1.0
        community_bias = sum(weighted) / total_w
        result.community_bias = round(community_bias, 3)
        label = "bullish" if community_bias > 0.15 else "bearish" if community_bias < -0.15 else "mixed/neutral"
        result.evidence.append(
            f"community sentiment {label} ({len(community_signals)} signals)"
        )

    # --- 6. Derive floor/stretch band around quick_sale/fair_value
    if quick_sale is not None:
        result.floor = round(quick_sale * 0.92, 2)
        result.quick_sale = round(quick_sale, 2)
    if fair_value is not None:
        result.fair_value = round(fair_value, 2)
        # Stretch price leans slightly bullish if community sentiment agrees
        # AND there is real liquidity evidence — otherwise stays conservative.
        stretch_mult = 1.08
        if community_bias > 0.2 and result.confirmed_sales >= 3:
            stretch_mult = 1.12
        result.stretch = round(fair_value * stretch_mult, 2)

    # --- 7. Confidence score
    if quick_sale is not None and live_cap is not None:
        gap_pct = abs((history_est or quick_sale) - live_cap) / max(live_cap, 1) * 100
    elif quick_sale is not None:
        gap_pct = 30.0  # no live comparables => moderate uncertainty
    else:
        gap_pct = 100.0

    result.confidence = _valuation_confidence(
        result.live_comparables, result.confirmed_sales, result.inferred_sales,
        gap_pct, used_fallback, community_bias, result.community_signals,
    )

    if quick_sale is None:
        result.evidence.append("insufficient evidence — no alert-quality valuation")

    return result


def has_cheaper_comparable(candidate_unit_price: float, comparable_prices: Sequence[float]) -> bool:
    """True if a live equal-or-better comparable already undercuts the candidate."""
    return any(p < candidate_unit_price for p in comparable_prices)
