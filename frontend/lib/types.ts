export type Rarity = 0|1|2|3|4|5|6;
export interface ArtifactStat { key:string; name:string; display:string; group:string; harmful:boolean; kind:string; min:number; max:number }
export interface Artifact { item_id:string; item_name:string; artifact_class:string; icon_url:string; description?:string; stats:ArtifactStat[]; stat_groups?:string[] }
export interface Listing { item_id:string; item_name:string; qlt:Rarity; qlt_name:string; ptn:number; pattern_label:string; tier_color:string; upgrade_level:number; upgrade_label:string; live_floor:number|null; live_median:number|null; live_listings:number; sale_median:number|null; sale_average:number|null; sale_count:number; sell_quick?:number|null; sell_recommended?:number|null; sell_high_margin?:number|null; latest_observation?:number }
export interface PriceHistoryPoint { item_id:string; item_name:string; qlt:Rarity; upgrade_level:number; unit_price:number; amount:number; observed_at:number }
export interface Opportunity { item_id:string; item_name:string; qlt:Rarity; ptn:number; upgrade_level:number; buy_price:number; resale_target:number; estimated_profit:number; roi_pct:number; tax_rate?:number }
