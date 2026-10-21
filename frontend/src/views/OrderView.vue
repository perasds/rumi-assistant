<script setup lang="ts">
import { computed, shallowRef, useTemplateRef } from 'vue'
import OrderSettingsForm from '../components/orders/OrderSettingsForm.vue'
import CurrentOrdersList from '../components/orders/CurrentOrdersList.vue'
import OrderMaterialList from '../components/orders/OrderMaterialList.vue'
import OrderProductionList from '../components/orders/OrderProductionList.vue'
import OrderTaskStatus from '../components/orders/OrderTaskStatus.vue'
import AccountBuildingsList from '../components/orders/AccountBuildingsList.vue'
import WorkInspectionPanel from '../components/orders/WorkInspectionPanel.vue'
import { useOrderAssistant } from '../composables/useOrderAssistant'
import { orderReason } from '../utils/orderPresentation'
import type { OrderTab } from '../router'

const props = defineProps<{ activeTab?: OrderTab }>()
const emit = defineEmits<{ expired: []; 'tab-change': [tab: OrderTab] }>()
const { state, plan, loading, refreshing, pending, error, notice, canPlan, canStart, canStartInspection, canResolve, startReason, inspectionStartReason,
  load, updateDraft, save, preview, start, startInspection, stop, resolve } = useOrderAssistant(() => emit('expired'))
const localTab = shallowRef<OrderTab>('orders')
const activeTab = computed(() => props.activeTab ?? localTab.value)
function selectTab(tab: OrderTab) {
  localTab.value = tab
  emit('tab-change', tab)
}
const orderTab = useTemplateRef<HTMLButtonElement>('orderTab')
const inspectionTab = useTemplateRef<HTMLButtonElement>('inspectionTab')
function tabKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const nextTab = event.key === 'Home' ? 'orders' : event.key === 'End' ? 'inspection' : activeTab.value === 'orders' ? 'inspection' : 'orders'
  selectTab(nextTab)
  const target = nextTab === 'orders' ? orderTab.value : inspectionTab.value
  target?.focus()
}
</script>

<template>
  <main class="order-view" aria-label="订单助手" :aria-busy="refreshing || pending">
    <div class="page-heading"><h1 class="heading">订单</h1><button class="refresh-button" type="button" aria-label="刷新订单状态" :disabled="refreshing || pending" @click="load(true)">{{ refreshing ? '同步中…' : '刷新状态' }}</button></div>
    <div class="order-tabs" role="tablist" aria-label="订单功能">
      <button id="order-task-tab" ref="orderTab" type="button" role="tab" aria-controls="order-task-panel" :aria-selected="activeTab === 'orders'" :tabindex="activeTab === 'orders' ? 0 : -1" @click="selectTab('orders')" @keydown="tabKeydown">订单任务</button>
      <button id="work-inspection-tab" ref="inspectionTab" type="button" role="tab" aria-controls="work-inspection-panel" :aria-selected="activeTab === 'inspection'" :tabindex="activeTab === 'inspection' ? 0 : -1" @click="selectTab('inspection')" @keydown="tabKeydown">工作巡检</button>
    </div>
    <div class="order-content">
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
      <p v-if="!state && !error" class="hint">正在同步订单与库存…</p>
      <template v-if="state">
        <div id="order-task-panel" v-show="activeTab === 'orders'" class="order-panel" role="tabpanel" aria-labelledby="order-task-tab">
          <p v-if="state.message" class="hint">{{ state.message }}</p>
          <OrderTaskStatus :task="state.task" :pending="pending" :can-start="canStart" :can-resolve="canResolve" :start-reason="startReason" @start="start" @stop="stop" @resolve="resolve" />
          <OrderSettingsForm :settings="state.settings" :busy="pending || loading" :can-plan="canPlan" :running="state.task.running || state.task.status === 'uncertain'" @change="updateDraft" @save="save" @preview="preview" />
          <p v-if="!state.inventory_ready" class="hint">库存未同步，未知库存不会当成零。</p>
          <p v-if="!state.production_ready" class="hint">生产数据未同步，相关补货时间暂不可用。</p>
          <CurrentOrdersList :orders="state.orders" :ready="state.ready" />
          <AccountBuildingsList :buildings="state.buildings" :ready="state.production_ready" />
          <template v-if="plan">
            <section class="plan-summary" aria-label="订单计划摘要"><h2 class="section-title">预计完成 {{ plan.predicted_count }} 单 · 预计罐头 {{ plan.predicted_canned_count }}</h2>
              <p class="hint">现货组合：{{ plan.inventory_selection.count }} 单，罐头 {{ plan.inventory_selection.canned_count }}；{{ plan.inventory_selection.optimal ? '当前库存组合最优' : '当前找到的可行方案' }}。</p>
              <p v-if="plan.inventory_selection.reason" class="hint">{{ orderReason(plan.inventory_selection.reason) }}</p>
              <p v-for="(warning, index) in plan.warnings" :key="index" class="hint">{{ orderReason(warning) }}</p>
            </section>
            <OrderMaterialList :materials="plan.materials" :base-requirements="plan.base_requirements" />
            <OrderProductionList :production="plan.production" :server-time="state.server_time_seconds" />
          </template>
        </div>
        <div id="work-inspection-panel" v-show="activeTab === 'inspection'" class="order-panel" role="tabpanel" aria-labelledby="work-inspection-tab">
          <WorkInspectionPanel :inspection="state.work_inspection" :task="state.task" :pending="pending" :loading="loading" :can-start="canStartInspection" :can-resolve="canResolve" :start-reason="inspectionStartReason" :horizon-hours="state.settings.horizon_hours" @start="startInspection" @stop="stop" @resolve="resolve" />
        </div>
      </template>
    </div>
  </main>
</template>

<style scoped>
.order-view { display: flex; flex: 1; flex-direction: column; min-height: 0; width: 100%; max-width: 900px; margin: 26px auto 0; padding: 0 24px 24px; overflow: hidden; }
.page-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-shrink: 0; padding-bottom: 20px; }
.heading { margin: 0; font-size: 28px; }
.refresh-button { flex-shrink: 0; min-height: 36px; padding: 6px 11px; border: 1px solid var(--border); border-radius: 10px; color: var(--accent-deep); background: var(--surface); cursor: pointer; }
.refresh-button:disabled { opacity: .5; cursor: default; }
.order-tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px; flex-shrink: 0; margin-bottom: 20px; padding: 5px; border: 1px solid var(--border); border-radius: 13px; background: var(--surface); }
.order-tabs button { min-width: 0; min-height: 40px; padding: 8px 10px; border: 0; border-radius: 9px; color: var(--text-muted); background: transparent; font-size: 14px; cursor: pointer; }
.order-tabs button[aria-selected="true"] { color: #fff; background: var(--accent-deep); }
.order-panel { display: flex; flex-direction: column; gap: 22px; min-width: 0; }
.order-content { display: flex; flex-direction: column; gap: 22px; min-height: 0; overflow-y: auto; overscroll-behavior-y: contain; padding: 0 2px 8px; }
.error, .notice { margin: 0; border-radius: 12px; padding: 14px; font-size: 13px; line-height: 1.7; }
.error { color: var(--danger); background: #fff0e9; }
.notice { color: var(--success); background: var(--success-light); }
.hint { margin: 0; font-size: 13px; color: var(--text-muted); line-height: 1.7; }
.section-title { margin: 0 0 10px; font-size: 18px; line-height: 1.6; overflow-wrap: anywhere; }
@media (max-width: 640px) { .order-view { margin-top: 20px; padding: 0 16px 20px; } .heading { font-size: 25px; } }
</style>
