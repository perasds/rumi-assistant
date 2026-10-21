<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, shallowRef, watch } from 'vue'
import { Button as VanButton, Tabs as VanTabs, Tab as VanTab, NoticeBar as VanNoticeBar, Loading as VanLoading } from 'vant'
import type { AuthCapabilities, LoginCredentials, LoginMethod, QrChallenge, QrState, Session, SmsContact, SmsLoginCredentials } from '../api/types'
import GameLoginForm from '../components/auth/GameLoginForm.vue'
import SmsLoginForm from '../components/auth/SmsLoginForm.vue'
import QrLoginCard from '../components/auth/QrLoginCard.vue'
import RoleSelector from '../components/auth/RoleSelector.vue'
const appIcon = '/app-icon.svg'
const props = withDefaults(defineProps<{ capabilities: AuthCapabilities | null; session: Session; error: string | null; busy: boolean; smsResendAfterSeconds?: number; qrBusy: boolean; qrChallenge: QrChallenge | null; qrState: QrState | null }>(), { smsResendAfterSeconds: 0 })
const emit = defineEmits<{ login: [credentials: LoginCredentials]; smsSend: [contact: SmsContact]; smsLogin: [credentials: SmsLoginCredentials]; cancelLogin: []; resumeLogin: []; qrStart: []; qrCheck: []; selectRole: [roleId: string]; reload: []; logout: [] }>()
const tab = shallowRef<LoginMethod>('account')
const methods = computed(() => props.capabilities?.integration_ready ? props.capabilities.methods : [])
watch(methods, value => { if (value.length && !value.includes(tab.value)) tab.value = value[0]! }, { immediate: true })
function cancelPendingLogin() { emit('cancelLogin'); emit('resumeLogin') }
watch(tab, cancelPendingLogin, { flush: 'sync' })
onMounted(() => emit('resumeLogin'))
onBeforeUnmount(() => emit('cancelLogin'))
</script>
<template>
  <main class="login-view">
    <div class="intro"><img class="app-symbol" :src="appIcon" width="96" height="96" alt="噜咪小助手"><h1 class="heading">噜咪小助手</h1></div>
    <section class="login-panel">
      <VanLoading v-if="busy && !capabilities" class="initial-loading">读取登录状态</VanLoading>
      <template v-else-if="session.status === 'role_selection'">
        <RoleSelector :roles="session.roles" :busy="busy" @select="emit('selectRole', $event)" />
        <VanButton class="secondary" block plain :disabled="busy" @click="emit('logout')">退出登录</VanButton>
      </template>
      <VanNoticeBar v-else-if="session.status === 'needs_verification'" wrapable :scrollable="false" :text="session.message ?? '游戏要求额外身份验证，请完成官方认证流程。'" />
      <template v-else-if="methods.length">
        <VanTabs v-model:active="tab" class="login-tabs" color="var(--accent)">
          <VanTab v-if="methods.includes('account')" title="密码登录" name="account"><GameLoginForm v-if="tab === 'account'" :disabled="!methods.includes('account')" :busy="busy" @login="emit('login', $event)" /></VanTab>
          <VanTab v-if="methods.includes('sms')" title="验证码登录" name="sms"><SmsLoginForm v-if="tab === 'sms'" :disabled="!methods.includes('sms')" :busy="busy" :resend-after-seconds="smsResendAfterSeconds" @send="emit('smsSend', $event)" @login="emit('smsLogin', $event)" @contact-change="cancelPendingLogin" /></VanTab>
          <VanTab v-if="methods.includes('qr')" title="扫码登录" name="qr"><QrLoginCard :challenge="qrChallenge" :state="qrState" :busy="qrBusy" @start="emit('qrStart')" @check="emit('qrCheck')" /></VanTab>
        </VanTabs>
        <p v-if="busy && (tab === 'account' || tab === 'sms')" class="login-wait" role="status">请求处理中；如弹出官方验证页，请在该页手动完成验证。</p>
      </template>
      <div v-else class="unavailable" role="status">
        <span class="connection-icon" aria-hidden="true">○</span><h2 class="unavailable-title">游戏登录暂不可用</h2>
        <p class="reason">{{ capabilities?.message ?? '登录服务尚未就绪，请检查后端连接。' }}</p>
        <VanButton class="retry" plain type="primary" :loading="busy" @click="emit('reload')">重新检查</VanButton>
      </div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </section>
  </main>
</template>
<style scoped>
.login-view { max-width: 460px; margin: 38px auto 50px; padding: 0 20px; }
.intro { margin-bottom: 26px; text-align: center; }
.app-symbol { display: block; width: 96px; height: 96px; margin: 0 auto 10px; }
.heading { color: var(--ink); font-size: 26px; font-weight: 750; margin: 10px 0; letter-spacing: 1px; }
.login-panel { background: var(--surface); padding: 27px; border: 1px solid #e5dfcc; border-top: 4px solid #f1cf79; border-radius: 24px; box-shadow: 0 5px 0 #e7e6d8, 0 14px 32px #805c450a; }
.initial-loading { text-align: center; padding: 40px 0; }
.unavailable { text-align: center; padding: 15px 0; }
.connection-icon { display: flex; width: 56px; height: 56px; margin: 0 auto 22px; align-items: center; justify-content: center; border-radius: 16px; color: var(--accent); background: var(--accent-light); font-size: 32px; }
.unavailable-title { font-size: 19px; margin: 0 0 12px; }
.reason { line-height: 1.8; font-size: 13px; color: var(--text-muted); }
.retry { margin-top: 16px; min-width: 140px; border-radius: 9px; }
.error { color: var(--danger); font-size: 13px; line-height: 1.8; margin: 18px 0 0; }
.login-tabs :deep(.van-tabs__content) { padding-top: 25px; }
.secondary { margin-top: 16px; }
.login-wait { color: var(--text-muted); font-size: 13px; line-height: 1.8; margin: 18px 0 0; }
@media (max-width: 480px) { .login-view { margin-top: 24px; padding: 0 18px; } .login-panel { padding: 22px 18px; } .heading { font-size: 23px; } }
</style>
