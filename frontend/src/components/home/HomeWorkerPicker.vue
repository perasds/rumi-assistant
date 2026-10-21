<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { Button as VanButton, Popup as VanPopup } from 'vant'
import type { HomeBuildingState, HomeWorker } from '../../api/types'
import { canSelectHomeWorker } from '../../composables/useHomeManagement'

const props = defineProps<{ show: boolean; building: HomeBuildingState | null; roster: HomeWorker[]; selected: string[] }>()
const emit = defineEmits<{ close: []; confirm: [uids: string[]] }>()
const selection = shallowRef<string[]>([])
watch(() => props.show, show => { if (show) selection.value = [...props.selected] }, { immediate: true })
const limit = computed(() => props.building?.assignment_limit ?? 0)
const canSave = computed(() => selection.value.length > 0 && selection.value.length <= limit.value)
function available(worker: HomeWorker) { return !!props.building && canSelectHomeWorker(worker, props.building) }
function toggle(worker: HomeWorker) {
  if (!available(worker) || !limit.value) return
  if (limit.value === 1) selection.value = [worker.lumi_uid]
  else if (selection.value.includes(worker.lumi_uid)) selection.value = selection.value.filter(id => id !== worker.lumi_uid)
  else if (selection.value.length < limit.value) selection.value = [...selection.value, worker.lumi_uid]
}
</script>

<template>
  <VanPopup :show="show" class="home-worker-picker" position="bottom" round :close-on-click-overlay="true" @update:show="emit('close')">
    <header class="picker-header"><h2 class="picker-title">选择工作噜咪</h2><span class="picker-limit">最多 {{ limit }} 只</span></header>
    <div class="worker-roster">
      <button v-for="worker in roster" :key="worker.lumi_uid" class="worker-option" type="button" :data-lumi-uid="worker.lumi_uid"
        :disabled="!available(worker)" :aria-pressed="selection.includes(worker.lumi_uid)" @click="toggle(worker)">
        <img v-if="worker.image_url" class="worker-image" :src="worker.image_url" alt="" loading="lazy">
        <span class="worker-info"><strong>{{ worker.name }}</strong><small>实例 {{ worker.lumi_uid }}</small>
          <small>工作资质 {{ worker.work_aptitude ?? '未同步' }} · 体力 {{ worker.work_energy ?? '未同步' }}</small>
          <small v-if="!available(worker)">{{ worker.reason ?? '不适合当前建筑或正在其他位置工作' }}</small>
        </span><span class="selection-mark" aria-hidden="true">{{ selection.includes(worker.lumi_uid) ? '✓' : '○' }}</span>
      </button>
      <p v-if="!roster.length" class="empty-roster">噜咪列表尚未同步</p>
    </div>
    <footer class="picker-footer"><VanButton plain @click="emit('close')">取消</VanButton>
      <VanButton type="primary" aria-label="保存工人选择" :disabled="!canSave" @click="emit('confirm', [...selection])">保存选择</VanButton></footer>
  </VanPopup>
</template>

<style scoped>
.home-worker-picker { display: flex; flex-direction: column; width: 100%; max-width: 680px; max-height: 85vh; max-height: 85dvh; left: 50%; transform: translateX(-50%); padding: 20px 18px calc(16px + env(safe-area-inset-bottom)); }
.picker-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; }
.picker-title { margin: 0; font-size: 19px; }.picker-limit { font-size: 12px; color: var(--text-muted); }
.worker-roster { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.worker-option { display: flex; align-items: center; gap: 10px; width: 100%; padding: 12px; margin-bottom: 9px; text-align: left; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; color: var(--ink); cursor: pointer; }
.worker-option[aria-pressed='true'] { border-color: var(--accent); background: var(--accent-light); }.worker-option:disabled { opacity: .55; cursor: default; }
.worker-image { width: 42px; height: 42px; object-fit: contain; border-radius: 9px; }.worker-info { flex: 1; min-width: 0; font-size: 14px; }.worker-info small { display: block; margin-top: 4px; color: var(--text-muted); font-size: 11px; overflow-wrap: anywhere; }.selection-mark { color: var(--accent-deep); }
.picker-footer { display: flex; flex-shrink: 0; justify-content: flex-end; gap: 12px; margin-top: 16px; }.picker-footer :deep(.van-button) { min-width: 90px; border-radius: 10px; }.empty-roster { text-align: center; color: var(--text-muted); }
</style>
