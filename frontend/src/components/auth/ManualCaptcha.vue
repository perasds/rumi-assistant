<script setup lang="ts">
import { shallowRef, watch } from 'vue'
import { Button as VanButton, Loading as VanLoading, Popup as VanPopup, showToast } from 'vant'
import type { ManualCaptchaFrame, ManualCaptchaInput } from '../../api/types'

const props = defineProps<{ challenge: ManualCaptchaFrame; cancelling: boolean }>()
const emit = defineEmits<{ input: [challengeId: string, value: ManualCaptchaInput]; cancel: [challengeId: string] }>()
const loading = shallowRef(true)
let gesture: { id: string; pointer: number; x: number; y: number; clientX: number; clientY: number; dragging: boolean; lastMove: number; lastX: number; lastY: number } | null = null
watch(() => props.challenge.challenge_id, () => { gesture = null })
watch(() => props.challenge.image, () => { loading.value = true })
watch(() => props.cancelling, value => { if (value) gesture = null })
function point(event: PointerEvent) {
  const rect = (event.currentTarget as HTMLImageElement).getBoundingClientRect()
  if (!rect.width || !rect.height) return null
  return { x: Math.max(0, Math.min(props.challenge.width - 1, Math.round((event.clientX - rect.left) * props.challenge.width / rect.width))),
    y: Math.max(0, Math.min(props.challenge.height - 1, Math.round((event.clientY - rect.top) * props.challenge.height / rect.height))) }
}
function down(event: PointerEvent) {
  if (props.cancelling || event.button !== 0 || gesture) return
  const value = point(event)
  if (!value) return
  gesture = { id: props.challenge.challenge_id, pointer: event.pointerId, ...value,
    clientX: event.clientX, clientY: event.clientY, dragging: false, lastMove: -Infinity, lastX: value.x, lastY: value.y }
  ;(event.currentTarget as HTMLImageElement).setPointerCapture?.(event.pointerId)
}
function move(event: PointerEvent) {
  const current = gesture
  if (!current || props.cancelling || current.pointer !== event.pointerId || current.id !== props.challenge.challenge_id) return
  const value = point(event)
  if (!value) return
  if (!current.dragging && Math.hypot(event.clientX - current.clientX, event.clientY - current.clientY) >= 3) {
    current.dragging = true
    emit('input', current.id, { type: 'pointerdown', x: current.x, y: current.y })
  }
  if (current.dragging && Date.now() - current.lastMove >= 50) {
    emit('input', current.id, { type: 'pointermove', ...value })
    current.lastMove = Date.now(); current.lastX = value.x; current.lastY = value.y
  }
}
function up(event: PointerEvent, cancelled = false) {
  const current = gesture
  if (!current || current.pointer !== event.pointerId) return
  gesture = null
  if (props.cancelling || current.id !== props.challenge.challenge_id) return
  const value = point(event) ?? { x: current.lastX, y: current.lastY }
  if (current.dragging) {
    if (value.x !== current.lastX || value.y !== current.lastY) emit('input', current.id, { type: 'pointermove', ...value })
    emit('input', current.id, { type: 'pointerup', ...value })
  } else if (!cancelled) emit('input', current.id, { type: 'click', ...value })
  ;(event.currentTarget as HTMLImageElement).releasePointerCapture?.(event.pointerId)
}
function imageError() {
  loading.value = false
  showToast({ message: '验证码图片加载失败，请稍后重试', duration: 2500 })
}
</script>

<template>
  <VanPopup :show="true" class="captcha-popup" round :close-on-click-overlay="false" aria-label="人工安全验证">
    <div class="captcha-heading"><h2 class="captcha-title">安全验证</h2><p class="captcha-help">请手动完成下方官方验证</p></div>
    <div class="captcha-picture">
      <img class="captcha-image" :src="challenge.image" :width="challenge.width" :height="challenge.height" alt="官方安全验证页面"
        draggable="false" @pointerdown.prevent="down" @pointermove.prevent="move" @pointerup.prevent="up($event)"
        @pointercancel.prevent="up($event, true)" @lostpointercapture="up($event, true)" @click.prevent
        @load="loading = false" @error="imageError">
      <VanLoading v-if="loading" class="captcha-loading" size="22px" aria-label="正在更新验证图片" />
    </div>
    <div class="captcha-footer"><VanButton block plain :loading="cancelling" :disabled="cancelling" aria-label="取消人工验证" @click="emit('cancel', challenge.challenge_id)">取消验证</VanButton></div>
  </VanPopup>
</template>

<style scoped>
.captcha-popup { width: min(544px, calc(100vw - 24px)); max-height: 90vh; max-height: 90dvh; display: flex; flex-direction: column; overflow: hidden; background: #fff; }
.captcha-heading { flex-shrink: 0; padding: 14px 16px 10px; text-align: center; }
.captcha-title { margin: 0; font-size: 18px; color: var(--ink); }
.captcha-help { margin: 5px 0 0; font-size: 12px; color: var(--muted); }
.captcha-picture { display: flex; justify-content: center; min-height: 0; position: relative; overflow: hidden; }
.captcha-image { display: block; width: auto; height: auto; max-width: 100%; max-height: calc(90vh - 145px); max-height: calc(90dvh - 145px); object-fit: contain; touch-action: none; user-select: none; }
.captcha-loading { position: absolute; top: 8px; right: 8px; padding: 5px; border-radius: 50%; background: #ffffffd9; pointer-events: none; }
.captcha-footer { flex-shrink: 0; padding: 10px 16px 14px; }
</style>
