<script setup lang="ts">
import { computed, shallowRef, watch } from 'vue'
import { Image as VanImage } from 'vant'
import type { PetSkill } from '../../api/types'

const props = defineProps<{ fixedSkill: PetSkill | null; replaceableSkills: PetSkill[] }>()
const selectedId = shallowRef<string | null>(null)
const groups = computed(() => [
  { key: 'fixed', label: '固定技能', skills: props.fixedSkill ? [props.fixedSkill] : [] },
  { key: 'replaceable', label: '可替换技能池', skills: props.replaceableSkills },
])
const selectedSkill = computed(() => groups.value.flatMap(group => group.skills).find(skill => skill.skill_id === selectedId.value) ?? null)
watch(() => [props.fixedSkill, props.replaceableSkills], () => { selectedId.value = props.fixedSkill?.skill_id ?? null }, { immediate: true })
</script>

<template>
  <div class="skills-panel">
    <section v-for="group in groups" :key="group.key" :class="`${group.key}-skills`">
      <h3 class="section-title">{{ group.label }}</h3>
      <div v-if="group.skills.length" class="skill-options">
        <button v-for="skill in group.skills" :key="skill.skill_id" class="skill-option" type="button" :data-skill-id="skill.skill_id" :aria-pressed="selectedId === skill.skill_id" @click="selectedId = skill.skill_id">
          <VanImage v-if="skill.image_url" class="skill-image" :src="skill.image_url" :alt="skill.name" fit="contain" />
          <span class="skill-summary">
            <span class="skill-name">{{ skill.name }}</span>
            <span class="skill-cost">消耗 {{ skill.cost_text ?? '—' }}</span>
          </span>
        </button>
      </div>
      <p v-else class="unknown">未收录</p>
    </section>
    <section v-if="selectedSkill" class="skill-detail" aria-live="polite">
      <h3 class="section-title">{{ selectedSkill.name }}</h3>
      <dl class="skill-values"><div><dt>元素</dt><dd>{{ selectedSkill.element?.name ?? '—' }}</dd></div><div><dt>威力</dt><dd>{{ selectedSkill.power ?? '—' }}</dd></div><div><dt>消耗</dt><dd>{{ selectedSkill.cost_text ?? '—' }}</dd></div></dl>
      <p class="skill-description">{{ selectedSkill.description ?? '技能说明未收录' }}</p>
    </section>
  </div>
</template>

<style scoped>
.skills-panel { display: grid; gap: 20px; }
.section-title { margin: 0 0 12px; font-size: 15px; }
.skill-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.skill-option { display: flex; align-items: center; gap: 9px; padding: 10px; min-width: 0; text-align: left; border: 1px solid var(--border); border-radius: 11px; color: var(--ink); background: var(--surface); cursor: pointer; }
.skill-option[aria-pressed='true'] { border-color: var(--accent); background: var(--accent-light); }
.skill-image { flex-shrink: 0; width: 32px; height: 32px; }
.skill-summary { display: grid; flex: 1; min-width: 0; gap: 4px; }
.skill-name { font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
.skill-cost { color: var(--text-muted); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.skill-detail { padding: 16px; border-radius: 12px; background: var(--surface-muted); }
.skill-values { display: flex; flex-wrap: wrap; gap: 20px; margin: 0; font-size: 12px; }
.skill-values > div { display: flex; gap: 8px; }
.skill-values dt, .unknown { color: var(--text-muted); }
.skill-values dd { margin: 0; }
.skill-description { margin: 14px 0 0; font-size: 13px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.unknown { margin: 0; font-size: 13px; }
</style>
