<script setup lang="ts">
import { computed } from 'vue'
import type { PersonalInfo } from '../../api/types'

const props = defineProps<{ info: PersonalInfo }>()
const balance = computed(() => {
  const value = props.info.balance_gold
  return typeof value === 'string' && /^\d+$/.test(value)
    ? `${BigInt(value).toLocaleString('en-US')} 金币` : '未同步'
})
</script>

<template>
  <section class="personal-info-card" aria-label="账号资料">
    <h2 class="card-heading">个人信息</h2>
    <dl class="info-list">
      <div class="info-row"><dt class="info-label">昵称</dt><dd class="info-value">{{ info.nickname || '未同步' }}</dd></div>
      <div class="info-row"><dt class="info-label">角色 ID</dt><dd class="info-value">{{ info.role_id || '未同步' }}</dd></div>
      <div class="info-row"><dt class="info-label">区服</dt><dd class="info-value">{{ info.server_name || '未同步' }}</dd></div>
      <div class="info-row"><dt class="info-label">金币余额</dt><dd class="info-value gold-balance">{{ balance }}</dd></div>
    </dl>
  </section>
</template>

<style scoped>
.personal-info-card { border: 1px solid var(--border); border-radius: 20px; padding: 24px; background: #fff; }
.card-heading { margin: 0 0 12px; color: var(--ink); font-size: 18px; }
.info-list { margin: 0; }
.info-row { display: grid; grid-template-columns: 88px minmax(0, 1fr); align-items: baseline; gap: 16px; padding: 18px 0; border-bottom: 1px solid var(--border); }
.info-row:last-child { border-bottom: 0; padding-bottom: 0; }
.info-label { color: var(--text-muted); font-size: 14px; }
.info-value { min-width: 0; margin: 0; color: var(--ink); font-size: 15px; line-height: 1.6; text-align: right; overflow-wrap: anywhere; }
.gold-balance { color: var(--gold); font-weight: 700; }
@media (max-width: 480px) { .personal-info-card { padding: 20px; } .info-row { gap: 12px; grid-template-columns: 76px minmax(0, 1fr); } }
</style>
