import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as vant from 'vant'
import App from '../App.vue'
import MarketView from '../views/MarketView.vue'
import HomeView from '../views/HomeView.vue'
import type { Session } from '../api/types'
import { loginChallenge, testCrypto } from './loginEncryptionFixture'
import { orderState } from './orderFixtures'

const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => { vi.stubGlobal('crypto', testCrypto) })
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vant.closeToast(true)
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
const anonymous: Session = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const connected: Session = { ...anonymous, status: 'connected', authenticated: true }
const capabilities = { provider: 'synthetic', integration_ready: true, methods: ['account'], message: null, reason_code: null }
const pets = [
  { pet_id: '101', name: '测试蜜桃', display_name: '测试蜜桃', name_verified: true, image_url: null },
  { pet_id: '102', name: '测试灰灰', display_name: '测试灰灰', name_verified: true, image_url: null },
]
function setup(options: { connected?: boolean; capabilitiesRequest?: () => Promise<Response>; logoutRequest?: () => Promise<Response> } = {}) {
  let authenticated = options.connected ?? false
  const fetcher = vi.fn((path: string, init: RequestInit = {}) => {
    if (path === '/api/time') return Promise.resolve(json({ unix_ms: 1_800_000_000_123, time_zone: 'Asia/Shanghai' }))
    if (path === '/api/auth/capabilities') return options.capabilitiesRequest?.() ?? Promise.resolve(json(capabilities))
    if (path === '/api/auth/login-challenge') return Promise.resolve(json(loginChallenge()))
    if (path === '/api/auth/captcha') return Promise.resolve(json({ status: 'waiting' }))
    if (path === '/api/auth/login') { authenticated = true; return Promise.resolve(json(connected)) }
    if (path === '/api/session' && init.method === 'DELETE') {
      if (options.logoutRequest) return options.logoutRequest()
      authenticated = false
      return Promise.resolve(json(anonymous))
    }
    if (path === '/api/session') return Promise.resolve(json(authenticated ? connected : anonymous))
    if (path === '/api/me') return Promise.resolve(json({
      role_id: 'synthetic-game-role', nickname: '合成角色', server_name: '合成区服', balance_gold: '20000000',
    }))
    if (path === '/api/home/orders/state') return Promise.resolve(json(orderState()))
    if (path === '/api/pets') return Promise.resolve(json({ items: pets, total: 2, named_count: 2, image_count: 0 }))
    if (path === '/api/pets/101') return Promise.resolve(json({ ...pets[0], available: true, pokedex_id: '001',
      elements: [], stats: [], normal_attack: null, traits: [], fixed_skill: null, replaceable_skills: [], missing_fields: [] }))
    if (path === '/api/home/production') return Promise.resolve(json({ scope: 'configuration', buildings: [], total: 0, recipe_count: 0 }))
    if (path === '/api/rush-purchase') return Promise.resolve(json({ job_id: null, status: 'idle', settings: null,
      balance_gold: null, counts: {}, spent_gold: '0', message: null, attempts: 0, updated_at: null }))
    if (path === '/api/market/refresh') return Promise.resolve(json({ refresh_id: 'synthetic', scope: 'top10',
      status: 'complete', complete: true, items: [], loaded_count: 0, matched_count: 0, total_count: 0,
      started_at: null, updated_at: null, message: null, name_coverage: { named: 2, total: 2 } }))
    throw new Error(`Unexpected synthetic request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(App, { attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}
function menu(wrapper: ReturnType<typeof mount>, label: string) {
  expect(wrapper.find('nav[aria-label="功能导航"]').exists()).toBe(true)
  return wrapper.get('nav').findAll('button').find(button => button.text() === label)!
}
async function submitLogin(wrapper: ReturnType<typeof mount>) {
  await wrapper.get('input[name="account"]').setValue('synthetic-account')
  await wrapper.get('input[name="password"]').setValue('synthetic-password')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('.van-button--loading').exists()).toBe(false))
}
async function logoutFromMine(wrapper: ReturnType<typeof mount>) {
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  await wrapper.get('main[aria-label="个人信息"] button[aria-label="退出登录"]').trigger('click')
  await flushPromises()
}
const privateCalls = (fetcher: ReturnType<typeof setup>['fetcher']) => fetcher.mock.calls.filter(([path]) =>
  path === '/api/me' || path.startsWith('/api/home/') || path.startsWith('/api/market/') || path.startsWith('/api/rush-purchase'))

it('匿名默认公开图鉴，登录能力请求尚未完成也能阅读，四菜单持续可用', async () => {
  let resolve!: (response: Response) => void
  const pending = new Promise<Response>(done => { resolve = done })
  const { wrapper, fetcher } = setup({ capabilitiesRequest: () => pending })
  await flushPromises()
  expect(wrapper.find('.pet-book-view').exists()).toBe(true)
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
  expect(wrapper.get('nav').findAll('button').map(button => button.text())).toEqual(['交易', '图鉴', '订单', '我的'])
  expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
  expect(wrapper.get('.pet-book-grid').text()).toContain('测试蜜桃')
  expect(wrapper.find('.login-panel').exists()).toBe(false)
  expect(wrapper.findComponent(MarketView).exists()).toBe(false)
  expect(wrapper.findComponent(HomeView).exists()).toBe(false)
  expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
  expect(privateCalls(fetcher)).toHaveLength(0)
  resolve(json({ ...capabilities, integration_ready: false, methods: [], message: '合成不可用' }))
  await flushPromises()
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
  expect(wrapper.find('header button').exists()).toBe(false)
})

it('已有登录会话首次进入也默认图鉴，不提前获取交易或个人资料', async () => {
  const { wrapper, fetcher } = setup({ connected: true })
  await flushPromises()
  expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
  expect(wrapper.find('header button').exists()).toBe(false)
  expect(wrapper.findComponent({ name: 'ServerClock' }).exists()).toBe(false)
  expect(wrapper.findComponent(MarketView).exists()).toBe(false)
  expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
  expect(privateCalls(fetcher)).toHaveLength(0)
})

it.each(['network', 'unauthorized'])('认证初始化%s失败不阻挡公开图鉴，也不误报已登录会话过期', async mode => {
  const toast = vi.spyOn(vant, 'showToast')
  const { wrapper, fetcher } = setup({ capabilitiesRequest: () => mode === 'network'
    ? Promise.reject(new Error('synthetic-private-auth-error'))
    : Promise.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '合成未登录' } }, 401)) })
  await flushPromises()
  expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
  expect(wrapper.get('.pet-book-grid').text()).toContain('测试蜜桃')
  expect(wrapper.find('.login-panel').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('synthetic-private-auth-error')
  expect(toast).not.toHaveBeenCalled()
  expect(privateCalls(fetcher)).toHaveLength(0)
})

it('匿名图鉴可搜索和查看详情，不启动登录请求', async () => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(wrapper.find('.pet-book-view').exists()).toBe(true)
  const book = wrapper.get('.pet-book-view')
  await book.get('input[type="search"]').setValue('蜜桃')
  expect(book.findAll('.pet-book-grid button')).toHaveLength(1)
  await book.get('button[data-pet-id="101"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('.pet-detail-popup').text()).toContain('测试蜜桃')
  expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/login')).toBe(false)
  expect(privateCalls(fetcher)).toHaveLength(0)
})

it.each(['交易', '订单', '我的'])('匿名点%s只显示登录页，可以通过底栏返回原图鉴', async label => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(wrapper.find('.pet-book-view').exists()).toBe(true)
  await wrapper.get('.pet-book-view input[type="search"]').setValue('蜜桃')
  await menu(wrapper, label).trigger('click')
  await flushPromises()
  expect(wrapper.find('.login-panel').exists()).toBe(true)
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(false)
  expect(wrapper.findComponent(MarketView).exists()).toBe(false)
  expect(wrapper.findComponent(HomeView).exists()).toBe(false)
  expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
  expect(privateCalls(fetcher)).toHaveLength(0)
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(wrapper.find('.login-panel').exists()).toBe(false)
  expect((wrapper.get('.pet-book-view input').element as HTMLInputElement).value).toBe('蜜桃')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(1)
})

it.each(['交易', '订单', '我的'])('从%s发起登录，成功后进入刚才所选菜单', async label => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  await menu(wrapper, label).trigger('click')
  await flushPromises()
  await submitLogin(wrapper)
  expect(menu(wrapper, label).attributes('aria-current')).toBe('page')
  expect(wrapper.find('.login-panel').exists()).toBe(false)
  expect(wrapper.find('header button').exists()).toBe(false)
  if (label === '交易') expect(wrapper.getComponent(MarketView).isVisible()).toBe(true)
  else {
    expect(wrapper.findComponent(MarketView).exists()).toBe(false)
    expect(fetcher.mock.calls.some(([path]) => path.startsWith('/api/market/'))).toBe(false)
  }
  expect(wrapper.findComponent(HomeView).exists()).toBe(false)
  if (label === '我的') {
    expect(wrapper.get('main[aria-label="个人信息"]').isVisible()).toBe(true)
    expect(wrapper.get('main[aria-label="个人信息"]').text()).toContain('合成角色')
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/me')).toHaveLength(1)
  } else {
    expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
    expect(fetcher.mock.calls.some(([path]) => path === '/api/me')).toBe(false)
  }
  if (label === '订单') expect(wrapper.get('main[aria-label="订单助手"]').text()).toContain('任务设置')
})

it('匿名顶栏仅保留品牌且不请求时间，从我的进入登录后可以返回图鉴', async () => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(wrapper.find('header button').exists()).toBe(false)
  expect(wrapper.get('header').text()).toBe('噜咪小助手')
  expect(fetcher.mock.calls.some(([path]) => path === '/api/time')).toBe(false)
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  expect(wrapper.find('.login-panel').exists()).toBe(true)
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(wrapper.find('.login-panel').exists()).toBe(false)
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  await submitLogin(wrapper)
  expect(menu(wrapper, '我的').attributes('aria-current')).toBe('page')
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
  expect(wrapper.findComponent(MarketView).exists()).toBe(false)
  expect(fetcher.mock.calls.some(([path]) => path.startsWith('/api/market/'))).toBe(false)
})

it('交易首次访问后才挂载，菜单往返保留搜索，不重复初始化交易', async () => {
  const { wrapper, fetcher } = setup({ connected: true })
  await flushPromises()
  expect(wrapper.findComponent(MarketView).exists()).toBe(false)
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  const market = wrapper.getComponent(MarketView)
  await market.get('input[type="search"]').setValue('合成查询')
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  expect((market.get('input[type="search"]').element as HTMLInputElement).value).toBe('合成查询')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase')).toHaveLength(1)
})

it('我的只在选中且已登录时挂载，离开清除个人页面，返回重新获取资料', async () => {
  const { wrapper, fetcher } = setup({ connected: true })
  await flushPromises()
  expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  expect(wrapper.get('main[aria-label="个人信息"]').text()).toContain('合成角色')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/me')).toHaveLength(1)
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('合成角色')
  expect(wrapper.findComponent(HomeView).exists()).toBe(false)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/me')).toHaveLength(1)
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  expect(wrapper.get('main[aria-label="个人信息"]').text()).toContain('合成角色')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/me')).toHaveLength(2)
})

it('成功退出回到公开图鉴，保留图鉴搜索缓存并卸载私有状态', async () => {
  const { wrapper, fetcher } = setup({ connected: true })
  await flushPromises()
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  await wrapper.get('.pet-book-view input[type="search"]').setValue('蜜桃')
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  const catalogCallsBeforeLogout = fetcher.mock.calls.filter(([path]) => path === '/api/pets').length
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  expect(wrapper.get('main[aria-label="个人信息"]').text()).toContain('合成角色')
  await logoutFromMine(wrapper)
  expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
  expect(wrapper.find('.login-panel').exists()).toBe(false)
  expect(wrapper.find('header button').exists()).toBe(false)
  expect(wrapper.findComponent(MarketView).exists()).toBe(false)
  expect(wrapper.findComponent(HomeView).exists()).toBe(false)
  expect(wrapper.find('main[aria-label="个人信息"]').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('合成角色')
  expect((wrapper.get('.pet-book-view input').element as HTMLInputElement).value).toBe('蜜桃')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(catalogCallsBeforeLogout)
})

it('个人中心退出失败保留我的和退出错误，不能伪装成已退出，可返回交易', async () => {
  const { wrapper } = setup({ connected: true, logoutRequest: () => Promise.reject(new Error('synthetic-private-logout')) })
  await flushPromises()
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  await logoutFromMine(wrapper)
  expect(menu(wrapper, '我的').attributes('aria-current')).toBe('page')
  expect(wrapper.getComponent(MarketView).isVisible()).toBe(false)
  expect(wrapper.get('main[aria-label="个人信息"] button[aria-label="退出登录"]').text()).toBe('退出登录')
  expect(wrapper.find('header button').exists()).toBe(false)
  expect(wrapper.get('.session-error').text()).toContain('退出未确认')
  expect(wrapper.text()).not.toContain('synthetic-private-logout')
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(MarketView).isVisible()).toBe(true)
})

it('私有页面过期显示登录与一次轻提示，公开图鉴仍可返回', async () => {
  const toast = vi.spyOn(vant, 'showToast')
  const { wrapper } = setup({ connected: true })
  await flushPromises()
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  const market = wrapper.getComponent(MarketView)
  market.vm.$emit('expired')
  market.vm.$emit('expired')
  await flushPromises()
  expect(wrapper.find('.login-panel').exists()).toBe(true)
  expect(wrapper.findComponent(MarketView).exists()).toBe(false)
  expect(menu(wrapper, '交易').attributes('aria-current')).toBe('page')
  expect(toast).toHaveBeenCalledOnce()
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
})

it('在图鉴阅读期间过期只卸载私有状态，不劫持图鉴或关闭已打开详情', async () => {
  const { wrapper, fetcher } = setup({ connected: true })
  await flushPromises()
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  const market = wrapper.getComponent(MarketView)
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  await wrapper.get('.pet-book-view button[data-pet-id="101"]').trigger('click')
  await flushPromises()
  const catalogCallsBeforeExpiry = fetcher.mock.calls.filter(([path]) => path === '/api/pets').length
  market.vm.$emit('expired')
  await flushPromises()
  expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
  expect(wrapper.get('.pet-detail-popup').text()).toContain('测试蜜桃')
  expect(wrapper.find('.login-panel').exists()).toBe(false)
  expect(wrapper.findComponent(MarketView).exists()).toBe(false)
  expect(wrapper.findComponent(HomeView).exists()).toBe(false)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(catalogCallsBeforeExpiry)
})
