export interface OrderSettings {
  horizon_hours: number
  max_orders: number
  reserves: Record<string, string>
  allow_production: boolean
  allow_refresh: boolean
}
export interface OrderMaterial {
  item_id: string
  name: string
  count: string
  available: string | null
  shortage: string | null
}
export interface CurrentOrder {
  slot_id: string
  is_unlock: boolean
  order_type: number | null
  materials: OrderMaterial[]
  rewards: { item_id: string; name: string; count: string }[]
  refresh_time: number | null
  refresh_seconds: number | null
  can_complete: boolean | null
}
export interface OrderTask {
  kind?: 'orders' | 'inspection'
  status: 'idle' | 'running' | 'waiting_materials' | 'waiting_refresh' | 'confirming' | 'stopped' | 'completed' | 'uncertain' | 'failed'
  running: boolean
  completed_orders: number
  canned_received: string
  message: string | null
  inspected_actions?: number
}
export interface Workplace {
  building_uid: string
  building_id: string
  name: string
  level: number | null
  status: 'idle' | 'working' | 'missing_worker' | 'blocked' | 'unknown'
  message: string
  worker_uid: string | null
  worker_name?: string | null
  worker_image_url?: string | null
  current_recipe_id: string | null
  current_recipe_products?: { item_id: string; name: string; count: string }[] | null
  candidate: { recipe_id: string; item_id: string; name: string; stock: string; output_count: string; recipe_times: string } | null
}
export interface WorkInspection {
  ready: boolean
  reason: string | null
  buildings: Workplace[]
}
export interface OrderBuildingState {
  building_uid: string
  building_id: string
  level: number | null
  current_recipe_id: string | null
  assigned_lumi_uids: string[]
  production_count: string | null
  completed_count: string | null
  quantity_mode: 'batch' | 'continuous' | 'unverified'
  reason: string | null
  production_reason: string | null
}
export interface OrderState {
  ready: boolean
  revision: string | null
  observed_at: string | null
  server_time_seconds: number | null
  message: string | null
  orders: CurrentOrder[]
  inventory_ready: boolean
  production_ready: boolean
  buildings: OrderBuildingState[]
  settings: OrderSettings
  task: OrderTask
  execution_ready: boolean
  execution_reason: string | null
  work_inspection?: WorkInspection
}
export interface OrderProduction {
  building_uid: string | null
  building_id: string | null
  recipe_id: string | null
  item_id: string
  name: string
  count: string
  time_source: 'calculated' | 'server' | 'unknown'
  duration_seconds: number | null
  ready_at_server_seconds: number | null
  blocked_reason: string | null
}
export interface OrderPlan {
  revision: string
  inventory_selection: { slot_ids: string[]; count: string; canned_count: string; optimal: boolean; reason: string | null }
  materials: { item_id: string; name: string | null; required: string; available: string | null; shortage: string | null }[]
  base_requirements?: Record<string, string> | null
  production: OrderProduction[]
  predicted_count: string
  predicted_canned_count: string
  warnings: string[]
}
