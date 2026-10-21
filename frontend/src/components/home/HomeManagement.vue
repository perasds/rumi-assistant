<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { Button as VanButton, Empty as VanEmpty, Loading as VanLoading } from 'vant'
import type { HomeProductionCatalog } from '../../api/types'
import { useHomeManagement } from '../../composables/useHomeManagement'
import HomeInstanceCard from './HomeInstanceCard.vue'
import HomeWorkerPicker from './HomeWorkerPicker.vue'

const props = defineProps<{ active: boolean; catalog: HomeProductionCatalog | null }>()
const emit = defineEmits<{ expired: [] }>()
const { state, drafts, loading, applying, error, notice, pendingRequestId, reviewRequired, dirty, conflict, locked, validation, canApply,
  refresh, apply, discardDrafts, changeRecipe, changeCount, changeWorkers } = useHomeManagement({
  active: () => props.active, catalog: () => props.catalog, onUnauthorized: () => emit('expired'),
})
const pickerUid = shallowRef<string | null>(null)
const pickerBuilding = computed(() => state.value?.buildings.find(building => building.building_uid === pickerUid.value) ?? null)
const pickerSelection = computed(() => pickerBuilding.value
  ? drafts.value[pickerBuilding.value.building_uid]?.workerUids ?? pickerBuilding.value.assigned_lumi_uids : [])
watch(() => props.active, active => { if (!active) pickerUid.value = null })
watch(locked, value => { if (value) pickerUid.value = null })
function saveWorkers(uids: string[]) {
  if (pickerUid.value) changeWorkers(pickerUid.value, uids)
  pickerUid.value = null
}
</script>

<template>
  <section class="home-management" aria-label="家园生产管理">
    <div class="management-toolbar">
      <span class="management-hint">选择后点击应用到游戏</span>
      <VanButton size="small" plain type="primary" aria-label="刷新家园状态" :disabled="loading || applying" @click="refresh">刷新</VanButton>
    </div>
    <VanLoading v-if="loading && !state" class="management-loading" size="22">正在读取家园状态</VanLoading>
    <p v-if="error" class="management-error" role="alert">{{ error }}</p>
    <p v-if="state?.message" class="management-message">{{ state.message }}</p>
    <p v-if="notice" class="management-message" role="status">{{ notice }}</p>
    <p v-if="pendingRequestId && !applying" class="management-warning" role="status">应用结果尚未确认，请刷新核实。不会自动重发。</p>
    <p v-if="conflict" class="management-warning" role="alert">家园状态已变化，草稿已保留，请核对后放弃草稿并重新选择。</p>
    <p v-else-if="reviewRequired" class="management-warning" role="alert">请核对实际状态后放弃草稿并重新选择。</p>
    <p v-if="validation" class="management-warning">{{ validation }}</p>
    <div v-if="state?.buildings.length" class="instance-list">
      <HomeInstanceCard v-for="building in state.buildings" :key="building.building_uid" :building="building"
        :definition="catalog?.buildings.find(row => row.building_id === building.building_id)" :roster="state.roster"
        :draft="drafts[building.building_uid]" :disabled="locked || !state.ready"
        :result-message="state.last_apply?.results.find(row => row.building_uid === building.building_uid)?.message"
        @recipe="changeRecipe(building.building_uid, $event)" @count="changeCount(building.building_uid, $event)"
        @workers="pickerUid = building.building_uid" />
    </div>
    <VanEmpty v-else-if="state?.ready" description="当前家园没有生产建筑" />
    <div class="management-actions">
      <VanButton v-if="dirty" plain size="small" :disabled="applying || !!pendingRequestId" @click="discardDrafts">放弃草稿</VanButton>
      <VanButton type="primary" aria-label="应用到游戏" :disabled="!canApply" :loading="applying" loading-text="应用中" @click="apply">应用到游戏</VanButton>
    </div>
    <HomeWorkerPicker :show="!!pickerBuilding" :building="pickerBuilding" :roster="state?.roster ?? []" :selected="pickerSelection" @close="pickerUid = null" @confirm="saveWorkers" />
  </section>
</template>

<style scoped>
.management-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; }.management-hint { font-size: 12px; color: var(--text-muted); }.management-toolbar :deep(.van-button) { border-radius: 9px; }
.management-loading { padding: 35px 12px; text-align: center; }.management-error, .management-message, .management-warning { margin: 0 0 14px; padding: 11px 13px; font-size: 13px; line-height: 1.7; border-radius: 11px; overflow-wrap: anywhere; }.management-error { background: #fff0e9; color: var(--danger); }.management-message { background: var(--accent-light); color: var(--accent-deep); }.management-warning { background: #fff1d0; color: #896123; }
.instance-list { display: grid; align-items: start; gap: 16px; }.management-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 12px; padding: 18px 0 4px; }.management-actions :deep(.van-button) { border-radius: 10px; }
@media (min-width: 760px) { .instance-list { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
