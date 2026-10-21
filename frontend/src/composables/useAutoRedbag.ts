import { computed, onMounted, onScopeDispose, shallowReadonly, shallowRef } from 'vue'
import { ApiError, request } from '../api/client'
import type { RedbagTask } from '../api/redbagTypes'

export function useAutoRedbag(onUnauthorized: () => void) {
  const task = shallowRef<RedbagTask | null>(null)
  const busy = shallowRef(false)
  const reading = shallowRef(false)
  const error = shallowRef<string | null>(null)
  const controllers = new Set<AbortController>()
  let disposed = false
  let expired = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let readController: AbortController | null = null
  let startRequestId: string | null = null
  const canStart = computed(() => !expired && !busy.value && task.value?.available === true && !task.value.running
    && !['starting', 'stopping', 'uncertain'].includes(task.value.status))
  const canStop = computed(() => !expired && !busy.value && task.value?.running === true && task.value.status !== 'stopping')

  function stopPolling() {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }
  function schedulePoll() {
    stopPolling()
    if (!disposed && !expired && !document.hidden && task.value?.running && !busy.value && !reading.value) {
      timer = setTimeout(() => { void load() }, 3000)
    }
  }
  function cancelRead() {
    readController?.abort()
    readController = null
    reading.value = false
  }
  function failed(reason: unknown, action?: 'start' | 'stop') {
    if (reason instanceof ApiError && reason.status === 401) {
      expired = true
      stopPolling()
      controllers.forEach(controller => controller.abort())
      task.value = null
      error.value = '登录已过期，请重新登录'
      onUnauthorized()
      return
    }
    const definiteRejection = reason instanceof ApiError && reason.status >= 400 && reason.status < 500
    if (action === 'start' && !definiteRejection) {
      error.value = '启动结果尚未确认，请重试确认同一次启动，或刷新任务状态'
      return
    }
    if (action === 'start') startRequestId = null
    if (action === 'stop' && !definiteRejection) error.value = '取消结果尚未确认，请刷新任务状态或重试取消'
    else if (reason instanceof ApiError && reason.code === 'NETWORK_ERROR') error.value = '无法连接服务，请检查网络后重试'
    else if (reason instanceof ApiError && reason.code === 'TIMEOUT') error.value = '请求超时，请稍后刷新任务状态'
    else error.value = '请求失败，请稍后刷新任务状态'
  }
  async function load() {
    if (disposed || expired || document.hidden || busy.value || reading.value) return
    stopPolling()
    const controller = new AbortController()
    readController = controller
    controllers.add(controller)
    reading.value = true
    try {
      const next = await request<RedbagTask>('/api/redbags/task', { signal: controller.signal })
      if (disposed || expired || controller.signal.aborted) return
      task.value = next
      if (next.running || next.status === 'uncertain') startRequestId = null
      error.value = null
    } catch (reason) {
      if (!disposed && !expired && !controller.signal.aborted) failed(reason)
    } finally {
      controllers.delete(controller)
      if (readController === controller) {
        readController = null
        reading.value = false
        schedulePoll()
      }
    }
  }
  async function action(kind: 'start' | 'stop') {
    if (disposed || expired || busy.value || (kind === 'start' ? !canStart.value : !canStop.value)) return
    stopPolling()
    cancelRead()
    const controller = new AbortController()
    controllers.add(controller)
    busy.value = true
    error.value = null
    if (kind === 'start') startRequestId ??= crypto.randomUUID()
    try {
      const next = await request<RedbagTask>(`/api/redbags/task/${kind}`, {
        method: 'POST', signal: controller.signal,
        ...(kind === 'start' ? { body: JSON.stringify({ request_id: startRequestId }) } : {}),
      })
      if (disposed || expired || controller.signal.aborted) return
      task.value = next
      startRequestId = null
    } catch (reason) {
      if (!disposed && !expired && !controller.signal.aborted) failed(reason, kind)
    } finally {
      controllers.delete(controller)
      if (!disposed) {
        busy.value = false
        schedulePoll()
      }
    }
  }
  function visibilityChanged() {
    stopPolling()
    if (document.hidden) cancelRead()
    else void load()
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
  })
  return { task: shallowReadonly(task), busy: shallowReadonly(busy), error: shallowReadonly(error), canStart, canStop,
    load, start: () => action('start'), stop: () => action('stop') }
}
