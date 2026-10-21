import { expect, it } from 'vitest'
import type { MarketItem } from '../api/types'
import { firstColorListings } from '../utils/marketRanking'

function row(key: string, price: number, category: MarketItem['category'] = 'color'): MarketItem {
  return { listing_key: key, category, pet_id: key, name: key, display_name: key,
    image_url: null, direct_buy_price: price, expires_at: null, observed_at: 'test-only' }
}

it('按服务端原生顺序取彩色前十，不按价格重新排序', () => {
  const rows = [row('normal', 0, 'normal'), row('item', 0, 'item'),
    ...Array.from({ length: 12 }, (_, index) => row(String(index), 11 - index))]
  expect(firstColorListings(rows).map(item => item.direct_buy_price)).toEqual([11, 10, 9, 8, 7, 6, 5, 4, 3, 2])
  expect(firstColorListings(rows).every(item => item.category === 'color')).toBe(true)
})

it('零价和同价都保持原生到达顺序，不变异数组或条目', () => {
  const rows = Object.freeze([Object.freeze(row('first', 5)), Object.freeze(row('second', 5)),
    Object.freeze(row('zero', 0)), Object.freeze(row('third', 5))])
  const result = firstColorListings(rows)
  expect(result.map(item => item.listing_key)).toEqual(['first', 'second', 'zero', 'third'])
  expect(rows.map(item => item.listing_key)).toEqual(['first', 'second', 'zero', 'third'])
  expect(result).not.toBe(rows)
  expect(result[0]).toBe(rows[0])
})

it('对后端已筛选的集合取十，少于十和空集合不补造条目', () => {
  const serverFiltered = [row('query-match-outside-unfiltered-top-ten', 99)]
  expect(firstColorListings(serverFiltered)).toEqual(serverFiltered)
  expect(firstColorListings([])).toEqual([])
})
