import { computed, onMounted, onScopeDispose, shallowReadonly, shallowRef } from 'vue'
import { ApiError, request } from '../api/client'
import type { OrderPlan, OrderSettings, OrderState } from '../api/orderTypes'

function settingsKey(settings: OrderSettings | null) {
  return settings === null ? '' : JSON.stringify([settings.horizon_hours, settings.max_orders,
    Object.entries(settings.reserves).sort(([left], [right]) => left.localeCompare(right)), settings.allow_production, settings.allow_refresh])
}
const rejectionMessages: Record<string, string> = {
  ORDER_ROLE_BUSY: '该角色已有订单任务，或有操作结果待核实，请先检查当前任务',
  ORDER_TASK_ACTIVE: '请先取消任务；结果待核实的任务须先在游戏中核对',
  ORDER_REFRESH_UNVERIFIED: '自动换单暂未开放，请关闭后再开始',
  ORDER_REQUEST_LIMIT: '本次登录任务请求已达上限，请重新登录',
  ORDER_STATE_CHANGED: '账号状态已变化，请刷新后重新预览',
  ORDER_STATE_UNAVAILABLE: '订单、库存或家园状态尚未就绪，请刷新后重试',
  ORDER_REQUEST_CONFLICT: '任务请求的状态版本冲突，请刷新后重试',
}

export function useOrderAssistant(onUnauthorized: () => void) {
  const state = shallowRef<OrderState | null>(null)
  const plan = shallowRef<OrderPlan | null>(null)
  const draft = shallowRef<OrderSettings | null>(null)
  const loading = shallowRef(false)
  const refreshing = shallowRef(false)
  const pending = shallowRef(false)
  const error = shallowRef<string | null>(null)
  const notice = shallowRef<string | null>(null)
  const uncertain = shallowRef(false)
  const controllers = new Set<AbortController>()
  let disposed = false
  let expired = false
  let initialReadPending = true
  let poll: ReturnType<typeof setTimeout> | null = null
  let readController: AbortController | null = null
  const dirty = computed(() => !!state.value && settingsKey(draft.value) !== settingsKey(state.value.settings))
  const canPlan = computed(() => !!state.value?.ready && !!state.value.revision && !!draft.value && !pending.value && !loading.value)
  const startReason = computed(() => {
    if (!state.value?.ready || !state.value.revision) return state.value?.message || '账号数据尚未同步完整'
    if (!state.value.inventory_ready || !state.value.production_ready) return '订单、库存与家园数据尚未同步完整'
    if (!state.value.execution_ready) return state.value.execution_reason || '当前连接未开放订单执行'
    if (uncertain.value || state.value.task.status === 'uncertain') return '操作结果待核实，请先刷新状态'
    if (dirty.value || !draft.value) return '请先保存当前有效设置'
    if (state.value.task.status === 'confirming') return '正在确认游戏操作，请稍候'
    return null
  })
  const canStart = computed(() => !startReason.value && !state.value?.task.running && !pending.value && !loading.value)
  const inspectionStartReason = computed(() => {
    if (!state.value?.revision) return '账号数据尚未同步完整'
    if (!state.value.work_inspection?.ready) return state.value.work_inspection?.reason || '工作巡检数据尚未同步'
    if (!state.value.inventory_ready || !state.value.production_ready) return '库存与家园数据尚未同步完整'
    if (uncertain.value || state.value.task.status === 'uncertain') return '操作结果待核实，请先刷新状态'
    if (dirty.value || !draft.value) return '请先保存当前有效设置'
    if (state.value.task.status === 'confirming') return '正在确认游戏操作，请稍候'
    if (state.value.task.running && state.value.task.kind !== 'inspection') return '订单任务正在运行，请先停止订单任务'
    return null
  })
  const canStartInspection = computed(() => !inspectionStartReason.value && !state.value?.task.running && !pending.value && !loading.value)
  const canResolve = computed(() => !!state.value && (state.value.task.kind === 'inspection' ? !!state.value.work_inspection?.ready : state.value.ready)
    && !!state.value.revision && state.value.inventory_ready && state.value.production_ready
    && state.value.task.status === 'uncertain' && !state.value.task.running && !pending.value && !loading.value)

  function stopPolling() {
    if (poll !== null) clearTimeout(poll)
    poll = null
  }
  function schedulePoll() {
    stopPolling()
    if (!disposed && !expired && state.value?.task.running === true && !document.hidden && !pending.value && !loading.value) {
      poll = setTimeout(() => { void load() }, 1000)
    }
  }
  function clearAccount() {
    state.value = null
    draft.value = null
    plan.value = null
    notice.value = null
    uncertain.value = false
    refreshing.value = false
  }
  function acceptState(next: OrderState, acknowledgeUncertain = true) {
    if (state.value?.revision !== next.revision) plan.value = null
    const settingsChanged = !state.value || settingsKey(state.value.settings) !== settingsKey(next.settings)
    state.value = next
    if (settingsChanged) draft.value = structuredClone(next.settings)
    if (acknowledgeUncertain || next.task.running || next.task.status === 'uncertain') uncertain.value = false
  }
  function failed(reason: unknown, mutating = false) {
    if (reason instanceof ApiError && reason.status === 401) {
      expired = true
      stopPolling()
      controllers.forEach(controller => controller.abort())
      clearAccount()
      onUnauthorized()
      return
    }
    if (mutating && !(reason instanceof ApiError && reason.status >= 400 && reason.status < 500)) uncertain.value = true
    plan.value = null
    if (reason instanceof ApiError && rejectionMessages[reason.code]) error.value = rejectionMessages[reason.code]
    else if (reason instanceof ApiError && reason.status === 409) error.value = '账号状态已变化，请刷新后重新预览'
    else if (reason instanceof ApiError && reason.status === 422) error.value = '设置未通过服务校验，请检查输入范围后重试'
    else if (reason instanceof ApiError && reason.code === 'NETWORK_ERROR') error.value = '无法连接服务，请检查网络后重试'
    else if (reason instanceof ApiError && reason.code === 'TIMEOUT') error.value = '请求超时，请刷新状态后重试'
    else error.value = '请求失败，请刷新状态后重试'
  }
  async function load(manual = false) {
    if (disposed || expired || pending.value || document.hidden) return
    if (loading.value) {
      if (manual) refreshing.value = true
      return
    }
    stopPolling()
    const controller = new AbortController()
    readController = controller
    controllers.add(controller)
    loading.value = true
    refreshing.value = manual || state.value === null
    try {
      const next = await request<OrderState>('/api/home/orders/state', { signal: controller.signal })
      if (disposed || expired || controller.signal.aborted) return
      acceptState(next, manual || refreshing.value)
      initialReadPending = false
      error.value = null
    } catch (reason) {
      if (!disposed && !expired && !controller.signal.aborted) {
        initialReadPending = false
        failed(reason)
      }
    } finally {
      controllers.delete(controller)
      if (readController === controller) {
        readController = null
        if (!disposed) {
          loading.value = false
          refreshing.value = false
          schedulePoll()
        }
      }
    }
  }
  async function action(path: string, method: string, body: unknown, kind: 'plan' | 'save' | 'task') {
    if (disposed || expired || pending.value) return
    if (loading.value) {
      if (path !== 'task/stop') return
      readController?.abort()
      readController = null
      loading.value = false
      refreshing.value = false
    }
    stopPolling()
    const controller = new AbortController()
    controllers.add(controller)
    pending.value = true
    error.value = null
    notice.value = null
    try {
      const next = await request<OrderState | OrderPlan>(`/api/home/orders/${path}`, { method, body: JSON.stringify(body), signal: controller.signal })
      if (disposed || expired || controller.signal.aborted) return
      if (kind === 'plan') {
        if ((next as OrderPlan).revision === state.value?.revision) plan.value = next as OrderPlan
        else error.value = '账号状态已变化，请刷新后重新预览'
      } else {
        acceptState(next as OrderState)
        if (kind === 'save') {
          draft.value = structuredClone((next as OrderState).settings)
          plan.value = null
          notice.value = '设置已保存，任务尚未启动'
        }
        if (path === 'task/resolve') notice.value = '已解除暂停，任务尚未启动'
      }
    } catch (reason) {
      if (!disposed && !expired && !controller.signal.aborted) failed(reason, kind === 'task')
    } finally {
      controllers.delete(controller)
      if (!disposed) {
        pending.value = false
        schedulePoll()
      }
    }
  }
  function updateDraft(settings: OrderSettings | null) {
    draft.value = settings
    plan.value = null
    notice.value = null
  }
  function save() {
    if (draft.value && !state.value?.task.running && state.value?.task.status !== 'uncertain') return action('settings', 'PUT', draft.value, 'save')
  }
  function preview() {
    if (canPlan.value) return action('plan', 'POST', { expected_revision: state.value!.revision, settings: draft.value }, 'plan')
  }
  function start() {
    if (canStart.value) return action('task/start', 'POST', { request_id: crypto.randomUUID(), expected_revision: state.value!.revision }, 'task')
  }
  function startInspection() {
    if (canStartInspection.value) return action('inspection/start', 'POST', { request_id: crypto.randomUUID(), expected_revision: state.value!.revision }, 'task')
  }
  function stop() {
    if (state.value?.task.running) return action('task/stop', 'POST', {}, 'task')
  }
  function resolve() {
    if (canResolve.value) return action('task/resolve', 'POST', { expected_revision: state.value!.revision, acknowledged: true }, 'task')
  }
  function visibilityChanged() {
    if (document.hidden) {
      stopPolling()
      readController?.abort()
      readController = null
      loading.value = false
      refreshing.value = false
    } else if (initialReadPending || state.value?.task.running === true) void load()
  }
  onMounted(() => {
    document.addEventListener('visibilitychange', visibilityChanged)
    void load()
  })
  onScopeDispose(() => {
    disposed = true
    stopPolling()
    controllers.forEach(controller => controller.abort())
    controllers.clear()
    document.removeEventListener('visibilitychange', visibilityChanged)
    clearAccount()
  })
  return { state: shallowReadonly(state), plan: shallowReadonly(plan), loading: shallowReadonly(loading), refreshing: shallowReadonly(refreshing), pending: shallowReadonly(pending),
    error: shallowReadonly(error), notice: shallowReadonly(notice), dirty, canPlan, canStart, canStartInspection, canResolve, startReason, inspectionStartReason,
    load, updateDraft, save, preview, start, startInspection, stop, resolve }
}
