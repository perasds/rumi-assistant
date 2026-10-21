<script setup lang="ts">
import { computed } from 'vue'
import { Search as VanSearch, Image as VanImage, Loading as VanLoading, Button as VanButton } from 'vant'
import type { PetCatalog } from '../../api/types'
const props = defineProps<{ catalog: PetCatalog | null; busy: boolean; error: string | null; activeQuery?: string }>()
const emit = defineEmits<{ search: [] }>()
const query = defineModel<string>('query', { required: true })
const petIds = defineModel<string[]>('petIds', { required: true })
const matchesActiveQuery = computed(() => props.activeQuery === undefined || query.value.trim() === props.activeQuery)
const matchedPets = computed(() => query.value.trim() && matchesActiveQuery.value ? props.catalog?.items.filter(pet => pet.name_verified) ?? [] : [])
function submitSearch() {
  if (!props.busy) emit('search')
}
function toggle(id: string) {
  petIds.value = petIds.value.includes(id) ? petIds.value.filter(value => value !== id) : [...petIds.value, id]
}
</script>
<template>
  <section class="pet-search" aria-label="宠物名称筛选">
    <VanSearch v-model="query" shape="round" placeholder="搜索宠物中文名" aria-label="搜索宠物中文名" show-action :disabled="busy" @search="submitSearch">
      <template #action><VanButton class="search-submit" native-type="button" type="primary" aria-label="搜索商品" :disabled="busy" @click="submitSearch">搜索</VanButton></template>
    </VanSearch>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <VanLoading v-if="busy" class="loading" size="18">查询中</VanLoading>
    <div v-if="matchedPets.length" class="pet-options">
      <button v-for="pet in matchedPets" :key="pet.pet_id" class="pet-option" type="button" :data-pet-id="pet.pet_id" :aria-pressed="petIds.includes(pet.pet_id)" :disabled="busy" @click="toggle(pet.pet_id)">
        <VanImage class="pet-image" width="36" height="36" :src="pet.image_url ?? undefined" fit="contain" :alt="pet.display_name"><template #error>?</template></VanImage>
        <span>{{ pet.display_name }}<small>ID {{ pet.pet_id }}</small></span>
      </button>
      <button v-if="petIds.length" class="clear-selection" type="button" @click="petIds = []">全部形态</button>
    </div>
    <p v-else-if="query.trim() && matchesActiveQuery && catalog && !busy" class="hint">暂无已核实名称匹配，不能据此判断交易行没有商品。</p>
  </section>
</template>
<style scoped>
.pet-search { padding: 20px; background: #fff; border: 1px solid var(--border); border-radius: 20px; }
.pet-search :deep(.van-search) { padding: 0; background: transparent; }
.pet-search :deep(.van-search__content) { padding: 3px 12px; border: 1px solid var(--border); background: var(--field-background); }
.pet-search :deep(.van-field__left-icon) { color: var(--accent); }
.search-submit { height: 36px; min-height: 36px; padding: 0 14px; border-radius: 10px; }
.pet-options { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
.pet-option { display: flex; align-items: center; gap: 8px; border: 1px solid var(--border); background: #fff; border-radius: 12px; padding: 8px 12px; text-align: left; cursor: pointer; }
.pet-option:hover:not(:disabled) { border-color: var(--accent); }
.pet-option[aria-pressed='true'] { border-color: var(--accent); background: var(--accent-light); }
.pet-option small { display: block; margin-top: 2px; color: var(--text-muted); font-size: 10px; }
.clear-selection { border: 0; background: var(--accent-light); color: var(--accent); border-radius: 12px; padding: 10px 14px; cursor: pointer; }
.hint, .error { margin: 12px 4px 0; font-size: 12px; line-height: 1.7; color: var(--text-muted); }
.error { color: var(--danger); }
.loading { margin-top: 12px; }
@media (max-width: 520px) { .pet-search { padding: 16px; border-radius: 18px; } }
</style>
