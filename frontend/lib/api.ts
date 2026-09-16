import type { Artifact, Listing, Opportunity, PriceHistoryPoint } from './types';
const API='/api';
async function get<T>(path:string):Promise<T>{const r=await fetch(`${API}${path}`,{cache:'no-store'});if(!r.ok)throw new Error(`${path}: HTTP ${r.status}`);return r.json()}
export const api={artifacts:()=>get<Artifact[]>('/artifacts'),market:()=>get<Listing[]>('/market?region=na'),opportunities:()=>get<Opportunity[]>('/opportunities?region=na&min_profit=0&min_roi=0&limit=500'),history:(itemId:string,hours=24)=>get<{rows:PriceHistoryPoint[]}>(`/price-history?item_id=${encodeURIComponent(itemId)}&region=na&hours=${hours}`)};
