import { onMounted, onScopeDispose, shallowReadonly, shallowRef } from 'vue'
import { request } from '../api/client'

interface ServerTime {
  unix_ms: number
  time_zone: 'Asia/Shanghai'
}

export function useServerClock() {
  const displayTime = shallowRef('--:--:--')
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', hourCycle: 'h23', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
  let baseline: { unixMs: number; monotonicMs: number } | null = null
  let controller: AbortController | null = null
  let tickTimer: ReturnType<typeof setInterval> | null = null
  let syncTimer: ReturnType<typeof setInterval> | null = null
  let disposed = false

  function update() {
    if (disposed || !baseline) return
    const unixMs = baseline.unixMs + performance.now() - baseline.monotonicMs
    displayTime.value = formatter.format(new Date(unixMs))
  }
  async function sync() {
    if (disposed || controller) return
    const current = new AbortController()
    controller = current
    const started = performance.now()
    try {
      const value = await request<ServerTime>('/api/time', { signal: current.signal })
      const received = performance.now()
      if (disposed || controller !== current || current.signal.aborted) return
      if (!value || !Number.isSafeInteger(value.unix_ms) || value.unix_ms < 0
        || value.unix_ms > 8_640_000_000_000_000 || value.time_zone !== 'Asia/Shanghai') return
      // Anchor to the monotonic clock; device wall-clock adjustments cannot change elapsed time.
      baseline = { unixMs: value.unix_ms + (received - started) / 2, monotonicMs: received }
      update()
    } catch {
      // Keep an existing server baseline; never substitute the device's time after a failure.
    } finally {
      if (controller === current) controller = null
    }
  }
  function onVisibilityChange() {
    if (document.visibilityState !== 'visible') return
    update()
    void sync()
  }

  onMounted(() => {
    tickTimer = setInterval(update, 1000)
    syncTimer = setInterval(() => { void sync() }, 60_000)
    document.addEventListener('visibilitychange', onVisibilityChange)
    void sync()
  })
  onScopeDispose(() => {
    disposed = true
    if (tickTimer !== null) clearInterval(tickTimer)
    if (syncTimer !== null) clearInterval(syncTimer)
    document.removeEventListener('visibilitychange', onVisibilityChange)
    controller?.abort()
    controller = null
  })

  return { displayTime: shallowReadonly(displayTime) }
}
