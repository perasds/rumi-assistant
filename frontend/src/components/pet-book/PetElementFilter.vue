<script setup lang="ts">
import type { PetElement } from '../../api/types'

defineProps<{ elements: PetElement[]; disabled?: boolean }>()
const selected = defineModel<string>({ required: true })
</script>

<template>
  <label class="element-filter">
    <span>属性</span>
    <select v-model="selected" aria-label="筛选噜咪属性" :disabled="disabled || !elements.length">
      <option value="">全部属性</option>
      <option v-for="element in elements" :key="element.id" :value="String(element.id)">{{ element.name }}</option>
    </select>
  </label>
</template>

<style scoped>
.element-filter { display: flex; flex-shrink: 0; align-items: center; gap: 12px; margin-top: 12px; color: var(--text-muted); font-size: 14px; }
.element-filter select { min-width: 144px; max-width: 100%; min-height: 40px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 10px; color: var(--ink); background: var(--surface); font: inherit; cursor: pointer; }
.element-filter select:disabled { opacity: .5; cursor: default; }
.element-filter select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
