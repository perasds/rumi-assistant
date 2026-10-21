import type { MarketItem } from '../api/types'

export function firstColorListings(items: readonly MarketItem[]): MarketItem[] {
  return items.filter(item => item.category === 'color').slice(0, 10)
}
