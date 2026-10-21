<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ feature: 'adventure' | 'home' }>()
const label = computed(() => props.feature === 'home' ? '家园' : '冒险')
</script>

<template>
  <main class="feature-placeholder" :aria-label="label">
    <div class="placeholder-card">
      <svg class="placeholder-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path v-if="feature === 'home'" d="m3 11 9-8 9 8M5 10v11h14V10M9 21v-7h6v7" />
        <template v-else><circle cx="12" cy="12" r="9" /><path d="m16 8-2.5 5.5L8 16l2.5-5.5L16 8Z" /></template>
      </svg>
      <h1 class="placeholder-title">{{ label }}</h1>
      <p class="placeholder-state">暂未开放</p>
    </div>
  </main>
</template>

<style scoped>
.feature-placeholder { width: 100%; max-width: 600px; margin: 56px auto; padding: 0 20px; }
.placeholder-card { display: flex; flex-direction: column; align-items: center; padding: 60px 24px; border: 1px solid var(--border); border-radius: 26px; background: var(--surface); box-shadow: 0 5px 0 #e7e6d8; }
.placeholder-icon { width: 65px; height: 65px; padding: 13px; color: var(--accent); background: var(--accent-light); border-radius: 21px; }
.placeholder-title { margin: 22px 0 10px; color: var(--ink); font-size: 24px; }
.placeholder-state { margin: 0; color: var(--text-muted); font-size: 14px; }
@media (max-width: 480px) { .feature-placeholder { margin-top: 32px; } .placeholder-card { padding: 48px 20px; } }
</style>
