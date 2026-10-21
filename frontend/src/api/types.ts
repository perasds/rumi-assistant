export type LoginMethod = 'account' | 'sms' | 'qr'
export interface AuthCapabilities {
  provider: string
  integration_ready: boolean
  methods: LoginMethod[]
  reason_code: string | null
  message: string | null
}
export interface Role { id: string; name: string; server_name: string }
export interface PersonalInfo {
  role_id: string | null
  nickname: string | null
  server_name: string | null
  balance_gold: string | null
}
export interface Session {
  status: 'anonymous' | 'authenticating' | 'needs_verification' | 'role_selection' | 'connected' | 'expired'
  authenticated: boolean
  role: Role | null
  roles: Role[]
  message: string | null
}
export interface LoginCredentials { account: string; password: string }
export interface SmsContact { mobile: string; country_code: string }
export interface SmsLoginCredentials extends SmsContact { otp: string }
export interface SmsSendResult { resend_after_seconds: number }
export interface ManualCaptchaFrame {
  status: 'active'
  challenge_id: string
  width: number
  height: number
  image: string
}
export type ManualCaptchaState = { status: 'waiting' } | ManualCaptchaFrame
export interface ManualCaptchaInput {
  type: 'click' | 'pointerdown' | 'pointermove' | 'pointerup'
  x: number
  y: number
}
export interface QrChallenge { challenge_id: string; qr_payload: string; expires_at: string }
export interface QrState {
  status: 'waiting' | 'scanned' | 'confirmed' | 'expired' | 'failed'
  session: Session | null
  message: string | null
}
export interface Pet {
  pet_id: string
  name: string | null
  display_name: string
  image_url: string | null
  name_verified: boolean
  elements?: PetElement[]
}
export interface PetCatalog { items: Pet[]; total: number; named_count: number; image_count: number }
export interface PetElement { id: number; name: string }
export interface PetStatRange { key: string; label: string; min: number; max: number }
export interface PetNormalAttack {
  element: PetElement | null
  power: number | null
  energy_gain: number | null
  interval_seconds: number | null
}
export interface PetTrait {
  trait_id: string
  kind: 'battle' | 'home'
  name: string
  description: string | null
}
export interface PetSkill {
  skill_id: string
  name: string
  description: string | null
  element: PetElement | null
  cost_text: string | null
  power: number | null
  image_url: string | null
}
export interface PetDetail extends Pet {
  available: boolean
  pokedex_id: string | null
  elements: PetElement[]
  stats: PetStatRange[]
  normal_attack: PetNormalAttack | null
  traits: PetTrait[]
  fixed_skill: PetSkill | null
  replaceable_skills: PetSkill[]
  missing_fields: string[]
}
export interface HomeProductionItem {
  item_id: string
  name: string
  count: number
  image_url: string | null
}
export interface HomeProductionRecipe {
  recipe_id: string
  min_building_level: number
  materials: HomeProductionItem[]
  products: HomeProductionItem[]
}
export interface HomeProductionBuilding {
  building_id: string
  name: string
  island_id: number
  island_name: string
  work_type: string | null
  recipes: HomeProductionRecipe[]
}
export interface HomeProductionCatalog {
  scope: 'configuration'
  buildings: HomeProductionBuilding[]
  total: number
  recipe_count: number
}
export interface HomeBuildingState {
  building_uid: string
  building_id: string
  level: number | null
  current_recipe_id: string | null
  assigned_lumi_uids: string[]
  production_count: number | null
  completed_count: number | null
  quantity_mode: 'batch' | 'continuous' | 'unverified'
  editable: boolean
  reason: string | null
  production_editable: boolean
  production_reason: string | null
  allowed_recipe_ids: string[] | null
  assignment_limit: number | null
}
export interface HomeWorker {
  lumi_uid: string
  lumi_id: string
  name: string
  image_url: string | null
  work_aptitude: number | null
  work_energy: number | null
  assigned_building_uid: string | null
  eligible_building_uids: string[] | null
  selectable: boolean
  reason: string | null
}
export interface HomeApplyResult {
  request_id: string
  status: 'applying' | 'applied' | 'partial' | 'rejected' | 'uncertain'
  message: string
  results: { building_uid: string; status: 'applied' | 'partial' | 'rejected' | 'uncertain' | 'skipped'; message: string }[]
}
export interface HomeAccountState {
  scope: 'account'
  ready: boolean
  revision: string | null
  observed_at: string | null
  message: string | null
  buildings: HomeBuildingState[]
  roster: HomeWorker[]
  last_apply: HomeApplyResult | null
}
export interface HomeSettingsChange {
  building_uid: string
  recipe_id?: string
  lumi_uids?: string[]
  production_count?: number
}
export type Category = 'all' | 'color' | 'normal' | 'item'
export interface MarketItem {
  listing_key: string
  category: Exclude<Category, 'all'>
  pet_id: string | null
  name: string | null
  display_name: string
  image_url: string | null
  direct_buy_price: number
  expires_at: string | null
  observed_at: string
}
export interface MarketSnapshot {
  refresh_id: string | null
  scope: 'all' | 'top10'
  status: 'idle' | 'loading' | 'complete' | 'partial' | 'failed'
  complete: boolean
  items: MarketItem[]
  loaded_count: number
  matched_count: number
  total_count: number | null
  started_at: string | null
  updated_at: string | null
  message: string | null
  name_coverage: { named: number; total: number }
}
export interface MarketFilters { query: string; category: Category; pet_ids: string[] }
export type RushQuantityMode = 'each_once' | 'total' | 'custom'
export interface RushPurchaseApiSettings {
  pet_ids: string[]
  current_season_only?: boolean
  max_price_gold: number
  // 兼容字段：旧设置空选择为总数、指定为1；新设置由 quantity_mode 决定。
  quantity_per_pet: number
  quantity_mode?: RushQuantityMode | null
  pet_quantities?: Record<string, number> | null
  min_balance_gold: number
}
export interface RushPurchaseState {
  job_id: string | null
  status: 'idle' | 'running' | 'stopping' | 'stopped' | 'completed' | 'failed' | 'uncertain'
  settings: RushPurchaseApiSettings | null
  balance_gold: string | null
  counts: Record<string, number>
  spent_gold: string
  message: string | null
  attempts: number
  updated_at: string | null
  season_ready?: boolean
  current_season_id?: number | null
  target_season_id?: number | null
  season_message?: string | null
}
