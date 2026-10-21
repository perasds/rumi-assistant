import { computed, onScopeDispose, shallowReadonly, shallowRef } from 'vue'
import { ApiError, request, safeMessage } from '../api/client'
import type { RushPurchaseApiSettings, RushPurchaseState } from '../api/types'
import { effectiveRushQuantityMode, type RushPurchaseSettings } from '../utils/rushPurchaseSettings'

const statuses = new Set<RushPurchaseState['status']>(['idle', 'running', 'stopping', 'stopped', 'completed', 'failed', 'uncertain'])
const integer = (value: unknown, minimum = 0) => typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
const textOrNull = (value: unknown) => value === null || typeof value === 'string'
const gold = (value: unknown) => typeof value === 'string' && /^\d+$/.test(value)

function validQuantitySettings(settings: RushPurchaseApiSettings): boolean {
  const mode = settings.quantity_mode
  if (settings.current_season_only !== undefined && typeof settings.current_season_only !== 'boolean') return false
  if (mode != null && !['each_once', 'total', 'custom'].includes(mode)) return false
  if (mode === 'each_once' && !settings.pet_ids.length) return false
  const quantities = settings.pet_quantities
  if (settings.current_season_only && (settings.pet_ids.length || mode !== 'total')) return false
  if (mode !== 'custom') return quantities == null || (typeof quantities === 'object' && !Array.isArray(quantities) && !Object.keys(quantities).length)
  if (!settings.pet_ids.length || !quantities || typeof quantities !== 'object' || Array.isArray(quantities)) return false
  const ids = new Set(settings.pet_ids)
  const entries = Object.entries(quantities)
  if (entries.length !== ids.size || entries.some(([id]) => !ids.has(id))) return false
  let total = 0
  for (const [, count] of entries) {
    if (!integer(count, 1) || !Number.isSafeInteger(total + count)) return false
    total += count
  }
  return true
}

function validateState(value: RushPurchaseState): RushPurchaseState {
  const settings = value?.settings
  if (!value || !statuses.has(value.status) || !textOrNull(value.job_id) || !textOrNull(value.message)
    || !textOrNull(value.updated_at) || !(value.balance_gold === null || gold(value.balance_gold))
    || !gold(value.spent_gold) || !integer(value.attempts)
    || !(value.season_ready === undefined || typeof value.season_ready === 'boolean')
    || !(value.current_season_id == null || integer(value.current_season_id, 1))
    || !(value.target_season_id == null || integer(value.target_season_id, 1))
    || !(value.season_message === undefined || textOrNull(value.season_message))
    || !value.counts || typeof value.counts !== 'object' || Array.isArray(value.counts)
    || !Object.values(value.counts).every(count => integer(count))
    || !(settings === null || (settings && Array.isArray(settings.pet_ids)
      && settings.pet_ids.every(id => typeof id === 'string' && id.length > 0)
      && integer(settings.max_price_gold, 1) && integer(settings.quantity_per_pet, 1) && integer(settings.min_balance_gold)
      && validQuantitySettings(settings)))) {
    throw new ApiError(502, 'INVALID_RESPONSE', '无法读取抢购状态，请重新查询')
  }
  return settings ? {
    ...value, settings: { ...settings, pet_ids: [...settings.pet_ids],
      quantity_per_pet: effectiveRushQuantityMode({ petIds: settings.pet_ids, quantityMode: settings.quantity_mode }) === 'each_once' ? 1 : settings.quantity_per_pet,
      ...(settings.pet_quantities ? { pet_quantities: { ...settings.pet_quantities } } : {}),
    },
  } : value
}

export function useRushPurchase(options: { onUnauthorized: () => void }) {
  const state = shallowRef<RushPurchaseState | null>(null)
  const error = shallowRef<string | null>(null)
  const busy = shallowRef(false)
  const checking = shallowRef(false)
  const ready = shallowRef(false)
  const uncertainObserved = shallowRef(false)
  const reconcile = shallowRef(false)
  const unresolvedStart = shallowRef<{ previousJobId: string | null } | null>(null)
  const active = computed(() => state.value?.status === 'running' || state.value?.status === 'stopping')
  const settingsLocked = computed(() => active.value || busy.value || reconcile.value || !!unresolvedStart.value || uncertainObserved.value)
  const canStart = computed(() => ready.value && !checking.value && !settingsLocked.value)
  const currentSeasonReady = computed(() => state.value?.season_ready === true && integer(state.value.current_season_id, 1))
  const canStop = computed(() => ready.value && !busy.value && !reconcile.value && state.value?.status === 'running')
  let generation = 0
  let controller: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let disposed = false
  let unauthorized = false

  function clearTimer() {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }
  function cancelRequest() {
    generation++
    controller?.abort()
    controller = null
    clearTimer()
  }
  function schedule() {
    clearTimer()
    if (!disposed && !unauthorized && (active.value || reconcile.value || unresolvedStart.value)) {
      timer = setTimeout(() => { timer = null; void loadState() }, 1000)
    }
  }
  function accept(value: RushPurchaseState) {
    const checked = validateState(value)
    if (checked.status === 'uncertain') uncertainObserved.value = true
    if (unresolvedStart.value && checked.job_id && checked.job_id !== unresolvedStart.value.previousJobId) unresolvedStart.value = null
    state.value = checked
    ready.value = true
    reconcile.value = false
    error.value = unresolvedStart.value ? '启动结果待核实，请稍后查看状态' : null
  }
  function fail(reason: unknown) {
    if (reason instanceof ApiError && reason.status === 401) {
      unauthorized = true
      cancelRequest()
      ready.value = false
      state.value = null
      busy.value = false
      checking.value = false
      error.value = null
      options.onUnauthorized()
      return
    }
    error.value = `${safeMessage(reason)}${unresolvedStart.value ? '；启动结果待核实' : ''}`
  }
  async function loadState() {
    if (disposed || unauthorized || busy.value || checking.value) return
    cancelRequest()
    const id = generation
    const current = new AbortController()
    controller = current
    checking.value = true
    try {
      const value = await request<RushPurchaseState>('/api/rush-purchase', { signal: current.signal })
      if (id === generation) accept(value)
    } catch (reason) {
      if (id === generation) fail(reason)
    } finally {
      if (id === generation) {
        controller = null
        checking.value = false
        schedule()
      }
    }
  }
  async function mutate(action: 'start' | 'stop', body: unknown) {
    const previousJobId = state.value?.job_id ?? null
    cancelRequest()
    const id = generation
    const current = new AbortController()
    controller = current
    checking.value = false
    busy.value = true
    error.value = null
    try {
      const value = await request<RushPurchaseState>(`/api/rush-purchase/${action}`, {
        method: 'POST', body: JSON.stringify(body), signal: current.signal,
      })
      if (id === generation) accept(value)
    } catch (reason) {
      if (id === generation) {
        // 请求失败不代表服务端没有启动。只读取状态核实，不自动重发开始请求。
        if (action === 'start' && (!(reason instanceof ApiError) || reason.status === 0 || reason.status >= 500)) {
          unresolvedStart.value = { previousJobId }
        }
        reconcile.value = true
        fail(reason)
      }
    } finally {
      if (id === generation) {
        controller = null
        busy.value = false
        schedule()
      }
    }
  }
  async function start(settings: RushPurchaseSettings) {
    if (!canStart.value || disposed || unauthorized) return
    if (settings.currentSeasonOnly && !currentSeasonReady.value) {
      error.value = state.value?.season_message || '当前赛季尚未同步，暂不能开始抢购'
      return
    }
    await mutate('start', {
      request_id: crypto.randomUUID(), pet_ids: settings.currentSeasonOnly ? [] : [...settings.petIds], max_price_gold: settings.maxPriceGold,
      quantity_per_pet: effectiveRushQuantityMode(settings) === 'each_once' ? 1 : settings.quantityPerPet, min_balance_gold: settings.minBalanceGold,
      ...(settings.currentSeasonOnly ? { current_season_only: true, quantity_mode: 'total' }
        : { ...(settings.quantityMode != null ? { quantity_mode: settings.quantityMode } : {}),
          ...(settings.petQuantities ? { pet_quantities: { ...settings.petQuantities } } : {}) }),
    })
  }
  async function stop() {
    if (!canStop.value || !state.value?.job_id || disposed || unauthorized) return
    await mutate('stop', { job_id: state.value.job_id })
  }
  onScopeDispose(() => {
    disposed = true
    cancelRequest()
    ready.value = false
    state.value = null
    busy.value = false
    checking.value = false
  })
  return {
    state: shallowReadonly(state), error: shallowReadonly(error), busy: shallowReadonly(busy), checking: shallowReadonly(checking),
    canStart, canStop, currentSeasonReady, settingsLocked, active, loadState, start, stop,
  }
}
