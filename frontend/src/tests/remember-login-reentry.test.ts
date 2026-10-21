import { flushPromises, mount } from '@vue/test-utils'
import { effectScope } from 'vue'
import { afterEach, expect, it, vi } from 'vitest'
import { createMemoryHistory, type Router } from 'vue-router'
import App from '../App.vue'
import { createAppRouter } from '../router'
import { useGameSession } from '../composables/useGameSession'
import type { AuthCapabilities, Session } from '../api/types'

const anonymous: Session = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const expired: Session = { ...anonymous, status: 'expired' }
const connected: Session = { ...anonymous, status: 'connected', authenticated: true, role: { id: 'synthetic-reentry-role', name: '', server_name: 'synthetic-zone' } }
// 旧服务端字段被保留在合成响应，证明客户端不会再恢复登录。
const capabilities = { provider: 'synthetic', integration_ready: true, methods: ['account'], remember_login: true, reason_code: null, message: null } as AuthCapabilities
const profile = { role_id: '123', nickname: null, server_name: 'synthetic-zone', balance_gold: '100' }
const wrappers: ReturnType<typeof mount>[] = []
const routers: Router[] = []
const scopes: ReturnType<typeof effectScope>[] = []
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(accept => { resolve = accept }); return { promise, resolve } }
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount()); routers.splice(0).forEach(router => router.options.history.destroy())
  scopes.splice(0).forEach(scope => scope.stop()); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
})
async function setup(options: { firstBusinessRequestExpired?: boolean; initialSession?: Session; nextSession?: Session; initialResponse?: () => Promise<Response>; nextResponse?: () => Promise<Response>; businessResponse?: () => Promise<Response> } = {}) {
  let reads = 0, rejectBusiness = options.firstBusinessRequestExpired ?? false
  const fetcher = vi.fn((path: string, init: RequestInit = {}) => {
    if (path === '/api/auth/capabilities') return Promise.resolve(json(capabilities))
    if (path === '/api/session') {
      if (init.method === 'DELETE') return Promise.resolve(json(anonymous))
      return reads++ === 0 ? options.initialResponse?.() ?? Promise.resolve(json(options.initialSession ?? connected)) : options.nextResponse?.() ?? Promise.resolve(json(options.nextSession ?? expired))
    }
    if (path === '/api/auth/restore') return Promise.resolve(json(connected))
    if (path === '/api/me') return rejectBusiness ? options.businessResponse?.() ?? Promise.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '登录已过期，请重新登录' } }, 401)) : Promise.resolve(json(profile))
    throw new Error(`Unexpected synthetic reentry request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const router = createAppRouter(createMemoryHistory()); routers.push(router); await router.push('/mine'); await router.isReady()
  const wrapper = mount(App, { global: { plugins: [router], stubs: { PetBookView: { template: '<main />' }, ServerClock: { template: '<span />' } } } })
  wrappers.push(wrapper); await flushPromises()
  return { wrapper, router, fetcher, rejectBusiness: () => { rejectBusiness = true } }
}
function noAutomaticLogin(fetcher: ReturnType<typeof vi.fn>) {
  expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/restore' || path === '/api/auth/login' || path === '/api/auth/sms/login' || String(path).endsWith('/start'))).toBe(false)
}
function visible() { vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible'); document.dispatchEvent(new Event('visibilitychange')) }

it('已连接后业务401保持登录页，显式再次登录也不恢复旧凭据', async () => {
  const { wrapper, fetcher, rejectBusiness } = await setup()
  expect(wrapper.classes()).toContain('is-connected'); rejectBusiness()
  await wrapper.get('button[aria-label="刷新个人信息"]').trigger('click'); await flushPromises()
  expect(wrapper.classes()).not.toContain('is-connected'); expect(wrapper.find('input[name="password"]').exists()).toBe(true)
  await wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === '我的')!.trigger('click'); await flushPromises()
  expect(wrapper.classes()).not.toContain('is-connected'); expect(wrapper.find('input[name="password"]').exists()).toBe(true)
  expect(wrapper.text()).not.toContain('正在恢复登录'); noAutomaticLogin(fetcher)
})
it('整页加载的旧authenticated被业务401驳回后只能手动登录', async () => {
  const { wrapper, fetcher } = await setup({ firstBusinessRequestExpired: true })
  expect(fetcher.mock.calls.some(([path]) => path === '/api/me')).toBe(true)
  expect(wrapper.classes()).not.toContain('is-connected'); expect(wrapper.find('input[name="password"]').exists()).toBe(true)
  noAutomaticLogin(fetcher)
})
it('整页加载匿名状态直接显示登录表单，无恢复请求', async () => {
  const { wrapper, fetcher } = await setup({ initialSession: anonymous })
  expect(wrapper.classes()).not.toContain('is-connected'); expect(wrapper.find('input[name="password"]').exists()).toBe(true)
  noAutomaticLogin(fetcher)
})
it.each(['visibility', 'pageshow'] as const)('%s重入只检查当前会话，过期后保留登录页且不定时重试', async event => {
  const { wrapper, fetcher } = await setup()
  if (event === 'visibility') { visible(); visible() }
  else { window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: false })); await flushPromises(); expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1); window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })) }
  await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(2)
  expect(wrapper.classes()).not.toContain('is-connected'); expect(wrapper.find('input[name="password"]').exists()).toBe(true)
  vi.useFakeTimers(); await vi.advanceTimersByTimeAsync(60_000)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(2); noAutomaticLogin(fetcher)
})
it('重入检查到正常会话时继续使用当前连接', async () => {
  const { wrapper, fetcher } = await setup({ nextSession: connected }); visible(); await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(2)
  expect(wrapper.classes()).toContain('is-connected'); noAutomaticLogin(fetcher)
})
it('会话检查繁忙时重入事件不会打断或重复检查', async () => {
  const pending = deferred<Response>(); const { fetcher } = await setup({ initialResponse: () => pending.promise })
  const signal = fetcher.mock.calls.find(([path]) => path === '/api/session')![1]!.signal!
  visible(); window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })); await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1); expect(signal.aborted).toBe(false)
  pending.resolve(json(connected)); await flushPromises(); noAutomaticLogin(fetcher)
})
it.each(['logout', 'unmount'] as const)('%s后不再响应重入事件', async action => {
  const { wrapper, fetcher } = await setup()
  if (action === 'logout') { await wrapper.get('button[aria-label="退出登录"]').trigger('click'); await flushPromises() }
  else { wrapper.unmount(); wrappers.splice(wrappers.indexOf(wrapper), 1) }
  const count = fetcher.mock.calls.length; visible(); window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })); await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(count); noAutomaticLogin(fetcher)
})
it('检查期间旧业务迟到401不取消当前检查，检查返回过期后不恢复', async () => {
  const business = deferred<Response>(), session = deferred<Response>()
  const { wrapper, fetcher, rejectBusiness } = await setup({ businessResponse: () => business.promise, nextResponse: () => session.promise })
  rejectBusiness(); await wrapper.get('button[aria-label="刷新个人信息"]').trigger('click'); await flushPromises(); visible(); await flushPromises()
  const signal = fetcher.mock.calls.filter(([path]) => path === '/api/session').at(-1)![1]!.signal!
  business.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '合成旧请求已失效' } }, 401)); await flushPromises()
  expect(signal.aborted).toBe(false); session.resolve(json(expired)); await flushPromises()
  expect(wrapper.classes()).not.toContain('is-connected'); expect(wrapper.find('input[name="password"]').exists()).toBe(true); noAutomaticLogin(fetcher)
})
it('二维码创建繁忙或挑战有效时重入不打断操作', async () => {
  const pending = deferred<Response>()
  const fetcher = vi.fn((path: string) => {
    if (path === '/api/auth/capabilities') return Promise.resolve(json({ ...capabilities, methods: ['account', 'qr'] }))
    if (path === '/api/session') return Promise.resolve(json(connected))
    if (path === '/api/auth/qr') return pending.promise
    throw new Error(`Unexpected synthetic QR request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher); const scope = effectScope(); scopes.push(scope); const game = scope.run(useGameSession)!
  await game.initialize(); const action = game.createQr(); visible(); await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1)
  pending.resolve(json({ challenge_id: 'synthetic-qr', qr_payload: 'synthetic-only', expires_at: new Date(Date.now() + 60_000).toISOString() })); await action
  window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })); await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(1); noAutomaticLogin(fetcher)
})
it('重入检查自身401会使会话失效', async () => {
  const { wrapper, fetcher } = await setup({ nextResponse: () => Promise.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '当前会话已失效' } }, 401)) })
  visible(); await flushPromises()
  expect(wrapper.classes()).not.toContain('is-connected'); expect(wrapper.find('input[name="password"]').exists()).toBe(true); noAutomaticLogin(fetcher)
})
