<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { Button as VanButton, Field as VanField, CellGroup as VanCellGroup } from 'vant'
import type { SmsContact, SmsLoginCredentials } from '../../api/types'

const props = defineProps<{ disabled: boolean; busy: boolean; resendAfterSeconds: number }>()
const emit = defineEmits<{ send: [contact: SmsContact]; login: [credentials: SmsLoginCredentials]; contactChange: [] }>()
const credentials = reactive<SmsLoginCredentials>({ mobile: '', country_code: '86', otp: '' })
const validContact = computed(() => /^\d{5,20}$/.test(credentials.mobile) && /^[1-9][0-9]{0,3}$/.test(credentials.country_code))
const validOtp = computed(() => /^\d{6}$/.test(credentials.otp))
watch(() => [credentials.mobile, credentials.country_code], () => {
  credentials.otp = ''
  emit('contactChange')
}, { flush: 'sync' })
function send() {
  if (props.disabled || props.busy || props.resendAfterSeconds > 0 || !validContact.value) return
  emit('send', { mobile: credentials.mobile, country_code: credentials.country_code })
}
function submit() {
  if (props.disabled || props.busy || !validContact.value || !validOtp.value) return
  emit('login', { ...credentials })
}
</script>

<template>
  <form class="sms-login-form" @submit.prevent="submit">
    <VanCellGroup inset>
      <VanField v-model="credentials.mobile" name="mobile" label="手机号" placeholder="请输入手机号" type="tel" inputmode="numeric" autocomplete="tel-national" maxlength="20" :disabled="disabled" />
      <VanField v-model="credentials.otp" name="otp" label="验证码" placeholder="请输入6位短信验证码" inputmode="numeric" autocomplete="one-time-code" maxlength="6" :disabled="disabled || busy">
        <template #button>
          <VanButton class="send-code" native-type="button" size="small" plain type="primary" aria-label="获取验证码" :disabled="disabled || busy || !validContact || resendAfterSeconds > 0" @click="send">{{ resendAfterSeconds > 0 ? `${resendAfterSeconds}秒后重发` : '获取验证码' }}</VanButton>
        </template>
      </VanField>
    </VanCellGroup>
    <p v-if="resendAfterSeconds > 0" class="sent-status" role="status">验证码已发送，请查看短信。</p>
    <VanButton class="submit" native-type="submit" type="primary" block :disabled="disabled || !validContact || !validOtp" :loading="busy">登录游戏</VanButton>
  </form>
</template>

<style scoped>
.sms-login-form { display: grid; gap: 20px; }
.sms-login-form :deep(.van-cell-group--inset) { margin: 0; }
.send-code { padding: 0 8px; border-radius: 8px; }
.submit { border-radius: 10px; }
.sent-status { margin: -6px 0 0; color: var(--text-muted); font-size: 13px; line-height: 1.6; }
</style>
