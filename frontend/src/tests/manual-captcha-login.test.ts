import { effectScope } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeToast } from 'vant'
import { encryptLogin } from '../api/loginEncryption'
import { useGameSession } from '../composables/useGameSession'
import App from '../App.vue'

vi.mock('../api/loginEncryption', () => ({ encryptLogin: vi.fn() }))
const scopes: ReturnType<typeof effectScope>[] = []
const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => { vi.useFakeTimers(); vi.mocked(encryptLogin).mockResolvedValue(encrypted) })
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  scopes.splice(0).forEach(scope => scope.stop())
  closeToast(true); document.body.innerHTML = ''; vi.useRealTimers(); vi.unstubAllGlobals()
})
const id = 'c'.repeat(32)
const frame = { status: 'active', challenge_id: id, width: 520, height: 900, image: 'data:image/png;base64,iVBORw0KGgo=' }
const anonymous = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const connected = { ...anonymous, status: 'connected', authenticated: true, role: { id: 'synthetic', name: '', server_name: '' } }
const encrypted = { challenge_id: 'e'.repeat(32), encrypted_key: 'synthetic', iv: 'synthetic', ciphertext: 'synthetic' }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function pending() {
  let resolve!: (value: Response) => void
  const promise = new Promise<Response>(accept => { resolve = accept })
  return { promise, resolve: (body: unknown, status = 200) => resolve(json(body, status)) }
}
function setup() {
  const loginResponse = pending()
  const fetcher = vi.fn((path: string, init: RequestInit) => {
    if (path === '/api/time') return Promise.resolve(json({ unix_ms: 1_800_000_000_123, time_zone: 'Asia/Shanghai' }))
    if (path === '/api/auth/capabilities') return Promise.resolve(json({ integration_ready: true, methods: ['account'] }))
    if (path === '/api/auth/login-challenge') return Promise.resolve(json({ status: 'synthetic-encryption-challenge' }))
    if (path === '/api/session') return Promise.resolve(json(anonymous))
    if (path === '/api/pets') return Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
    if (path === '/api/auth/login') return loginResponse.promise
    if (path === '/api/auth/captcha') return Promise.resolve(json(frame))
    if (path === `/api/auth/captcha/${id}` && init.method === 'DELETE') {
      loginResponse.resolve({ error: { code: 'AUTH_CAPTCHA_CANCELLED', message: '已取消人工验证' } }, 401)
      return Promise.resolve(json({ status: 'cancelled' }))
    }
    throw new Error('Unexpected synthetic request')
  })
  vi.stubGlobal('fetch', fetcher)
  const scope = effectScope(); scopes.push(scope)
  return { game: scope.run(useGameSession)!, scope, fetcher, loginResponse }
}
describe('账号登录中的网页人工验证', () => {
  it('只有加密payload已提交才开始轮询，登录响应后立即停止且保留加密请求体', async () => {
    let finishEncryption!: (value: typeof encrypted) => void
    vi.mocked(encryptLogin).mockImplementation(() => new Promise(accept => { finishEncryption = accept }))
    const { game, fetcher, loginResponse } = setup()
    await game.initialize()
    const login = game.login({ account: 'synthetic-account', password: 'synthetic-password' })
    await flushPromises(); await vi.advanceTimersByTimeAsync(1200)
    expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/captcha')).toBe(false)
    finishEncryption(encrypted); await flushPromises()
    expect(JSON.parse(fetcher.mock.calls.find(([path]) => path === '/api/auth/login')![1].body as string)).toEqual(encrypted)
    await vi.advanceTimersByTimeAsync(600)
    expect(game.captchaChallenge.value?.challenge_id).toBe(id)
    loginResponse.resolve(connected); await login
    expect(game.connected.value).toBe(true)
    expect(game.captchaChallenge.value).toBeNull()
    const calls = fetcher.mock.calls.length
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetcher).toHaveBeenCalledTimes(calls)
  })

  it('captcha轮询401不覆盖login拒绝的实质错误', async () => {
    const { game, fetcher, loginResponse } = setup()
    const original = fetcher.getMockImplementation()!
    fetcher.mockImplementation((path, init) => path === '/api/auth/captcha'
      ? Promise.resolve(json({ error: { code: 'AUTH_REQUIRED', message: 'private-captcha-message' } }, 401)) : original(path, init))
    await game.initialize()
    const login = game.login({ account: 'synthetic', password: 'synthetic' })
    await flushPromises(); await vi.advanceTimersByTimeAsync(600)
    expect(game.busy.value).toBe(true)
    expect(game.error.value).toBeNull()
    loginResponse.resolve({ error: { code: 'AUTH_REJECTED', message: '账号登录未通过，请检查输入' } }, 401)
    await login
    expect(game.error.value).toBe('账号登录未通过，请检查输入')
    expect(game.captchaChallenge.value).toBeNull()
  })

  it('新登录已出现验证后，旧登录响应不能关闭新弹窗或清除busy', async () => {
    const { game, fetcher, loginResponse } = setup()
    const original = fetcher.getMockImplementation()!
    const nextResponse = pending()
    let logins = 0
    fetcher.mockImplementation((path, init) => path === '/api/auth/login'
      ? (++logins === 1 ? loginResponse.promise : nextResponse.promise) : original(path, init))
    await game.initialize()
    const old = game.login({ account: 'synthetic-old', password: 'synthetic' })
    await flushPromises(); game.expire()
    const next = game.login({ account: 'synthetic-next', password: 'synthetic' })
    await flushPromises(); await vi.advanceTimersByTimeAsync(600)
    expect(game.captchaChallenge.value?.challenge_id).toBe(id)
    loginResponse.resolve(connected); await old
    expect(game.busy.value).toBe(true)
    expect(game.captchaChallenge.value?.challenge_id).toBe(id)
    nextResponse.resolve(connected); await next
    expect(game.captchaChallenge.value).toBeNull()
  })

  it.each(['expire', 'unmount'])('%s取消轮询，迟到截图与登录响应均不恢复旧会话', async mode => {
    const { game, scope, fetcher, loginResponse } = setup()
    const original = fetcher.getMockImplementation()!
    const late = pending()
    fetcher.mockImplementation((path, init) => path === '/api/auth/captcha' ? late.promise : original(path, init))
    await game.initialize()
    const login = game.login({ account: 'synthetic', password: 'synthetic' })
    await flushPromises(); await vi.advanceTimersByTimeAsync(600)
    if (mode === 'expire') game.expire(); else scope.stop()
    late.resolve(frame); loginResponse.resolve(connected)
    await login; await flushPromises()
    expect(game.connected.value).toBe(false)
    expect(game.captchaChallenge.value).toBeNull()
    const count = fetcher.mock.calls.length
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetcher).toHaveBeenCalledTimes(count)
  })

  it('App显示人工验证码，点击取消调用DELETE并结束登录弹窗', async () => {
    const { fetcher } = setup()
    const wrapper = mount(App, { attachTo: document.body, global: { stubs: { teleport: true } } })
    wrappers.push(wrapper)
    await flushPromises()
    await wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === '我的')!.trigger('click')
    await flushPromises()
    await wrapper.get('input[name="account"]').setValue('synthetic')
    await wrapper.get('input[name="password"]').setValue('synthetic')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.find('.captcha-image').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(600)
    expect(wrapper.get('.captcha-image').attributes('src')).toBe(frame.image)
    await wrapper.get('button[aria-label="取消人工验证"]').trigger('click'); await flushPromises()
    expect(fetcher.mock.calls.filter(([path, init]) => path === `/api/auth/captcha/${id}` && init.method === 'DELETE')).toHaveLength(1)
    expect(wrapper.find('.captcha-image').exists()).toBe(false)
    expect(wrapper.text()).toContain('已取消人工验证')
  })
})
