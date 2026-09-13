const API = "/api";

const TIER_COLORS = {0:"#8b8f86",1:"#79b84b",2:"#4f98d1",3:"#9a63d8",4:"#e6a33c",5:"#e05252"};
const TIER_NAMES = {0:"Common",1:"Uncommon",2:"Special",3:"Rare",4:"Exclusive",5:"Legendary"};

const state = {
  artifacts: [],
  market: [],
  summary: {},
  tiers: [],
  tier: null,
  artifactClass: "",
  statGroup: "",
  search: "",
  sort: "name",
  compact: false,
};

function money(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const value = Number(n);
  if (value >= 1_000_000) return `${(value/1_000_000).toFixed(value >= 10_000_000 ? 1 : 2)}m ₽`;
  if (value >= 1_000) return `${Math.round(value/1000)}k ₽`;
  return `${Math.round(value).toLocaleString()} ₽`;
}

function number(n) { return Number(n || 0).toLocaleString(); }
function esc(s="") { return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

async function get(path) {
  const r = await fetch(API + path, {cache:"no-store"});
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

function marketByItem() {
  const map = new Map();
  for (const row of state.market) {
    if (!map.has(row.item_id)) map.set(row.item_id, []);
    map.get(row.item_id).push(row);
  }
  for (const rows of map.values()) rows.sort((a,b)=>a.qlt-b.qlt);
  return map;
}

function chooseMarketRow(rows) {
  if (!rows?.length) return null;
  if (state.tier !== null) return rows.find(r => r.qlt === state.tier) || null;
  return [...rows].sort((a,b)=>b.qlt-a.qlt)[0];
}

function artifactSearchText(a) {
  return [a.item_name,a.artifact_class,...(a.stat_groups||[]),...(a.stats||[]).map(s=>s.name)].join(" ").toLowerCase();
}

function filteredArtifacts() {
  const marketMap = marketByItem();
  const q = state.search.trim().toLowerCase();
  let items = state.artifacts.filter(a => {
    const rows = marketMap.get(a.item_id) || [];
    if (state.tier !== null && !rows.some(r => r.qlt === state.tier)) return false;
    if (state.artifactClass && a.artifact_class !== state.artifactClass) return false;
    if (state.statGroup && !(a.stat_groups||[]).includes(state.statGroup)) return false;
    if (q && !artifactSearchText(a).includes(q)) return false;
    return true;
  });

  items.sort((a,b) => {
    const ar = chooseMarketRow(marketMap.get(a.item_id));
    const br = chooseMarketRow(marketMap.get(b.item_id));
    if (state.sort === "floor") return (ar?.live_floor ?? Infinity) - (br?.live_floor ?? Infinity);
    if (state.sort === "sales") return (br?.sale_count ?? -1) - (ar?.sale_count ?? -1);
    if (state.sort === "tier") return (br?.qlt ?? -1) - (ar?.qlt ?? -1) || a.item_name.localeCompare(b.item_name);
    return a.item_name.localeCompare(b.item_name);
  });
  return items;
}

function tierPill(q) {
  const color = TIER_COLORS[q] || "#888";
  return `<span class="tier-pill" style="color:${color}">${esc(TIER_NAMES[q] || `Q${q}`)}</span>`;
}

function renderFilters() {
  const tierBox = document.getElementById("tier-filters");
  tierBox.innerHTML = `<button class="chip ${state.tier===null?'active':''}" data-tier="all">All tiers</button>` +
    state.tiers.map(t => `<button class="chip ${state.tier===t.qlt?'active':''}" data-tier="${t.qlt}" style="--tier:${t.color}">${esc(t.name)}</button>`).join("");
  tierBox.querySelectorAll("[data-tier]").forEach(btn => btn.onclick = () => {
    state.tier = btn.dataset.tier === "all" ? null : Number(btn.dataset.tier);
    renderFilters(); renderGrid();
  });

  const classes = [...new Set(state.artifacts.map(a=>a.artifact_class).filter(Boolean))].sort();
  const groups = [...new Set(state.artifacts.flatMap(a=>a.stat_groups||[]))].sort();
  const cf = document.getElementById("class-filter");
  const sf = document.getElementById("stat-filter");
  cf.innerHTML = `<option value="">All classes</option>` + classes.map(v=>`<option ${state.artifactClass===v?'selected':''}>${esc(v)}</option>`).join("");
  sf.innerHTML = `<option value="">All stat groups</option>` + groups.map(v=>`<option ${state.statGroup===v?'selected':''}>${esc(v)}</option>`).join("");
}

function renderKpis() {
  document.getElementById("kpi-artifacts").textContent = number(state.artifacts.length);
  document.getElementById("kpi-sales").textContent = number(state.summary.sales);
  document.getElementById("kpi-snaps").textContent = number(state.summary.snapshots);
  document.getElementById("kpi-tiers").textContent = number(Object.keys(state.summary.tier_breakdown||{}).length);
  const last = Number(state.summary.last_ingestion || 0);
  document.getElementById("source-label").textContent = state.summary.data_source === "official_na_live"
    ? `Official NA auction feed${last ? ` · last ingestion ${new Date(last*1000).toLocaleString()}` : ""}`
    : "Waiting for official NA market ingestion";
}

function renderGrid() {
  const grid = document.getElementById("artifact-grid");
  grid.classList.toggle("compact", state.compact);
  const marketMap = marketByItem();
  const items = filteredArtifacts();
  document.getElementById("result-count").textContent = `${items.length} artifact${items.length===1?'':'s'}`;

  if (!items.length) {
    grid.innerHTML = `<div class="empty">No artifacts match these filters. Legendary is tier 5; clear filters if you want to see every official artifact.</div>`;
    return;
  }

  grid.innerHTML = items.map(a => {
    const rows = marketMap.get(a.item_id) || [];
    const chosen = chooseMarketRow(rows);
    const tiers = rows.map(r=>tierPill(r.qlt)).join("") || `<span class="tier-pill" style="color:var(--muted)">No observed tier yet</span>`;
    const groups = (a.stat_groups||[]).slice(0,state.compact?2:4).map(g=>`<span class="stat-tag">${esc(g)}</span>`).join("");
    return `<article class="artifact-card">
      <div class="artifact-top">
        <div class="icon-wrap"><img src="${esc(a.icon_url)}" alt="${esc(a.item_name)}" loading="lazy" onerror="this.style.opacity=.18"></div>
        <div><div class="artifact-name">${esc(a.item_name)}</div><div class="artifact-class">${esc(a.artifact_class)} · ID ${esc(a.item_id)}</div><div class="tier-line">${tiers}</div></div>
      </div>
      <div class="prices">
        <div class="metric official"><span>Live floor${chosen?` · ${esc(chosen.qlt_name)}`:""}</span><strong>${money(chosen?.live_floor)}</strong></div>
        <div class="metric official"><span>Official sale median</span><strong>${money(chosen?.sale_median)}</strong></div>
        <div class="metric"><span>Sale records · 7d</span><strong>${number(chosen?.sale_count)}</strong></div>
        <div class="metric model"><span>Model estimate</span><strong>${money(chosen?.model_fair_value)}</strong></div>
      </div>
      <div class="stats">${groups}<span class="more">Details →</span></div>
      <button class="card-btn" data-id="${esc(a.item_id)}">Open ${esc(a.item_name)}</button>
    </article>`;
  }).join("");

  grid.querySelectorAll(".card-btn").forEach(b => b.onclick = () => openDetail(b.dataset.id));
}

function detailStatRows(stats) {
  if (!stats?.length) return `<div class="desc">No official stat ranges were present in the current EXBO item record.</div>`;
  return stats.map(s => `<div class="stat-row"><span>${esc(s.name)} <small style="color:var(--faint)">· ${esc(s.group)}</small></span><b class="${s.harmful?'harm':'good'}">${esc(s.display || String(s.value ?? ""))}</b></div>`).join("");
}

function detailMarketRows(rows) {
  if (!rows?.length) return `<div class="desc">No NA auction observation has been stored for this artifact yet.</div>`;
  return [...rows].sort((a,b)=>b.qlt-a.qlt).map(r => `<div class="market-tier">
    <b style="color:${TIER_COLORS[r.qlt]}">${esc(r.qlt_name)}</b>
    <span>Floor<br><strong style="color:var(--good)">${money(r.live_floor)}</strong></span>
    <span>Sale median<br><strong style="color:var(--good)">${money(r.sale_median)}</strong></span>
    <span class="model-col">Model<br><strong style="color:var(--accent2)">${money(r.model_fair_value)}</strong></span>
  </div>`).join("");
}

function openDetail(id) {
  const a = state.artifacts.find(x=>x.item_id===id);
  if (!a) return;
  const rows = marketByItem().get(id) || [];
  const modal = document.getElementById("modal");
  document.getElementById("dialog-content").innerHTML = `
    <div class="dialog-head">
      <img src="${esc(a.icon_url)}" alt="${esc(a.item_name)}"><div><h4 id="detail-title">${esc(a.item_name)}</h4><p>${esc(a.artifact_class)} · Official EXBO metadata</p></div>
      <button class="close" id="close-modal" aria-label="Close">×</button>
    </div>
    <div class="dialog-body">
      <div class="detail-grid">
        <div>
          <div class="detail-section"><h5>Official artifact stats</h5>${detailStatRows(a.stats)}</div>
          <div class="detail-section" style="margin-top:12px"><h5>Description</h5><div class="desc">${esc(a.description || "No description in the current official record.")}</div></div>
        </div>
        <div>
          <div class="detail-section"><h5>NA market by tier</h5>${detailMarketRows(rows)}<div class="desc" style="margin-top:10px">Green values are observed official auction data. Purple values are model estimates and are not presented as real sales.</div></div>
          <div class="detail-section" style="margin-top:12px"><h5>Source</h5><div class="desc">Stats and icon: EXBO-Studio/stalzone-database.<br>Prices: official STALCRAFT NA auction API.</div></div>
        </div>
      </div>
    </div>`;
  modal.classList.add("open");
  document.getElementById("close-modal").onclick = closeDetail;
}

function closeDetail(){ document.getElementById("modal").classList.remove("open"); }

async function load() {
  const grid = document.getElementById("artifact-grid");
  grid.innerHTML = `<div class="loading">Loading official artifact catalog and NA market data…</div>`;
  try {
    const [artifacts, market, summary, tiers] = await Promise.all([
      get("/artifacts"), get("/market?region=na"), get("/summary?region=na"), get("/quality-tiers")
    ]);
    state.artifacts = artifacts;
    state.market = market;
    state.summary = summary;
    state.tiers = tiers;
    renderKpis(); renderFilters(); renderGrid();
  } catch (e) {
    console.error(e);
    grid.innerHTML = `<div class="empty error">Could not load the official market feed: ${esc(e.message)}. The site will not invent sample prices.</div>`;
    document.getElementById("source-label").textContent = "Official data unavailable — no fake fallback is used";
  }
}

function bind() {
  const savedTheme = localStorage.getItem("sz-theme") || "purple";
  document.documentElement.dataset.theme = savedTheme;
  document.getElementById("theme").value = savedTheme;
  document.getElementById("theme").onchange = e => { document.documentElement.dataset.theme=e.target.value; localStorage.setItem("sz-theme",e.target.value); };
  document.getElementById("density").onclick = e => { state.compact=!state.compact; e.currentTarget.textContent=state.compact?"Comfortable":"Compact"; renderGrid(); };
  document.getElementById("refresh").onclick = load;
  document.getElementById("search").oninput = e => { state.search=e.target.value; renderGrid(); };
  document.getElementById("class-filter").onchange = e => { state.artifactClass=e.target.value; renderGrid(); };
  document.getElementById("stat-filter").onchange = e => { state.statGroup=e.target.value; renderGrid(); };
  document.getElementById("sort").onchange = e => { state.sort=e.target.value; renderGrid(); };
  document.getElementById("modal").onclick = e => { if(e.target.id==="modal") closeDetail(); };
  document.addEventListener("keydown", e => { if(e.key==="Escape") closeDetail(); });
}

bind();
load();
