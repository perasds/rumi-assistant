<script setup lang="ts">
import type { OrderProduction } from '../../api/orderTypes'
import { orderReason } from '../../utils/orderPresentation'
defineProps<{ production: OrderProduction[]; serverTime: number | null }>()
function timing(item: OrderProduction) {
  if (item.time_source === 'unknown') return orderReason(item.blocked_reason) || '生产条件未同步，完成时间未知'
  if (item.time_source === 'server' && item.ready_at_server_seconds !== null) return `服务器完成时刻 ${item.ready_at_server_seconds}`
  if (item.time_source === 'calculated' && item.duration_seconds !== null) return `按当前速度推算 ${item.duration_seconds} 秒；工况变化会重算`
  return orderReason(item.blocked_reason) || '时间依据不完整，完成时间未知'
}
</script>

<template>
  <section aria-label="生产计划">
    <h2 class="section-title">建筑生产计划</h2>
    <p v-if="!production.length" class="hint">当前方案没有生产安排。</p>
    <article v-for="(item, index) in production" :key="`${item.building_uid}:${item.recipe_id}:${index}`" class="production-card">
      <h3 class="production-title">{{ item.name || `物品 ${item.item_id}` }}{{ item.time_source === 'unknown' && item.count === '0' ? ' · 尚待安排' : ` × ${item.count}` }}</h3>
      <p class="detail">建筑 {{ item.building_uid ?? '未找到' }} · 配方 {{ item.recipe_id ?? '未核实' }}</p>
      <p class="timing">{{ timing(item) }}</p>
      <p v-if="item.time_source !== 'unknown' && item.blocked_reason" class="blocked">{{ orderReason(item.blocked_reason) }}</p>
    </article>
    <p class="hint">预计产物需经游戏确认并领取后才计入库存；不会把未知耗时当成零。</p>
  </section>
</template>

<style scoped>
.section-title { margin: 0 0 14px; font-size: 18px; }
.production-card { margin-top: 10px; border: 1px solid var(--border); border-radius: 12px; padding: 14px; background: var(--surface); overflow-wrap: anywhere; }
.production-title { font-size: 14px; margin: 0 0 8px; }
.detail, .timing, .blocked { font-size: 13px; line-height: 1.7; margin: 5px 0 0; }
.detail, .hint { color: var(--text-muted); }
.blocked { color: var(--danger); }
.hint { font-size: 12px; line-height: 1.7; }
</style>
