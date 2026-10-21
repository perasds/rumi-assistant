<script setup lang="ts">
import { computed } from 'vue'
import { Empty as VanEmpty, List as VanList } from 'vant'
import type { MarketSnapshot } from '../../api/types'
import MarketItem from './MarketItem.vue'
const props = defineProps<{ snapshot: MarketSnapshot | null; busy: boolean }>()
const emptyText = computed(() => !props.snapshot || props.snapshot.status === 'idle' ? '刷新后查看本轮商品' : props.snapshot.complete ? '本轮已取得商品中暂无匹配' : '当前已取得商品中暂无匹配，本轮尚未完成')
</script>
<template>
  <VanList class="market-list" :loading="busy" :finished="true" :immediate-check="false">
    <div v-if="snapshot?.items.length" class="items">
      <MarketItem v-for="(item, index) in snapshot.items" :key="item.listing_key" :item="item" :rank="index + 1" />
    </div>
    <VanEmpty v-else image="search" :description="emptyText" />
  </VanList>
</template>
<style scoped>
.items { display: grid; gap: 10px; }
</style>
