(()=>{
// Ported from will-bot2026/stalcraft_v1 packages/stalcraft-core/src/index.ts
// and packages/stalcraft-market/src/pricing-helpers.ts.
// Corrects quality brackets, artifact interpolation, container effects and EHP.
if(Array.isArray(RARITIES)){
  const common=RARITIES.find(r=>Number(r.qlt)===0);if(common){common.min=85;common.max=100;common.def=100}
  if(!RARITIES.some(r=>Number(r.qlt)===6))RARITIES.push({qlt:6,name:'Unique',key:'rarity.unique',min:175,max:190,def:190,color:'#ef78c8'});
}
// Exact artifact +level math is handled by build-calculator.js using official EXBO variant JSON.
// Do not override artifactRawStat here; this file only keeps container/EHP corrections.
finalStatValue=function(st,raw,c){
  let v=Number(raw||0);
  if(st.origin==='artefact'){
    if(st.isPositive&&!ACCUM.has(st.key))v*=Number(c.effectiveness||100)/100;
    if(ACCUM.has(st.key)&&v>0)v*=Number(c.effectiveness||100)/100;
    if(PROTECTABLE.has(st.key))v*=1-Number(c.protection||0)/100;
  }
  return v;
};
derivedStats=function(stats){
  const get=k=>Number(stats.get(PREFIX+k)?.value||0);
  const health=Number(state.baseHealth||100)+get('health_bonus');
  const ehp=(res)=>health/Math.max(.01,1-Number(res||0)/100);
  return{
    health,
    bullet:ehp(get('bullet_dmg_factor')),
    explosion:ehp(get('explosion_dmg_factor')),
    laceration:ehp(get('tear_dmg_factor')),
    fire:ehp(get('burn_dmg_factor')),
    electric:ehp(get('electra_dmg_factor'))
  };
};
})();
