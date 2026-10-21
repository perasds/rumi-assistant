<script setup lang="ts">
import type { Pet } from '../../api/types'
import PetBookImage from './PetBookImage.vue'

defineProps<{ pets: Pet[] }>()
const emit = defineEmits<{ select: [pet: Pet] }>()
</script>

<template>
  <div v-if="pets.length" class="pet-book-grid">
    <button v-for="pet in pets" :key="pet.pet_id" class="pet-card" type="button" :data-pet-id="pet.pet_id" @click="emit('select', pet)">
      <PetBookImage class="pet-image" :src="pet.image_url" :alt="pet.display_name" />
      <span class="pet-name">{{ pet.display_name }}</span>
      <span class="pet-id">ID {{ pet.pet_id }}</span>
    </button>
  </div>
  <p v-else class="empty-pets">没有匹配的噜咪</p>
</template>

<style scoped>
.pet-book-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 2px 2px 12px; }
.pet-card { display: flex; align-items: center; flex-direction: column; min-width: 0; padding: 14px 7px 12px; border: 1px solid var(--border); border-radius: 16px; color: var(--ink); background: var(--surface); cursor: pointer; }
.pet-image { width: 76px; height: 76px; margin-bottom: 9px; }
.pet-name { max-width: 100%; font-size: 14px; font-weight: 650; overflow-wrap: anywhere; }
.pet-id { margin-top: 6px; color: var(--text-muted); font-size: 11px; overflow-wrap: anywhere; }
.empty-pets { padding: 44px 12px; text-align: center; color: var(--text-muted); }
@media (min-width: 640px) { .pet-book-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; } .pet-image { width: 100px; height: 100px; } }
@media (min-width: 900px) { .pet-book-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); } }
@media (max-width: 380px) { .pet-book-grid { gap: 8px; } .pet-image { width: 64px; height: 64px; } .pet-name { font-size: 12px; } }
</style>
