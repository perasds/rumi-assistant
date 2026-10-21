import { onScopeDispose, shallowReadonly, shallowRef } from 'vue'
import { ApiError, request, safeMessage } from '../api/client'
import type { MarketFilters, MarketSnapshot, PetCatalog } from '../api/types'

export function useMarketListings(options: { onUnauthorized: () => void }) {
  const snapshot = shallowRef<MarketSnapshot | null>(null)
  const error = shallowRef<string | null>(null)
  const busy = shallowRef(false)
  const catalog = shallowRef<PetCatalog | null>(null)
  const catalogError = shallowRef<string | null>(null)
  let filters: MarketFilters = { query: '', category: 'color', pet_ids: [] }
  let generation = 0
  let catalogGeneration = 0
  let controller: AbortController | null = null
  let catalogController: AbortController | null = null
  function reset() {
    generation++
    catalogGeneration++
    controller?.abort()
    catalogController?.abort()
    snapshot.value = null
    catalog.value = null
    error.value = null
    busy.value = false
  }
  function fail(reason: unknown) {
    if (reason instanceof ApiError && reason.status === 401) {
      reset()
      options.onUnauthorized()
    } else error.value = safeMessage(reason)
  }
  async function load() {
    const id = ++generation
    controller?.abort()
    controller = new AbortController()
    busy.value = true
    error.value = null
    const query = new URLSearchParams({ query: filters.query, category: filters.category })
    if (filters.pet_ids.length) query.set('pet_ids', filters.pet_ids.join(','))
    try {
      const value = await request<MarketSnapshot>(`/api/market/listings?${query}`, { signal: controller.signal })
      if (id !== generation) return
      snapshot.value = value
    } catch (reason) {
      if (id === generation) fail(reason)
    } finally {
      if (id === generation) busy.value = false
    }
  }
  async function refresh(value?: MarketFilters) {
    if (value) {
      filters = { ...value, category: 'color', query: value.query.trim(), pet_ids: [...value.pet_ids] }
    }
    void loadCatalog(filters.query)
    const id = ++generation
    controller?.abort()
    controller = new AbortController()
    snapshot.value = null
    error.value = null
    busy.value = true
    try {
      const value = await request<MarketSnapshot>('/api/market/refresh', {
        method: 'POST', body: JSON.stringify({ ...filters, limit: 10 }), signal: controller.signal,
      }, { timeoutMs: 35_000 })
      if (id !== generation) return
      snapshot.value = value
    } catch (reason) {
      if (id === generation) fail(reason)
    } finally {
      if (id === generation) busy.value = false
    }
  }
  async function loadCatalog(query = filters.query) {
    const id = ++catalogGeneration
    catalogController?.abort()
    catalogController = new AbortController()
    catalogError.value = null
    try {
      const value = await request<PetCatalog>(`/api/pets?${new URLSearchParams({ query })}`, { signal: catalogController.signal })
      if (id === catalogGeneration) catalog.value = value
    } catch (reason) {
      if (id === catalogGeneration) catalogError.value = safeMessage(reason)
    }
  }
  async function setFilters(value: MarketFilters) {
    catalog.value = null
    await refresh(value)
  }
  onScopeDispose(reset)
  return {
    snapshot: shallowReadonly(snapshot), error: shallowReadonly(error), busy: shallowReadonly(busy),
    catalog: shallowReadonly(catalog), catalogError: shallowReadonly(catalogError),
    load, refresh, setFilters, loadCatalog, reset,
  }
}
