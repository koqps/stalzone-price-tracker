const API='/api';
const $=id=>document.getElementById(id);
const q=s=>document.querySelector(s);
const qa=s=>[...document.querySelectorAll(s)];
const refState={artifacts:[],market:[],opportunities:[],stateMode:'all',minProfit:0,minRoi:0,method:'median'};

function style(){
  const s=document.createElement('style');
  s.textContent=`
  .reference-filter-row{display:contents}.reference-control{display:grid;gap:4px}.reference-control label{font-size:8px;color:#728092;text-transform:uppercase;letter-spacing:.08em;font-weight:800}.reference-control select,.reference-control input{height:34px;min-width:132px;padding:0 10px;border:1px solid #263445;border-radius:7px;background:#111923;color:#dce6f2;outline:0;font-size:10px}.reference-control select:focus,.reference-control input:focus{border-color:#4d8de0;box-shadow:0 0 0 2px #4d8de018}.reference-control.artifact select{min-width:180px}.reference-control.money input{width:135px;min-width:110px}.reference-actions{display:flex;align-items:end;gap:7px}.reference-actions button{height:34px;border:1px solid #2a394a;background:#16212d;color:#bdcad9;border-radius:7px;padding:0 11px;font-size:9px;font-weight:750;cursor:pointer;white-space:nowrap}.reference-actions button:hover{border-color:#4a76a5;color:white}.reference-actions .primary{background:#4f83d8;border-color:#5a94ee;color:#fff}.reference-profit-fields{display:contents}.reference-profit-fields.is-collapsed{display:none}.filter-bar.reference-ready{align-items:end;flex-wrap:wrap}.filter-bar.reference-ready .filter-spacer{display:none}.filter-bar.reference-ready .result-count{margin-left:auto}.ref-hidden{display:none!important}.reference-profit-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin:0 13px 10px}.reference-profit-strip>div{padding:7px 8px;border:1px solid #22303d;background:#0d141b;border-radius:7px}.reference-profit-strip small{display:block;color:#69798b;font-size:7px;text-transform:uppercase;letter-spacing:.07em}.reference-profit-strip b{display:block;margin-top:3px;font-size:10px}.reference-profit-strip .profit b{color:#59d693}.reference-profit-strip .cost b{color:#ef777d}.reference-profit-strip .target b{color:#e8f1fb}@media(max-width:950px){.reference-control.artifact,.reference-control,.reference-control select,.reference-control input{min-width:0;width:100%}.filter-bar.reference-ready{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.reference-actions{grid-column:1/-1;flex-wrap:wrap}.filter-bar.reference-ready .result-count{margin-left:0}}
  `;
  document.head.appendChild(s);
}
async function get(path){const r=await fetch(API+path,{cache:'no-store'});if(!r.ok)throw new Error(`${path}: HTTP ${r.status}`);return r.json()}
function fire(el,type='change'){el.dispatchEvent(new Event(type,{bubbles:true}))}
function money(n){return Number.isFinite(Number(n))?`${Math.round(Number(n)).toLocaleString()} ₽`:'—'}
function rowItemId(card){return card.querySelector('[data-history]')?.dataset.history||card.querySelector('[data-detail]')?.dataset.detail||''}
function marketRows(itemId){return refState.market.filter(r=>r.item_id===itemId)}
function opportunityMetrics(row){
  if(!row)return null;
  const buy=Number(row.buy_price||0),tax=Number(row.tax_rate??.05);
  let target;
  if(refState.method==='live')target=Number(row.next_live_price||row.resale_target||0);
  else target=Number(row.sale_median||row.resale_target||0);
  if(!(buy>0&&target>0))return{profit:Number(row.estimated_profit||0),roi:Number(row.roi_pct||0),buy,target:Number(row.resale_target||0)};
  const profit=target*(1-tax)-buy;
  return{buy,target,profit,roi:buy?profit/buy*100:0};
}
function bestOpportunity(itemId){
  const rows=refState.opportunities.filter(r=>r.item_id===itemId).map(row=>({row,metrics:opportunityMetrics(row)})).filter(x=>x.metrics);
  rows.sort((a,b)=>b.metrics.profit-a.metrics.profit||b.metrics.roi-a.metrics.roi);
  return rows[0]||null;
}
function passesReferenceFilters(itemId){
  const rows=marketRows(itemId),best=bestOpportunity(itemId);
  if(refState.stateMode==='live'&&!rows.some(r=>Number(r.live_listings||0)>0||Number(r.live_floor||0)>0))return false;
  if(refState.stateMode==='sales'&&!rows.some(r=>Number(r.sale_count||0)>0))return false;
  if(refState.minProfit>0&&(!best||best.metrics.profit<refState.minProfit))return false;
  if(refState.minRoi>0&&(!best||best.metrics.roi<refState.minRoi))return false;
  return true;
}
function addProfitStrip(card,itemId){
  card.querySelector('.reference-profit-strip')?.remove();
  const best=bestOpportunity(itemId);if(!best||best.metrics.profit<=0)return;
  const strip=document.createElement('div');strip.className='reference-profit-strip';
  strip.innerHTML=`<div class="cost"><small>Cost</small><b>${money(best.metrics.buy)}</b></div><div class="target"><small>Target price</small><b>${money(best.metrics.target)}</b></div><div class="profit"><small>Profit</small><b>+${money(best.metrics.profit)} (${Math.round(best.metrics.roi)}%)</b></div>`;
  const footer=card.querySelector('.card-footer');card.insertBefore(strip,footer||null);
}
function applyReferenceFilters(){
  const grid=$('artifact-grid');if(!grid)return;
  const cards=[...grid.querySelectorAll('.artifact-card')];let shown=0;
  for(const card of cards){const id=rowItemId(card),ok=!id||passesReferenceFilters(id);card.classList.toggle('ref-hidden',!ok);if(id)addProfitStrip(card,id);if(ok)shown++}
  const count=$('result-count');if(count&&cards.length)count.textContent=`${shown} artifact${shown===1?'':'s'}`;
}
function populateArtifactSelect(){
  const sel=$('reference-artifact');if(!sel)return;
  const current=sel.value;
  sel.innerHTML='<option value="">All artifacts</option>'+[...refState.artifacts].sort((a,b)=>String(a.item_name).localeCompare(String(b.item_name))).map(a=>`<option value="${String(a.item_id).replaceAll('"','&quot;')}">${String(a.item_name)}</option>`).join('');
  if([...sel.options].some(o=>o.value===current))sel.value=current;
}
function buildControls(){
  const bar=q('.filter-bar');if(!bar||$('reference-artifact'))return;
  bar.classList.add('reference-ready');
  const block=document.createElement('div');block.className='reference-filter-row';
  block.innerHTML=`
    <div class="reference-control artifact"><label>Artifact name</label><select id="reference-artifact"><option value="">All artifacts</option></select></div>
    <div class="reference-control"><label>State</label><select id="reference-state"><option value="all">All</option><option value="live">Live listings</option><option value="sales">Has sales</option></select></div>
    <div class="reference-profit-fields" id="reference-profit-fields">
      <div class="reference-control money"><label>Min profit</label><input id="reference-profit" type="number" min="0" step="1000" placeholder="Enter min profit"></div>
      <div class="reference-control money"><label>Min % profit</label><input id="reference-roi" type="number" min="0" step="1" placeholder="Enter min %"></div>
      <div class="reference-control"><label>Method</label><select id="reference-method"><option value="median">Median</option><option value="live">Live floor</option></select></div>
    </div>
    <div class="reference-actions"><button class="primary" id="reference-advanced">Advanced filters</button><button id="reference-clear">Clear filters</button><button id="reference-history">⌁ Price history</button></div>`;
  bar.prepend(block);
  $('reference-artifact').onchange=e=>{const art=refState.artifacts.find(a=>a.item_id===e.target.value);const search=$('search');if(search){search.value=art?.item_name||'';fire(search,'input')}};
  $('reference-state').onchange=e=>{refState.stateMode=e.target.value;applyReferenceFilters()};
  $('reference-profit').oninput=e=>{refState.minProfit=Math.max(0,Number(e.target.value||0));applyReferenceFilters()};
  $('reference-roi').oninput=e=>{refState.minRoi=Math.max(0,Number(e.target.value||0));applyReferenceFilters()};
  $('reference-method').onchange=e=>{refState.method=e.target.value;applyReferenceFilters()};
  $('reference-advanced').onclick=()=>{$('reference-profit-fields').classList.toggle('is-collapsed')};
  $('reference-clear').onclick=()=>{
    $('reference-artifact').value='';$('reference-state').value='all';$('reference-profit').value='';$('reference-roi').value='';$('reference-method').value='median';
    refState.stateMode='all';refState.minProfit=0;refState.minRoi=0;refState.method='median';
    const search=$('search');if(search){search.value='';fire(search,'input')}
    const rarity=$('rarity-filter'),level=$('level-filter'),sort=$('sort-filter');if(rarity){rarity.value='';fire(rarity)}if(level){level.value='';fire(level)}if(sort){sort.value='name';fire(sort)}
    applyReferenceFilters();
  };
  $('reference-history').onclick=()=>{
    const selected=$('reference-artifact').value;
    let btn=selected?q(`[data-history="${CSS.escape(selected)}"]`):q('.artifact-card:not(.ref-hidden) [data-history]');
    if(btn){btn.click();return}
    if(selected){const art=refState.artifacts.find(a=>a.item_id===selected),search=$('search');if(search&&art){search.value=art.item_name;fire(search,'input');setTimeout(()=>q(`[data-history="${CSS.escape(selected)}"]`)?.click(),80)}}
  };
}
async function loadReferenceData(){
  try{
    const [arts,market,ops]=await Promise.all([get('/artifacts'),get('/market?region=na'),get('/opportunities?region=na&min_profit=0&min_roi=0&limit=500')]);
    refState.artifacts=arts||[];refState.market=market||[];refState.opportunities=ops||[];populateArtifactSelect();applyReferenceFilters();
  }catch(e){console.warn('reference controls data load failed',e)}
}
function observeGrid(){const grid=$('artifact-grid');if(!grid)return;new MutationObserver(()=>queueMicrotask(applyReferenceFilters)).observe(grid,{childList:true,subtree:true})}
function wireDedicatedAuth(){const auth=$('auth-button');if(!auth)return;auth.addEventListener('click',e=>{if(($('auth-button-label')?.textContent||'').trim().toLowerCase()==='login'){e.preventDefault();e.stopImmediatePropagation();location.href='/static/login.html'}},true)}
function init(){style();buildControls();observeGrid();wireDedicatedAuth();loadReferenceData();setInterval(()=>loadReferenceData(),60000)}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
