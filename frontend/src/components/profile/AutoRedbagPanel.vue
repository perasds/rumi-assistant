<script setup lang="ts">
import { computed } from 'vue'
import { Button as VanButton } from 'vant'
import type { RedbagTaskStatus } from '../../api/redbagTypes'
import { useAutoRedbag } from '../../composables/useAutoRedbag'

const emit = defineEmits<{ expired: [] }>()
const { task, busy, error, canStart, canStop, load, start, stop } = useAutoRedbag(() => emit('expired'))
const statusLabels: Record<RedbagTaskStatus, string> = {
  idle: '尚未启动', starting: '正在启动', running: '运行中', stopping: '正在取消', stopped: '已取消',
  daily_limit: '今日已达上限', conditions_not_met: '条件不满足', rate_limited: '请求受限', uncertain: '结果待核实', failed: '任务失败',
}
const resultLabels = { claimed: '已领取', skipped: '已跳过', uncertain: '待核实' }
const status = computed(() => task.value ? statusLabels[task.value.status] : '任务状态尚未读取')
const recent = computed(() => task.value?.recent.slice(0, 20) ?? [])
function formatStamina(value: string) {
  return /^\d+$/.test(value) ? BigInt(value).toLocaleString('en-US') : '未同步'
}
const staminaTotal = computed(() => task.value ? formatStamina(task.value.stamina_total) : '未同步')
function formatTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未同步' : date.toLocaleTimeString('zh-CN', { hour12: false })
}
</script>

<template>
  <section class="auto-redbag-panel" aria-label="自动抢红包">
    <div class="task-heading">
      <h2 class="card-heading">自动抢红包</h2>
      <span class="task-status" role="status">{{ status }}</span>
    </div>
    <p class="task-help">只抢启动后出现的新红包。切换页面或关闭网页后，任务仍会继续；需要停止时请点取消。</p>
    <p v-if="task?.message" class="task-message">{{ task.message }}</p>
    <p v-if="task?.status === 'uncertain'" class="task-warning">请先在游戏中核实领取结果，核实后重新登录，再手动开始。</p>
    <p v-if="error" class="task-warning" role="alert">{{ error }}</p>
    <div class="task-statistics">
      <span>本次成功 {{ task ? task.claimed_count : '未同步' }}</span>
      <span>体力合计 {{ staminaTotal }}</span>
    </div>
    <div class="task-actions">
      <VanButton v-if="task?.running" plain type="primary" :loading="busy" :disabled="!canStop" aria-label="取消自动抢红包" @click="stop">{{ task.status === 'stopping' ? '正在取消' : '取消' }}</VanButton>
      <VanButton v-else type="primary" :loading="busy" :disabled="!canStart" aria-label="开始自动抢红包" @click="start">开始</VanButton>
      <VanButton v-if="error" plain size="small" :disabled="busy" aria-label="刷新红包任务" @click="load">刷新任务</VanButton>
    </div>
    <h3 class="results-heading">最近结果 <span class="results-limit">最多 20 条</span></h3>
    <ul v-if="recent.length" class="task-results">
      <li v-for="result in recent" :key="result.id" class="task-result">
        <div class="result-heading"><span>{{ resultLabels[result.status] }}</span><time :datetime="result.at">{{ formatTime(result.at) }}</time></div>
        <p class="result-message">{{ result.message }}</p>
        <span class="result-stamina">体力 {{ formatStamina(result.stamina) }}</span>
      </li>
    </ul>
    <p v-else class="results-empty">暂无领取记录</p>
  </section>
</template>

<style scoped>
.auto-redbag-panel { margin-top: 20px; border: 1px solid var(--border); border-radius: 20px; padding: 24px; background: var(--surface); }
.task-heading, .result-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.card-heading { margin: 0; color: var(--ink); font-size: 18px; }
.task-status { flex-shrink: 0; color: var(--text-muted); font-size: 13px; }
.task-help, .task-message, .task-warning { margin: 12px 0; font-size: 14px; line-height: 1.7; overflow-wrap: anywhere; }
.task-help { color: var(--text-muted); }
.task-message { color: var(--ink); }
.task-warning { color: var(--danger); }
.task-statistics { display: flex; flex-wrap: wrap; gap: 8px 24px; margin: 18px 0; color: var(--ink); font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
.task-actions { display: flex; align-items: center; gap: 12px; }
.task-actions :deep(.van-button) { border-radius: 10px; }
.results-heading { display: flex; align-items: baseline; gap: 8px; margin: 24px 0 12px; color: var(--ink); font-size: 14px; }
.results-limit { color: var(--text-muted); font-size: 12px; font-weight: 400; }
.task-results { margin: 0; padding: 0; list-style: none; }
.task-result { padding: 12px 0; border-top: 1px solid var(--border); color: var(--ink); font-size: 13px; }
.result-heading { color: var(--text-muted); }
.result-message { margin: 8px 0; line-height: 1.6; overflow-wrap: anywhere; }
.result-stamina { color: var(--text-muted); }
.results-empty { margin: 0; color: var(--text-muted); font-size: 13px; }
@media (max-width: 480px) { .auto-redbag-panel { padding: 20px; } }
</style>
