"""Discord artifact command backed by official metadata and exact-level NA market data."""
from __future__ import annotations

import statistics
import time
from typing import Any

import discord

from artifact_catalog import QUALITY_NAMES, load_artifact_catalog
from market_db import MarketDB

QUALITY_COLORS = {0:0x8B8F86,1:0x79B84B,2:0x4F98D1,3:0x9A63D8,4:0xE05252,5:0xE6A33C}


def _money(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f} ₽"


def _median(values: list[float]) -> float | None:
    vals=[float(v) for v in values if v is not None and float(v)>0]
    return statistics.median(vals) if vals else None


def _find_artifact(catalog: dict[str,dict[str,Any]], query: str):
    q=query.strip().lower()
    if not q: return None
    if q in catalog: return catalog[q]
    for pool in (
        [a for a in catalog.values() if a.get("item_name","").lower()==q],
        [a for a in catalog.values() if a.get("item_name","").lower().startswith(q)],
        [a for a in catalog.values() if q in a.get("item_name","").lower()],
    ):
        if pool: return sorted(pool,key=lambda a:len(a.get("item_name", "")))[0]
    return None


def _targets(floor,live_med,live_count,sale_med,sale_count):
    observed=[x for x in (floor,live_med,sale_med) if x and x>0]
    if not observed: return None,None,None,"none"
    quick=min(floor*.99,sale_med*.97) if floor and sale_med else floor*.99 if floor else sale_med*.95
    rec=max(quick,statistics.median(observed))
    high=None
    if sale_count>=5 and (live_count>=2 or sale_count>=15):
        c=[rec*1.06]
        if sale_med: c.append(sale_med*1.08)
        if live_med: c.append(live_med*1.05)
        high=max(c)
    conf="high" if sale_count>=20 and live_count>=3 else "medium" if sale_count>=5 or live_count>=3 else "low"
    return quick,rec,high,conf


def _market_by_variant(db: MarketDB,item_id: str,region: str="na"):
    now=time.time()
    with db._conn() as conn:
        live=conn.execute(
            "SELECT qlt,upgrade_level,unit_price,lot_key,observed_at FROM auction_snapshot "
            "WHERE item_id=? AND region=? AND observed_at>=? AND unit_price>0 AND upgrade_level BETWEEN 0 AND 15",
            (item_id,region,now-20*60),
        ).fetchall()
        sales=conn.execute(
            "SELECT qlt,upgrade_level,unit_price FROM sale_observation WHERE item_id=? AND region=? "
            "AND source='official_history' AND observed_at>=? AND unit_price>0 AND upgrade_level BETWEEN 0 AND 15",
            (item_id,region,now-7*86400),
        ).fetchall()
    latest={}
    for r in live:
        k=r["lot_key"] or f"{r['qlt']}:{r['upgrade_level']}:{r['unit_price']}:{r['observed_at']}"
        if k not in latest or (r["observed_at"] or 0)>(latest[k]["observed_at"] or 0): latest[k]=r
    grouped={}
    for row in latest.values():
        key=(int(row["qlt"]),int(row["upgrade_level"]))
        grouped.setdefault(key,{"live":[],"sales":[]})["live"].append(float(row["unit_price"]))
    for row in sales:
        key=(int(row["qlt"]),int(row["upgrade_level"]))
        grouped.setdefault(key,{"live":[],"sales":[]})["sales"].append(float(row["unit_price"]))
    out={}
    for (qlt,level),data in grouped.items():
        floor=min(data["live"]) if data["live"] else None
        live_med=_median(data["live"]); sale_med=_median(data["sales"])
        quick,rec,high,conf=_targets(floor,live_med,len(data["live"]),sale_med,len(data["sales"]))
        out[(qlt,level)]={"qlt":qlt,"upgrade_level":level,"live_floor":floor,"live_count":len(data["live"]),"sale_median":sale_med,"sale_count":len(data["sales"]),"sell_quick":quick,"sell_recommended":rec,"sell_high_margin":high,"sell_confidence":conf}
    return out


def _group_stats(stats):
    grouped={}
    for stat in stats: grouped.setdefault(stat.get("group") or "Utility",[]).append(stat)
    return grouped


def register_catalog_commands(bot: discord.Client, region: str="na") -> None:
    @bot.tree.command(name="artifact",description="Show official artifact stats, exact +level NA prices, and sell guidance")
    @discord.app_commands.describe(item_name="Artifact name, for example Link or Firebird")
    async def artifact_command(interaction: discord.Interaction,item_name: str):
        await interaction.response.defer(thinking=True)
        try: catalog=await load_artifact_catalog()
        except Exception as exc:
            await interaction.followup.send(f"Could not load official artifact metadata: {exc}",ephemeral=True); return
        artifact=_find_artifact(catalog,item_name)
        if not artifact:
            await interaction.followup.send(f"Artifact '{item_name}' was not found in the official catalog.",ephemeral=True); return
        market=_market_by_variant(MarketDB(),artifact["item_id"],region)
        tiers=[q for q,_ in market]
        embed=discord.Embed(title=artifact["item_name"],description=(f"**{artifact['artifact_class']}** artifact · `{artifact['item_id']}`\nPrices are separated by rarity and exact enhancement level (+0 through +15)."),color=QUALITY_COLORS[max(tiers)] if tiers else 0x9B6CFF)
        if artifact.get("icon_url"): embed.set_thumbnail(url=artifact["icon_url"])
        market_lines=[]; sell_lines=[]
        for m in sorted(market.values(),key=lambda x:(x["qlt"],x["upgrade_level"])):
            tier_name=QUALITY_NAMES.get(m["qlt"],f"Q{m['qlt']}")
            variant=f"{tier_name} +{m['upgrade_level']}"
            market_lines.append(f"**{variant}** — floor {_money(m['live_floor'])} ({m['live_count']} live) · 7d median {_money(m['sale_median'])} ({m['sale_count']} sales)")
            sell_lines.append(f"**{variant}** — quick {_money(m['sell_quick'])} · recommended {_money(m['sell_recommended'])} · higher-margin {_money(m['sell_high_margin'])} · {m['sell_confidence']} confidence")
        if not market_lines:
            market_lines=["No current exact-level evidence for this artifact."]; sell_lines=["No evidence-based prediction yet."]
        embed.add_field(name="NA market by rarity + level",value="\n".join(market_lines)[:1024],inline=False)
        embed.add_field(name="Sell guidance",value="\n".join(sell_lines)[:1024],inline=False)
        for group,stats in list(_group_stats(artifact.get("stats") or []).items())[:4]:
            lines=[]
            for stat in stats[:5]:
                value=stat.get("display") or stat.get("value") or "—"
                lines.append(f"{'⚠ ' if stat.get('harmful') else ''}**{stat['name']}**: {value}")
            if len(stats)>5: lines.append(f"… +{len(stats)-5} more")
            embed.add_field(name=group,value="\n".join(lines)[:1024],inline=True)
        embed.set_footer(text="Observed official NA data first · predictions are estimates, not guarantees")
        await interaction.followup.send(embed=embed)
