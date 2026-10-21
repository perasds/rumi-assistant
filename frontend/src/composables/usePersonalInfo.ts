import { onMounted, onScopeDispose, shallowReadonly, shallowRef } from 'vue'
import { ApiError, request, safeMessage } from '../api/client'
import type { PersonalInfo } from '../api/types'

export function usePersonalInfo(onUnauthorized: () => void) {
  const info = shallowRef<PersonalInfo | null>(null)
  const loading = shallowRef(false)
  const error = shallowRef<string | null>(null)
  let disposed = false
  let controller: AbortController | null = null

  async function load() {
    if (disposed || loading.value) return
    controller = new AbortController()
    loading.value = true
    error.value = null
    try {
      const current = await request<PersonalInfo>('/api/me', { signal: controller.signal })
      if (!disposed) info.value = current
    } catch (reason) {
      if (disposed) return
      info.value = null
      if (reason instanceof ApiError && reason.status === 401) onUnauthorized()
      else error.value = safeMessage(reason)
    } finally {
      if (!disposed) {
        loading.value = false
        controller = null
      }
    }
  }
  onMounted(load)
  onScopeDispose(() => {
    disposed = true
    controller?.abort()
    info.value = null
  })
  return { info: shallowReadonly(info), loading: shallowReadonly(loading), error: shallowReadonly(error), load }
}
