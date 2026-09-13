"""Discord commands backed by official artifact metadata and observed NA market data."""
from __future__ import annotations

import statistics
import time
from typing import Any

import discord

from artifact_catalog import QUALITY_NAMES, load_artifact_catalog
from market_db import MarketDB

QUALITY_COLORS = {
    0: 0x8B8F86,
    1: 0x79B84B,
    2: 0x4F98D1,
    3: 0x9A63D8,
    4: 0xE05252,  # Exclusive
    5: 0xE6A33C,  # Legendary
}


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} ₽"


def _median(values: list[float]) -> float | None:
    values = [float(v) for v in values if v is not None and float(v) > 0]
    return statistics.median(values) if values else None


def _find_artifact(catalog: dict[str, dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    if not q:
        return None
    if q in catalog:
        return catalog[q]
    exact = [a for a in catalog.values() if a.get("item_name", "").lower() == q]
    if exact:
        return exact[0]
    starts = [a for a in catalog.values() if a.get("item_name", "").lower().startswith(q)]
    if starts:
        return sorted(starts, key=lambda a: len(a.get("item_name", "")))[0]
    contains = [a for a in catalog.values() if q in a.get("item_name", "").lower()]
    if contains:
        return sorted(contains, key=lambda a: len(a.get("item_name", "")))[0]
    return None


def _sell_targets(live_floor, live_median, live_count, sale_median, sale_count):
    observed = [x for x in (live_floor, live_median, sale_median) if x and x > 0]
    if not observed:
        return None, None, None, "none"
    if live_floor and sale_median:
        quick = min(live_floor * 0.99, sale_median * 0.97)
    elif live_floor:
        quick = live_floor * 0.99
    else:
        quick = sale_median * 0.95
    recommended = max(quick, statistics.median(observed))
    high = None
    if sale_count >= 5 and (live_count >= 2 or sale_count >= 15):
        candidates = [recommended * 1.06]
        if sale_median:
            candidates.append(sale_median * 1.08)
        if live_median:
            candidates.append(live_median * 1.05)
        high = max(candidates)
    confidence = "high" if sale_count >= 20 and live_count >= 3 else "medium" if sale_count >= 5 or live_count >= 3 else "low"
    return quick, recommended, high, confidence


def _market_by_tier(db: MarketDB, item_id: str, region: str = "na") -> dict[int, dict[str, Any]]:
    now = time.time()
    with db._conn() as conn:
        live = conn.execute(
            "SELECT qlt, unit_price FROM auction_snapshot WHERE item_id=? AND region=? AND observed_at>=? AND unit_price>0",
            (item_id, region, now - 20 * 60),
        ).fetchall()
        sales = conn.execute(
            "SELECT qlt, unit_price FROM sale_observation WHERE item_id=? AND region=? AND source='official_history' AND observed_at>=? AND unit_price>0",
            (item_id, region, now - 7 * 86400),
        ).fetchall()

    grouped: dict[int, dict[str, Any]] = {qlt: {"live": [], "sales": []} for qlt in QUALITY_NAMES}
    for row in live:
        grouped.setdefault(int(row["qlt"]), {"live": [], "sales": []})["live"].append(float(row["unit_price"]))
    for row in sales:
        grouped.setdefault(int(row["qlt"]), {"live": [], "sales": []})["sales"].append(float(row["unit_price"]))

    result: dict[int, dict[str, Any]] = {}
    for qlt in QUALITY_NAMES:
        data = grouped.get(qlt, {"live": [], "sales": []})
        floor = min(data["live"]) if data["live"] else None
        live_med = _median(data["live"])
        sale_med = _median(data["sales"])
        quick, recommended, high, confidence = _sell_targets(
            floor, live_med, len(data["live"]), sale_med, len(data["sales"])
        )
        result[qlt] = {
            "live_floor": floor,
            "live_count": len(data["live"]),
            "sale_median": sale_med,
            "sale_count": len(data["sales"]),
            "sell_quick": quick,
            "sell_recommended": recommended,
            "sell_high_margin": high,
            "sell_confidence": confidence,
        }
    return result


def _group_stats(stats: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for stat in stats:
        grouped.setdefault(stat.get("group") or "Utility", []).append(stat)
    return grouped


def register_catalog_commands(bot: discord.Client, region: str = "na") -> None:
    tree = bot.tree

    @tree.command(name="artifact", description="Show official artifact stats, NA prices, and sell guidance")
    @discord.app_commands.describe(item_name="Artifact name, for example Link or Firebird")
    async def artifact_command(interaction: discord.Interaction, item_name: str):
        await interaction.response.defer(thinking=True)
        try:
            catalog = await load_artifact_catalog()
        except Exception as exc:
            await interaction.followup.send(f"Could not load official artifact metadata: {exc}", ephemeral=True)
            return

        artifact = _find_artifact(catalog, item_name)
        if not artifact:
            await interaction.followup.send(f"Artifact '{item_name}' was not found in the official catalog.", ephemeral=True)
            return

        db = MarketDB()
        market = _market_by_tier(db, artifact["item_id"], region=region)
        observed_tiers = [q for q, m in market.items() if m["live_count"] or m["sale_count"]]
        embed_color = QUALITY_COLORS[max(observed_tiers)] if observed_tiers else 0x9B6CFF

        embed = discord.Embed(
            title=artifact["item_name"],
            description=(
                f"**{artifact['artifact_class']}** artifact · `{artifact['item_id']}`\n"
                "Observed prices come from the official NA auction feed. Sell targets are predictions based on those observations, not guarantees."
            ),
            color=embed_color,
        )
        if artifact.get("icon_url"):
            embed.set_thumbnail(url=artifact["icon_url"])

        market_lines: list[str] = []
        sell_lines: list[str] = []
        for qlt in range(6):
            m = market[qlt]
            tier = QUALITY_NAMES[qlt]
            if m["live_count"] or m["sale_count"]:
                market_lines.append(
                    f"**{tier}** — floor {_money(m['live_floor'])} ({m['live_count']} live) · "
                    f"7d median {_money(m['sale_median'])} ({m['sale_count']} sales)"
                )
                sell_lines.append(
                    f"**{tier}** — quick {_money(m['sell_quick'])} · recommended {_money(m['sell_recommended'])} · "
                    f"higher-margin {_money(m['sell_high_margin'])} · {m['sell_confidence']} confidence"
                )
            else:
                market_lines.append(f"**{tier}** — no current observation")
                sell_lines.append(f"**{tier}** — no evidence-based prediction")

        embed.add_field(name="NA market by quality", value="\n".join(market_lines)[:1024], inline=False)
        embed.add_field(name="Sell guidance", value="\n".join(sell_lines)[:1024], inline=False)

        grouped_stats = _group_stats(artifact.get("stats") or [])
        for group, stats in list(grouped_stats.items())[:4]:
            lines = []
            for stat in stats[:5]:
                value = stat.get("display") or stat.get("value") or "—"
                sign = "⚠ " if stat.get("harmful") else ""
                lines.append(f"{sign}**{stat['name']}**: {value}")
            if len(stats) > 5:
                lines.append(f"… +{len(stats) - 5} more")
            embed.add_field(name=group, value="\n".join(lines)[:1024], inline=True)

        embed.set_footer(text="Quick = faster sale · Recommended = balanced · Higher-margin = slower/aggressive; not guaranteed")
        await interaction.followup.send(embed=embed)
