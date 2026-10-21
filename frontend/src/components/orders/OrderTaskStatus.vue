<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import type { OrderTask } from '../../api/orderTypes'
const props = defineProps<{ task: OrderTask; pending: boolean; canStart: boolean; canResolve: boolean; startReason: string | null }>()
const emit = defineEmits<{ start: []; stop: []; resolve: [] }>()
const acknowledged = shallowRef(false)
watch(() => `${props.task.status}:${props.task.running}`, () => { acknowledged.value = false })
const labels: Record<OrderTask['status'], string> = { idle: '未开始', running: '执行中', waiting_materials: '等待物资', waiting_refresh: '等待刷新',
  confirming: '确认操作中', stopped: '已停止', completed: '已完成', uncertain: '结果待核实', failed: '失败' }
const label = computed(() => labels[props.task.status] ?? '状态待核实')
const inspectionTask = computed(() => props.task.kind === 'inspection')
</script>

<template>
  <section class="task-status" aria-label="订单任务状态" aria-live="polite">
    <div class="status-heading"><h2 class="status-title">{{ inspectionTask ? '工作巡检任务' : label }}</h2>
      <button v-if="task.running && !inspectionTask" class="task-button stop" type="button" aria-label="取消订单任务" :disabled="pending" @click="emit('stop')">{{ pending ? '处理中…' : '取消任务' }}</button>
      <button v-else class="task-button" type="button" aria-label="开始订单任务" :disabled="!canStart || pending" @click="emit('start')">{{ pending ? '处理中…' : '开始任务' }}</button>
    </div>
    <p v-if="inspectionTask" class="message">{{ task.running ? '工作巡检正在运行，请先在工作巡检页停止任务。' : task.status === 'uncertain' ? '巡检结果待核实，请前往工作巡检核对。' : '上次执行的是工作巡检，可开始订单任务。' }}</p>
    <p v-else class="totals">已确认完成 {{ task.completed_orders }} 单 · 实得罐头 {{ task.canned_received }}</p>
    <p v-if="task.message" class="message">{{ task.message }}</p>
    <p v-if="!task.running && startReason" class="message">{{ startReason }}</p>
    <div v-if="task.status === 'uncertain' && !task.running && !inspectionTask" class="resolution">
      <label class="acknowledgment"><input v-model="acknowledged" type="checkbox" name="order_task_acknowledged" :disabled="pending">我已在游戏中核对交单、领取和库存结果</label>
      <button class="resolve-button" type="button" aria-label="解除订单任务暂停" :disabled="!acknowledged || !canResolve || pending" @click="acknowledged && canResolve && emit('resolve')">{{ pending ? '处理中…' : '解除暂停' }}</button>
      <p class="hint">解除只移除保护暂停，不重试游戏操作，也不启动新任务。</p>
    </div>
    <p class="hint">取消会停止后续动作；游戏里已经开始的生产仍会继续。离开本页后后台任务继续运行。</p>
  </section>
</template>

<style scoped>
.task-status { padding: 18px; border: 1px solid var(--border); background: var(--surface-muted); border-radius: 16px; }
.status-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.status-title { margin: 0; font-size: 18px; }
.task-button { min-height: 42px; flex-shrink: 0; padding: 10px 15px; border: 0; border-radius: 10px; background: var(--accent-deep); color: white; cursor: pointer; }
.task-button.stop { color: var(--danger); background: #ffeadf; }
.task-button:disabled { opacity: .5; cursor: default; }
.totals, .message { font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.hint { margin: 8px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.7; }
.resolution { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); }
.acknowledgment { display: flex; align-items: flex-start; gap: 8px; font-size: 13px; line-height: 1.7; }
.acknowledgment input { margin-top: 5px; flex-shrink: 0; }
.resolve-button { margin-top: 12px; padding: 9px 14px; min-height: 40px; color: var(--accent-deep); background: #fffefa; border: 1px solid var(--border); border-radius: 9px; cursor: pointer; }
.resolve-button:disabled { opacity: .5; cursor: default; }
</style>
