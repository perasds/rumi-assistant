<script setup lang="ts">
import { Button as VanButton, Loading as VanLoading } from 'vant'
import PersonalInfoCard from '../components/profile/PersonalInfoCard.vue'
import AutoRedbagPanel from '../components/profile/AutoRedbagPanel.vue'
import { usePersonalInfo } from '../composables/usePersonalInfo'

defineProps<{ logoutBusy?: boolean }>()
const emit = defineEmits<{ expired: []; logout: [] }>()
let expired = false
function onExpired() {
  if (expired) return
  expired = true
  emit('expired')
}
const { info, loading, error, load } = usePersonalInfo(onExpired)
</script>

<template>
  <main class="my-view" aria-label="个人信息" :aria-busy="loading">
    <div class="page-heading">
      <h1 class="heading">我的</h1>
      <VanButton plain size="small" type="primary" :loading="loading" :disabled="loading" aria-label="刷新个人信息" @click="load">刷新</VanButton>
    </div>
    <div class="profile-content">
      <VanLoading v-if="loading && !info" class="profile-loading" size="22">正在加载个人信息</VanLoading>
      <p v-if="error" class="profile-error" role="alert">{{ error }}</p>
      <PersonalInfoCard v-if="info" :info="info" />
      <AutoRedbagPanel @expired="onExpired" />
      <VanButton class="profile-logout" block plain type="primary" :loading="logoutBusy" :disabled="logoutBusy" aria-label="退出登录" @click="emit('logout')">退出登录</VanButton>
    </div>
  </main>
</template>

<style scoped>
.my-view { display: flex; flex: 1; flex-direction: column; min-height: 0; width: 100%; max-width: 900px; margin: 30px auto 0; padding: 0 24px 24px; overflow: hidden; }
.page-heading { display: flex; flex-shrink: 0; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 24px; }
.heading { margin: 0; font-size: 28px; letter-spacing: -.5px; }
.profile-content { min-height: 0; overflow-y: auto; overscroll-behavior-y: contain; }
.profile-logout { margin-top: 24px; border-radius: 12px; background: var(--surface); }
.profile-loading { display: flex; justify-content: center; padding: 44px 12px; }
.profile-error { margin: 0; padding: 16px; border-radius: 12px; color: var(--danger); background: #fff0e9; font-size: 14px; line-height: 1.6; }
@media (max-width: 640px) { .my-view { padding: 0 16px 20px; margin-top: 24px; } .heading { font-size: 25px; } }
</style>
