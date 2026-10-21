import { onScopeDispose, shallowReadonly, shallowRef, watch } from 'vue'
import { ApiError, request, safeMessage } from '../api/client'
import type { HomeProductionCatalog } from '../api/types'

export function useHomeProduction(options: { active: () => boolean; onUnauthorized: () => void }) {
  const catalog = shallowRef<HomeProductionCatalog | null>(null)
  const loading = shallowRef(false)
  const error = shallowRef<string | null>(null)
  let initialized = false
  let disposed = false
  let generation = 0
  let controller: AbortController | null = null

  async function load() {
    if (disposed || !options.active() || loading.value || catalog.value) return
    const current = ++generation
    controller = new AbortController()
    loading.value = true
    error.value = null
    try {
      const value = await request<HomeProductionCatalog>('/api/home/production', { signal: controller.signal })
      if (!disposed && current === generation) catalog.value = value
    } catch (reason) {
      if (disposed || current !== generation) return
      if (reason instanceof ApiError && reason.status === 401) options.onUnauthorized()
      else error.value = safeMessage(reason)
    } finally {
      if (!disposed && current === generation) {
        controller = null
        loading.value = false
      }
    }
  }

  watch(options.active, active => {
    if (active && !initialized) {
      initialized = true
      void load()
    }
  }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => {
    disposed = true
    generation++
    controller?.abort()
    controller = null
    catalog.value = null
    loading.value = false
    error.value = null
  })
  return { catalog: shallowReadonly(catalog), loading: shallowReadonly(loading), error: shallowReadonly(error), load }
}
