<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, shallowRef, useId, watch } from 'vue'
import { Button as VanButton, Field as VanField, Icon as VanIcon, Image as VanImage, Loading as VanLoading, Popup as VanPopup } from 'vant'
import { request, safeMessage } from '../../api/client'
import type { PetCatalog } from '../../api/types'
import { effectiveRushQuantityMode, rushPurchaseDraft, validateRushPurchaseDraft, type RushPurchaseErrors, type RushPurchaseSettings } from '../../utils/rushPurchaseSettings'

const props = defineProps<{ settings: RushPurchaseSettings | null; seasonReady?: boolean; currentSeasonId?: number | null; seasonMessage?: string | null }>()
const show = defineModel<boolean>('show', { required: true })
const emit = defineEmits<{ save: [settings: RushPurchaseSettings] }>()
const titleId = useId()
const draft = reactive(rushPurchaseDraft(null))
const errors = shallowRef<RushPurchaseErrors>({})
const catalog = shallowRef<PetCatalog | null>(null)
const catalogLoading = shallowRef(false)
const catalogError = shallowRef<string | null>(null)
const expanded = shallowRef(false)
const query = shallowRef('')
let catalogController: AbortController | null = null
let catalogGeneration = 0
const verifiedPets = computed(() => catalog.value?.items.filter(pet => pet.name_verified) ?? [])
const visiblePets = computed(() => {
  const term = query.value.trim().toLocaleLowerCase()
  return verifiedPets.value.filter(pet => !term || pet.display_name.toLocaleLowerCase().includes(term) || pet.pet_id.includes(term))
})
const selectedNames = computed(() => verifiedPets.value.filter(pet => draft.petIds.includes(pet.pet_id)).map(pet => pet.display_name).join('、'))
const selectedPets = computed(() => verifiedPets.value.filter(pet => draft.petIds.includes(pet.pet_id)))
const quantityMode = computed(() => effectiveRushQuantityMode(draft))
const seasonDescription = computed(() => props.seasonReady && Number.isSafeInteger(props.currentSeasonId) && (props.currentSeasonId ?? 0) > 0
  ? `当前赛季 S${props.currentSeasonId}，仅按购买总数量抢购`
  : props.seasonMessage || '当前赛季尚未同步，保存后需等待同步才能开始')
const selectionUnavailable = computed(() => draft.petIds.length > 0 && (!catalog.value || catalogLoading.value || !!catalogError.value))
watch(() => draft.petIds, ids => {
  draft.petQuantities = Object.fromEntries(ids.map(id => [id, draft.petQuantities?.[id] ?? '1']))
  if (!ids.length && draft.quantityMode !== 'total') draft.quantityMode = undefined
}, { flush: 'sync' })
watch(() => draft.currentSeasonOnly, enabled => {
  if (!enabled) return
  draft.petIds = []
  draft.petQuantities = {}
  draft.quantityMode = 'total'
  expanded.value = false
  query.value = ''
  errors.value = { ...errors.value, petIds: undefined, petQuantities: undefined, quantityMode: undefined }
}, { flush: 'sync' })

function stopCatalogLoading() {
  catalogGeneration++
  catalogController?.abort()
  catalogController = null
  catalogLoading.value = false
}
async function loadCatalog() {
  if (!show.value) return
  const generation = ++catalogGeneration
  catalogController?.abort()
  const controller = new AbortController()
  catalogController = controller
  catalogLoading.value = true
  catalogError.value = null
  try {
    const value = await request<PetCatalog>('/api/pets', { signal: controller.signal })
    if (generation === catalogGeneration && show.value) catalog.value = value
  } catch (reason) {
    if (generation === catalogGeneration && show.value) catalogError.value = safeMessage(reason)
  } finally {
    if (generation === catalogGeneration) catalogLoading.value = false
  }
}
function togglePet(id: string) {
  if (draft.currentSeasonOnly) return
  draft.petIds = draft.petIds.includes(id) ? draft.petIds.filter(value => value !== id) : [...draft.petIds, id]
}
function togglePicker() {
  if (!draft.currentSeasonOnly) expanded.value = !expanded.value
}
function clearPetSelection() {
  draft.petIds = []
  errors.value = { ...errors.value, petIds: undefined }
}
function save() {
  if (selectionUnavailable.value) return
  const result = validateRushPurchaseDraft(draft, new Set(verifiedPets.value.map(pet => pet.pet_id)))
  errors.value = result.errors
  if (result.settings) {
    emit('save', result.settings)
    show.value = false
  }
}
watch(show, open => {
  if (!open) {
    stopCatalogLoading()
    return
  }
  Object.assign(draft, { quantityMode: undefined, petQuantities: {} }, rushPurchaseDraft(props.settings))
  errors.value = {}
  expanded.value = false
  query.value = ''
  if (!catalog.value) void loadCatalog()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(stopCatalogLoading)
</script>
<template>
  <VanPopup v-model:show="show" class="rush-settings-popup" position="center" round closeable role="dialog" :aria-labelledby="titleId">
    <form class="rush-settings" @submit.prevent="save">
      <h2 :id="titleId" class="title">抢购设置</h2>
      <div class="season-setting">
        <label class="season-option"><input v-model="draft.currentSeasonOnly" type="checkbox" name="currentSeasonOnly" /><span>仅当前赛季</span></label>
        <p v-if="draft.currentSeasonOnly" class="season-description">{{ seasonDescription }}</p>
      </div>
      <section class="pet-setting" aria-label="指定噜咪">
        <button class="pet-trigger" type="button" aria-label="选择指定噜咪" :aria-expanded="expanded" :disabled="draft.currentSeasonOnly" @click="togglePicker">
          <span>指定噜咪<small>{{ draft.currentSeasonOnly ? '已启用当前赛季，不能自定义噜咪' : draft.petIds.length ? `已选 ${draft.petIds.length} 种` : '可选，不选则不限噜咪' }}</small></span>
          <VanIcon :name="expanded ? 'arrow-up' : 'arrow-down'" aria-hidden="true" />
        </button>
        <p v-if="selectedNames" class="selected-names">{{ selectedNames }}</p>
        <VanButton v-if="draft.petIds.length" class="clear-pets" native-type="button" plain type="primary" size="mini" aria-label="清空指定噜咪" @click="clearPetSelection">清空选择</VanButton>
        <p v-if="errors.petIds" class="field-error" role="alert">{{ errors.petIds }}</p>
        <VanLoading v-if="catalogLoading" class="catalog-loading" size="18">正在加载噜咪目录</VanLoading>
        <div v-else-if="catalogError" class="catalog-error">
          <p class="field-error" role="alert">{{ catalogError }}</p>
          <VanButton native-type="button" size="small" plain type="primary" aria-label="重试加载噜咪目录" @click="loadCatalog">重试</VanButton>
        </div>
        <div v-if="expanded && catalog && !draft.currentSeasonOnly" class="pet-picker">
          <input v-model="query" class="pet-query" type="search" aria-label="搜索噜咪名称或ID" placeholder="搜索名称或 ID" @keydown.enter.prevent />
          <div class="pet-options">
            <label v-for="pet in visiblePets" :key="pet.pet_id" class="pet-option" :class="{ selected: draft.petIds.includes(pet.pet_id) }">
              <input type="checkbox" :data-pet-id="pet.pet_id" :checked="draft.petIds.includes(pet.pet_id)" @change="togglePet(pet.pet_id)" />
              <VanImage class="pet-image" width="38" height="38" :src="pet.image_url ?? undefined" fit="contain" :alt="pet.display_name"><template #error>?</template></VanImage>
              <span>{{ pet.display_name }}<small>ID {{ pet.pet_id }}</small></span>
            </label>
            <p v-if="!visiblePets.length" class="empty-pets">没有匹配的噜咪</p>
          </div>
        </div>
      </section>
      <fieldset v-if="draft.petIds.length && !draft.currentSeasonOnly" class="quantity-modes">
        <legend class="quantity-legend">购买方式</legend>
        <label class="quantity-mode"><input v-model="draft.quantityMode" type="radio" name="quantityMode" value="each_once" :checked="quantityMode === 'each_once'" /><span>每种一只</span></label>
        <label class="quantity-mode"><input v-model="draft.quantityMode" type="radio" name="quantityMode" value="total" :checked="quantityMode === 'total'" /><span>合计数量</span></label>
        <label class="quantity-mode"><input v-model="draft.quantityMode" type="radio" name="quantityMode" value="custom" :checked="quantityMode === 'custom'" /><span>独立数量</span></label>
      </fieldset>
      <div class="number-settings">
        <VanField v-model="draft.priceWan" name="priceWan" label="低于此价抢购" placeholder="请输入价格" inputmode="decimal"><template #button><span class="unit">万</span></template></VanField>
        <p v-if="errors.priceWan" class="field-error" role="alert">{{ errors.priceWan }}</p>
        <template v-if="quantityMode === 'total'">
          <VanField v-model="draft.quantity" name="quantity" label="购买总数量" placeholder="请输入数量" inputmode="numeric" />
          <p v-if="errors.quantity" class="field-error" role="alert">{{ errors.quantity }}</p>
          <p v-if="draft.petIds.length" class="quantity-note">所选噜咪合计购买此数量，形态不限配比</p>
        </template>
        <template v-else-if="quantityMode === 'custom'">
          <VanField v-for="pet in selectedPets" :key="pet.pet_id" :model-value="draft.petQuantities?.[pet.pet_id] ?? '1'" :name="`petQuantity-${pet.pet_id}`" :label="pet.display_name" placeholder="请输入数量" inputmode="numeric" @update:model-value="draft.petQuantities = { ...draft.petQuantities, [pet.pet_id]: $event }"><template #button><span class="unit">只</span></template></VanField>
          <p v-if="errors.petQuantities" class="field-error" role="alert">{{ errors.petQuantities }}</p>
        </template>
        <p v-else class="quantity-note">每种指定噜咪购买 1 只</p>
        <VanField v-model="draft.minBalanceGold" name="minBalanceGold" label="账号低于金币停止" label-width="9em" placeholder="请输入金币数" inputmode="numeric"><template #button><span class="unit">金币</span></template></VanField>
        <p v-if="errors.minBalanceGold" class="field-error" role="alert">{{ errors.minBalanceGold }}</p>
      </div>
      <footer class="actions">
        <VanButton native-type="button" plain type="primary" aria-label="取消抢购设置" @click="show = false">取消</VanButton>
        <VanButton native-type="submit" type="primary" aria-label="保存抢购设置" :disabled="selectionUnavailable">保存</VanButton>
      </footer>
    </form>
  </VanPopup>
</template>
<style scoped>
.rush-settings-popup { width: min(520px, calc(100vw - 32px)); max-height: min(86svh, 820px); background: var(--surface); border: 1px solid #e6dac5; }
.rush-settings { padding: 26px 22px 20px; }
.title { margin: 0 0 24px; padding-right: 26px; color: var(--ink); font-size: 21px; }
.pet-setting { border: 1px solid var(--border); border-radius: 14px; padding: 14px; background: #fff; }
.pet-trigger { display: flex; justify-content: space-between; align-items: center; width: 100%; padding: 0; border: 0; color: var(--ink); background: transparent; text-align: left; cursor: pointer; }
.pet-trigger small { display: block; margin-top: 6px; color: var(--text-muted); font-size: 12px; }
.pet-trigger:disabled { cursor: default; opacity: .6; }
.season-setting { margin-bottom: 14px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 14px; background: #fff; }
.season-option { display: flex; align-items: center; gap: 8px; color: var(--ink); font-size: 13px; cursor: pointer; }
.season-option input { width: 16px; height: 16px; margin: 0; accent-color: var(--accent-deep); }
.season-description { margin: 8px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.6; }
.selected-names { margin: 12px 0 0; color: var(--accent-deep); font-size: 12px; line-height: 1.6; }
.clear-pets { margin-top: 10px; border-radius: 7px; }
.catalog-loading, .catalog-error { margin-top: 12px; }
.pet-query { width: 100%; min-height: 38px; margin: 14px 0 10px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--field-background); color: var(--ink); font-size: 13px; }
.pet-options { display: grid; gap: 6px; max-height: 230px; overflow-y: auto; }
.pet-option { display: flex; align-items: center; gap: 10px; padding: 7px 9px; border: 1px solid transparent; border-radius: 10px; cursor: pointer; }
.pet-option.selected { border-color: var(--accent-border); background: var(--accent-light); }
.pet-option input { width: 16px; height: 16px; accent-color: var(--accent-deep); }
.pet-option span { min-width: 0; color: var(--ink); font-size: 13px; }
.pet-option small { display: block; margin-top: 3px; color: var(--text-muted); font-size: 11px; }
.empty-pets { color: var(--text-muted); font-size: 13px; text-align: center; }
.quantity-modes { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 16px 0 0; padding: 12px; border: 1px solid var(--border); border-radius: 14px; background: #fff; }
.quantity-legend { padding: 0 5px; color: var(--ink); font-size: 13px; }
.quantity-mode { display: flex; align-items: center; gap: 6px; min-width: 0; color: var(--ink); font-size: 12px; cursor: pointer; }
.quantity-mode input { flex-shrink: 0; width: 15px; height: 15px; margin: 0; accent-color: var(--accent-deep); }
.number-settings { margin-top: 16px; overflow: hidden; border: 1px solid var(--border); border-radius: 14px; background: #fff; }
.number-settings :deep(.van-field) { padding: 13px 14px; }
.number-settings :deep(.van-field__label) { overflow-wrap: anywhere; }
.unit, .quantity-note { color: var(--text-muted); font-size: 12px; }
.quantity-note { margin: 0; padding: 13px 14px; line-height: 1.6; }
.field-error { margin: 8px 0 0; color: var(--danger); font-size: 12px; line-height: 1.6; }
.number-settings > .field-error { padding: 0 14px 10px; margin-top: 0; }
.actions { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 18px; }
.actions :deep(.van-button) { border-radius: 11px; }
@media (max-width: 420px) {
  .rush-settings { padding: 24px 16px 18px; }
  .number-settings :deep(.van-field) { display: block; }
  .number-settings :deep(.van-field__label) { width: auto !important; margin: 0 0 8px; }
  .number-settings :deep(.van-field__value) { width: 100%; }
}
</style>
