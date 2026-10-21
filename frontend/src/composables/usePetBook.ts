import { onScopeDispose, shallowReadonly, shallowRef, watch } from 'vue'
import { ApiError, request, safeMessage } from '../api/client'
import type { Pet, PetCatalog, PetDetail } from '../api/types'

export function usePetBook(options: { active: () => boolean; onUnauthorized: () => void }) {
  const catalog = shallowRef<PetCatalog | null>(null)
  const catalogLoading = shallowRef(false)
  const catalogError = shallowRef<string | null>(null)
  const detailVisible = shallowRef(false)
  const selectedPet = shallowRef<Pet | null>(null)
  const detail = shallowRef<PetDetail | null>(null)
  const detailLoading = shallowRef(false)
  const detailError = shallowRef<string | null>(null)
  const details = new Map<string, PetDetail>()
  let catalogGeneration = 0
  let detailGeneration = 0
  let catalogController: AbortController | null = null
  let detailController: AbortController | null = null
  let disposed = false

  function closeDetail() {
    detailVisible.value = false
    detailGeneration++
    detailController?.abort()
    detailController = null
  }
  function clearDetail() {
    selectedPet.value = null
    detail.value = null
    detailLoading.value = false
    detailError.value = null
  }
  function finishCloseDetail() {
    if (!detailVisible.value) clearDetail()
  }
  function fail(reason: unknown, target: typeof detailError) {
    if (reason instanceof ApiError && reason.status === 401) options.onUnauthorized()
    else target.value = safeMessage(reason)
  }
  async function loadCatalog() {
    if (disposed || !options.active() || catalogLoading.value || catalog.value) return
    const generation = ++catalogGeneration
    const controller = new AbortController()
    catalogController = controller
    catalogLoading.value = true
    catalogError.value = null
    try {
      const value = await request<PetCatalog>('/api/pets', { signal: controller.signal })
      if (generation === catalogGeneration) catalog.value = value
    } catch (reason) {
      if (generation === catalogGeneration) fail(reason, catalogError)
    } finally {
      if (generation === catalogGeneration) {
        catalogController = null
        catalogLoading.value = false
      }
    }
  }
  async function loadDetail() {
    const pet = selectedPet.value
    if (disposed || !options.active() || !detailVisible.value || !pet) return
    const generation = ++detailGeneration
    detailController?.abort()
    const controller = new AbortController()
    detailController = controller
    detail.value = null
    detailLoading.value = true
    detailError.value = null
    try {
      const value = await request<PetDetail>(`/api/pets/${encodeURIComponent(pet.pet_id)}`, { signal: controller.signal })
      if (generation !== detailGeneration) return
      if (value.pet_id !== pet.pet_id) throw new ApiError(502, 'INVALID_RESPONSE', '无法读取噜咪详情，请重试')
      details.set(pet.pet_id, value)
      detail.value = value
    } catch (reason) {
      if (generation === detailGeneration) fail(reason, detailError)
    } finally {
      if (generation === detailGeneration) {
        detailController = null
        detailLoading.value = false
      }
    }
  }
  function selectPet(pet: Pet) {
    if (disposed || !options.active()) return
    closeDetail()
    clearDetail()
    selectedPet.value = pet
    detailVisible.value = true
    const cached = details.get(pet.pet_id)
    if (cached) detail.value = cached
    else void loadDetail()
  }

  watch(options.active, active => {
    if (active) void loadCatalog()
    else closeDetail()
  }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => {
    disposed = true
    catalogGeneration++
    catalogController?.abort()
    closeDetail()
    clearDetail()
    details.clear()
    catalog.value = null
  })
  return {
    catalog: shallowReadonly(catalog), catalogLoading: shallowReadonly(catalogLoading), catalogError: shallowReadonly(catalogError),
    detailVisible: shallowReadonly(detailVisible), selectedPet: shallowReadonly(selectedPet), detail: shallowReadonly(detail),
    detailLoading: shallowReadonly(detailLoading), detailError: shallowReadonly(detailError),
    loadCatalog, selectPet, loadDetail, closeDetail, finishCloseDetail,
  }
}
