<script setup lang="ts">
import { computed } from 'vue'
import { Loading as VanLoading } from 'vant'
import type { MarketSnapshot } from '../../api/types'
const props = defineProps<{ snapshot: MarketSnapshot | null }>()
const statusText = computed(() => {
  const value = props.snapshot
  if (!value || value.status === 'idle') return '尚未刷新'
  if (value.complete) return value.scope === 'top10' ? '本次前十查询完成' : '本轮已加载完'
  if (value.status === 'loading') return value.scope === 'top10' ? '按游戏一口价升序读取，本次前十查询未完成' : '正在取得商品，本轮未完成'
  if (value.scope === 'top10' && value.status === 'partial') return '本次前十查询未完成，请刷新重试'
  return value.status === 'failed' ? '本轮查询失败，未完成' : '本轮未完成'
})
const updatedAt = computed(() => props.snapshot?.updated_at ? new Date(props.snapshot.updated_at).toLocaleString('zh-CN') : null)
</script>
<template>
  <section class="refresh-status" aria-live="polite">
    <div class="status-line">
      <VanLoading v-if="snapshot?.status === 'loading' && !snapshot.complete" size="16" />
      <span class="status-badge" :class="{ 'is-complete': snapshot?.complete }">{{ statusText }}</span>
      <span v-if="snapshot">已取得 {{ snapshot.loaded_count }} 条<span v-if="snapshot.total_count !== null"> / {{ snapshot.total_count }}</span> · 匹配 {{ snapshot.matched_count }} 条</span>
    </div>
    <p v-if="snapshot" class="details">本轮商品名称覆盖 {{ snapshot.name_coverage.named }} / {{ snapshot.name_coverage.total }}<span v-if="updatedAt"> · 更新于 {{ updatedAt }}</span></p>
    <p v-if="snapshot?.message" class="message">{{ snapshot.message }}</p>
  </section>
</template>
<style scoped>
.refresh-status { padding: 16px 18px; border: 1px solid var(--border); background: var(--field-background); border-radius: 16px; color: var(--text-muted); font-size: 11px; }
.status-line { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.status-badge { padding: 4px 8px; border-radius: 7px; background: #fff3da; color: #9b6b2c; font-weight: 600; }
.status-badge.is-complete { background: var(--success-light); color: var(--success); }
.details, .message { margin: 8px 0 0; line-height: 1.7; }
.message { color: var(--danger); }
</style>
