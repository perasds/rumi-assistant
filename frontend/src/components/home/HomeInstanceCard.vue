<script setup lang="ts">
import { computed } from 'vue'
import { Button as VanButton } from 'vant'
import type { HomeBuildingState, HomeProductionBuilding, HomeWorker } from '../../api/types'
import type { HomeBuildingDraft } from '../../composables/useHomeManagement'

const props = defineProps<{ building: HomeBuildingState; definition?: HomeProductionBuilding; roster: HomeWorker[];
  draft?: HomeBuildingDraft; disabled: boolean; resultMessage?: string }>()
const emit = defineEmits<{ recipe: [id: string]; count: [text: string]; workers: [] }>()
const recipes = computed(() => props.definition?.recipes.filter(recipe => props.building.allowed_recipe_ids?.includes(recipe.recipe_id)) ?? [])
const currentRecipe = computed(() => props.definition?.recipes.find(recipe => recipe.recipe_id === props.building.current_recipe_id))
const selectedRecipe = computed(() => props.draft?.recipeId ?? props.building.current_recipe_id ?? '')
const selectedCount = computed(() => props.draft?.countInput ?? (props.draft?.recipeId ? '' : String(props.building.production_count ?? '')))
const workers = computed(() => props.building.assigned_lumi_uids.map(uid => ({ uid, worker: props.roster.find(worker => worker.lumi_uid === uid) })))
const selectedWorkers = computed(() => props.draft?.workerUids?.map(uid => props.roster.find(worker => worker.lumi_uid === uid)?.name ?? `噜咪实例 #${uid}`).join('、'))
const productLabel = (recipe: NonNullable<typeof props.definition>['recipes'][number]) => recipe.products.map(item => `${item.name} × ${item.count}`).join('、')
</script>

<template>
  <article class="home-instance-card" :data-building-uid="building.building_uid">
    <header class="instance-heading">
      <h2 class="instance-name">{{ definition?.name ?? `建筑 #${building.building_id}` }}</h2>
      <span class="instance-level">{{ building.level === null ? '等级未同步' : `${building.level} 级` }}</span>
    </header>
    <p class="instance-meta">{{ definition?.island_name }} · 建筑实例 {{ building.building_uid }}</p>
    <div class="current-production">
      <span class="field-label">当前生产</span>
      <strong v-if="currentRecipe">{{ productLabel(currentRecipe) }}</strong>
      <strong v-else>{{ building.current_recipe_id ? `配方 #${building.current_recipe_id}` : '未设置生产物品' }}</strong>
      <small v-if="currentRecipe" class="base-yield">每次基础产出</small>
      <p v-if="building.quantity_mode === 'batch'" class="production-counts">计划次数 {{ building.production_count ?? '未同步' }} · 已完成 {{ building.completed_count ?? '未同步' }}</p>
      <p v-else-if="building.quantity_mode === 'continuous'" class="production-counts">持续生产</p>
      <p v-else class="production-counts">生产数量规则尚未核实</p>
    </div>
    <div class="current-workers">
      <span class="field-label">工作噜咪</span>
      <div v-for="entry in workers" :key="entry.uid" class="worker-summary">
        <img v-if="entry.worker?.image_url" class="worker-image" :src="entry.worker.image_url" alt="" loading="lazy">
        <div><strong>{{ entry.worker?.name ?? '未同步噜咪' }}</strong><small class="worker-uid">实例 {{ entry.uid }}</small></div>
      </div>
      <p v-if="!workers.length" class="muted">尚未派工</p>
    </div>
    <div class="instance-editor">
      <label class="editor-label">
        <span>生产物品</span>
        <select :value="selectedRecipe" :aria-label="`选择生产物品 - 建筑实例${building.building_uid}`"
          :disabled="disabled || !building.editable || !building.production_editable || building.quantity_mode === 'unverified' || !recipes.length"
          @change="emit('recipe', ($event.target as HTMLSelectElement).value)">
          <option v-if="!selectedRecipe" value="" disabled>选择生产物品</option>
          <option v-if="selectedRecipe && !recipes.some(recipe => recipe.recipe_id === selectedRecipe)" :value="selectedRecipe" disabled>当前配方 #{{ selectedRecipe }}</option>
          <option v-for="recipe in recipes" :key="recipe.recipe_id" :value="recipe.recipe_id">{{ productLabel(recipe) }}</option>
        </select>
      </label>
      <label v-if="building.quantity_mode === 'batch'" class="editor-label">
        <span>生产次数</span>
        <input type="number" inputmode="numeric" min="1" max="2147483647" step="1" placeholder="填写正整数次数" :value="selectedCount"
          :aria-label="`生产次数 - 建筑实例${building.building_uid}`" :disabled="disabled || !building.editable || !building.production_editable"
          @input="emit('count', ($event.target as HTMLInputElement).value)">
      </label>
      <VanButton class="worker-select" plain size="small" type="primary" aria-label="选择工作噜咪"
        :disabled="disabled || !building.editable || !building.assignment_limit" @click="emit('workers')">选择工作噜咪</VanButton>
      <p v-if="selectedWorkers" class="draft-note">待应用工人：{{ selectedWorkers }}</p>
      <p v-if="draft" class="draft-note">有未应用的修改</p>
      <p v-if="building.reason" class="muted">{{ building.reason }}</p>
      <p v-if="building.production_reason" class="muted">{{ building.production_reason }}</p>
      <p v-if="resultMessage" class="instance-result">{{ resultMessage }}</p>
    </div>
  </article>
</template>

<style scoped>
.home-instance-card { min-width: 0; padding: 20px; border: 1px solid #eadcbf; border-radius: 20px; background: #fffefd; }
.instance-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.instance-name { margin: 0; font-size: 19px; overflow-wrap: anywhere; }
.instance-level { flex-shrink: 0; color: var(--gold); font-size: 13px; }
.instance-meta, .muted, .production-counts { color: var(--text-muted); font-size: 12px; line-height: 1.6; }
.instance-meta { margin: 8px 0 18px; overflow-wrap: anywhere; }
.current-production { padding: 12px; border-radius: 12px; background: var(--accent-light); font-size: 14px; }
.field-label { display: block; margin-bottom: 7px; color: var(--text-muted); font-size: 12px; }
.production-counts { margin: 8px 0 0; }
.base-yield { display: block; margin-top: 5px; color: var(--text-muted); font-size: 11px; }
.current-workers { margin: 16px 0; }
.worker-summary { display: flex; align-items: center; gap: 9px; margin: 8px 0; font-size: 14px; }
.worker-image { width: 40px; height: 40px; object-fit: contain; border-radius: 10px; background: var(--surface-muted); }
.worker-uid { display: block; margin-top: 4px; color: var(--text-muted); font-size: 11px; overflow-wrap: anywhere; }
.instance-editor { padding-top: 14px; border-top: 1px dashed var(--border); }
.editor-label { display: grid; gap: 7px; margin-bottom: 12px; color: var(--text-muted); font-size: 12px; }
.editor-label select, .editor-label input { width: 100%; min-width: 0; min-height: 40px; padding: 9px 10px; color: var(--ink); background: var(--surface); border: 1px solid var(--border); border-radius: 9px; font: inherit; font-size: 14px; }
.editor-label :disabled { opacity: .65; }
.worker-select { border-radius: 9px; }
.draft-note, .instance-result { margin: 9px 0 0; color: var(--accent-deep); font-size: 12px; line-height: 1.6; }
@media (max-width: 640px) { .home-instance-card { padding: 16px; } }
</style>
