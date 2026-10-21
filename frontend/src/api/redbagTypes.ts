export type RedbagTaskStatus = 'idle' | 'starting' | 'running' | 'stopping' | 'stopped' | 'daily_limit'
  | 'conditions_not_met' | 'rate_limited' | 'uncertain' | 'failed'

export interface RedbagResult {
  id: number
  at: string
  status: 'claimed' | 'skipped' | 'uncertain'
  message: string
  stamina: string
}

export interface RedbagTask {
  available: boolean
  running: boolean
  status: RedbagTaskStatus
  message: string
  claimed_count: number
  skipped_count: number
  stamina_total: string
  recent: RedbagResult[]
  started_at: string | null
  updated_at: string | null
}
