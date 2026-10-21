<script setup lang="ts">
import type { OrderBuildingState } from '../../api/orderTypes'
import { orderReason } from '../../utils/orderPresentation'
defineProps<{ buildings: OrderBuildingState[]; ready: boolean }>()
</script>

<template>
  <section aria-label="账号建筑">
    <h2 class="section-title">当前建筑与生产</h2>
    <p v-if="!ready" class="hint">家园生产数据尚未同步完整。</p>
    <p v-else-if="!buildings.length" class="hint">当前没有已同步建筑。</p>
    <article v-for="building in buildings" :key="building.building_uid" class="building-card">
      <h3 class="building-title">建筑 {{ building.building_uid }} · 等级 {{ building.level ?? '未同步' }}</h3>
      <p class="detail">配方 {{ building.current_recipe_id ?? '未设置' }} · 工人 {{ building.assigned_lumi_uids.join('、') || '未派工' }}</p>
      <p class="detail">{{ building.quantity_mode === 'continuous' ? '持续生产' : building.quantity_mode === 'batch' ? `已完成 ${building.completed_count ?? '未知'} / ${building.production_count ?? '未知'} 批` : '生产方式待核实' }}</p>
      <p v-if="building.production_reason || building.reason" class="hint">{{ orderReason(building.production_reason || building.reason) }}</p>
    </article>
  </section>
</template>

<style scoped>
.section-title { margin: 0 0 14px; font-size: 18px; }
.building-card { margin-top: 10px; border: 1px solid var(--border); border-radius: 12px; padding: 14px; background: var(--surface); overflow-wrap: anywhere; }
.building-title { font-size: 14px; line-height: 1.6; margin: 0 0 8px; }
.detail { margin: 5px 0 0; font-size: 13px; line-height: 1.7; }
.hint { font-size: 12px; line-height: 1.7; color: var(--text-muted); }
</style>
