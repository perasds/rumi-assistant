<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import type { OrderTask, WorkInspection, Workplace } from '../../api/orderTypes'
import { orderReason } from '../../utils/orderPresentation'

const props = defineProps<{ inspection?: WorkInspection; task: OrderTask; pending: boolean; loading: boolean;
  canStart: boolean; canResolve: boolean; startReason: string | null; horizonHours: number }>()
const emit = defineEmits<{ start: []; stop: []; resolve: [] }>()
const acknowledged = shallowRef(false)
const ownTask = computed(() => props.task.kind === 'inspection')
const running = computed(() => ownTask.value && props.task.running)
const taskLabels: Record<OrderTask['status'], string> = { idle: '未开始', running: '巡检中', waiting_materials: '等待生产条件',
  waiting_refresh: '等待状态更新', confirming: '确认安排中', stopped: '已停止', completed: '已完成', uncertain: '结果待核实', failed: '失败' }
const statusLabels: Record<Workplace['status'], string> = { idle: '闲置', working: '生产中', missing_worker: '缺少工作噜咪',
  blocked: '暂不可安排', unknown: '状态未同步' }
function workerUnconfigured(building: Workplace) {
  return building.status === 'missing_worker' || building.worker_uid === '0' || building.worker_uid === '2147483647'
}
function recipeLabel(building: Workplace) {
  if (building.current_recipe_id === '0') return '未设置'
  if (building.current_recipe_products?.length) {
    return building.current_recipe_products.map(item => `${item.name} × ${item.count}`).join('、')
  }
  return building.current_recipe_id ? `配方 #${building.current_recipe_id}（名称未同步）` : '未同步'
}
watch(() => `${props.task.kind}:${props.task.status}:${props.task.running}`, () => { acknowledged.value = false })
</script>

<template>
  <section class="work-inspection" aria-label="工作巡检状态">
    <div class="inspection-heading">
      <h2 class="section-title">工作巡检</h2>
      <button v-if="running" class="task-button stop" type="button" aria-label="停止巡检" :disabled="pending" @click="emit('stop')">{{ pending ? '处理中…' : '停止巡检' }}</button>
      <button v-else class="task-button" type="button" aria-label="开始巡检" :disabled="!canStart || pending || loading" @click="emit('start')">{{ pending ? '处理中…' : '开始巡检' }}</button>
    </div>
    <p v-if="ownTask" class="task-progress" role="status">{{ taskLabels[task.status] }} · 已确认安排 {{ task.inspected_actions ?? '未同步' }} 次</p>
    <p v-if="ownTask && task.message" class="message">{{ orderReason(task.message) }}</p>
    <p v-if="!inspection?.ready" class="message">{{ orderReason(inspection?.reason ?? '工作巡检数据尚未同步') }}</p>
    <p v-else-if="!running && startReason" class="message">{{ orderReason(startReason) }}</p>
    <p class="hint">为明确闲置且配方为空的工位选择可生产物品中当前库存最少的一种。普通生产默认 1 批，持续类保持持续生产；有耗材生产逐批确认后再安排下一批。</p>
    <p class="hint">巡检沿用已保存的 {{ horizonHours }} 小时计划时域，保留已有生产配方。停止巡检会停止后续安排，已经开始的生产继续运行。</p>
    <div v-if="ownTask && task.status === 'uncertain' && !task.running" class="resolution">
      <label class="acknowledgment"><input v-model="acknowledged" type="checkbox" name="inspection_task_acknowledged" :disabled="pending">我已在游戏中核对生产、领取和库存结果</label>
      <button class="resolve-button" type="button" aria-label="解除巡检任务暂停" :disabled="!acknowledged || !canResolve || pending" @click="acknowledged && canResolve && emit('resolve')">{{ pending ? '处理中…' : '解除暂停' }}</button>
      <p class="hint">解除只移除保护暂停，不重试游戏操作，也不启动新任务。</p>
    </div>
    <div class="workplaces">
      <article v-for="building in inspection?.buildings ?? []" :key="building.building_uid" class="workplace-card" :data-building-uid="building.building_uid">
        <div class="workplace-heading"><h3 class="workplace-title">{{ building.name }}</h3><span class="workplace-status" :class="{ 'missing-worker': building.status === 'missing_worker' }">{{ statusLabels[building.status] }}</span></div>
        <p class="detail">等级 {{ building.level ?? '未同步' }} · 工位 {{ building.building_uid }}</p>
        <p class="detail worker-detail">工作噜咪
          <img v-if="building.worker_image_url && !workerUnconfigured(building)" class="worker-image" :src="building.worker_image_url" alt="" loading="lazy">
          <span>{{ workerUnconfigured(building) ? '未配置' : building.worker_name || (building.worker_uid ? '噜咪名称未同步' : '未同步') }}</span>
          <small v-if="building.worker_uid && !workerUnconfigured(building)" class="worker-instance">实例 {{ building.worker_uid }}</small>
        </p>
        <p class="detail">当前配方 {{ recipeLabel(building) }}</p>
        <p class="message">{{ orderReason(building.message) }}</p>
        <div v-if="building.candidate" class="candidate">
          <h4>候选最低库存物品：{{ building.candidate.name }}</h4>
          <p>当前库存 {{ building.candidate.stock }} · 每批产出 {{ building.candidate.output_count }}</p>
          <p>配方 {{ building.candidate.recipe_id }} · {{ building.candidate.recipe_times === '0' ? '持续类保持持续生产' : `生产批次 ${building.candidate.recipe_times}` }}</p>
        </div>
      </article>
    </div>
    <p v-if="inspection?.ready && !inspection.buildings.length" class="hint">当前没有已同步的工作工位。</p>
  </section>
</template>

<style scoped>
.work-inspection { min-width: 0; max-width: 100%; padding: 18px; border: 1px solid var(--border); border-radius: 16px; background: var(--surface); overflow-wrap: anywhere; }
.inspection-heading, .workplace-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.section-title { margin: 0; font-size: 18px; }
.task-button { min-height: 42px; padding: 10px 15px; border: 0; border-radius: 10px; background: var(--accent-deep); color: white; cursor: pointer; }
.task-button.stop { color: var(--danger); background: #ffeadf; }
.task-button:disabled, .resolve-button:disabled { opacity: .5; cursor: default; }
.task-progress, .message, .detail { margin: 10px 0 0; font-size: 13px; line-height: 1.7; }
.worker-detail { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.worker-image { width: 32px; height: 32px; flex-shrink: 0; object-fit: contain; border-radius: 8px; }
.worker-instance { color: var(--text-muted); font-size: 12px; }
.hint { margin: 10px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.7; }
.workplaces { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 18px; }
.workplace-card { min-width: 0; border: 1px solid var(--border); border-radius: 12px; padding: 14px; background: #fffefa; }
.workplace-title { min-width: 0; margin: 0; font-size: 14px; line-height: 1.6; }
.workplace-status { color: var(--accent-deep); font-size: 12px; }
.workplace-status.missing-worker { color: var(--danger); font-weight: 600; }
.candidate { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }
.candidate h4 { margin: 0; font-size: 13px; line-height: 1.7; }
.candidate p { margin: 5px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.7; }
.resolution { margin-top: 16px; padding: 14px; border: 1px solid #e6cfbc; border-radius: 12px; background: #fff7ef; }
.acknowledgment { display: flex; align-items: flex-start; gap: 8px; font-size: 13px; line-height: 1.7; }
.acknowledgment input { margin-top: 5px; flex-shrink: 0; }
.resolve-button { margin-top: 12px; padding: 9px 14px; min-height: 40px; border: 1px solid var(--border); border-radius: 9px; color: var(--accent-deep); background: #fffefa; cursor: pointer; }
@media (max-width: 640px) { .work-inspection { padding: 14px; } .workplaces { grid-template-columns: minmax(0, 1fr); } }
</style>
