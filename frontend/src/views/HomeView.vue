<script setup lang="ts">
import { computed, shallowRef } from 'vue'
import { Button as VanButton, Empty as VanEmpty, Loading as VanLoading, Tabs as VanTabs, Tab as VanTab } from 'vant'
import HomeBuildingCard from '../components/home/HomeBuildingCard.vue'
import HomeManagement from '../components/home/HomeManagement.vue'
import { useHomeProduction } from '../composables/useHomeProduction'

const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ expired: [] }>()
const tab = shallowRef('management')
const managementActive = computed(() => props.active && tab.value === 'management')
const { catalog, loading, error, load } = useHomeProduction({ active: () => props.active, onUnauthorized: () => emit('expired') })
</script>

<template>
  <main class="home-view" aria-label="家园生产">
    <h1 class="heading">家园生产</h1>
    <VanTabs v-model:active="tab" class="home-tabs" color="var(--accent)">
      <VanTab title="生产管理" name="management" />
      <VanTab title="配方目录" name="catalog" />
    </VanTabs>
    <section class="home-production-scroll" aria-label="家园内容" tabindex="0" :aria-busy="loading">
      <HomeManagement v-show="tab === 'management'" :active="managementActive" :catalog="catalog" @expired="emit('expired')" />
      <div v-show="tab === 'catalog'">
      <p class="configuration-note">配置目录 · 全部生产建筑与配方</p>
      <VanLoading v-if="loading" class="home-loading" size="22">正在加载生产目录</VanLoading>
      <div v-else-if="error" class="home-state">
        <p role="alert">{{ error }}</p>
        <VanButton size="small" plain type="primary" aria-label="重试加载家园生产" @click="load">重试</VanButton>
      </div>
      <template v-else-if="catalog">
        <div v-if="catalog.buildings.length" class="home-building-list">
          <HomeBuildingCard v-for="building in catalog.buildings" :key="building.building_id" :building="building" />
        </div>
        <VanEmpty v-else description="暂无生产建筑" />
      </template>
      </div>
    </section>
  </main>
</template>

<style scoped>
.home-view { display: flex; flex: 1; flex-direction: column; min-height: 0; width: 100%; max-width: 900px; margin: 30px auto 0; padding: 0 24px 24px; overflow: hidden; }
.heading { flex-shrink: 0; margin: 0 0 10px; font-size: 28px; letter-spacing: -.5px; }
.configuration-note { flex-shrink: 0; margin: 0 0 20px; font-size: 13px; line-height: 1.6; color: var(--text-muted); }
.home-tabs { flex-shrink: 0; margin-bottom: 18px; --van-tabs-nav-background: transparent; }
.home-production-scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior-y: contain; scrollbar-gutter: stable; }
.home-building-list { display: grid; align-items: start; gap: 16px; }
.home-loading, .home-state { padding: 44px 12px; text-align: center; color: var(--text-muted); }
@media (min-width: 760px) { .home-building-list { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 640px) { .home-view { padding: 0 16px 20px; margin-top: 24px; } .heading { font-size: 25px; } }
</style>
