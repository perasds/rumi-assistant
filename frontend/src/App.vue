<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, shallowRef, watch } from 'vue'
import { createMemoryHistory, routerKey } from 'vue-router'
import { NoticeBar as VanNoticeBar, showToast } from 'vant'
import { useGameSession } from './composables/useGameSession'
import BottomNavigation from './components/layout/BottomNavigation.vue'
import OrderView from './views/OrderView.vue'
import LoginView from './views/LoginView.vue'
import MarketView from './views/MarketView.vue'
import PetBookView from './views/PetBookView.vue'
import MyView from './views/MyView.vue'
import ManualCaptcha from './components/auth/ManualCaptcha.vue'
import { createAppRouter, menuPaths, safeReturnPath, type AppMenu, type OrderTab } from './router'
const { capabilities, session, error, busy, smsResendAfterSeconds, qrBusy, qrChallenge, qrState, connected, initialize, login, sendSms, loginSms, cancelLogin, resumeLogin, createQr, checkQr, selectRole, logout, expire,
  captchaChallenge, captchaCancelling, captchaError, sendCaptchaInput, cancelCaptcha } = useGameSession()
const gameIcon = '/app-icon.svg'
const injectedRouter = inject(routerKey, null)
const router = injectedRouter ?? createAppRouter(createMemoryHistory())
if (!injectedRouter) {
  void router.replace('/book')
  onUnmounted(() => router.options.history.destroy())
}
const route = router.currentRoute
const activeMenu = computed(() => route.value.meta.menu ?? 'book')
const orderTab = computed(() => route.value.meta.orderTab ?? 'orders')
const tradeVisited = shallowRef(false)
const showingBook = computed(() => activeMenu.value === 'book' && route.value.path !== '/login')
const showingLogin = computed(() => !connected.value && (route.value.path === '/login' || route.value.meta.requiresAuth))
function selectMenu(menu: AppMenu) {
  void router.push(menuPaths[menu])
}
function selectOrderTab(tab: OrderTab) {
  void router.push(tab === 'inspection' ? '/orders/inspection' : '/orders')
}
async function logoutToBook() {
  await logout()
  if (session.value.status === 'anonymous' || session.value.status === 'expired') await router.replace('/book')
}
watch(connected, value => {
  if (!value) tradeVisited.value = false
}, { flush: 'sync' })
watch(() => [connected.value, route.value.fullPath], () => {
  if (connected.value && route.value.path === '/login') void router.replace(safeReturnPath(route.value.query.redirect))
}, { immediate: true })
watch(() => connected.value && activeMenu.value === 'trade', active => {
  if (active) tradeVisited.value = true
}, { immediate: true, flush: 'sync' })
watch(captchaError, message => { if (message) showToast({ message, duration: 2500 }) })
watch(session, (current, previous) => {
  if (current.status === 'expired' && previous.authenticated) {
    showToast({ message: '登录已过期，请重新登录', duration: 3_000 })
  }
})
onMounted(initialize)
</script>
<template>
  <div class="app-shell" :class="{ 'is-connected': connected, 'is-content-view': !showingLogin }">
    <header class="app-header">
      <div class="brand"><img class="brand-icon" :src="gameIcon" alt="噜咪小助手图标"><span>噜咪小助手</span></div>
    </header>
    <VanNoticeBar v-if="error && (connected || !showingLogin)" class="session-error" role="alert" wrapable :scrollable="false" :text="error" />
    <VanNoticeBar v-if="connected && session.message" class="session-notice" role="status" wrapable :scrollable="false" :text="session.message" />
    <PetBookView v-show="showingBook" :active="showingBook" @expired="expire" />
    <template v-if="connected">
      <MarketView v-if="tradeVisited" v-show="activeMenu === 'trade'" :session="session" @expired="expire" @logout="logoutToBook" />
      <MyView v-if="activeMenu === 'mine'" :logout-busy="busy" @expired="expire" @logout="logoutToBook" />
      <OrderView v-if="activeMenu === 'orders'" :active-tab="orderTab" @tab-change="selectOrderTab" @expired="expire" />
    </template>
    <LoginView v-if="showingLogin" :capabilities="capabilities" :session="session" :error="error" :busy="busy" :sms-resend-after-seconds="smsResendAfterSeconds" :qr-busy="qrBusy" :qr-challenge="qrChallenge" :qr-state="qrState" @login="login" @sms-send="sendSms" @sms-login="loginSms" @cancel-login="cancelLogin" @resume-login="resumeLogin" @qr-start="createQr" @qr-check="checkQr" @select-role="selectRole" @reload="initialize" @logout="logoutToBook" />
    <BottomNavigation :model-value="activeMenu" @update:model-value="selectMenu" />
    <ManualCaptcha v-if="captchaChallenge" :challenge="captchaChallenge" :cancelling="captchaCancelling" @input="sendCaptchaInput" @cancel="cancelCaptcha" />
  </div>
</template>
<style scoped>
.app-shell { min-height: 100svh; padding-bottom: calc(100px + env(safe-area-inset-bottom)); }
.app-shell.is-content-view { display: flex; flex-direction: column; height: 100vh; height: 100dvh; min-height: 0; overflow: hidden; }
.app-shell.is-content-view > .app-header { width: 100%; flex-shrink: 0; }
.session-error, .session-notice { width: 100%; max-width: 980px; margin: 0 auto; flex-shrink: 0; }
.app-header { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 16px; max-width: 980px; min-height: 84px; padding: 14px 24px; margin: 0 auto; border-bottom: 1px solid var(--border); }
.brand { grid-column: 2; display: flex; align-items: center; justify-content: center; gap: 11px; min-width: 0; font-size: 18px; font-weight: 750; color: var(--ink); white-space: nowrap; }
.brand-icon { width: 44px; height: 44px; flex: 0 0 auto; object-fit: contain; border-radius: 13px; box-shadow: 0 3px 0 #e5d8c7; }
@media (max-width: 480px) {
  .app-header { min-height: 72px; padding: 12px; gap: 8px; }
  .brand { font-size: 15px; gap: 8px; }
  .brand-icon { width: 34px; height: 34px; border-radius: 10px; }
}
</style>
