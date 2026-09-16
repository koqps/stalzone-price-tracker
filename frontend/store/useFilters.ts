import { create } from 'zustand';
type StateMode='unexplored'|'all'|'explored';type Method='median'|'average'|'lowest';
interface Filters{artifactId:string;rarity:string;patternMin:number;patternMax:number;state:StateMode;minProfit:string;minProfitPct:string;method:Method;sound:boolean;commission:boolean;set:<K extends keyof Omit<Filters,'set'|'reset'>>(key:K,value:Filters[K])=>void;reset:()=>void}
const defaults={artifactId:'',rarity:'',patternMin:0,patternMax:15,state:'all' as StateMode,minProfit:'',minProfitPct:'',method:'median' as Method,sound:false,commission:true};
export const useFilters=create<Filters>((set)=>({...defaults,set:(key,value)=>set({[key]:value} as Partial<Filters>),reset:()=>set(defaults)}));
