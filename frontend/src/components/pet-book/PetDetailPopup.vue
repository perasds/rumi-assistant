<script setup lang="ts">
import { computed, shallowRef, useId, watch } from 'vue'
import { Button as VanButton, Image as VanImage, Loading as VanLoading, Popup as VanPopup, Tab as VanTab, Tabs as VanTabs } from 'vant'
import type { Pet, PetDetail } from '../../api/types'
import PetSkillsPanel from './PetSkillsPanel.vue'

const props = defineProps<{ show: boolean; pet: Pet | null; detail: PetDetail | null; loading: boolean; error: string | null }>()
const emit = defineEmits<{ close: []; closed: []; retry: [] }>()
const titleId = useId()
const activeTab = shallowRef('basic')
const traitGroups = computed(() => [
  { kind: 'battle', label: '战斗特性', items: props.detail?.traits.filter(trait => trait.kind === 'battle') ?? [] },
  { kind: 'home', label: '家园特性', items: props.detail?.traits.filter(trait => trait.kind === 'home') ?? [] },
])
watch(() => [props.show, props.pet?.pet_id] as const, ([show]) => {
  if (show) activeTab.value = 'basic'
})
</script>

<template>
  <VanPopup :show="show" class="pet-detail-popup" position="center" round role="dialog" :aria-labelledby="titleId" @update:show="!$event && emit('close')" @closed="emit('closed')">
    <div class="detail-shell">
      <header class="detail-header">
        <VanImage class="detail-image" :src="pet?.image_url ?? undefined" fit="contain" :alt="pet?.display_name"><template #error>?</template></VanImage>
        <div class="detail-heading"><h2 :id="titleId" class="detail-title">{{ pet?.display_name }}</h2><p class="pet-id">ID {{ pet?.pet_id }}</p></div>
        <button class="close-detail" type="button" aria-label="关闭详情" @click="emit('close')">×</button>
      </header>
      <div v-if="loading" class="detail-state"><VanLoading size="22">正在加载详情</VanLoading></div>
      <div v-else-if="error" class="detail-state"><p role="alert">{{ error }}</p><VanButton size="small" plain type="primary" aria-label="重试加载噜咪详情" @click="emit('retry')">重试</VanButton></div>
      <p v-else-if="detail && !detail.available" class="detail-state">详细资料暂未收录</p>
      <VanTabs v-else-if="detail" v-model:active="activeTab" class="detail-tabs" color="var(--accent-deep)">
        <VanTab name="basic" title="基本">
          <div class="tab-content">
            <dl class="basic-values"><div><dt>图鉴编号</dt><dd>{{ detail.pokedex_id ?? '—' }}</dd></div><div><dt>元素</dt><dd>{{ detail.elements.map(element => element.name).join(' / ') || '—' }}</dd></div></dl>
            <h3 class="section-title">物种数值范围</h3>
            <dl v-if="detail.stats.length" class="basic-values"><div v-for="stat in detail.stats" :key="stat.key"><dt>{{ stat.label }}</dt><dd>{{ stat.min }}–{{ stat.max }}</dd></div></dl>
            <p v-else class="unknown">未收录</p>
            <h3 class="section-title">普通攻击</h3>
            <dl class="basic-values"><div><dt>元素</dt><dd>{{ detail.normal_attack?.element?.name ?? '—' }}</dd></div><div><dt>威力</dt><dd>{{ detail.normal_attack?.power ?? '—' }}</dd></div><div><dt>能量回复</dt><dd>{{ detail.normal_attack?.energy_gain ?? '—' }}</dd></div><div><dt>攻击间隔</dt><dd>{{ detail.normal_attack?.interval_seconds == null ? '—' : `${detail.normal_attack.interval_seconds} 秒` }}</dd></div></dl>
          </div>
        </VanTab>
        <VanTab name="traits" title="特性">
          <div class="tab-content trait-groups"><section v-for="group in traitGroups" :key="group.kind"><h3 class="section-title">{{ group.label }}</h3><div v-for="trait in group.items" :key="trait.trait_id" class="trait"><h4 class="trait-name">{{ trait.name }}</h4><p class="trait-description">{{ trait.description ?? '特性说明未收录' }}</p></div><p v-if="!group.items.length" class="unknown">未收录</p></section></div>
        </VanTab>
        <VanTab name="skills" title="技能"><div class="tab-content"><PetSkillsPanel :fixed-skill="detail.fixed_skill" :replaceable-skills="detail.replaceable_skills" /></div></VanTab>
      </VanTabs>
    </div>
  </VanPopup>
</template>

<style scoped>
.pet-detail-popup { width: min(600px, calc(100vw - 28px)); max-height: 86vh; max-height: 86dvh; overflow: hidden; background: var(--surface); }
.detail-shell { display: flex; flex-direction: column; max-height: 86vh; max-height: 86dvh; }
.detail-header { display: flex; flex-shrink: 0; align-items: center; gap: 14px; padding: 22px 22px 14px; }
.detail-image { width: 72px; height: 72px; flex-shrink: 0; }
.detail-heading { min-width: 0; flex: 1; }
.detail-title { margin: 0; font-size: 21px; overflow-wrap: anywhere; }
.pet-id { margin: 7px 0 0; color: var(--text-muted); font-size: 12px; }
.close-detail { align-self: flex-start; width: 32px; height: 32px; padding: 0; color: var(--text-muted); font-size: 26px; border: 0; border-radius: 50%; background: var(--surface-muted); cursor: pointer; }
.detail-state { padding: 30px 22px 44px; text-align: center; color: var(--text-muted); }
.detail-tabs { min-height: 0; overflow-y: auto; overscroll-behavior-y: contain; }
.detail-tabs :deep(.van-tabs__wrap) { position: sticky; top: 0; z-index: 1; }
.detail-tabs :deep(.van-tabs__nav) { background: var(--surface); }
.tab-content { padding: 20px 24px 28px; }
.basic-values { display: grid; gap: 12px; margin: 0; font-size: 13px; }
.basic-values > div { display: flex; justify-content: space-between; gap: 20px; }
.basic-values dt, .unknown { color: var(--text-muted); }
.basic-values dd { margin: 0; text-align: right; }
.section-title { margin: 24px 0 14px; font-size: 15px; }
.trait-groups { display: grid; gap: 22px; }
.trait-groups .section-title { margin-top: 0; }
.trait { padding: 13px 14px; margin-top: 10px; border-radius: 11px; background: var(--surface-muted); }
.trait-name { margin: 0; font-size: 14px; }
.trait-description { margin: 8px 0 0; font-size: 13px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.unknown { margin: 0; font-size: 13px; }
@media (max-width: 380px) { .detail-header { padding: 18px 16px 12px; } .detail-title { font-size: 18px; } .tab-content { padding: 18px 18px 24px; } }
</style>
