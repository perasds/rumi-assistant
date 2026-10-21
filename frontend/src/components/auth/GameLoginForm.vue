<script setup lang="ts">
import { reactive, shallowRef, useId } from 'vue'
import { Button as VanButton, Field as VanField, CellGroup as VanCellGroup, Icon as VanIcon } from 'vant'
import type { LoginCredentials } from '../../api/types'
const props = defineProps<{ disabled: boolean; busy: boolean }>()
const emit = defineEmits<{ login: [credentials: LoginCredentials] }>()
const credentials = reactive<LoginCredentials>({ account: '', password: '' })
const passwordVisible = shallowRef(false)
const passwordId = `game-password-${useId()}`
function togglePasswordVisibility() {
  if (props.disabled || props.busy) return
  passwordVisible.value = !passwordVisible.value
}
function submit() {
  if (props.disabled || props.busy || !credentials.account.trim() || !credentials.password) return
  emit('login', { account: credentials.account.trim(), password: credentials.password })
  credentials.password = ''
  passwordVisible.value = false
}
</script>
<template>
  <form class="login-form" @submit.prevent="submit">
    <VanCellGroup inset>
      <VanField v-model="credentials.account" name="account" label="账号" placeholder="请输入账号" autocomplete="username" :disabled="disabled || busy" />
      <VanField :id="passwordId" v-model="credentials.password" name="password" :type="passwordVisible ? 'text' : 'password'" label="密码" placeholder="请输入密码" autocomplete="current-password" :disabled="disabled || busy">
        <template #button>
          <VanButton class="password-toggle" native-type="button" :disabled="disabled || busy" :aria-label="passwordVisible ? '隐藏密码' : '显示密码'" :aria-pressed="passwordVisible" :aria-controls="passwordId" @click="togglePasswordVisibility">
            <VanIcon :name="passwordVisible ? 'closed-eye' : 'eye-o'" aria-hidden="true" />
          </VanButton>
        </template>
      </VanField>
    </VanCellGroup>
    <VanButton class="submit" native-type="submit" type="primary" block :disabled="disabled" :loading="busy">登录游戏</VanButton>
  </form>
</template>
<style scoped>
.login-form { display: grid; gap: 20px; }
.login-form :deep(.van-cell-group--inset) { margin: 0; }
.password-toggle { width: 38px; height: 38px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--accent-deep, var(--accent)); font-size: 20px; }
.submit { border-radius: 10px; }
</style>
