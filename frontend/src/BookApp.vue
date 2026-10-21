<script setup lang="ts">
import PetBookView from './views/PetBookView.vue'

const gameIcon = '/app-icon.svg'
</script>

<template>
  <div class="book-shell">
    <header class="book-header">
      <div class="brand"><img class="brand-icon" :src="gameIcon" alt="噜咪小助手图标"><span>噜咪小助手</span></div>
    </header>
    <PetBookView :active="true" />
  </div>
</template>

<style scoped>
.book-shell { display: flex; flex-direction: column; height: 100vh; height: 100dvh; min-height: 0; padding-bottom: env(safe-area-inset-bottom); overflow: hidden; }
.book-header { display: flex; flex-shrink: 0; align-items: center; justify-content: center; width: 100%; max-width: 980px; min-height: 84px; padding: 14px 24px; margin: 0 auto; border-bottom: 1px solid var(--border); }
.brand { display: flex; align-items: center; justify-content: center; gap: 11px; min-width: 0; font-size: 18px; font-weight: 750; color: var(--ink); white-space: nowrap; }
.brand-icon { width: 44px; height: 44px; flex: 0 0 auto; object-fit: contain; border-radius: 13px; box-shadow: 0 3px 0 #e5d8c7; }
@media (max-width: 480px) {
  .book-header { min-height: 72px; padding: 12px; }
  .brand { font-size: 15px; gap: 8px; }
  .brand-icon { width: 34px; height: 34px; border-radius: 10px; }
}
</style>
