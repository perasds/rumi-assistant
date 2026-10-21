import { computed, onScopeDispose, shallowReadonly, shallowRef, watch } from 'vue'
import { ApiError, request, safeMessage } from '../api/client'
import type { HomeAccountState, HomeBuildingState, HomeProductionCatalog, HomeSettingsChange, HomeWorker } from '../api/types'

export interface HomeBuildingDraft { recipeId?: string; workerUids?: string[]; countInput?: string }
export function canSelectHomeWorker(worker: HomeWorker, building: HomeBuildingState) {
  return building.assigned_lumi_uids.includes(worker.lumi_uid)
    || (worker.selectable && worker.assigned_building_uid === null
      && worker.eligible_building_uids?.includes(building.building_uid) === true)
}
const positiveCount = (value: string | undefined) => value !== undefined && /^[1-9]\d*$/.test(value)
  && Number.isSafeInteger(Number(value)) && Number(value) <= 2_147_483_647 ? Number(value) : null
function rejectedBeforeWrite(reason: unknown): reason is ApiError {
  return reason instanceof ApiError && (
    (reason.status === 422 && ['HOME_INVALID_SETTINGS', 'INVALID_INPUT'].includes(reason.code))
    || (reason.status === 409 && ['HOME_STATE_CONFLICT', 'HOME_REQUEST_CONFLICT', 'REQUEST_CONFLICT'].includes(reason.code))
    || (reason.status === 403 && reason.code === 'ORIGIN_REJECTED'))
}

export function useHomeManagement(options: {
  active: () => boolean; catalog: () => HomeProductionCatalog | null; onUnauthorized: () => void
}) {
  const state = shallowRef<HomeAccountState | null>(null)
  const drafts = shallowRef<Record<string, HomeBuildingDraft>>({})
  const loading = shallowRef(false)
  const applying = shallowRef(false)
  const error = shallowRef<string | null>(null)
  const notice = shallowRef<string | null>(null)
  const draftRevision = shallowRef<string | null>(null)
  const pendingRequestId = shallowRef<string | null>(null)
  const reviewRequired = shallowRef(false)
  let submittedUids: string[] = []
  let getGeneration = 0
  let applyGeneration = 0
  let getController: AbortController | null = null
  let applyController: AbortController | null = null
  let disposed = false
  let followPriorApply = false
  let priorApply = false
  const dirty = computed(() => Object.keys(drafts.value).length > 0)
  const conflict = computed(() => dirty.value && draftRevision.value !== state.value?.revision)
  const locked = computed(() => applying.value || pendingRequestId.value !== null || loading.value || conflict.value || reviewRequired.value)

  function buildingFor(uid: string) { return state.value?.buildings.find(building => building.building_uid === uid) }
  function editable(uid: string) {
    const building = buildingFor(uid)
    return building && state.value?.ready && state.value.revision !== null && !locked.value && options.active()
      && building.editable ? building : null
  }
  function setDraft(uid: string, value: HomeBuildingDraft) {
    const next = { ...drafts.value }
    if (Object.values(value).some(item => item !== undefined)) next[uid] = value
    else delete next[uid]
    drafts.value = next
    if (!Object.keys(next).length) draftRevision.value = null
    else if (draftRevision.value === null) draftRevision.value = state.value?.revision ?? null
  }
  function changeRecipe(uid: string, recipeId: string) {
    const building = editable(uid)
    if (!building || !building.production_editable || building.quantity_mode === 'unverified' || !building.allowed_recipe_ids?.includes(recipeId)) return
    const definition = options.catalog()?.buildings.find(row => row.building_id === building.building_id)
    if (!definition?.recipes.some(recipe => recipe.recipe_id === recipeId)) return
    const value = { ...drafts.value[uid] }
    if (recipeId === building.current_recipe_id) { delete value.recipeId; delete value.countInput }
    else {
      value.recipeId = recipeId
      if (building.quantity_mode === 'batch') value.countInput = ''
      else delete value.countInput
    }
    setDraft(uid, value)
  }
  function changeCount(uid: string, text: string) {
    const building = editable(uid)
    if (!building || !building.production_editable || building.quantity_mode !== 'batch') return
    const value: HomeBuildingDraft = { ...drafts.value[uid], countInput: text }
    if (!value.recipeId && text === String(building.production_count ?? '')) delete value.countInput
    setDraft(uid, value)
  }
  function changeWorkers(uid: string, uids: string[]) {
    const building = editable(uid)
    if (!building || !building.assignment_limit || !uids.length || uids.length > building.assignment_limit
      || new Set(uids).size !== uids.length) return
    if (!uids.every(id => building.assigned_lumi_uids.includes(id)
      || state.value?.roster.some(worker => worker.lumi_uid === id && canSelectHomeWorker(worker, building)))) return
    const value = { ...drafts.value[uid] }
    if (uids.length === building.assigned_lumi_uids.length && uids.every((id, index) => id === building.assigned_lumi_uids[index])) delete value.workerUids
    else value.workerUids = [...uids]
    setDraft(uid, value)
  }
  const validation = computed(() => {
    for (const [uid, draft] of Object.entries(drafts.value)) {
      const building = buildingFor(uid)
      if (!building?.editable) return '建筑状态不可编辑，请刷新核实'
      if ((draft.recipeId !== undefined || draft.countInput !== undefined) && !building.production_editable) {
        return building.production_reason ?? '当前不能修改生产物品或次数，请刷新核实'
      }
      if ((draft.recipeId && building.quantity_mode === 'batch') || draft.countInput !== undefined) {
        if (positiveCount(draft.countInput) === null) return '请填写 1–2147483647 范围内的整数生产次数'
      }
    }
    return null
  })
  const canApply = computed(() => options.active() && state.value?.ready === true && state.value.revision !== null
    && dirty.value && !locked.value && !validation.value)
  function discardDrafts() {
    if (applying.value || pendingRequestId.value) return
    drafts.value = {}; draftRevision.value = null; reviewRequired.value = false; notice.value = null
  }
  function accept(value: HomeAccountState) {
    if (value?.scope !== 'account' || !Array.isArray(value.buildings) || !Array.isArray(value.roster)) {
      throw new ApiError(502, 'INVALID_RESPONSE', '无法读取家园状态，请重试')
    }
    state.value = value
    const result = value.last_apply
    if (followPriorApply && result) {
      pendingRequestId.value = result.request_id
      submittedUids = []
      followPriorApply = false
      priorApply = true
    }
    if (!pendingRequestId.value && result && (result.status === 'applying' || result.status === 'uncertain'
      || result.results.some(item => item.status === 'uncertain'))) {
      pendingRequestId.value = result.request_id
      submittedUids = []
      priorApply = true
    }
    if (!pendingRequestId.value || result?.request_id !== pendingRequestId.value) return
    notice.value = priorApply ? `前次操作：${result.message}${dirty.value ? '；当前草稿尚未应用' : ''}` : result.message
    const next = { ...drafts.value }
    if (result.status === 'applied') submittedUids.forEach(uid => { delete next[uid] })
    else result.results.filter(item => item.status === 'applied' && submittedUids.includes(item.building_uid))
      .forEach(item => { delete next[item.building_uid] })
    drafts.value = next
    if (!Object.keys(next).length) draftRevision.value = null
    if (result.status === 'partial' || result.results.some(item => item.status === 'partial')) reviewRequired.value = true
    if (result.status !== 'applying' && result.status !== 'uncertain'
      && !result.results.some(item => item.status === 'uncertain')) pendingRequestId.value = null
  }
  async function refresh() {
    if (disposed || !options.active() || loading.value || applying.value) return
    const generation = ++getGeneration
    const controller = new AbortController()
    getController = controller
    loading.value = true; error.value = null
    try {
      const value = await request<HomeAccountState>('/api/home/state', { signal: controller.signal })
      if (!disposed && generation === getGeneration) accept(value)
    } catch (reason) {
      if (disposed || generation !== getGeneration) return
      if (reason instanceof ApiError && reason.status === 401) options.onUnauthorized()
      else error.value = safeMessage(reason)
    } finally {
      if (!disposed && generation === getGeneration) { loading.value = false; getController = null }
    }
  }
  async function apply() {
    if (!canApply.value || draftRevision.value === null) return
    const generation = ++applyGeneration
    const requestId = crypto.randomUUID()
    const changes: HomeSettingsChange[] = Object.entries(drafts.value).map(([uid, draft]) => ({
      building_uid: uid,
      ...(draft.recipeId !== undefined ? { recipe_id: draft.recipeId } : {}),
      ...(draft.workerUids !== undefined ? { lumi_uids: [...draft.workerUids] } : {}),
      ...(draft.countInput !== undefined ? { production_count: positiveCount(draft.countInput)! } : {}),
    }))
    getGeneration++; getController?.abort(); getController = null; loading.value = false
    const controller = new AbortController()
    applyController = controller
    pendingRequestId.value = requestId; submittedUids = changes.map(change => change.building_uid)
    followPriorApply = false; priorApply = false
    applying.value = true; error.value = null; notice.value = null
    let verify = false
    let refusalMessage: string | null = null
    try {
      const value = await request<HomeAccountState>('/api/home/settings', { method: 'PUT', signal: controller.signal,
        body: JSON.stringify({ request_id: requestId, expected_revision: draftRevision.value, changes }) }, { timeoutMs: 60_000 })
      if (!disposed && generation === applyGeneration) { accept(value); verify = pendingRequestId.value !== null }
    } catch (reason) {
      if (disposed || generation !== applyGeneration) return
      if (reason instanceof ApiError && reason.status === 401) options.onUnauthorized()
      else if (rejectedBeforeWrite(reason)) {
        pendingRequestId.value = null; submittedUids = []
        refusalMessage = safeMessage(reason); error.value = refusalMessage
        if (reason.status === 409) { reviewRequired.value = true; verify = true }
      }
      else if (reason instanceof ApiError && reason.status === 409 && reason.code === 'HOME_OUTCOME_UNKNOWN') {
        followPriorApply = true; priorApply = true; submittedUids = []
        notice.value = '上次应用结果尚未确认，请刷新核实'; verify = true
      }
      else { notice.value = '应用结果尚未确认，请刷新核实'; verify = true }
    } finally {
      if (!disposed && generation === applyGeneration) { applying.value = false; applyController = null }
    }
    if (verify && !disposed && generation === applyGeneration) await refresh()
    if (refusalMessage && !disposed && generation === applyGeneration) error.value = refusalMessage
  }
  watch(options.active, active => { if (active) void refresh() }, { immediate: true, flush: 'sync' })
  onScopeDispose(() => {
    disposed = true; getGeneration++; applyGeneration++
    getController?.abort(); applyController?.abort()
    getController = null; applyController = null
    state.value = null; drafts.value = {}; draftRevision.value = null; pendingRequestId.value = null
    loading.value = false; applying.value = false; error.value = null; notice.value = null; submittedUids = []
    reviewRequired.value = false; followPriorApply = false; priorApply = false
  })
  return { state: shallowReadonly(state), drafts: shallowReadonly(drafts), loading: shallowReadonly(loading),
    applying: shallowReadonly(applying), error: shallowReadonly(error), notice: shallowReadonly(notice),
    pendingRequestId: shallowReadonly(pendingRequestId), reviewRequired: shallowReadonly(reviewRequired),
    dirty, conflict, locked, validation, canApply, refresh, apply, discardDrafts, changeRecipe, changeCount, changeWorkers }
}
