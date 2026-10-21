<script setup lang="ts">
import type { CurrentOrder } from '../../api/orderTypes'
defineProps<{ orders: CurrentOrder[]; ready: boolean }>()
</script>

<template>
  <section class="order-list" aria-label="当前订单">
    <h2 class="section-title">当前订单</h2>
    <p v-if="!ready" class="hint">订单尚未同步完整，暂不判断可交付数量。</p>
    <p v-else-if="!orders.length" class="hint">当前没有已同步订单。</p>
    <article v-for="order in orders" :key="order.slot_id" class="order-card">
      <div class="card-heading"><h3 class="order-title">槽位 {{ order.slot_id }}</h3><span class="order-state">{{ !order.is_unlock ? '未解锁' : order.can_complete === true ? '现货可交付' : order.can_complete === false ? '等待物资' : '库存待同步' }}</span></div>
      <ul class="material-lines">
        <li v-for="item in order.materials" :key="item.item_id" class="material-line"><span>{{ item.name }} × {{ item.count }}</span><small>库存 {{ item.available ?? '未同步' }} · 缺少 {{ item.shortage ?? '待计算' }}</small></li>
      </ul>
      <p class="reward-line">奖励：{{ order.rewards.map(item => `${item.name} × ${item.count}`).join('、') || '未同步' }}</p>
      <p class="hint">{{ order.refresh_seconds === null ? '刷新时间未同步' : `刷新等待 ${order.refresh_seconds} 秒` }}</p>
    </article>
  </section>
</template>

<style scoped>
.section-title { margin: 0 0 14px; font-size: 18px; }
.order-card { padding: 16px; margin-top: 10px; border: 1px solid var(--border); border-radius: 14px; background: var(--surface); overflow-wrap: anywhere; }
.card-heading { display: flex; flex-wrap: wrap; align-items: start; justify-content: space-between; gap: 8px; }
.order-title { margin: 0; font-size: 14px; }
.order-state { color: var(--accent-deep); font-size: 12px; }
.material-lines { padding: 0; margin: 13px 0; list-style: none; }
.material-line { display: flex; flex-direction: column; gap: 4px; margin-top: 10px; font-size: 13px; line-height: 1.6; }
.material-line small { color: var(--text-muted); }
.reward-line { margin: 0; font-size: 13px; line-height: 1.6; }
.hint { color: var(--text-muted); font-size: 12px; line-height: 1.7; margin-bottom: 0; }
</style>
