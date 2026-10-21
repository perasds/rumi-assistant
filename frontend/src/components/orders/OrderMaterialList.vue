<script setup lang="ts">
import { computed } from 'vue'
import type { OrderPlan } from '../../api/orderTypes'
const props = defineProps<{ materials: OrderPlan['materials']; baseRequirements?: OrderPlan['base_requirements'] }>()
const baseMaterials = computed(() => Object.entries(props.baseRequirements ?? {}))
</script>

<template>
  <section aria-label="缺料清单">
    <h2 class="section-title">物资需求与缺料</h2>
    <p v-if="!materials.length" class="hint">当前方案没有缺料需求。</p>
    <ul v-else class="materials">
      <li v-for="item in materials" :key="item.item_id" class="material">
        <strong class="name">{{ item.name || `物品 ${item.item_id}` }}</strong><span>需要 {{ item.required }} · 可用 {{ item.available ?? '未同步' }} · 缺少 {{ item.shortage ?? '待计算' }}</span>
      </li>
    </ul>
    <template v-if="baseRequirements !== undefined">
      <h3 class="base-title">上游基础物资</h3>
      <p v-if="baseRequirements === null" class="hint">上游需求暂不可计算，请查看方案提示。</p>
      <p v-else-if="!baseMaterials.length" class="hint">当前没有额外的上游基础物资需求。</p>
      <ul v-else class="materials"><li v-for="[id, count] in baseMaterials" :key="id" class="material">物品 {{ id }} × {{ count }}</li></ul>
    </template>
  </section>
</template>

<style scoped>
.section-title { margin: 0 0 14px; font-size: 18px; }
.materials { list-style: none; padding: 0; margin: 0; }
.material { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; border: 1px solid var(--border); border-radius: 12px; padding: 14px; background: var(--surface); font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
.name { font-size: 14px; }
.base-title { margin: 18px 0 10px; font-size: 14px; }
.hint { color: var(--text-muted); font-size: 13px; }
</style>
