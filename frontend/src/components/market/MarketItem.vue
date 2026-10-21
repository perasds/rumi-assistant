<script setup lang="ts">
import { computed } from 'vue'
import { Image as VanImage } from 'vant'
import type { MarketItem } from '../../api/types'
const props = defineProps<{ item: MarketItem; rank?: number }>()
const price = computed(() => new Intl.NumberFormat('zh-CN').format(props.item.direct_buy_price))
</script>
<template>
  <article class="market-item" :class="{ 'top-three': rank && rank <= 3 }">
    <span v-if="rank" class="rank" :aria-label="`第${rank}名`">{{ String(rank).padStart(2, '0') }}</span>
    <VanImage class="avatar" :src="item.image_url ?? undefined" fit="contain" :alt="item.display_name">
      <template #error><span class="fallback">?</span></template>
    </VanImage>
    <div class="item-info">
      <h3>{{ item.display_name }}</h3>
      <p class="pet-id">{{ item.pet_id ? `ID ${item.pet_id}` : '物品' }}</p>
    </div>
    <div class="item-price">
      <span class="price-label">一口价</span>
      <span class="price-value"><span class="coin" aria-hidden="true">G</span><strong class="price-amount">{{ price }}</strong><span class="currency">金币</span></span>
    </div>
  </article>
</template>
<style scoped>
.market-item { display: grid; grid-template-columns: 30px 76px minmax(0, 1fr) auto; gap: 16px; align-items: center; padding: 17px 20px; background: #fff; border: 1px solid var(--border); border-radius: 18px; }
.top-three { border-color: #efdfbc; background: linear-gradient(100deg, #fffcf4, #fff 55%); }
.rank { color: var(--text-muted); font-size: 20px; font-weight: 750; font-variant-numeric: tabular-nums; }
.top-three .rank { color: #b28b44; }
.avatar { width: 76px; height: 76px; background: var(--surface-muted); border: 1px solid var(--border); border-radius: 18px; overflow: hidden; }
.fallback { color: var(--text-muted); font-size: 28px; }
.item-info { min-width: 0; }
h3 { margin: 0; font-size: 16px; font-weight: 700; line-height: 1.5; overflow-wrap: anywhere; }
.pet-id { margin: 4px 0 0; font-size: 11px; color: var(--text-muted); line-height: 1.5; }
.item-price { min-width: 108px; text-align: right; }
.price-label { display: block; color: var(--text-muted); font-size: 11px; margin-bottom: 6px; }
.price-value { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.price-amount { color: #9b6b2c; font-size: 22px; letter-spacing: -.5px; font-variant-numeric: tabular-nums; }
.coin { display: inline-grid; place-items: center; width: 19px; height: 19px; background: #ffedb8; border: 1px solid #e5c676; border-radius: 50%; color: #9b6b2c; font-size: 10px; font-weight: 800; }
.currency { color: var(--text-muted); font-size: 10px; }
@media (max-width: 520px) {
  .market-item { grid-template-columns: 23px 60px minmax(0, 1fr); gap: 3px 11px; padding: 14px; border-radius: 16px; }
  .rank, .avatar { grid-row: 1 / 3; }
  .rank { font-size: 17px; }
  .avatar { width: 60px; height: 68px; border-radius: 14px; }
  h3 { font-size: 15px; }
  .item-price { grid-column: 3; text-align: left; min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin-top: 4px; }
  .price-label { margin: 0; font-size: 10px; }
  .price-amount { font-size: 18px; min-width: 0; overflow-wrap: anywhere; }
  .price-value { gap: 4px; max-width: 100%; flex-wrap: wrap; white-space: normal; }
  .coin { width: 16px; height: 16px; font-size: 9px; }
}
</style>
