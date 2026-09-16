(()=>{
// Ported from will-bot2026/stalcraft_v1 packages/stalcraft-core/src/index.ts
// and packages/stalcraft-market/src/pricing-helpers.ts.
// Corrects quality brackets, artifact interpolation, container effects and EHP.
if(Array.isArray(RARITIES)){
  const common=RARITIES.find(r=>Number(r.qlt)===0);if(common){common.min=85;common.max=100;common.def=100}
  if(!RARITIES.some(r=>Number(r.qlt)===6))RARITIES.push({qlt:6,name:'Unique',key:'rarity.unique',min:175,max:190,def:190,color:'#ef78c8'});
}
const QUALITY_RARITY_KEYS={
  0:'rarity.ordinary',
  1:'rarity.unordinary',
  2:'rarity.special',
  3:'rarity.rare',
  4:'rarity.exclusive',
  5:'rarity.legendary',
  6:'rarity.unique'
};
const RARITY_INDEX_CORRECT={
  'rarity.unordinary':0,
  'rarity.special':1,
  'rarity.rare':2,
  'rarity.exclusive':3,
  'rarity.legendary':4,
  'rarity.unique':5
};
function wikiRangeCorrect(stat,multiplier=1){
  const rawMax=Number(stat.max||0),rawMin=Number(stat.min||0);
  let max=Math.max(rawMax,rawMin),min=Math.min(rawMax,rawMin);
  if(rawMax<=0&&rawMin<=0){max=Math.min(rawMax,rawMin);min=Math.max(rawMax,rawMin)}
  return{max:max*multiplier,min:min*multiplier,baseMax:max,baseMin:min};
}
function calculateNegativeRawStatCorrect(stat,slot){
  const {max,min}=wikiRangeCorrect(stat,1),quality=Number(slot.quality||0),rarityKey=QUALITY_RARITY_KEYS[Number(slot.qlt)]||'rarity.ordinary';
  if(quality<=100){
    if(quality===100&&rarityKey==='rarity.unordinary'){
      const rarityIndex=RARITY_INDEX_CORRECT[rarityKey];
      const start=.9*max;
      return start+((max-start)/100)*((quality-(100+10*rarityIndex))*10);
    }
    return min+((max-min)/100)*quality;
  }
  const rarityFromAssumption=RARITY_INDEX_CORRECT[rarityKey];
  const inferredRarity=Math.floor((quality-100)/15);
  let rarityBand=Math.max(0,Math.min(rarityFromAssumption??inferredRarity,5));
  if([115,130,145,160,175,190].includes(quality)&&RARITY_INDEX_CORRECT[rarityKey]!=null)rarityBand=RARITY_INDEX_CORRECT[rarityKey];
  const bandProgress=Math.max(0,Math.min(quality-(100+15*rarityBand),15))/15;
  const bandStart=.85*max;
  return bandStart+(max-bandStart)*bandProgress;
}
artifactRawStat=function(stat,slot){
  if(stat.isPositive){
    const {max}=wikiRangeCorrect(stat,1);
    return max*(Number(slot.quality)/100)*(1+(2*Number(slot.level))/100);
  }
  return calculateNegativeRawStatCorrect(stat,slot);
};
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
