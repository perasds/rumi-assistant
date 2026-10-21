<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import QRCode from 'qrcode'
import { Button as VanButton, Loading as VanLoading, Image as VanImage } from 'vant'
import type { QrChallenge, QrState } from '../../api/types'
const props = defineProps<{ challenge: QrChallenge | null; state: QrState | null; busy: boolean }>()
const emit = defineEmits<{ start: []; check: [] }>()
const image = shallowRef<string | null>(null)
const imageError = shallowRef(false)
const statusText = computed(() => ({ waiting: '等待扫码', scanned: '已扫码，请在游戏登录页面确认', confirmed: '已确认登录', expired: '二维码已过期，请重新获取', failed: '扫码登录失败，请重试' }[props.state?.status ?? 'waiting']))
watch(() => props.challenge?.qr_payload, async (payload, _previous, onCleanup) => {
  let canceled = false
  onCleanup(() => { canceled = true })
  image.value = null
  imageError.value = false
  if (!payload) return
  try {
    const data = await QRCode.toDataURL(payload, { width: 224, margin: 2 })
    if (!canceled) image.value = data
  } catch { if (!canceled) imageError.value = true }
}, { immediate: true })
watch(() => [props.challenge?.challenge_id, props.state?.status, props.busy], (_value, _previous, onCleanup) => {
  if (!props.challenge || props.busy || !['waiting', 'scanned'].includes(props.state?.status ?? 'waiting')) return
  const timer = setTimeout(() => emit('check'), 3_000)
  onCleanup(() => clearTimeout(timer))
}, { immediate: true })
</script>
<template>
  <section class="qr-card" aria-live="polite">
    <template v-if="challenge">
      <VanImage v-if="image" class="qr-image" :src="image" width="224" height="224" alt="游戏登录二维码" />
      <VanLoading v-else-if="!imageError" class="qr-loading">生成二维码</VanLoading>
      <p v-if="imageError" class="message">二维码无法显示，请重新获取。</p>
      <p class="status">{{ statusText }}</p>
      <p v-if="state?.message" class="message">{{ state.message }}</p>
      <p class="expiry">有效期至 {{ new Date(challenge.expires_at).toLocaleString('zh-CN') }}</p>
      <VanButton v-if="['waiting', 'scanned'].includes(state?.status ?? 'waiting')" size="small" :loading="busy" @click="emit('check')">检查扫码状态</VanButton>
    </template>
    <p v-else class="message">使用游戏实际支持的扫码方式完成登录。</p>
    <VanButton class="start" block type="primary" :loading="busy" @click="emit('start')">{{ challenge ? '重新获取二维码' : '获取登录二维码' }}</VanButton>
  </section>
</template>
<style scoped>
.qr-card { text-align: center; }
.qr-image { margin: 0 auto; }
.qr-loading { padding: 60px 0; }
.status { margin: 16px 0 8px; font-weight: 600; }
.message, .expiry { color: var(--text-muted); font-size: 12px; line-height: 1.7; }
.start { margin-top: 20px; border-radius: 10px; }
</style>
