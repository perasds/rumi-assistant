import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, type Router } from 'vue-router'
import App from '../App.vue'
import { createAppRouter } from '../router'
import type { AuthCapabilities, Session } from '../api/types'
import { loginChallenge, testCrypto } from './loginEncryptionFixture'

const notice = '工具适配版本：1.5.3 / 热更 72；游戏实际版本：1.5.3 / 热更 73'
const anonymous: Session = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const connected: Session = { status: 'connected', authenticated: true, roles: [], message: notice,
  role: { id: 'synthetic-version-role', name: '合成角色', server_name: '合成区服' } }
const capabilities: AuthCapabilities = { provider: 'synthetic', integration_ready: true, methods: ['account'],
  reason_code: null, message: null }
const wrappers: ReturnType<typeof mount>[] = []
const routers: Router[] = []
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

beforeEach(() => { vi.stubGlobal('crypto', testCrypto) })
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  routers.splice(0).forEach(router => router.options.history.destroy())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

async function setup(options: {
  path?: string; initialSession?: Session; loginResponse?: Response
  initialResponse?: () => Promise<Response>; logout?: () => Promise<Response>
} = {}) {
  const fetcher = vi.fn((path: string, init: RequestInit = {}) => {
    if (path === '/api/auth/capabilities') return Promise.resolve(json(capabilities))
    if (path === '/api/session' && init.method === 'DELETE') return options.logout?.() ?? Promise.resolve(json(anonymous))
    if (path === '/api/session') return options.initialResponse?.() ?? Promise.resolve(json(options.initialSession ?? anonymous))
    if (path === '/api/auth/login-challenge') return Promise.resolve(json(loginChallenge()))
    if (path === '/api/auth/login') return Promise.resolve(options.loginResponse ?? json(connected))
    if (path === '/api/session/role') return Promise.resolve(json(connected))
    if (path === '/api/me') return Promise.resolve(json({ role_id: connected.role!.id, nickname: null,
      server_name: connected.role!.server_name, balance_gold: '100' }))
    throw new Error(`Unexpected synthetic version notice request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const router = createAppRouter(createMemoryHistory())
  routers.push(router)
  await router.push(options.path ?? '/book')
  await router.isReady()
  const wrapper = mount(App, { global: { plugins: [router], stubs: {
    PetBookView: { template: '<main />' }, MarketView: { template: '<main />' },
    OrderView: { template: '<main />' }, ServerClock: { template: '<span />' }, AutoRedbagPanel: true,
  } } })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router }
}

async function submitLogin(wrapper: ReturnType<typeof mount>) {
  await wrapper.get('input[name="account"]').setValue('synthetic-account')
  await wrapper.get('input[name="password"]').setValue('synthetic-password')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  await vi.waitFor(() => expect(wrapper.find('.van-button--loading').exists()).toBe(false))
}

function menu(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === label)!
}

describe('登录后的版本差异提示', () => {
  it('手动登录后显示后台原文，并按纯文本输出', async () => {
    const message = `${notice} <b>合成文本</b>`
    const { wrapper } = await setup({ path: '/login', loginResponse: json({ ...connected, message }) })
    expect(wrapper.find('.session-notice').exists()).toBe(false)
    await submitLogin(wrapper)
    expect(wrapper.get('.session-notice[role="status"]').text()).toBe(message)
    expect(wrapper.get('.session-notice').find('b').exists()).toBe(false)
    expect(wrapper.find('.session-error').exists()).toBe(false)
  })

  it('当前会话检查完成后显示，检查期间不显示恢复状态', async () => {
    let finishSession!: (response: Response) => void
    const current = new Promise<Response>(resolve => { finishSession = resolve })
    const { wrapper } = await setup({ initialResponse: () => current })
    expect(wrapper.find('.restore-status').exists()).toBe(false)
    expect(wrapper.find('.session-notice').exists()).toBe(false)
    finishSession(json(connected))
    await flushPromises()
    expect(wrapper.get('.session-notice').text()).toBe(notice)
  })

  it.each([null, ''])('版本匹配且 message 为 %s 时不显示提示', async message => {
    const { wrapper } = await setup({ initialSession: { ...connected, message } })
    expect(wrapper.classes()).toContain('is-connected')
    expect(wrapper.find('.session-notice').exists()).toBe(false)
  })

  it('匿名会话即使带 message，登录失败后仍不显示版本提示', async () => {
    const { wrapper } = await setup({ path: '/login', initialSession: { ...anonymous, message: notice },
      loginResponse: json({ error: { code: 'AUTH_REQUIRED', message: '合成登录失败' } }, 401) })
    expect(wrapper.find('.session-notice').exists()).toBe(false)
    await submitLogin(wrapper)
    expect(wrapper.get('.login-panel .error').text()).toBe('合成登录失败')
    expect(wrapper.find('.session-notice').exists()).toBe(false)
  })

  it('选择角色连接成功后显示，选择前不显示', async () => {
    const { wrapper } = await setup({ path: '/login', initialSession: { ...anonymous,
      status: 'role_selection', roles: [connected.role!], message: notice } })
    expect(wrapper.find('.session-notice').exists()).toBe(false)
    await wrapper.get('.role-selector button.confirm').trigger('click')
    await flushPromises()
    expect(wrapper.get('.session-notice').text()).toBe(notice)
  })

  it('切换交易、订单和我的时保留提示，退出后移除', async () => {
    const { wrapper, router } = await setup({ initialSession: connected })
    for (const [label, path] of [['交易', '/trade'], ['订单', '/orders'], ['我的', '/mine']] as const) {
      await menu(wrapper, label).trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.path).toBe(path)
      expect(menu(wrapper, label).attributes('aria-current')).toBe('page')
      expect(wrapper.get('.session-notice').isVisible()).toBe(true)
      expect(wrapper.get('.session-notice').text()).toBe(notice)
    }
    await wrapper.get('button[aria-label="退出登录"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/book')
    expect(wrapper.find('.session-notice').exists()).toBe(false)
  })

  it('操作失败的错误条与版本提示独立显示', async () => {
    const { wrapper } = await setup({ path: '/mine', initialSession: connected,
      logout: () => Promise.resolve(json({ error: { code: 'UPSTREAM_UNAVAILABLE', message: '合成退出失败' } }, 503)) })
    await wrapper.get('button[aria-label="退出登录"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.session-error[role="alert"]').text()).toBe('退出未确认，请重试，自动任务可能仍在运行')
    expect(wrapper.get('.session-notice[role="status"]').text()).toBe(notice)
  })
})
