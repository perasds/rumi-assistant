import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { closeToast, PullRefresh } from 'vant'
import { expect, it, vi } from 'vitest'
import MarketView from '../views/MarketView.vue'
import PetSearch from '../components/market/PetSearch.vue'
import MarketItem from '../components/market/MarketItem.vue'
import type { MarketItem as Listing, MarketSnapshot, Session } from '../api/types'

const session: Session = {
  status: 'connected', authenticated: true, message: null, roles: [],
  role: { id: 'test-role', name: 'test-only-role', server_name: 'test-only-server' },
}
const snapshot = (items: Listing[] = [], status: MarketSnapshot['status'] = 'complete'): MarketSnapshot => ({
  refresh_id: 'test-refresh', status, complete: status === 'complete', items, scope: 'top10',
  loaded_count: items.length, matched_count: items.length, total_count: null,
  started_at: null, updated_at: null, message: null, name_coverage: { named: 1, total: 1 },
})
const row = (key: string, price: number, category: Listing['category'] = 'color'): Listing => ({
  listing_key: key, category, pet_id: '1000101', name: key, display_name: key,
  image_url: null, direct_buy_price: price, expires_at: null, observed_at: 'test-only',
})
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
const idleRush = { job_id: null, status: 'idle', settings: null, balance_gold: null, counts: {}, spent_gold: '0', message: null, attempts: 0, updated_at: null }
const emptyCatalog = { items: [], total: 0, named_count: 0, image_count: 0 }

it('标题右侧打开抢购设置，保存每种数量且不触发交易查询', async () => {
  const fetcher = vi.fn((path: string) => Promise.resolve(json(path === '/api/rush-purchase' ? idleRush : path.startsWith('/api/pets') ? emptyCatalog : snapshot())))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(MarketView, { props: { session } })
  try {
    await flushPromises()
    const button = wrapper.get('.market-heading button[aria-label="抢购设置"]')
    expect(button.text()).toBe('抢购设置')
    await button.trigger('click')
    await flushPromises()
    const dialog = wrapper.getComponent({ name: 'RushPurchaseSettingsDialog' })
    expect(dialog.props('show')).toBe(true)
    expect(dialog.props('settings')).toBeNull()
    const settings = { petIds: ['1000101', '1000201'], maxPriceGold: 3_000_001, quantityPerPet: 2, minBalanceGold: 100_000 }
    dialog.vm.$emit('save', settings)
    await nextTick()
    expect(dialog.props('show')).toBe(false)
    settings.petIds.push('not-selected')
    await button.trigger('click')
    expect(dialog.props('settings')).toEqual({
      petIds: ['1000101', '1000201'], maxPriceGold: 3_000_001, quantityPerPet: 2, minBalanceGold: 100_000,
    })
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')).toHaveLength(0)
  } finally {
    wrapper.unmount()
    closeToast(true)
  }
})

it('输入与形态仅保留草稿，点击搜索才提交一次完整条件', async () => {
  vi.useFakeTimers()
  const fetcher = vi.fn((path: string) => Promise.resolve(json(
    path === '/api/rush-purchase' ? idleRush : path.startsWith('/api/pets') ? emptyCatalog : snapshot(),
  )))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(MarketView, { props: { session } })
  try {
    await flushPromises()
    expect(wrapper.text()).toContain('一口价排行')
    expect(wrapper.text()).not.toContain('普通噜咪')
    expect(wrapper.text()).not.toContain('退出登录')
    const search = wrapper.findComponent(PetSearch)
    search.vm.$emit('update:query', '蜜桃')
    await nextTick()
    search.vm.$emit('update:petIds', ['1000101'])
    await nextTick()
    await vi.advanceTimersByTimeAsync(3_000)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/pets'))).toHaveLength(1)
    search.vm.$emit('search')
    await flushPromises()
    const refreshes = fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')
    expect(refreshes).toHaveLength(2)
    expect(refreshes.map(call => JSON.parse((call as unknown as [string, RequestInit])[1].body as string))).toEqual([
      { category: 'color', query: '', pet_ids: [], limit: 10 },
      { category: 'color', query: '蜜桃', pet_ids: ['1000101'], limit: 10 },
    ])
    const listings = fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))
    expect(listings).toHaveLength(0)
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/pets'))).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(300)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(2)
    expect(wrapper.find('button.refresh').exists()).toBe(false)
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
  }
})

it('保留服务端前十顺序，提交搜索会取得新结果而非过滤旧十条', async () => {
  vi.useFakeTimers()
  const all = Array.from({ length: 12 }, (_, index) => row(`其他${index}`, 12 - index))
  const target = row('蜜桃', 20)
  const complete = snapshot([...all, target])
  const filtered = { ...complete, items: [target], matched_count: 1 }
  const fetcher = vi.fn((path: string, init?: RequestInit) => Promise.resolve(json(
    path === '/api/rush-purchase' ? idleRush : path.startsWith('/api/pets') ? emptyCatalog
      : path === '/api/market/refresh' ? JSON.parse(init?.body as string).query ? filtered : complete
        : complete,
  )))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(MarketView, { props: { session } })
  try {
    await flushPromises()
    expect(wrapper.findAllComponents(MarketItem).map(item => item.props('item').direct_buy_price)).toEqual([12, 11, 10, 9, 8, 7, 6, 5, 4, 3])
    expect(wrapper.find('.toolbar').exists()).toBe(false)
    wrapper.findComponent(PetSearch).vm.$emit('update:query', '蜜桃')
    await nextTick()
    wrapper.findComponent(PetSearch).vm.$emit('search')
    await flushPromises()
    expect(wrapper.findAllComponents(MarketItem).map(item => item.props('item').display_name)).toEqual(['蜜桃'])
    const refreshes = fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')
    expect(refreshes).toHaveLength(2)
    expect(JSON.parse(refreshes.at(-1)![1]?.body as string)).toEqual({ category: 'color', query: '蜜桃', pet_ids: [], limit: 10 })
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))).toHaveLength(0)
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/pets'))).toHaveLength(2)
    expect(wrapper.find('.refresh-status').exists()).toBe(false)
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
  }
})

it('请求期间显示查询中，返回部分失败结果时保留暂定提示', async () => {
  let resolvePost!: (value: Response) => void
  const pendingPost = new Promise<Response>(resolve => { resolvePost = resolve })
  const current = snapshot(Array.from({ length: 12 }, (_, index) => row(`彩色${index}`, 12 - index)), 'partial')
  const fetcher = vi.fn((path: string) => path === '/api/rush-purchase' ? Promise.resolve(json(idleRush)) : path.startsWith('/api/pets')
    ? Promise.resolve(json(emptyCatalog)) : pendingPost)
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(MarketView, { props: { session } })
  try {
    await flushPromises()
    expect(wrapper.findAllComponents(MarketItem)).toHaveLength(0)
    expect(wrapper.findComponent(PetSearch).props('busy')).toBe(true)
    expect(wrapper.text()).toContain('查询中')
    expect(wrapper.text()).not.toContain('暂定排行')
    resolvePost(json(current))
    await flushPromises()
    expect(wrapper.findComponent(PetSearch).props('busy')).toBe(false)
    expect(wrapper.findAllComponents(MarketItem)).toHaveLength(10)
    expect(wrapper.text()).toContain('暂定排行')
    expect(wrapper.text()).toContain('本轮未完成，请刷新重试')
    expect(wrapper.text()).not.toContain('排行仍在更新')
    expect(wrapper.text()).not.toContain('本轮已加载完')
    expect(wrapper.find('.refresh-status').exists()).toBe(false)
    expect(wrapper.find('button.load-progress').exists()).toBe(false)
  } finally {
    resolvePost(json(current))
    wrapper.unmount()
  }
})

it('删除七处标注的装饰、标题、工具栏、说明及状态区', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(json(path === '/api/rush-purchase' ? idleRush : path.startsWith('/api/pets') ? emptyCatalog : snapshot()))))
  const wrapper = mount(MarketView, { props: { session } })
  try {
    await flushPromises()
    for (const selector of ['.eyebrow', '.ranking-emblem', '.search-heading', '.toolbar', '.coverage-note', '.collection-note', '.refresh-status']) {
      expect(wrapper.find(selector).exists(), selector).toBe(false)
    }
    expect(wrapper.text()).toContain('一口价排行')
    expect(wrapper.findComponent(PetSearch).exists()).toBe(true)
  } finally { wrapper.unmount() }
})

it('下拉刷新沿用已提交条件，不应用尚未搜索的草稿', async () => {
  const fetcher = vi.fn((path: string) => Promise.resolve(json(path === '/api/rush-purchase' ? idleRush : path.startsWith('/api/pets') ? emptyCatalog : snapshot())))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(MarketView, { props: { session } })
  try {
    await flushPromises()
    const search = wrapper.findComponent(PetSearch)
    search.vm.$emit('update:query', '蜜桃')
    await nextTick()
    search.vm.$emit('update:petIds', ['1000101'])
    await nextTick()
    search.vm.$emit('search')
    await flushPromises()
    search.vm.$emit('update:query', '灰灰')
    await nextTick()
    const pull = wrapper.findComponent(PullRefresh)
    expect(pull.exists()).toBe(true)
    pull.vm.$emit('refresh')
    await flushPromises()
    const refreshes = fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')
    expect(refreshes).toHaveLength(3)
    expect(JSON.parse((refreshes.at(-1) as unknown as [string, RequestInit])[1].body as string)).toEqual({ category: 'color', query: '蜜桃', pet_ids: ['1000101'], limit: 10 })
    expect((wrapper.get('input[type="search"]').element as HTMLInputElement).value).toBe('灰灰')
    expect(pull.props('modelValue')).toBe(false)
  } finally { wrapper.unmount() }
})

it('精简界面仍保留真实错误，搜索按钮可以重试请求', async () => {
  let attempts = 0
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/rush-purchase') return Promise.resolve(json(idleRush))
    if (path.startsWith('/api/pets')) return Promise.resolve(json(emptyCatalog))
    attempts++
    return Promise.resolve(attempts === 1
      ? new Response(JSON.stringify({ error: { code: 'INVALID_INPUT', message: '输入格式不合法' } }), {
        status: 422, headers: { 'Content-Type': 'application/json' },
      }) : json(snapshot([row('蜜桃', 20)])))
  }))
  const wrapper = mount(MarketView, { props: { session } })
  try {
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('输入格式不合法')
    await wrapper.get('button[aria-label="搜索商品"]').trigger('click')
    await flushPromises()
    expect(attempts).toBe(2)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.findAllComponents(MarketItem)).toHaveLength(1)
    expect(wrapper.find('.refresh-status').exists()).toBe(false)
  } finally { wrapper.unmount() }
})
