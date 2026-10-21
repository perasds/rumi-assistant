import { effectScope } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthCapabilities, LoginCredentials, Session } from '../api/types'
import { encryptLogin } from '../api/loginEncryption'
import { useGameSession } from '../composables/useGameSession'
import LoginView from '../views/LoginView.vue'
import App from '../App.vue'
import { decryptLogin, loginChallenge, testCrypto } from './loginEncryptionFixture'

const scopes: ReturnType<typeof effectScope>[] = []
const wrappers: ReturnType<typeof mount>[] = []
const anonymous: Session = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const connected: Session = { ...anonymous, status: 'connected', authenticated: true }
const capabilities = { provider: 'synthetic', integration_ready: true, methods: ['account'], reason_code: null, message: null, remember_login: true } as AuthCapabilities
const credentials = { account: 'synthetic-account', password: 'synthetic-password' }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(accept => { resolve = accept }); return { promise, resolve } }
function gameWith(session: () => Promise<Response> = () => Promise.resolve(json(anonymous))) {
  const fetch = vi.fn((path: string, init: RequestInit = {}) => {
    if (path === '/api/auth/capabilities') return Promise.resolve(json(capabilities))
    if (path === '/api/session') return init.method === 'DELETE' ? Promise.resolve(json(anonymous)) : session()
    if (path === '/api/auth/login-challenge') return Promise.resolve(json(loginChallenge()))
    if (path === '/api/auth/login') return Promise.resolve(json({ ...connected, message: 'new-login' }))
    if (path === '/api/time') return Promise.resolve(json({ unix_ms: 1_800_000_000_123, time_zone: 'Asia/Shanghai' }))
    if (path === '/api/pets') return Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
    throw new Error(`Unexpected synthetic request: ${path}`)
  })
  vi.stubGlobal('fetch', fetch)
  const scope = effectScope(); scopes.push(scope)
  return { game: scope.run(useGameSession)!, scope, fetch }
}
beforeEach(() => { vi.stubGlobal('crypto', testCrypto) })
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount()); scopes.splice(0).forEach(scope => scope.stop())
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})

describe('移除记住登录', () => {
  it.each([true, false])('旧调用夹带 remember_login=%s 也只加密账号密码', async remember_login => {
    const input = { ...credentials, remember_login } as LoginCredentials
    expect(await decryptLogin(await encryptLogin(input, loginChallenge(), () => true))).toEqual(credentials)
  })
  it('能力响应夹带旧开关也没有复选框，提交仅含账号密码', async () => {
    const wrapper = mount(LoginView, { props: { capabilities, session: anonymous, error: null, busy: false, qrBusy: false, qrChallenge: null, qrState: null } })
    wrappers.push(wrapper); await flushPromises()
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('记住登录')
    await wrapper.get('input[name="account"]').setValue(credentials.account)
    await wrapper.get('input[name="password"]').setValue(credentials.password)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('login')).toEqual([[credentials]])
  })
})

describe('初始化只检查当前会话', () => {
  it.each([anonymous, { ...anonymous, status: 'expired' } as Session, connected])('保留当前 $status 会话且不调用恢复', async current => {
    const { game, fetch } = gameWith(() => Promise.resolve(json(current)))
    await game.initialize()
    expect(game.session.value).toEqual(current); expect(game.busy.value).toBe(false)
    expect(fetch.mock.calls.map(([path]) => path).sort()).toEqual(['/api/auth/capabilities', '/api/session'])
    expect('restoring' in game).toBe(false)
  })
  it('当前会话检查失败按请求错误提示，不自动重试或恢复', async () => {
    vi.useFakeTimers()
    const { game, fetch } = gameWith(() => Promise.resolve(json({ error: { code: 'UNAVAILABLE', message: '当前会话检查暂不可用' } }, 503)))
    await game.initialize(); expect(game.error.value).toBe('当前会话检查暂不可用')
    await vi.advanceTimersByTimeAsync(60_000); expect(fetch).toHaveBeenCalledTimes(2)
  })
  it.each(['logout', 'login', 'unmount'] as const)('%s 取消初始化后迟到响应不能覆盖新状态', async action => {
    const response = deferred<Response>(); let reads = 0
    const { game, fetch, scope } = gameWith(() => ++reads === 1 ? Promise.resolve(json(anonymous)) : response.promise)
    await game.initialize()
    const initialization = game.initialize(); await flushPromises()
    const signal = fetch.mock.calls.filter(([path]) => path === '/api/session').at(-1)![1]!.signal!
    if (action === 'logout') await game.logout(); else if (action === 'login') await game.login(credentials); else scope.stop()
    expect(signal.aborted).toBe(true)
    response.resolve(json({ ...connected, message: 'stale-initialization' })); await initialization
    expect(game.session.value.message).not.toBe('stale-initialization')
    if (action === 'logout') expect(game.session.value).toEqual(anonymous)
    if (action === 'login') expect(game.session.value.message).toBe('new-login')
  })
  it('迟到初始化GET不能覆盖更新的初始化结果', async () => {
    const old = deferred<Response>(); let reads = 0
    const { game, fetch } = gameWith(() => ++reads === 1 ? old.promise : Promise.resolve(json(connected)))
    const first = game.initialize(); const signal = fetch.mock.calls.find(([path]) => path === '/api/session')![1]!.signal!
    await game.initialize(); expect(signal.aborted).toBe(true)
    old.resolve(json(anonymous)); await first; expect(game.connected.value).toBe(true)
  })
  it('公开图鉴保持可见，不显示恢复状态或取消恢复入口', async () => {
    const { fetch } = gameWith(); const wrapper = mount(App); wrappers.push(wrapper); await flushPromises()
    expect(wrapper.find('.pet-book-view').isVisible()).toBe(true)
    expect(wrapper.text()).not.toContain('正在恢复登录'); expect(wrapper.text()).not.toContain('取消恢复')
    expect(fetch.mock.calls.some(([path]) => path === '/api/auth/restore')).toBe(false)
  })
})
