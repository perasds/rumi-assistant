<script setup lang="ts">
import { computed, onMounted, shallowRef, watch } from 'vue'
import { Button as VanButton, NoticeBar as VanNoticeBar, PullRefresh as VanPullRefresh, showToast } from 'vant'
import type { MarketFilters, Session } from '../api/types'
import { useMarketListings } from '../composables/useMarketListings'
import { useRushPurchase } from '../composables/useRushPurchase'
import { firstColorListings } from '../utils/marketRanking'
import PetSearch from '../components/market/PetSearch.vue'
import MarketList from '../components/market/MarketList.vue'
import RushPurchaseSettingsDialog from '../components/market/RushPurchaseSettingsDialog.vue'
import RushPurchaseStatus from '../components/market/RushPurchaseStatus.vue'
import type { RushPurchaseSettings } from '../utils/rushPurchaseSettings'
defineProps<{ session: Session }>()
const emit = defineEmits<{ expired: []; logout: [] }>()
const query = shallowRef('')
const petIds = shallowRef<string[]>([])
const appliedFilters = shallowRef<MarketFilters>({ query: '', category: 'color', pet_ids: [] })
const pullRefreshing = shallowRef(false)
const rushSettingsOpen = shallowRef(false)
const rushSettings = shallowRef<RushPurchaseSettings | null>(null)
const { snapshot, busy, error, catalog, catalogError, refresh, setFilters } = useMarketListings({ onUnauthorized: () => emit('expired') })
const { state: rushState, error: rushError, busy: rushBusy, checking: rushChecking, active: rushActive,
  canStart: canStartRush, canStop: canStopRush, currentSeasonReady, settingsLocked: rushSettingsLocked, loadState: loadRushState, start: startRush, stop: stopRush,
} = useRushPurchase({ onUnauthorized: () => emit('expired') })
const seasonStartBlocked = computed(() => !!rushSettings.value?.currentSeasonOnly && !currentSeasonReady.value && !rushActive.value)
const canBeginRush = computed(() => canStartRush.value && !seasonStartBlocked.value)
const rankedSnapshot = computed(() => snapshot.value ? { ...snapshot.value, items: firstColorListings(snapshot.value.items) } : null)
const tentativeRanking = computed(() => snapshot.value && !snapshot.value.complete
  && (snapshot.value.status === 'loading' || snapshot.value.status === 'partial'))
watch(query, () => { petIds.value = [] }, { flush: 'sync' })
function submitSearch() {
  appliedFilters.value = { query: query.value.trim(), category: 'color', pet_ids: [...petIds.value] }
  void setFilters(appliedFilters.value)
}
async function refreshAppliedFilters() {
  pullRefreshing.value = true
  try { await refresh(appliedFilters.value) }
  finally { pullRefreshing.value = false }
}
function saveRushSettings(settings: RushPurchaseSettings) {
  if (rushSettingsLocked.value) return
  rushSettings.value = { ...settings, petIds: [...settings.petIds],
    ...(settings.petQuantities ? { petQuantities: { ...settings.petQuantities } } : {}) }
  rushSettingsOpen.value = false
  showToast('抢购设置已保存')
}
function openRushSettings() {
  if (!rushSettingsLocked.value) rushSettingsOpen.value = true
}
function beginRushPurchase() {
  if (!canBeginRush.value) return
  if (!rushSettings.value) openRushSettings()
  else void startRush(rushSettings.value)
}
watch(rushState, value => {
  if (value?.settings && (rushActive.value || !rushSettings.value)) {
    rushSettings.value = { petIds: [...value.settings.pet_ids], maxPriceGold: value.settings.max_price_gold,
      quantityPerPet: value.settings.quantity_per_pet, minBalanceGold: value.settings.min_balance_gold,
      ...(value.settings.quantity_mode ? { quantityMode: value.settings.quantity_mode } : {}),
      ...(value.settings.current_season_only ? { currentSeasonOnly: true } : {}),
      ...(value.settings.pet_quantities ? { petQuantities: { ...value.settings.pet_quantities } } : {}) }
  }
})
watch(rushSettingsLocked, locked => { if (locked) rushSettingsOpen.value = false })
onMounted(() => { void refresh(); void loadRushState() })
</script>
<template>
  <main class="market-view">
    <div class="market-heading">
      <h1 class="heading">一口价排行</h1>
      <div class="rush-actions">
        <VanButton class="rush-settings-button" type="primary" plain size="small" icon="setting-o" aria-label="抢购设置" :disabled="rushSettingsLocked" @click="openRushSettings">抢购设置</VanButton>
        <VanButton v-if="rushActive" class="rush-run-button" type="danger" plain size="small" aria-label="取消抢购" :disabled="!canStopRush" :loading="rushBusy || rushState?.status === 'stopping'" loading-text="取消中" @click="stopRush">取消抢购</VanButton>
        <VanButton v-else class="rush-run-button" type="primary" size="small" aria-label="开始抢购" :disabled="!canBeginRush" :loading="rushBusy" @click="beginRushPurchase">开始抢购</VanButton>
      </div>
    </div>
    <div v-if="seasonStartBlocked" class="rush-season-note" role="status">
      <span>{{ rushState?.season_message || '当前赛季尚未同步，暂不能开始抢购' }}</span>
      <VanButton plain type="primary" size="mini" aria-label="刷新赛季状态" :loading="rushChecking" :disabled="rushChecking || rushBusy" @click="loadRushState">刷新赛季状态</VanButton>
    </div>
    <PetSearch v-model:query="query" v-model:pet-ids="petIds" :active-query="appliedFilters.query" :catalog="catalog" :busy="busy" :error="catalogError" @search="submitSearch" />
    <VanNoticeBar v-if="error" class="error-bar" wrapable :scrollable="false" :text="error" role="alert" />
    <p v-if="tentativeRanking" class="ranking-note" role="status">暂定排行 · 当前已取得的前十条商品。{{ snapshot?.status === 'loading' ? '按游戏一口价升序读取，排行仍在更新。' : '本轮未完成，请刷新重试。' }}</p>
    <RushPurchaseStatus :state="rushState" :catalog="catalog" :error="rushError" :checking="rushChecking" @recheck="loadRushState" />
    <section class="market-scroll" aria-label="一口价宠物列表" tabindex="0">
      <VanPullRefresh v-model="pullRefreshing" class="market-pull-refresh" :disabled="busy" @refresh="refreshAppliedFilters">
        <MarketList :snapshot="rankedSnapshot" :busy="busy" />
      </VanPullRefresh>
    </section>
    <RushPurchaseSettingsDialog v-model:show="rushSettingsOpen" :settings="rushSettings" :season-ready="currentSeasonReady" :current-season-id="rushState?.current_season_id ?? null" :season-message="rushState?.season_message" @save="saveRushSettings" />
  </main>
</template>
<style scoped>
.market-view { display: flex; flex-direction: column; flex: 1; min-height: 0; width: 100%; max-width: 900px; margin: 30px auto 0; padding: 0 24px 24px; }
.market-view > :not(.market-scroll) { flex-shrink: 0; }
.market-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 24px; }
.heading { font-size: 28px; margin: 0; letter-spacing: -.5px; }
.rush-actions { display: flex; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.rush-season-note { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 14px; color: var(--text-muted); font-size: 12px; line-height: 1.6; }
.rush-settings-button, .rush-run-button { height: 36px; padding: 0 12px; border-radius: 10px; }
.ranking-note { color: #9b6b2c; background: #fff5df; border-radius: 10px; padding: 10px 12px; font-size: 12px; line-height: 1.7; }
.error-bar { border-radius: 9px; margin-top: 16px; }
.market-scroll { flex: 1; min-height: 0; margin-top: 20px; overflow-y: auto; overscroll-behavior-y: contain; scrollbar-gutter: stable; }
.market-scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 16px; }
.market-pull-refresh { min-height: 100%; }
@media (max-width: 640px) { .market-view { padding: 0 16px 20px; margin-top: 24px; } .heading { font-size: 25px; } }
@media (max-width: 420px) { .market-heading { align-items: flex-start; flex-wrap: wrap; } .heading { font-size: 23px; white-space: nowrap; } .rush-actions { max-width: 180px; margin-left: auto; } .rush-settings-button, .rush-run-button { padding: 0 9px; } }
</style>
