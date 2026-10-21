<script setup lang="ts">
type Menu = 'trade' | 'orders' | 'mine' | 'book'
defineProps<{ modelValue: Menu }>()
const emit = defineEmits<{ 'update:modelValue': [menu: Menu] }>()
const menus = [
  { value: 'trade', label: '交易' },
  { value: 'book', label: '图鉴' },
  { value: 'orders', label: '订单' },
  { value: 'mine', label: '我的' },
] as const
</script>

<template>
  <nav class="bottom-navigation" aria-label="功能导航">
    <button v-for="menu in menus" :key="menu.value" class="navigation-item" type="button" :aria-current="modelValue === menu.value ? 'page' : undefined" @click="emit('update:modelValue', menu.value)">
      <svg class="navigation-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path v-if="menu.value === 'trade'" d="M7 8V6a5 5 0 0 1 10 0v2M4 8h16l-1 13H5L4 8Z M9 12h.01M15 12h.01" />
        <template v-else-if="menu.value === 'orders'"><path d="M8 4H5v17h14V4h-3M9 2h6v4H9zM8 11h8M8 15h8" /></template>
        <template v-else-if="menu.value === 'mine'"><circle cx="12" cy="7" r="4" /><path d="M4 21v-2a8 8 0 0 1 16 0v2" /></template>
        <path v-else d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15" />
      </svg>
      <span>{{ menu.label }}</span>
    </button>
  </nav>
</template>

<style scoped>
.bottom-navigation { position: fixed; z-index: 10; bottom: 0; left: 50%; transform: translateX(-50%); width: 100%; max-width: 980px; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; padding: 9px 18px calc(9px + env(safe-area-inset-bottom)); border: 1px solid var(--border); border-bottom: 0; border-radius: 24px 24px 0 0; background: var(--surface); box-shadow: 0 -7px 26px #805c450b; }
.navigation-item { position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; min-width: 0; min-height: 58px; padding: 6px; color: var(--text-muted); font-size: 13px; font-weight: 650; border: 0; border-radius: 16px; background: transparent; cursor: pointer; }
.navigation-icon { width: 25px; height: 25px; }
.navigation-item[aria-current='page'] { color: var(--accent-deep); background: var(--accent-light); }
.navigation-item[aria-current='page']::before { position: absolute; top: -4px; width: 22px; height: 4px; border-radius: 4px; background: #e7ba5d; content: ''; }
@media (max-width: 480px) { .bottom-navigation { padding-right: 12px; padding-left: 12px; gap: 5px; } }
</style>
