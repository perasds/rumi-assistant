import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import type { MarketItem as Listing, MarketSnapshot } from '../api/types'
import MarketItem from '../components/market/MarketItem.vue'
import MarketList from '../components/market/MarketList.vue'

const item: Listing = {
  listing_key: 'test-listing', category: 'color', pet_id: '1000101',
  name: '蜜桃', display_name: '蜜桃', image_url: '/api/pets/1000101/image',
  direct_buy_price: 0, expires_at: null, observed_at: '2026-10-15T00:00:00Z',
}

describe('一口价排行展示', () => {
  it('展示名次、名称、真实ID和零价格金币，不隐藏0价', () => {
    const wrapper = mount(MarketItem, { props: { item, rank: 1 } })
    expect(wrapper.get('[aria-label="第1名"]').text()).toBe('01')
    expect(wrapper.text()).toContain('蜜桃')
    expect(wrapper.text()).toContain('ID 1000101')
    expect(wrapper.get('.price-amount').text()).toBe('0')
    expect(wrapper.text()).toContain('金币')
    expect(wrapper.get('img').attributes('alt')).toBe('蜜桃')
  })

  it('列表按收到的排行顺序标号，同名商品分别保留', () => {
    const snapshot: MarketSnapshot = {
      refresh_id: 'test-round', scope: 'all', status: 'complete', complete: true,
      items: [item, { ...item, listing_key: 'another-listing', direct_buy_price: 10000 }],
      loaded_count: 20, matched_count: 20, total_count: 20,
      started_at: null, updated_at: null, message: null,
      name_coverage: { named: 20, total: 20 },
    }
    const wrapper = mount(MarketList, { props: { snapshot, busy: false } })
    const rows = wrapper.findAll('article')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.get('[aria-label="第1名"]').text()).toBe('01')
    expect(rows[1]!.get('[aria-label="第2名"]').text()).toBe('02')
    expect(rows[1]!.get('.price-amount').text()).toBe('10,000')
  })

  it('商品名称作为普通文本呈现', () => {
    const name = '<img src=x onerror=alert(1)>'
    const wrapper = mount(MarketItem, { props: { item: { ...item, display_name: name }, rank: 10 } })
    expect(wrapper.text()).toContain(name)
    expect(wrapper.find('img[src="x"]').exists()).toBe(false)
  })
})
