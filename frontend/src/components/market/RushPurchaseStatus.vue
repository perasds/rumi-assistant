<script setup lang="ts">
import { computed } from 'vue'
import { Button as VanButton } from 'vant'
import type { PetCatalog, RushPurchaseState } from '../../api/types'

const props = defineProps<{ state: RushPurchaseState | null; catalog: PetCatalog | null; error: string | null; checking: boolean }>()
const emit = defineEmits<{ recheck: [] }>()
const labels: Record<RushPurchaseState['status'], string> = {
  idle: '抢购状态', running: '监测中', stopping: '正在停止', stopped: '已停止', completed: '已完成', failed: '任务失败', uncertain: '结果待核实',
}
const allPets = computed(() => props.state?.settings?.pet_ids.length === 0)
const scopeLabel = computed(() => {
  if (!props.state?.settings?.current_season_only) return allPets.value ? '全部彩色噜咪' : `指定 ${props.state?.settings?.pet_ids.length} 种`
  const season = props.state.target_season_id ?? props.state.current_season_id
  return `仅当前赛季${season && season > 0 ? ` S${season}` : ''}`
})
const quantityMode = computed(() => props.state?.settings?.quantity_mode ?? (allPets.value ? 'total' : 'each_once'))
const totalPurchased = computed(() => Object.values(props.state?.counts ?? {}).reduce((total, count) => total + count, 0))
const targetFor = (id: string) => quantityMode.value === 'custom' ? props.state?.settings?.pet_quantities?.[id] ?? 1 : 1
const completedKinds = computed(() => props.state?.settings?.pet_ids.filter(id => (props.state?.counts[id] ?? 0) >= targetFor(id)).length ?? 0)
const progress = computed(() => {
  const state = props.state
  if (!state?.settings || allPets.value) return []
  return state.settings.pet_ids.map(id => ({
    id, name: props.catalog?.items.find(pet => pet.pet_id === id)?.display_name ?? `ID ${id}`,
    count: state.counts[id] ?? 0, target: quantityMode.value === 'total' ? null : targetFor(id),
  }))
})
const formatGold = (value: string | null) => value === null ? '—' : value.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
</script>
<template>
  <section v-if="error || (state && state.status !== 'idle')" class="rush-status" aria-label="抢购状态" role="status">
    <div class="status-heading">
      <strong>{{ state ? labels[state.status] : '抢购状态' }}</strong>
      <span v-if="state">余额 {{ formatGold(state.balance_gold) }} 金币</span>
    </div>
    <p v-if="quantityMode === 'total'" class="scope-summary">{{ scopeLabel }} · 已购买 {{ totalPurchased }} / {{ state?.settings?.quantity_per_pet }} 只</p>
    <p v-else-if="state?.settings" class="scope-summary">指定 {{ state.settings.pet_ids.length }} 种 · 已完成 {{ completedKinds }} / {{ state.settings.pet_ids.length }} 种</p>
    <ul v-if="progress.length" class="pet-progress">
      <li v-for="pet in progress" :key="pet.id"><span :title="pet.name">{{ pet.name }}</span><b>{{ pet.count }}<template v-if="pet.target !== null"> / {{ pet.target }}</template></b></li>
    </ul>
    <p v-if="state?.message" class="status-message">{{ state.message }}</p>
    <p v-if="error" class="status-error" role="alert">{{ error }}</p>
    <VanButton v-if="error || state?.status === 'uncertain'" class="recheck" plain type="primary" size="mini" aria-label="重新读取抢购状态" :loading="checking" :disabled="checking" @click="emit('recheck')">刷新状态</VanButton>
  </section>
</template>
<style scoped>
.rush-status { margin-top: 16px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 12px; background: var(--field-background); color: var(--ink); font-size: 12px; }
.status-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 12px; align-items: center; }
.status-heading strong { color: var(--accent-deep); font-size: 13px; }
.status-heading > span { color: var(--text-muted); overflow-wrap: anywhere; }
.scope-summary { margin: 8px 0 0; color: var(--text-muted); }
.pet-progress { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 22px; max-height: 76px; overflow-y: auto; margin: 10px 0 0; padding: 0; list-style: none; }
.pet-progress li { display: flex; justify-content: space-between; gap: 10px; min-width: 0; }
.pet-progress li span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pet-progress b { flex-shrink: 0; font-weight: 500; font-variant-numeric: tabular-nums; }
.status-message, .status-error { margin: 8px 0 0; line-height: 1.6; overflow-wrap: anywhere; }
.status-error { color: var(--danger); }
.recheck { margin-top: 9px; border-radius: 7px; }
@media (max-width: 420px) { .pet-progress { grid-template-columns: 1fr; } }
</style>
