"""
daily_monitor.py — Daily price-deviation detector and alert dispatcher
=====================================================================

Runs as a scheduled task (see task-scheduling skill / cron) at a slow
cadence (once per day, or every few hours max — NOT inside the live
auction scan loop). It:

  1. (Optional) Pulls fresh community signals via community_sources.py
     and stores them in MarketDB.community_signal.
  2. Recomputes valuation reports for every tracked (item, region, qlt,
     bonus_bucket) using price_model.compute_valuation.
  3. Compares each new valuation's quick_sale / fair_value against the
     previous baseline to detect significant deviations:
        - snipe_opportunity    listing well below the rolling floor
        - overpriced_listing   listing far above the live comparable cap
        - history_vs_live_gap  stale history still dragging the estimate
        - community_vs_market_gap  Reddit/Discord claims wildly off from
                                   the auction-data valuation
        - volatility_spike     day-over-day fair_value moved > X%
  4. Logs price_deviation_alert rows and (optionally) posts Discord alerts.

Deviation thresholds are env-configurable; defaults are conservative so
the monitor doesn't spam alerts on normal noise.

Integration note: this module is deliberately decoupled from the live
scapi client — it consumes whatever `auction_snapshot` /
`sale_observation` rows the bot's scan loop has already written. If
you want it to pull fresh lots itself, wrap your scapi client and call
`record_snapshot` / `record_sale` first, then run `run_daily_monitor`.
"""

from __future__ import annotations

import os
import time
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from market_db import MarketDB
from price_model import compute_valuation, ValuationResult
import community_sources

# Deviation thresholds (percentages, 0-100).
SNIP_DEVIATION_PCT = 15.0      # listing ≥15% below fair_value -> snipe
OVERPRICED_DEVIATION_PCT = 25.0  # listing ≥25% above live cap -> overpriced
GAP_HISTORY_LIVE_PCT = 40.0     # history estimate ≥40% above live cap -> gap alert
GAP_COMMUNITY_PCT = 60.0        # community claim ≥60% off market -> review flag
VOLATILITY_DAY_PCT = 20.0       # fair_value moved ≥20% day-over-day

REGION = os.getenv("REGION", "na").lower()
# When false (default), the monitor will compute valuations and log alerts to
# the DB for dashboard visibility, but will NOT send Discord/notifications.
# Set LIVE_MARKET_DATA=true once your bot's scan loop is writing real
# auction_snapshot and sale_observation rows — this prevents the daily
# monitor from alerting on seeded/demo data.
LIVE_MARKET_DATA = os.getenv("LIVE_MARKET_DATA", "false").lower() == "true"
SALE_RECENCY_DAYS = 14
BASELINE_LOOKBACK_DAYS = 7
MIN_ALERT_CONFIDENCE = 65

QUALITY_NAMES = {
    0: "Common", 1: "Uncommon", 2: "Special",
    3: "Rare", 4: "Exclusive", 5: "Legendary",
}
QUALITY_MULTIPLIERS = {
    0: 1.0, 1: 1.20, 2: 1.50, 3: 2.50, 4: 4.00, 5: 6.00,
}


def fmt_rub(n: float | None) -> str:
    if n is None:
        return "—"
    return f"{int(n):,}"


def _severity(deviation_pct: float, threshold: float) -> str:
    ratio = deviation_pct / max(threshold, 1.0)
    if ratio >= 2.0:
        return "critical"
    if ratio >= 1.5:
        return "high"
    if ratio >= 1.0:
        return "watch"
    return "info"


def _gather_evidence(db: MarketDB, item_id: str, item_name: str,
                     region: str, qlt: int, bonus_bucket_val: int) -> dict:
    """Pull every evidence bucket for one (item, region, qlt, bucket).

    Reads whatever the bot's live scan loop + prior monitor runs have
    already recorded. Returns a kwargs dict ready for compute_valuation.
    """
    now = time.time()
    since_sales = now - SALE_RECENCY_DAYS * 86400

    live_rows = db.latest_snapshot(item_id, region, since_sales)
    live_prices = [
        r["unit_price"] for r in live_rows
        if r["qlt"] == qlt and r["bonus_bucket"] == bonus_bucket_val and r["unit_price"]
    ]

    sale_rows = db.recent_sales(item_id, region, qlt, bonus_bucket_val, since_sales)
    confirmed = [r["unit_price"] for r in sale_rows if r["source"] == "confirmed_bid_sale"]
    inferred = [r["unit_price"] for r in sale_rows if r["source"] == "inferred_buyout_sale"]
    official = [r["unit_price"] for r in sale_rows if r["source"] == "official_history"]

    community_rows = db.recent_community_signals(item_name, region, since_sales)
    community_signals = [
        {"sentiment_score": r["sentiment_score"], "confidence": r["confidence"]}
        for r in community_rows
    ]

    manual = db.manual_price(item_id, region, qlt, bonus_bucket_val / 1000.0)

    return {
        "live_comparable_unit_prices": live_prices,
        "confirmed_sale_prices": confirmed,
        "inferred_sale_prices": inferred,
        "official_history_prices": official,
        "quality_multiplier": QUALITY_MULTIPLIERS.get(qlt, 1.0),
        "community_signals": community_signals,
        "manual_override": dict(manual) if manual else None,
    }


def _previous_baseline(db: MarketDB, item_id: str, region: str, qlt: int,
                       bonus_bucket_val: int) -> ValuationResult | None:
    """Get the most recent prior valuation report (excluding today's run)."""
    since = time.time() - BASELINE_LOOKBACK_DAYS * 86400
    rows = db.valuation_history(item_id, region, qlt, bonus_bucket_val, BASELINE_LOOKBACK_DAYS)
    if len(rows) < 2:
        return None
    prev = rows[-2]  # second-to-last = previous baseline
    return ValuationResult(
        item_id=item_id, item_name=prev["item_name"], region=region,
        qlt=qlt, bonus_bucket=bonus_bucket_val,
        floor=prev["floor_price"], quick_sale=prev["quick_sale_price"],
        fair_value=prev["fair_value_price"], stretch=prev["stretch_price"],
        confidence=prev["confidence"],
    )


def run_daily_monitor(db: MarketDB, tracked_items: list[dict] | None = None,
                      collect_community: bool = True) -> list[dict]:
    """Main entry point. Recompute valuations and log deviation alerts.

    `tracked_items` is a list of dicts with at least:
        {item_id, item_name, region, qlt, bonus_bucket}
    If omitted, the monitor derives the distinct set from whatever
    auction_snapshot rows already exist in the DB (so it works even
    before the bot registers a formal tracked-items list here).
    """
    alerts: list[dict] = []

    if collect_community:
        item_filter = {it["item_name"] for it in (tracked_items or [])} or None
        for signal in community_sources.collect_all_community_signals(item_filter=item_filter):
            db.record_community_signal(
                item_name=signal["item_name"], item_id=signal.get("item_id"),
                region=signal.get("region", REGION),
                qlt=signal.get("qlt"), bonus_bucket=signal.get("bonus_bucket"),
                claimed_price=signal.get("claimed_price"),
                sentiment=signal["sentiment"], sentiment_score=signal["sentiment_score"],
                source=signal["source"], source_name=signal.get("source_name"),
                url=signal.get("url"), excerpt=signal.get("excerpt"),
                confidence=signal["confidence"],
            )

    if tracked_items is None:
        tracked_items = _derive_tracked_from_snapshots(db)

    for item in tracked_items:
        item_id = item["item_id"]
        item_name = item["item_name"]
        region = item.get("region", REGION)
        qlt = item["qlt"]
        bucket = item["bonus_bucket"]

        evidence = _gather_evidence(db, item_id, item_name, region, qlt, bucket)
        valuation = compute_valuation(
            item_id, item_name, region, qlt, bucket, **evidence,
        )
        db.record_valuation(**valuation.as_row())

        if valuation.confidence < MIN_ALERT_CONFIDENCE:
            # Low-confidence items still get a "needs review" alert if
            # community sentiment is loud enough to be worth a look.
            if valuation.community_signals >= 3 and abs(valuation.community_bias) > 0.3:
                alerts.append(_make_alert(
                    item_id, item_name, region, qlt, bucket,
                    alert_type="community_vs_market_gap",
                    baseline_price=valuation.fair_value,
                    observed_price=None,
                    deviation_pct=abs(valuation.community_bias) * 100,
                    severity="watch",
                    message=(
                        f"{QUALITY_NAMES.get(qlt, '?')} {item_name} — low confidence "
                        f"({valuation.confidence}/100) but {valuation.community_signals} "
                        f"community signals lean "
                        f"{'bullish' if valuation.community_bias > 0 else 'bearish'}. "
                        f"Manual review recommended."
                    ),
                ))
            continue

        prev = _previous_baseline(db, item_id, region, qlt, bucket)

        # 1. Volatility check (day-over-day fair_value movement)
        if prev and prev.fair_value and valuation.fair_value:
            move_pct = abs(valuation.fair_value - prev.fair_value) / max(prev.fair_value, 1) * 100
            if move_pct >= VOLATILITY_DAY_PCT:
                direction = "up" if valuation.fair_value > prev.fair_value else "down"
                alerts.append(_make_alert(
                    item_id, item_name, region, qlt, bucket,
                    alert_type="volatility_spike",
                    baseline_price=prev.fair_value,
                    observed_price=valuation.fair_value,
                    deviation_pct=move_pct,
                    severity=_severity(move_pct, VOLATILITY_DAY_PCT),
                    message=(
                        f"{QUALITY_NAMES.get(qlt, '?')} {item_name} fair value moved "
                        f"{direction} {move_pct:.1f}% ({fmt_rub(prev.fair_value)} -> "
                        f"{fmt_rub(valuation.fair_value)} RUB)."
                    ),
                ))

        # 2. History vs live gap
        if valuation.live_comparables and valuation.confirmed_sales + valuation.inferred_sales > 0:
            if evidence["confirmed_sale_prices"] or evidence["inferred_sale_prices"]:
                hist_est = evidence["confirmed_sale_prices"] or evidence["inferred_sale_prices"]
                from price_model import percentile
                hist_p = percentile(hist_est, 20)
                cheapest_live = min(evidence["live_comparable_unit_prices"])
                gap = (hist_p - cheapest_live) / max(cheapest_live, 1) * 100
                if gap >= GAP_HISTORY_LIVE_PCT:
                    alerts.append(_make_alert(
                        item_id, item_name, region, qlt, bucket,
                        alert_type="history_vs_live_gap",
                        baseline_price=hist_p,
                        observed_price=cheapest_live,
                        deviation_pct=gap,
                        severity=_severity(gap, GAP_HISTORY_LIVE_PCT),
                        message=(
                            f"{QUALITY_NAMES.get(qlt, '?')} {item_name}: historical sales "
                            f"({fmt_rub(hist_p)} RUB) are {gap:.0f}% above the cheapest live "
                            f"comparable ({fmt_rub(cheapest_live)} RUB). Estimate may be stale."
                        ),
                    ))

        # 3. Community vs market gap
        if evidence["community_signals"]:
            # Use the most recent community signal's claimed price, if any.
            community_rows = db.recent_community_signals(item_name, region, time.time() - SALE_RECENCY_DAYS * 86400)
            for cr in community_rows:
                if cr["claimed_price"] and valuation.fair_value:
                    dev = abs(cr["claimed_price"] - valuation.fair_value) / max(valuation.fair_value, 1) * 100
                    if dev >= GAP_COMMUNITY_PCT:
                        alerts.append(_make_alert(
                            item_id, item_name, region, qlt, bucket,
                            alert_type="community_vs_market_gap",
                            baseline_price=valuation.fair_value,
                            observed_price=cr["claimed_price"],
                            deviation_pct=dev,
                            severity=_severity(dev, GAP_COMMUNITY_PCT),
                            message=(
                                f"{QUALITY_NAMES.get(qlt, '?')} {item_name}: community claim "
                                f"({fmt_rub(cr['claimed_price'])} RUB via {cr['source']}) is "
                                f"{dev:.0f}% off market fair value ({fmt_rub(valuation.fair_value)} RUB). "
                                f"Source: {cr.get('url', cr['source'])}"
                            ),
                        ))
                        break  # one alert per item per run is enough

    for a in alerts:
        db.record_alert(**a)

    # Only dispatch external notifications (Discord, push) when the system
    # is running against real live market data, not seeded demo data.
    if LIVE_MARKET_DATA:
        post_discord_alerts(alerts)
    else:
        # Still log to DB (visible on dashboard) but don't spam Discord
        # with alerts derived from sample data.
        pass

    return alerts


def _derive_tracked_from_snapshots(db: MarketDB) -> list[dict]:
    """Fallback: build a tracked-items list from existing auction_snapshot rows."""
    since = time.time() - SALE_RECENCY_DAYS * 86400
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT item_id, item_name, region, qlt, bonus_bucket "
            "FROM auction_snapshot WHERE observed_at >= ? ORDER BY item_name",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def _make_alert(item_id, item_name, region, qlt, bucket, **fields) -> dict:
    return {
        "item_id": item_id, "item_name": item_name, "region": region,
        "qlt": qlt, "bonus_bucket": bucket, **fields,
    }


# ---------------------------------------------------------------------
# Optional Discord alert dispatcher
# ---------------------------------------------------------------------
def post_discord_alerts(alerts: list[dict], webhook_url: str | None = None) -> None:
    """Send a compact summary of today's alerts to a Discord webhook.

    Set DISCORD_ALERT_WEBHOOK in env to enable. This is fire-and-forget;
    failures are swallowed so the monitor itself never blocks on Discord.
    """
    webhook_url = webhook_url or os.getenv("DISCORD_ALERT_WEBHOOK", "")
    if not webhook_url or not alerts:
        return
    import json
    from urllib.request import Request, urlopen

    lines = [f"**StalZone Daily Monitor — {len(alerts)} alert(s)**"]
    for a in alerts[:15]:
        sev_emoji = {"critical": "🚨", "high": "⚠️", "watch": "👁️", "info": "ℹ️"}.get(a["severity"], "•")
        lines.append(f"{sev_emoji} **{a['alert_type']}** — {a['item_name']} ({a['severity']})\n{a['message']}")
    if len(alerts) > 15:
        lines.append(f"_...and {len(alerts) - 15} more._")

    payload = {"content": "\n\n".join(lines)}
    try:
        req = Request(webhook_url, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=15)
    except Exception:
        pass


if __name__ == "__main__":
    db = MarketDB()
    alerts = run_daily_monitor(db)
    if LIVE_MARKET_DATA:
        post_discord_alerts(alerts)
        print(f"Daily monitor complete. {len(alerts)} alerts logged and dispatched.")
    else:
        print(f"Daily monitor complete. {len(alerts)} alerts logged to DB (LIVE_MARKET_DATA=false — no Discord dispatch).")
