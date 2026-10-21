<script setup lang="ts">
import { computed, reactive, shallowRef, watch } from 'vue'
import type { OrderSettings } from '../../api/orderTypes'

const props = defineProps<{ settings: OrderSettings; busy: boolean; canPlan: boolean; running: boolean }>()
const emit = defineEmits<{ change: [settings: OrderSettings | null]; save: []; preview: [] }>()
const fields = reactive({ horizon: '', maximum: '', production: false, refresh: false, reserves: [] as { key: number; id: string; count: string }[] })
let rowKey = 0
const saved = shallowRef('')
watch(() => props.settings, settings => {
  const serialized = JSON.stringify(settings)
  if (serialized === saved.value) return
  saved.value = serialized
  fields.horizon = String(settings.horizon_hours)
  fields.maximum = String(settings.max_orders)
  fields.production = settings.allow_production
  fields.refresh = false
  fields.reserves = Object.entries(settings.reserves).map(([id, count]) => ({ key: ++rowKey, id, count }))
}, { immediate: true })
const validation = computed(() => {
  if (!/^\d+$/.test(fields.horizon) || Number(fields.horizon) < 1 || Number(fields.horizon) > 168) return '计划时域须为 1–168 的整数小时'
  if (!/^\d+$/.test(fields.maximum) || Number(fields.maximum) < 1 || Number(fields.maximum) > 10000) return '订单上限须为 1–10000 的整数'
  const ids = new Set<string>()
  if (fields.reserves.length > 256) return '保留物资不能超过 256 项'
  for (const row of fields.reserves) {
    if (!/^[1-9]\d*$/.test(row.id)) return '物资 ID 须为正十进制整数'
    if (!/^(0|[1-9]\d*)$/.test(row.count)) return '保留量须为非负十进制整数'
    if (row.id.length > 10 || BigInt(row.id) > 2147483647n) return '物资 ID 不能超过 2147483647'
    if (row.count.length > 19 || BigInt(row.count) > 9223372036854775807n) return '保留量不能超过 9223372036854775807'
    const id = BigInt(row.id).toString()
    if (ids.has(id)) return '保留物资 ID 不能重复'
    ids.add(id)
  }
  return null
})
watch(fields, () => {
  emit('change', validation.value ? null : { horizon_hours: Number(fields.horizon), max_orders: Number(fields.maximum),
    reserves: Object.fromEntries(fields.reserves.map(row => [row.id, BigInt(row.count).toString()])),
    allow_production: fields.production, allow_refresh: fields.refresh })
}, { deep: true, immediate: true })
</script>

<template>
  <section class="order-settings" aria-label="订单任务设置">
    <h2 class="section-title">任务设置</h2>
    <form class="settings-form" @submit.prevent="!validation && !busy && !running && emit('save')">
      <fieldset class="settings-fields" :disabled="busy || running">
        <div class="number-fields">
          <label class="field">计划时域（小时）<input v-model="fields.horizon" name="horizon_hours" inputmode="numeric" type="text" autocomplete="off"></label>
          <label class="field">完成订单上限<input v-model="fields.maximum" name="max_orders" inputmode="numeric" type="text" autocomplete="off"></label>
        </div>
        <label class="check-field"><input v-model="fields.production" type="checkbox" name="allow_production">允许安排生产</label>
        <label class="check-field"><input type="checkbox" name="allow_refresh" disabled :checked="false">自动换单暂未开放（换单消耗尚未核实）</label>
        <div class="reserve-heading"><h3 class="reserve-title">保留库存</h3><button type="button" class="text-button" aria-label="添加保留物资" :disabled="fields.reserves.length >= 256" @click="fields.reserves.length < 256 && fields.reserves.push({ key: ++rowKey, id: '', count: '0' })">添加物资</button></div>
        <p v-if="!fields.reserves.length" class="hint">未设置保留量；任务会按设置使用可用物资。</p>
        <div v-for="(row, index) in fields.reserves" :key="row.key" class="reserve-row">
          <label class="field">物资 ID<input v-model="row.id" name="reserve_item_id" :aria-label="`保留物资 ${index + 1} ID`" inputmode="numeric" autocomplete="off"></label>
          <label class="field">保留数量<input v-model="row.count" name="reserve_count" :aria-label="`保留物资 ${index + 1} 数量`" inputmode="numeric" autocomplete="off"></label>
          <button type="button" class="text-button remove-reserve" :aria-label="`移除保留物资 ${index + 1}`" @click="fields.reserves.splice(index, 1)">移除</button>
        </div>
      </fieldset>
      <p v-if="validation" class="validation" role="alert">{{ validation }}</p>
      <div class="form-actions">
        <button class="action-button" type="submit" aria-label="保存订单设置" :disabled="!!validation || busy || running">保存设置</button>
        <button class="action-button secondary" type="button" aria-label="预览订单计划" :disabled="!!validation || !canPlan || busy" @click="emit('preview')">预览计划</button>
      </div>
      <p class="hint">保存只更新设置；预览只计算方案。开始后才会执行游戏操作。</p>
    </form>
  </section>
</template>

<style scoped>
.order-settings { padding: 18px; border: 1px solid var(--border); border-radius: 16px; background: var(--surface); }
.section-title { margin: 0 0 16px; font-size: 18px; }
.settings-fields { min-width: 0; margin: 0; padding: 0; border: 0; }
.number-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.field { display: flex; flex-direction: column; min-width: 0; gap: 7px; font-size: 13px; }
.field input { width: 100%; min-width: 0; padding: 10px; border: 1px solid var(--border); border-radius: 9px; color: var(--ink); background: #fffefa; }
.check-field { display: flex; align-items: flex-start; gap: 7px; margin-top: 14px; font-size: 13px; line-height: 1.6; }
.check-field input { flex-shrink: 0; margin-top: 4px; }
.reserve-heading { display: flex; align-items: center; justify-content: space-between; margin-top: 18px; }
.reserve-title { margin: 0; font-size: 14px; }
.reserve-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; gap: 10px; align-items: end; margin-top: 12px; }
.text-button { border: 0; padding: 7px 0; color: var(--accent-deep); background: transparent; cursor: pointer; font-size: 13px; }
.text-button:disabled { opacity: .5; cursor: default; }
.remove-reserve { min-height: 40px; }
.form-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }
.action-button { min-height: 42px; border: 1px solid var(--accent-deep); padding: 9px 16px; border-radius: 10px; color: white; background: var(--accent-deep); cursor: pointer; }
.action-button.secondary { color: var(--accent-deep); background: transparent; }
.action-button:disabled { opacity: .5; cursor: default; }
.validation { color: var(--danger); font-size: 13px; line-height: 1.6; }
.hint { margin: 10px 0 0; font-size: 12px; color: var(--text-muted); line-height: 1.7; }
@media (max-width: 480px) { .order-settings { padding: 14px; } .reserve-row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } .remove-reserve { grid-column: 2; justify-self: end; min-height: 28px; } }
</style>
