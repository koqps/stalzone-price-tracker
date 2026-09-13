(()=>{
document.title='Stalzone Market';
const nativeFetch=window.fetch.bind(window);
const rules=[['/api/artifacts',6*60*60*1000],['/api/quality-tiers',24*60*60*1000],['/api/patch-signals',5*60*1000],['/api/opportunities',60*1000],['/api/summary',30*1000]];
const key=u=>'szFast:'+u;
const degradedArtifacts=data=>Array.isArray(data)&&data.some(x=>x&&x.metadata_degraded);
const fallbackFor=p=>p==='/api/quality-tiers'?[{qlt:0,name:'Common',color:'#8b8f86'},{qlt:1,name:'Uncommon',color:'#79b84b'},{qlt:2,name:'Special',color:'#4f98d1'},{qlt:3,name:'Rare',color:'#9a63d8'},{qlt:4,name:'Exclusive',color:'#e05252'},{qlt:5,name:'Legendary',color:'#e6a33c'}]:p==='/api/summary'?{}:[];
const response=data=>new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json','X-Stalzone-Fallback':'1'}});
window.fetch=async function(input,init={}){
 const raw=typeof input==='string'?input:(input&&input.url)||'',u=new URL(raw,location.origin),match=rules.find(([p])=>u.pathname===p);
 if(!match)return nativeFetch(input,init);
 const ttl=match[1],k=key(u.pathname+u.search);let cached=null;
 try{cached=JSON.parse(sessionStorage.getItem(k)||localStorage.getItem(k)||'null')}catch{}
 if(u.pathname==='/api/artifacts'&&degradedArtifacts(cached?.data)){try{sessionStorage.removeItem(k);localStorage.removeItem(k)}catch{}cached=null}
 const store=data=>{if(!data||(u.pathname==='/api/artifacts'&&degradedArtifacts(data)))return;const payload=JSON.stringify({at:Date.now(),data});try{if(ttl<=5*60*1000)sessionStorage.setItem(k,payload);else localStorage.setItem(k,payload)}catch{}};
 if(cached?.data&&Date.now()-Number(cached.at||0)<ttl){nativeFetch(input,{...init,cache:'no-store'}).then(r=>r.ok?r.clone().json():null).then(store).catch(()=>{});return response(cached.data)}
 try{
   const r=await nativeFetch(input,{...init,cache:'default'});
   if(r.ok&&r.headers.get('X-Stalzone-Metadata')!=='warming')r.clone().json().then(store).catch(()=>{});
   if(!r.ok&&u.pathname!=='/api/artifacts')return response(cached?.data??fallbackFor(u.pathname));
   return r;
 }catch(err){
   if(cached?.data)return response(cached.data);
   if(u.pathname!=='/api/artifacts')return response(fallbackFor(u.pathname));
   throw err;
 }
};
document.addEventListener('error',e=>{const img=e.target;if(!(img instanceof HTMLImageElement))return;const holder=img.closest('.icon');if(holder){holder.textContent='✦';holder.setAttribute('aria-label',img.alt||'Artifact')}} ,true);
})();