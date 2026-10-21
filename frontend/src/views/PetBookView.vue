<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { Button as VanButton, Loading as VanLoading, Search as VanSearch } from 'vant'
import PetBookGrid from '../components/pet-book/PetBookGrid.vue'
import PetElementFilter from '../components/pet-book/PetElementFilter.vue'
import PetDetailPopup from '../components/pet-book/PetDetailPopup.vue'
import { usePetBook } from '../composables/usePetBook'
import type { PetElement } from '../api/types'

const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ expired: [] }>()
const query = shallowRef('')
const selectedElement = shallowRef('')
const { catalog, catalogLoading, catalogError, detailVisible, selectedPet, detail, detailLoading, detailError,
  loadCatalog, selectPet, loadDetail, closeDetail, finishCloseDetail } = usePetBook({ active: () => props.active, onUnauthorized: () => emit('expired') })
const elementOptions = computed(() => {
  const elements = new Map<number, PetElement>()
  for (const pet of catalog.value?.items ?? []) {
    for (const element of pet.elements ?? []) elements.set(element.id, element)
  }
  return [...elements.values()].sort((left, right) => left.id - right.id)
})
const visiblePets = computed(() => {
  const term = query.value.trim().toLocaleLowerCase()
  const elementId = selectedElement.value === '' ? null : Number(selectedElement.value)
  const matches = catalog.value?.items.filter(pet =>
    (elementId === null || (pet.elements ?? []).some(element => element.id === elementId))
    && (!term || pet.display_name.toLocaleLowerCase().includes(term)
      || pet.name?.toLocaleLowerCase().includes(term) || pet.pet_id.includes(term))) ?? []
  return matches.reverse()
})
</script>

<template>
  <main class="pet-book-view" aria-label="噜咪图鉴">
    <h1 class="heading">噜咪图鉴</h1>
    <VanSearch v-model="query" class="book-search" placeholder="搜索名称或 ID" aria-label="搜索图鉴名称或ID" />
    <PetElementFilter v-model="selectedElement" :elements="elementOptions" :disabled="catalogLoading || !catalog" />
    <section class="pet-book-scroll" aria-label="图鉴目录" tabindex="0">
      <VanLoading v-if="catalogLoading" class="book-loading" size="22">正在加载图鉴</VanLoading>
      <div v-else-if="catalogError" class="book-state">
        <p role="alert">{{ catalogError }}</p>
        <VanButton size="small" plain type="primary" aria-label="重试加载图鉴" @click="loadCatalog">重试</VanButton>
      </div>
      <PetBookGrid v-else-if="catalog" :pets="visiblePets" @select="selectPet" />
    </section>
    <PetDetailPopup :show="detailVisible" :pet="selectedPet" :detail="detail" :loading="detailLoading" :error="detailError" @close="closeDetail" @closed="finishCloseDetail" @retry="loadDetail" />
  </main>
</template>

<style scoped>
.pet-book-view { display: flex; flex: 1; flex-direction: column; min-height: 0; width: 100%; max-width: 900px; margin: 30px auto 0; padding: 0 24px 24px; }
.heading { flex-shrink: 0; margin: 0 0 20px; font-size: 28px; letter-spacing: -.5px; }
.book-search { flex-shrink: 0; padding: 0; background: transparent; --van-search-content-background: var(--surface); --van-search-left-icon-color: var(--accent-deep); }
.book-search :deep(.van-search__content) { border: 1px solid var(--border); border-radius: 12px; }
.pet-book-scroll { flex: 1; min-height: 0; margin-top: 20px; overflow-y: auto; overscroll-behavior-y: contain; scrollbar-gutter: stable; }
.book-loading, .book-state { padding: 44px 12px; text-align: center; color: var(--text-muted); }
@media (max-width: 640px) { .pet-book-view { padding: 0 16px 20px; margin-top: 24px; } .heading { font-size: 25px; } }
</style>
