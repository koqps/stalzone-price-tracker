(()=>{
const $=id=>document.getElementById(id);
const q=s=>document.querySelector(s);
const qa=s=>[...document.querySelectorAll(s)];
let lastFilled=-1;
function filledSlots(){return qa('#slot-list .slot-card').filter(x=>!x.classList.contains('slot-empty')).length}
function totalSlots(){return qa('#slot-list .slot-card').length}
function hasSelectedArtifact(){return !!q('#artifact-editor .artifact-hero')}
function pickerOpen(){return $('picker-backdrop')?.getAttribute('aria-hidden')==='false'||$('picker-backdrop')?.classList.contains('show')}
function setStep(n,state){const el=q(`[data-flow-step="${n}"]`);if(el)el.dataset.state=state}
function updateGuide(){
 const filled=filledSlots(), total=totalSlots(), selected=hasSelectedArtifact();
 setStep(1,'done');
 setStep(2,filled? 'done':'active');
 setStep(3,filled?(selected?'done':'active'):'idle');
 setStep(4,filled?'active':'idle');
 const tip=$('customer-tip-text'), action=$('customer-next-action');
 if(!tip||!action)return;
 if(!total){tip.textContent='Loading containers and artifacts…';action.hidden=true;return}
 if(!filled){tip.textContent='Start by clicking an empty artifact slot or Find artifact. You can search by artifact name or the stat you want.';action.textContent='Add first artifact';action.dataset.action='add';action.hidden=false}
 else if(!selected){tip.textContent=`${filled} of ${total} slots filled. Select any artifact on the left to tune its exact rarity, quality, and enhancement.`;action.textContent='Tune selected artifact';action.dataset.action='select';action.hidden=false}
 else if(filled<total){tip.textContent=`Nice — ${filled} of ${total} slots are filled. Tune this artifact in the middle, or keep building the loadout.`;action.textContent='Add another artifact';action.dataset.action='add';action.hidden=false}
 else{tip.textContent='Build is full. Review final stats, safety, and exact-variant market cost on the right.';action.textContent='Review result';action.dataset.action='review';action.hidden=false}
 if(filled!==lastFilled){lastFilled=filled;const badge=$('customer-slot-progress');if(badge)badge.textContent=`${filled}/${total||0} artifacts`}
}
function runAction(){const action=$('customer-next-action')?.dataset.action;if(action==='add')$('add-first')?.click();else if(action==='select'){q('#slot-list .slot-card:not(.slot-empty)')?.click();q('.editor-pane')?.scrollIntoView({behavior:'smooth',block:'start'})}else if(action==='review')q('.result-pane')?.scrollIntoView({behavior:'smooth',block:'start'})}
function enhancePicker(){const search=$('artifact-search');if(search&&!search.dataset.customer){search.dataset.customer='1';search.setAttribute('aria-label','Search artifacts by name, class, or stat');search.placeholder='Search 103 artifacts by name or stat…'}const preview=$('picker-preview');if(preview)preview.setAttribute('aria-live','polite')}
function onPickerChange(){if(pickerOpen()){setTimeout(()=>{$('artifact-search')?.focus();$('artifact-search')?.select?.()},60)}}
function wire(){
 $('customer-next-action')?.addEventListener('click',runAction);
 $('quick-add-artifact')?.addEventListener('click',()=>$('add-first')?.click());
 document.addEventListener('keydown',e=>{
   const tag=(e.target?.tagName||'').toLowerCase();
   const typing=['input','textarea','select'].includes(tag)||e.target?.isContentEditable;
   if(e.key==='/'&&!typing&&!pickerOpen()){e.preventDefault();$('add-first')?.click();return}
   if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();if(pickerOpen())$('artifact-search')?.focus();else $('add-first')?.click()}
   if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();$('save')?.click()}
 });
 const obs=new MutationObserver(()=>{updateGuide();enhancePicker();onPickerChange()});
 ['slot-list','artifact-editor','picker-backdrop','kpi-danger','container-info'].forEach(id=>{const el=$(id);if(el)obs.observe(el,{childList:true,subtree:true,attributes:true,attributeFilter:['class','aria-hidden']})});
 updateGuide();enhancePicker();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',wire);else wire();
})();
