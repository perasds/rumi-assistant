import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as vant from 'vant'
import App from '../App.vue'
import MarketView from '../views/MarketView.vue'
import type { MarketSnapshot, RushPurchaseState, Session } from '../api/types'
import { loginChallenge, testCrypto } from './loginEncryptionFixture'
import { orderState } from './orderFixtures'

const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => { vi.stubGlobal('crypto', testCrypto) })
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vant.closeToast(true)
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
const serverTime = { unix_ms: 1_800_000_000_123, time_zone: 'Asia/Shanghai' }
const anonymous: Session = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const emptyCatalog = { items: [], total: 0, named_count: 0, image_count: 0 }
const connected: Session = {
  status: 'connected', authenticated: true, roles: [], message: null,
  role: { id: 'test-role', name: '', server_name: '测试区服' },
}
const snapshot: MarketSnapshot = {
  refresh_id: 'test-refresh', status: 'complete', complete: true, loaded_count: 1, matched_count: 1, scope: 'top10',
  total_count: 1, started_at: null, updated_at: null, message: null,
  name_coverage: { named: 1, total: 1 },
  items: [{ listing_key: 'test-listing', category: 'color', pet_id: '1000101', name: '蜜桃',
    display_name: '蜜桃', image_url: null, direct_buy_price: 100, expires_at: null, observed_at: 'test-only' }],
}

function connectedApp({ expireOnSearch = false, logoutRequest, rushState }: {
  expireOnSearch?: boolean; logoutRequest?: () => Promise<Response>; rushState?: RushPurchaseState
} = {}) {
  let authenticated = true
  let expired = false
  let refreshes = 0
  const fetcher = vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/time') return Promise.resolve(json(serverTime))
    if (path === '/api/auth/capabilities') return Promise.resolve(json({ integration_ready: true, methods: ['account'] }))
    if (path === '/api/auth/login-challenge') return Promise.resolve(json(loginChallenge()))
    if (path === '/api/session' && init?.method === 'DELETE') {
      if (logoutRequest) return logoutRequest().then(response => {
        if (response.ok) authenticated = false
        return response
      })
      authenticated = false
      return Promise.resolve(json(anonymous))
    }
    if (path === '/api/session') return Promise.resolve(json(authenticated ? connected : anonymous))
    if (path === '/api/me') return Promise.resolve(json({
      role_id: 'synthetic-game-role', nickname: null, server_name: '测试区服', balance_gold: '20000000',
    }))
    if (path === '/api/home/orders/state') return Promise.resolve(json(orderState()))
    if (path === '/api/auth/login') {
      authenticated = true
      return Promise.resolve(json(connected))
    }
    if (path === '/api/rush-purchase/stop' && rushState) {
      rushState = { ...rushState, status: 'stopped', message: '合成任务已停止' }
      return Promise.resolve(json(rushState))
    }
    if (path === '/api/rush-purchase') return Promise.resolve(json(rushState ?? {
      job_id: null, status: 'idle', settings: null, balance_gold: null, counts: {}, spent_gold: '0', message: null, attempts: 0, updated_at: null,
    }))
    if (path === '/api/home/production') return Promise.resolve(json({ scope: 'configuration', buildings: [], total: 0, recipe_count: 0 }))
    if (path.startsWith('/api/pets')) return Promise.resolve(json({
      items: [{ pet_id: '1000101', name: '蜜桃', display_name: '蜜桃', name_verified: true, image_url: null }],
      total: 1, named_count: 1, image_count: 0,
    }))
    if (path === '/api/market/refresh') {
      refreshes++
      if (expireOnSearch && refreshes > 1 && !expired) {
        authenticated = false
        expired = true
        return Promise.resolve(new Response(JSON.stringify({ error: { code: 'AUTH_REQUIRED', message: '会话过期' } }),
          { status: 401, headers: { 'Content-Type': 'application/json' } }))
      }
      return Promise.resolve(json(snapshot))
    }
    if (path.startsWith('/api/market/listings')) return Promise.resolve(json(snapshot))
    throw new Error(`Unexpected test request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(App, { attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}

function menu(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(item => item.text() === label)
  expect(button, `缺少菜单 ${label}`).toBeDefined()
  return button!
}

async function loginAgain(wrapper: ReturnType<typeof mount>) {
  if (!wrapper.find('input[name="account"]').exists()) {
    await menu(wrapper, '我的').trigger('click')
    await flushPromises()
  }
  await wrapper.get('input[name="account"]').setValue('test-account')
  await wrapper.get('input[name="password"]').setValue('test-password')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('.van-button--loading').exists()).toBe(false))
}
async function visitTrade(wrapper: ReturnType<typeof mount>) {
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
}
async function logoutFromMine(wrapper: ReturnType<typeof mount>) {
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  await wrapper.get('main[aria-label="个人信息"] button[aria-label="退出登录"]').trigger('click')
  await flushPromises()
}
describe('真实认证能力页面', () => {
  it('能力尚未接通时受保护页呈现不可用状态且没有模拟入口', async () => {
    vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(json(path === '/api/time' ? serverTime : path.includes('capabilities')
      ? { provider: 'bilibili_game', integration_ready: false, methods: [], reason_code: 'AUTH_PROTOCOL_UNVERIFIED', message: '游戏登录协议尚未接通' }
      : path === '/api/pets' ? emptyCatalog : anonymous))))
    const wrapper = mount(App)
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ServerClock' }).exists()).toBe(false)
    expect(wrapper.get('header').text()).toContain('噜咪小助手')
    expect(wrapper.get('header img').attributes('src')).toBe('/app-icon.svg')
    expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
    await menu(wrapper, '交易').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('游戏登录暂不可用')
    expect(wrapper.text()).toContain('游戏登录协议尚未接通')
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(wrapper.get('.login-panel').findAll('button').map(button => button.text())).toEqual(['重新检查'])
    expect(wrapper.find('nav').exists()).toBe(true)
  })
})

describe('噜咪小助手应用外壳', () => {
  it('从我的进入登录页，顶栏仅保留品牌，不显示时间和登录按钮', async () => {
    vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(json(path === '/api/time' ? serverTime : path.includes('capabilities')
      ? { integration_ready: true, methods: ['account'] } : path === '/api/pets' ? emptyCatalog : anonymous))))
    const wrapper = mount(App)
    wrappers.push(wrapper)
    await flushPromises()
    await menu(wrapper, '我的').trigger('click')
    await flushPromises()
    expect(wrapper.get('.brand').text()).toBe('噜咪小助手')
    expect(wrapper.findComponent({ name: 'ServerClock' }).exists()).toBe(false)
    expect(wrapper.find('header button').exists()).toBe(false)
    expect(wrapper.get('header').text()).toBe('噜咪小助手')
    expect(wrapper.find('.connection-status').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('登录游戏账号，查看彩色噜咪最低一口价。')
    expect(wrapper.text()).not.toContain('商品浏览与名称筛选 · 购买功能后续开放')
    expect(wrapper.find('input[name="account"]').exists()).toBe(true)
    expect(wrapper.find('input[name="password"]').exists()).toBe(true)
    expect(document.querySelector('.van-toast')).toBeNull()
  })
  it('登录后提供四菜单，交易往返保持搜索且不重复刷新', async () => {
    vi.useFakeTimers()
    const { wrapper, fetcher } = connectedApp()
    await flushPromises()
    expect(wrapper.get('header').text()).not.toContain('游戏已连接')
    expect(wrapper.find('header button').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'ServerClock' }).exists()).toBe(false)
    expect(wrapper.get('nav').findAll('button').map(button => button.text())).toEqual(['交易', '图鉴', '订单', '我的'])
    expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
    expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
    expect(fetcher.mock.calls.some(([path]) => path === '/api/me')).toBe(false)
    await visitTrade(wrapper)
    expect(menu(wrapper, '交易').attributes('aria-current')).toBe('page')
    const market = wrapper.findComponent(MarketView)
    const search = market.get('input[type="search"]')
    await search.setValue('蜜桃')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
    await market.get('button[aria-label="搜索商品"]').trigger('click')
    await flushPromises()
    await market.get('button[data-pet-id="1000101"]').trigger('click')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(2)
    await market.get('button[aria-label="搜索商品"]').trigger('click')
    await flushPromises()
    const refreshes = fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')
    expect(JSON.parse(refreshes.at(-1)![1]?.body as string)).toEqual({ category: 'color', query: '蜜桃', pet_ids: ['1000101'], limit: 10 })
    expect(refreshes).toHaveLength(3)

    await menu(wrapper, '订单').trigger('click')
    await flushPromises()
    expect(market.isVisible()).toBe(false)
    expect(wrapper.find('main[aria-label="订单助手"]').exists()).toBe(true)
    expect(menu(wrapper, '订单').attributes('aria-current')).toBe('page')
    expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
    await menu(wrapper, '我的').trigger('click')
    await flushPromises()
    expect(market.isVisible()).toBe(false)
    expect(wrapper.get('main[aria-label="个人信息"]').text()).toContain('测试区服')
    expect(wrapper.find('main[aria-label="家园生产"]').exists()).toBe(false)
    expect(fetcher.mock.calls.some(([path]) => path === '/api/home/production')).toBe(false)
    expect(wrapper.find('.feature-placeholder').exists()).toBe(false)
    expect(menu(wrapper, '我的').attributes('aria-current')).toBe('page')
    await menu(wrapper, '交易').trigger('click')
    await flushPromises()
    expect(market.isVisible()).toBe(true)
    expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
    expect((market.get('input[type="search"]').element as HTMLInputElement).value).toBe('蜜桃')
    expect(market.get('button[data-pet-id="1000101"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.text()).toContain('蜜桃')
    expect(wrapper.find('.feature-placeholder').exists()).toBe(false)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(refreshes.length)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1)
  })

  it('在我的统一退出回到图鉴，清除个人资料，重新登录后交易是全新状态', async () => {
    const { wrapper, fetcher } = connectedApp()
    await flushPromises()
    await visitTrade(wrapper)
    await menu(wrapper, '我的').trigger('click')
    await flushPromises()
    expect(wrapper.get('main[aria-label="个人信息"]').text()).toContain('测试区服')
    await logoutFromMine(wrapper)
    expect(wrapper.findComponent(MarketView).exists()).toBe(false)
    expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('测试区服')
    expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
    expect(wrapper.find('.feature-placeholder').exists()).toBe(false)
    expect(wrapper.find('.login-panel').exists()).toBe(false)
    expect(document.querySelector('.van-toast')).toBeNull()
    expect(fetcher.mock.calls.filter(([path, init]) => path === '/api/session' && init?.method === 'DELETE')).toHaveLength(1)
    await loginAgain(wrapper)
    expect(menu(wrapper, '我的').attributes('aria-current')).toBe('page')
    expect(wrapper.findComponent(MarketView).exists()).toBe(false)
    await visitTrade(wrapper)
    expect(menu(wrapper, '交易').attributes('aria-current')).toBe('page')
    expect(wrapper.find('.feature-placeholder').exists()).toBe(false)
    expect((wrapper.get('input[type="search"]').element as HTMLInputElement).value).toBe('')
  })

  it('抢购中退出失败保留取消入口和错误，用户可先取消再确认退出', async () => {
    vi.useFakeTimers()
    let attempts = 0
    let resolveLogout!: (response: Response) => void
    const confirmedLogout = new Promise<Response>(resolve => { resolveLogout = resolve })
    const rushState: RushPurchaseState = {
      job_id: 'synthetic-rush', status: 'running', settings: {
        pet_ids: ['1000101'], max_price_gold: 3_000_000, quantity_per_pet: 2, min_balance_gold: 100_000,
      }, balance_gold: '8000000', counts: { '1000101': 0 }, spent_gold: '0', attempts: 0,
      message: '正在监测', updated_at: null,
    }
    const { wrapper, fetcher } = connectedApp({ rushState, logoutRequest: () => ++attempts === 1
      ? Promise.reject(new Error('synthetic-private-logout-error')) : confirmedLogout })
    await flushPromises()
    await visitTrade(wrapper)
    await logoutFromMine(wrapper)
    expect(wrapper.findComponent(MarketView).exists()).toBe(true)
    expect(menu(wrapper, '我的').attributes('aria-current')).toBe('page')
    expect(wrapper.getComponent(MarketView).isVisible()).toBe(false)
    expect(wrapper.get('button[aria-label="取消抢购"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('.session-error[role="alert"]').text()).toBe('退出未确认，请重试，自动任务可能仍在运行')
    expect(wrapper.text()).not.toContain('synthetic-private-logout-error')
    expect(wrapper.get('main[aria-label="个人信息"] button[aria-label="退出登录"]').attributes('disabled')).toBeUndefined()
    await vi.advanceTimersByTimeAsync(2_000)
    expect(fetcher.mock.calls.filter(([path, init]) => path === '/api/session' && init?.method === 'DELETE')).toHaveLength(1)
    await visitTrade(wrapper)
    await wrapper.get('button[aria-label="取消抢购"]').trigger('click')
    await flushPromises()
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/stop')).toHaveLength(1)
    await logoutFromMine(wrapper)
    expect(wrapper.findComponent(MarketView).exists()).toBe(true)
    expect(wrapper.get('main[aria-label="个人信息"] button[aria-label="退出登录"]').attributes('disabled')).toBeDefined()
    resolveLogout(json(anonymous))
    await flushPromises()
    expect(wrapper.findComponent(MarketView).exists()).toBe(false)
    expect(wrapper.find('.session-error').exists()).toBe(false)
    expect(wrapper.find('.login-panel').exists()).toBe(false)
    expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
    const callsAfterLogout = fetcher.mock.calls.length
    await vi.advanceTimersByTimeAsync(3_000)
    expect(fetcher.mock.calls.length).toBe(callsAfterLogout)
  })

  it('抢购设置在菜单间保留，退出后重新登录清空', async () => {
    const { wrapper } = connectedApp()
    await flushPromises()
    await visitTrade(wrapper)
    await wrapper.get('button[aria-label="抢购设置"]').trigger('click')
    const dialog = wrapper.getComponent({ name: 'RushPurchaseSettingsDialog' })
    const settings = { petIds: ['1000101'], maxPriceGold: 3_000_000, quantityPerPet: 2, minBalanceGold: 100_000 }
    dialog.vm.$emit('save', settings)
    await flushPromises()
    await menu(wrapper, '订单').trigger('click')
    await flushPromises()
    await menu(wrapper, '交易').trigger('click')
    await flushPromises()
    await wrapper.get('button[aria-label="抢购设置"]').trigger('click')
    expect(dialog.props('settings')).toEqual(settings)
    dialog.vm.$emit('update:show', false)
    await flushPromises()
    await logoutFromMine(wrapper)
    expect(wrapper.findComponent({ name: 'RushPurchaseSettingsDialog' }).exists()).toBe(false)
    await loginAgain(wrapper)
    await visitTrade(wrapper)
    expect(wrapper.getComponent({ name: 'RushPurchaseSettingsDialog' }).props('settings')).toBeNull()
  })

  it('停在订单时不轮询交易，再次搜索收到401退出会话，重新登录返回交易', async () => {
    vi.useFakeTimers()
    const toast = vi.spyOn(vant, 'showToast')
    const { wrapper, fetcher } = connectedApp({ expireOnSearch: true })
    await flushPromises()
    await visitTrade(wrapper)
    await menu(wrapper, '订单').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()
    expect(wrapper.findComponent(MarketView).exists()).toBe(true)
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))).toHaveLength(0)
    await menu(wrapper, '交易').trigger('click')
    await flushPromises()
    await wrapper.get('button[aria-label="搜索商品"]').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(MarketView).exists()).toBe(false)
    expect(wrapper.find('nav').exists()).toBe(true)
    expect(wrapper.find('.login-panel').exists()).toBe(true)
    expect(wrapper.find('.login-panel .error').exists()).toBe(false)
    expect(wrapper.find('.login-panel .expired').exists()).toBe(false)
    expect(toast).toHaveBeenCalledOnce()
    const notification = document.querySelector('.van-toast') as HTMLElement
    expect(notification.textContent).toBe('登录已过期，请重新登录')
    expect(notification.style.display).not.toBe('none')
    await vi.advanceTimersByTimeAsync(3_000)
    expect(notification.style.display).toBe('none')
    await loginAgain(wrapper)
    expect(menu(wrapper, '交易').attributes('aria-current')).toBe('page')
    expect(wrapper.find('.feature-placeholder').exists()).toBe(false)
  })

  it('同轮重复过期只提示一次，重新登录后再次过期仍提示', async () => {
    const toast = vi.spyOn(vant, 'showToast')
    const { wrapper } = connectedApp()
    await flushPromises()
    await visitTrade(wrapper)
    const market = wrapper.getComponent(MarketView)
    market.vm.$emit('expired')
    market.vm.$emit('expired')
    await flushPromises()
    expect(toast).toHaveBeenCalledOnce()
    expect(wrapper.find('.login-panel .error').exists()).toBe(false)
    expect(wrapper.find('.login-panel .expired').exists()).toBe(false)
    await loginAgain(wrapper)
    wrapper.getComponent(MarketView).vm.$emit('expired')
    await flushPromises()
    expect(toast).toHaveBeenCalledTimes(2)
  })

  it('初始匿名请求401不弹已登录会话过期提示', async () => {
    const toast = vi.spyOn(vant, 'showToast')
    vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(path === '/api/time' ? json(serverTime) : path.includes('capabilities')
      ? json({ integration_ready: true, methods: ['account'] })
      : path === '/api/pets' ? json(emptyCatalog)
      : new Response(JSON.stringify({ error: { code: 'AUTH_REQUIRED', message: '登录已过期，请重新登录' } }),
        { status: 401, headers: { 'Content-Type': 'application/json' } }))))
    const wrapper = mount(App, { attachTo: document.body })
    wrappers.push(wrapper)
    await flushPromises()
    expect(toast).not.toHaveBeenCalled()
    expect(wrapper.find('.login-panel .error').exists()).toBe(false)
    expect(wrapper.find('.login-panel .expired').exists()).toBe(false)
  })

  it.each(['AUTH_REJECTED', 'AUTH_CAPTCHA_CANCELLED'])('账号登录%s仍显示对应错误，不弹过期提示', async code => {
    const toast = vi.spyOn(vant, 'showToast')
    const message = code === 'AUTH_REJECTED' ? '账号登录未通过，请检查输入' : '已取消人工验证'
    vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(path === '/api/auth/login'
      ? new Response(JSON.stringify({ error: { code, message } }),
        { status: 401, headers: { 'Content-Type': 'application/json' } })
      : json(path === '/api/time' ? serverTime : path === '/api/auth/login-challenge' ? loginChallenge()
        : path.includes('capabilities') ? { integration_ready: true, methods: ['account'] }
          : path === '/api/pets' ? emptyCatalog : anonymous))))
    const wrapper = mount(App, { attachTo: document.body })
    wrappers.push(wrapper)
    await flushPromises()
    await loginAgain(wrapper)
    expect(wrapper.get('.login-panel .error').text()).toBe(message)
    expect(toast).not.toHaveBeenCalled()
    expect(wrapper.find('.login-panel .expired').exists()).toBe(false)
  })
})
