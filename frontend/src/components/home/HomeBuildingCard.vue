<script setup lang="ts">
import { Icon as VanIcon } from 'vant'
import type { HomeProductionBuilding } from '../../api/types'

defineProps<{ building: HomeProductionBuilding }>()
</script>

<template>
  <article class="home-building-card" :data-building-id="building.building_id" :aria-label="building.name">
    <header class="building-header">
      <h2 class="building-name">{{ building.name }}</h2>
      <div class="building-meta">
        <span class="island-label">{{ building.island_name }}</span>
        <span v-if="building.work_type" class="work-label">{{ building.work_type }}</span>
      </div>
    </header>
    <div class="building-recipes">
      <details v-for="recipe in building.recipes" :key="recipe.recipe_id" class="home-recipe" :data-recipe-id="recipe.recipe_id">
        <summary class="recipe-summary">
          <span class="recipe-products">
            <span v-for="product in recipe.products" :key="product.item_id" class="recipe-item">
              <img v-if="product.image_url" class="item-image" :src="product.image_url" alt="" loading="lazy">
              <span class="item-name">{{ product.name }} × {{ product.count }}</span>
            </span>
          </span>
          <span class="recipe-caption">
            <span class="recipe-level">建筑 {{ recipe.min_building_level }} 级起</span>
            <span class="materials-toggle">材料 <VanIcon class="recipe-chevron" name="arrow-down" aria-hidden="true" /></span>
          </span>
        </summary>
        <div class="recipe-materials">
          <h3 class="materials-heading">所需材料</h3>
          <ul v-if="recipe.materials.length" class="materials-list">
            <li v-for="material in recipe.materials" :key="material.item_id" class="recipe-item">
              <img v-if="material.image_url" class="item-image" :src="material.image_url" alt="" loading="lazy">
              <span class="item-name">{{ material.name }} × {{ material.count }}</span>
            </li>
          </ul>
          <p v-else class="no-materials">无需材料</p>
        </div>
      </details>
    </div>
  </article>
</template>

<style scoped>
.home-building-card { min-width: 0; overflow: hidden; padding: 20px; border: 1px solid #eadcbf; border-radius: 22px; background: #fffefd; }
.building-header { margin-bottom: 16px; }
.building-name { margin: 0 0 10px; color: var(--ink); font-size: 19px; line-height: 1.4; overflow-wrap: anywhere; }
.building-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }
.island-label { color: var(--text-muted); }
.work-label { padding: 3px 8px; border-radius: 7px; color: var(--accent-deep); background: var(--accent-light); }
.building-recipes { display: grid; gap: 10px; }
.home-recipe { min-width: 0; border: 1px solid var(--border); border-radius: 13px; background: var(--surface); }
.recipe-summary { display: flex; flex-direction: column; gap: 11px; padding: 12px; cursor: pointer; list-style: none; border-radius: 13px; }
.recipe-summary::-webkit-details-marker { display: none; }
.recipe-summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.recipe-products, .materials-list { display: grid; gap: 10px; }
.recipe-item { display: flex; align-items: center; gap: 10px; min-width: 0; font-size: 14px; line-height: 1.5; }
.recipe-products .recipe-item { font-weight: 650; }
.item-image { flex: 0 0 auto; width: 34px; height: 34px; object-fit: contain; border-radius: 8px; background: var(--surface-muted); }
.item-name { overflow-wrap: anywhere; }
.recipe-caption { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; }
.recipe-level { color: var(--text-muted); }
.materials-toggle { display: inline-flex; align-items: center; gap: 5px; color: var(--accent-deep); }
.recipe-chevron { transition: transform .15s ease; }
.home-recipe[open] .recipe-chevron { transform: rotate(180deg); }
.recipe-materials { margin: 0 12px; padding: 12px 0; border-top: 1px dashed var(--border); }
.materials-heading { margin: 0 0 10px; font-size: 12px; font-weight: 500; color: var(--text-muted); }
.materials-list { margin: 0; padding: 0; list-style: none; }
.no-materials { margin: 0; font-size: 13px; color: var(--text-muted); }
@media (max-width: 640px) { .home-building-card { padding: 16px; border-radius: 18px; } .building-name { font-size: 18px; } }
@media (prefers-reduced-motion: reduce) { .recipe-chevron { transition: none; } }
</style>
