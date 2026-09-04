// app.js — StalZone Price Tracker dashboard logic

const API = "/api"; // frontend and API are served from the same FastAPI app/origin
const QUALITY_NAMES = { 0: "Common", 1: "Uncommon", 2: "Special", 3: "Rare", 4: "Exclusive", 5: "Legendary" };
const QUALITY_CLASS = { 0: "qname-common", 1: "qname-uncommon", 2: "qname-special", 3: "qname-rare", 4: "qname-exclusive", 5: "qname-legendary" };
const QUALITY_COLOR = { 0: "#7a7f72", 1: "#8fbc3f", 2: "#5591c7", 3: "#a86fdf", 4: "#e8a317", 5: "#c63838" };

let state = {
  view: "overview",
  valuations: [],
  alerts: [],
  community: [],
  summary: {},
  selectedItem: null,
  chart: null,
};

async function fetchJSON(path) {
  const res = await fetch(API + path);
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return res.json();
}

function fmtRub(n) {
  if (n === null || n === undefined) return "—";
  return Math.round(n).toLocaleString("en-US");
}

function fmtTimeAgo(ts) {
  const secs = Date.now() / 1000 - ts;
  if (secs < 60) return "just now";
  if (secs < 3600) return Math.floor(secs / 60) + "m ago";
  if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
  return Math.floor(secs / 86400) + "d ago";
}

function confClass(conf) {
  if (conf >= 70) return "conf-high";
  if (conf >= 45) return "conf-mid";
  return "conf-low";
}

async function loadAll() {
  try {
    const [valuations, alerts, community, summary] = await Promise.all([
      fetchJSON("/valuations?region=na"),
      fetchJSON("/alerts?region=na&days=7"),
      fetchJSON("/community?region=na&days=7"),
      fetchJSON("/summary?region=na"),
    ]);
    state.valuations = valuations;
    state.alerts = alerts;
    state.community = community;
    state.summary = summary;
    document.getElementById("last-update").textContent = "Updated " + new Date().toLocaleTimeString();
    renderSidebarItems();
    renderNavBadges();
    render();
  } catch (e) {
    console.error(e);
    document.getElementById("main-content").innerHTML = `
      <div class="empty-state">
        <div class="empty-state-title">No data yet</div>
        <div class="empty-state-desc">Seeding sample market data…</div>
      </div>`;
    try {
      await fetchJSON("/seed");
      await loadAll();
    } catch (e2) {
      console.error("Seed failed", e2);
    }
  }
}

function renderNavBadges() {
  const critical = state.alerts.filter(a => a.severity === "critical" || a.severity === "high").length;
  const badge = document.getElementById("nav-alert-count");
  if (critical > 0) {
    badge.style.display = "inline-block";
    badge.textContent = critical;
  } else {
    badge.style.display = "none";
  }
}

function renderSidebarItems() {
  const list = document.getElementById("item-list");
  const seen = new Map();
  for (const v of state.valuations) {
    if (!seen.has(v.item_id)) seen.set(v.item_id, v);
  }
  list.innerHTML = "";
  for (const [id, v] of seen) {
    const div = document.createElement("button");
    div.className = "item-list-item" + (state.selectedItem === id ? " active" : "");
    div.innerHTML = `<span class="qlt-dot" style="background:${QUALITY_COLOR[v.qlt]}"></span>${v.item_name}`;
    div.onclick = () => { state.selectedItem = id; state.view = "valuations"; setActiveNav("valuations"); render(); renderSidebarItems(); };
    list.appendChild(div);
  }
}

function setActiveNav(view) {
  document.querySelectorAll(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.view === view));
}

function render() {
  const titles = { overview: "Market Overview", valuations: "Valuations & Trends", alerts: "Deviation Alerts", community: "Community Sentiment" };
  document.getElementById("page-title").textContent = titles[state.view] || "Dashboard";
  const main = document.getElementById("main-content");
  if (state.view === "overview") main.innerHTML = renderOverview();
  else if (state.view === "valuations") main.innerHTML = renderValuations();
  else if (state.view === "alerts") main.innerHTML = renderAlerts();
  else if (state.view === "community") main.innerHTML = renderCommunity();

  if (state.view === "overview" || state.view === "valuations") {
    requestAnimationFrame(() => drawChart());
  }
  attachTableHandlers();
}

function renderOverview() {
  const s = state.summary;
  const dataSource = s.data_source === 'live' ? 'LIVE DATA' : 'SAMPLE DATA';
  const dataSourceClass = s.data_source === 'live' ? 'data-live' : 'data-sample';
  const tierBreakdown = s.tier_breakdown || {};
  const tierBars = [0,1,2,3,4,5].map(q => {
    const cnt = tierBreakdown[String(q)] || 0;
    const pct = s.total_items > 0 ? (cnt / s.total_items * 100) : 0;
    return `<div class="tier-bar-row">
      <span class="qlt-dot" style="background:${QUALITY_COLOR[q]}"></span>
      <span class="tier-name ${QUALITY_CLASS[q]}">${QUALITY_NAMES[q]}</span>
      <div class="tier-bar-track"><div class="tier-bar-fill" style="width:${pct}%;background:${QUALITY_COLOR[q]}"></div></div>
      <span class="tier-count">${cnt}</span>
    </div>`;
  }).join('');
  return `
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Tracked Artifacts</div>
        <div class="kpi-value">${s.total_items ?? 0}</div>
        <div class="kpi-sub">Across all quality tiers</div>
      </div>
      <div class="kpi-card ${s.critical_alerts > 0 ? 'danger' : ''}">
        <div class="kpi-label">Alerts (7d)</div>
        <div class="kpi-value">${s.total_alerts ?? 0}<span class="unit">total</span></div>
        <div class="kpi-sub">${s.critical_alerts ?? 0} critical</div>
      </div>
      <div class="kpi-card blue">
        <div class="kpi-label">Avg Confidence</div>
        <div class="kpi-value">${s.avg_confidence ?? 0}<span class="unit">/100</span></div>
        <div class="kpi-sub">24h rolling valuation quality</div>
      </div>
      <div class="kpi-card warn">
        <div class="kpi-label">Community Signals</div>
        <div class="kpi-value">${s.community_signals ?? 0}</div>
        <div class="kpi-sub">Reddit, Discord, forums, staldata (7d)</div>
      </div>
    </div>

    <div class="data-source-banner ${dataSourceClass}">
      <span class="data-source-badge">${dataSource}</span>
      <span class="data-source-detail">${s.snapshots ?? 0} snapshots · ${s.sales ?? 0} sales recorded</span>
    </div>

    <div class="section">
      <div class="section-header">
        <div class="section-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
          Price Trend — ${currentItemLabel()}
        </div>
      </div>
      <div class="section-body">
        <div class="chart-legend">
          <div class="legend-item"><span class="legend-dot" style="background:#8fbc3f"></span>Fair Value</div>
          <div class="legend-item"><span class="legend-dot" style="background:#5591c7"></span>Quick Sale Floor</div>
          <div class="legend-item"><span class="legend-dot" style="background:#e8a317"></span>Stretch (bullish ceiling)</div>
        </div>
        <div class="chart-container"><canvas id="trend-chart"></canvas></div>
      </div>
    </div>

    <div class="two-col">
      <div class="section">
        <div class="section-header">
          <div class="section-title">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/></svg>
            Recent Alerts
          </div>
        </div>
        <div class="section-body">
          ${renderAlertList(state.alerts.slice(0, 3))}
        </div>
      </div>
      <div class="section">
        <div class="section-header">
          <div class="section-title">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>
            Community Pulse
          </div>
        </div>
        <div class="section-body">
          ${renderCommunityMini(state.community.slice(0, 3))}
        </div>
      </div>
    </div>
  `;
}

function currentItemLabel() {
  const v = state.valuations.find(v => v.item_id === state.selectedItem) || state.valuations[0];
  return v ? `${QUALITY_NAMES[v.qlt]} ${v.item_name}` : "Select an artifact";
}

function renderValuations() {
  const rows = state.valuations.map(v => {
    const active = v.item_id === state.selectedItem ? "active" : "";
    return `
    <tr class="${active}" data-item="${v.item_id}">
      <td><span class="qname ${QUALITY_CLASS[v.qlt]}">${v.item_name}</span></td>
      <td><span class="qname ${QUALITY_CLASS[v.qlt]}">${QUALITY_NAMES[v.qlt]}</span></td>
      <td class="price">${fmtRub(v.floor_price)}</td>
      <td class="price price-up">${fmtRub(v.quick_sale_price)}</td>
      <td class="price">${fmtRub(v.fair_value_price)}</td>
      <td class="price">${fmtRub(v.stretch_price)}</td>
      <td><span class="conf-badge ${confClass(v.confidence)}">${v.confidence}</span></td>
      <td style="color:${v.community_bias > 0.1 ? '#8fbc3f' : v.community_bias < -0.1 ? '#c63838' : '#7a7f72'}">
        ${v.community_bias > 0.1 ? '▲ bullish' : v.community_bias < -0.1 ? '▼ bearish' : '— neutral'}
      </td>
    </tr>`;
  }).join("");

  return `
    <div class="section">
      <div class="section-header">
        <div class="section-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
          Price Trend — ${currentItemLabel()}
        </div>
      </div>
      <div class="section-body">
        <div class="chart-legend">
          <div class="legend-item"><span class="legend-dot" style="background:#8fbc3f"></span>Fair Value</div>
          <div class="legend-item"><span class="legend-dot" style="background:#5591c7"></span>Quick Sale Floor</div>
          <div class="legend-item"><span class="legend-dot" style="background:#e8a317"></span>Stretch (bullish ceiling)</div>
        </div>
        <div class="chart-container"><canvas id="trend-chart"></canvas></div>
      </div>
    </div>
    <div class="section">
      <div class="section-header">
        <div class="section-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/></svg>
          All Tracked Valuations
        </div>
      </div>
      <div class="section-body" style="padding:0;">
        <div style="max-height:480px; overflow-y:auto; overscroll-behavior:contain;">
          <table class="val-table">
            <thead><tr>
              <th>Artifact</th><th>Tier</th><th>Floor</th><th>Quick Sale</th><th>Fair Value</th><th>Stretch</th><th>Confidence</th><th>Sentiment</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}

function attachTableHandlers() {
  document.querySelectorAll(".val-table tbody tr").forEach(tr => {
    tr.onclick = () => { state.selectedItem = tr.dataset.item; render(); renderSidebarItems(); };
  });
  const refresh = document.getElementById("refresh-btn");
  if (refresh) refresh.onclick = loadAll;
  document.querySelectorAll(".nav-item[data-view]").forEach(n => {
    n.onclick = () => { state.view = n.dataset.view; setActiveNav(state.view); render(); };
  });
}

function alertIcon(sev) {
  return { critical: "🚨", high: "⚠️", watch: "👁️", info: "ℹ️" }[sev] || "•";
}

function renderAlertList(alerts) {
  if (!alerts.length) return `<div class="empty-state"><div class="empty-state-desc">No alerts in this window.</div></div>`;
  return `<div class="alert-list">` + alerts.map(a => `
    <div class="alert-item ${a.severity}">
      <div class="alert-icon">${alertIcon(a.severity)}</div>
      <div class="alert-content">
        <div class="alert-type">${a.alert_type.replace(/_/g, " ")} · ${a.item_name}</div>
        <div class="alert-msg">${a.message}</div>
      </div>
      <div class="alert-time">${fmtTimeAgo(a.created_at)}</div>
    </div>
  `).join("") + `</div>`;
}

function renderAlerts() {
  return `
    <div class="section">
      <div class="section-header">
        <div class="section-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/></svg>
          Daily Deviation Alerts (7d)
        </div>
      </div>
      <div class="section-body">
        ${renderAlertList(state.alerts)}
      </div>
    </div>
  `;
}

function sentimentIcon(s) {
  return { bullish: "▲", bearish: "▼", neutral: "—" }[s] || "—";
}

function renderCommunityMini(signals) {
  if (!signals.length) return `<div class="empty-state"><div class="empty-state-desc">No community signals yet.</div></div>`;
  return `<div class="signal-grid">` + signals.map(s => `
    <div class="signal-card ${s.sentiment}">
      <div class="signal-header">
        <span class="signal-item">${s.item_name}</span>
        <span class="signal-sentiment ${s.sentiment}">${sentimentIcon(s.sentiment)} ${s.sentiment}</span>
      </div>
      <div class="signal-source">${s.source_name || s.source}</div>
      ${s.claimed_price ? `<div class="signal-price">Claimed: ${fmtRub(s.claimed_price)} RUB</div>` : ""}
      <div class="signal-excerpt">${s.excerpt || ""}</div>
      <div class="signal-time">${fmtTimeAgo(s.collected_at)}</div>
    </div>
  `).join("") + `</div>`;
}

function renderCommunity() {
  return `
    <div class="section">
      <div class="section-header">
        <div class="section-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>
          Community Valuation Reports (7d)
        </div>
      </div>
      <div class="section-body">
        ${renderCommunityMini(state.community)}
      </div>
    </div>
  `;
}

function drawChart() {
  const canvas = document.getElementById("trend-chart");
  if (!canvas) return;
  const v = state.valuations.find(v => v.item_id === state.selectedItem) || state.valuations[0];
  if (!v) return;
  if (!state.selectedItem) state.selectedItem = v.item_id;

  fetchJSON(`/history/${v.item_id}?region=na&qlt=${v.qlt}&bucket=${v.bonus_bucket}&days=30`).then(history => {
    if (state.chart) state.chart.destroy();
    const labels = history.map(h => new Date(h.computed_at * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" }));
    const ctx = canvas.getContext("2d");
    state.chart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          { label: "Fair Value", data: history.map(h => h.fair_value_price), borderColor: "#8fbc3f", backgroundColor: "#8fbc3f22", fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2.5 },
          { label: "Quick Sale Floor", data: history.map(h => h.quick_sale_price), borderColor: "#5591c7", tension: 0.35, pointRadius: 0, borderWidth: 1.5, borderDash: [4, 3] },
          { label: "Stretch", data: history.map(h => h.stretch_price), borderColor: "#e8a317", tension: 0.35, pointRadius: 0, borderWidth: 1.5, borderDash: [4, 3] },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#1c1e1a",
            borderColor: "#2e302a",
            borderWidth: 1,
            titleColor: "#d4d8cc",
            bodyColor: "#d4d8cc",
            padding: 10,
            callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmtRub(ctx.raw)} RUB` }
          },
        },
        scales: {
          x: { grid: { color: "#262822" }, ticks: { color: "#7a7f72", font: { size: 11 } } },
          y: {
            grid: { color: "#262822" },
            ticks: { color: "#7a7f72", font: { size: 11 }, callback: (v) => fmtRub(v) },
          },
        },
      },
    });
  });
}

document.getElementById("refresh-btn").onclick = loadAll;
document.querySelectorAll(".nav-item[data-view]").forEach(n => {
  n.onclick = () => { state.view = n.dataset.view; setActiveNav(state.view); render(); };
});

loadAll();
