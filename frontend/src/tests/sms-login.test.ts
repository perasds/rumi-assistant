import { effectScope } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Field as VanField } from 'vant'
import { useGameSession } from '../composables/useGameSession'
import LoginView from '../views/LoginView.vue'
import SmsLoginForm from '../components/auth/SmsLoginForm.vue'
import App from '../App.vue'
import { decryptLogin, loginChallenge, testCrypto } from './loginEncryptionFixture'

const anonymous = { status: 'anonymous' as const, authenticated: false, role: null, roles: [], message: null }
const connected = { ...anonymous, status: 'connected', authenticated: true }
const capabilities = { provider: 'synthetic', integration_ready: true, methods: ['account', 'sms'] as const, reason_code: null, message: null }
const contact = { mobile: '12345678901', country_code: '86' }
const credentials = { ...contact, otp: '123456' }
const scopes: ReturnType<typeof effectScope>[] = []
const wrappers: ReturnType<typeof mount>[] = []
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(accept => { resolve = accept }); return { promise, resolve } }
function setup(options: { send?: () => Promise<Response>; login?: () => Promise<Response>; challenge?: () => Promise<Response> } = {}) {
  const fetcher = vi.fn((path: string, _init: RequestInit) => {
    if (path === '/api/auth/capabilities') return Promise.resolve(json(capabilities))
    if (path === '/api/session') return Promise.resolve(json(anonymous))
    if (path === '/api/auth/login-challenge') return options.challenge?.() ?? Promise.resolve(json(loginChallenge()))
    if (path === '/api/auth/sms/send') return options.send?.() ?? Promise.resolve(json({ resend_after_seconds: 60 }))
    if (path === '/api/auth/sms/login' || path === '/api/auth/login') return options.login?.() ?? Promise.resolve(json(connected))
    if (path.startsWith('/api/auth/pending/')) return Promise.resolve(json({ status: 'ok' }))
    if (path === '/api/auth/captcha') return Promise.resolve(json({ status: 'active', challenge_id: 'c'.repeat(32), width: 520, height: 900, image: 'data:image/png;base64,iVBORw0KGgo=' }))
    throw new Error(`Unexpected synthetic SMS request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const scope = effectScope(); scopes.push(scope)
  return { game: scope.run(useGameSession)!, fetcher, scope }
}
beforeEach(() => { vi.stubGlobal('crypto', testCrypto) })
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount()); scopes.splice(0).forEach(scope => scope.stop())
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})

describe('短信登录的加密请求', () => {
  it('发码和登录仅提交同一混合加密信封，私钥解密为各自最小字段', async () => {
    const { game, fetcher } = setup(); await game.initialize()
    expect(typeof game.sendSms).toBe('function')
    await game.sendSms(contact); await game.loginSms(credentials)
    for (const [path, input] of [['/api/auth/sms/send', contact], ['/api/auth/sms/login', credentials]] as const) {
      const call = fetcher.mock.calls.find(([url]) => url === path)!
      const body = JSON.parse(call[1].body as string)
      expect(Object.keys(body).sort()).toEqual(['challenge_id', 'ciphertext', 'encrypted_key', 'iv'])
      expect(await decryptLogin(body)).toEqual(input)
      expect(call[1].body).not.toContain(contact.mobile); expect(call[1].body).not.toContain('otp')
    }
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/auth/login-challenge')).toHaveLength(2)
    expect(game.connected.value).toBe(true)
  })
  it('发码成功后冷却60秒，重复点击与忙碌时登录均不重复请求', async () => {
    const response = deferred<Response>(); const { game, fetcher } = setup({ send: () => response.promise })
    await game.initialize(); expect(typeof game.sendSms).toBe('function')
    const first = game.sendSms(contact); await game.sendSms(contact); await game.loginSms(credentials)
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/sms/send')).toBe(true))
    vi.useFakeTimers(); response.resolve(json({ resend_after_seconds: 60 })); await first
    expect(game.smsResendAfterSeconds.value).toBe(60)
    await game.sendSms(contact)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/auth/sms/send')).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(59_000); expect(game.smsResendAfterSeconds.value).toBe(1)
    await vi.advanceTimersByTimeAsync(1_000); expect(game.smsResendAfterSeconds.value).toBe(0)
    expect(game.connected.value).toBe(false)
  })
  it.each(['cancel', 'expire', 'unmount'] as const)('%s后迟到发码结果不启动冷却或改变会话', async action => {
    const response = deferred<Response>(); const { game, fetcher, scope } = setup({ send: () => response.promise })
    await game.initialize(); expect(typeof game.sendSms).toBe('function')
    const first = game.sendSms(contact)
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/sms/send')).toBe(true))
    const signal = fetcher.mock.calls.find(([path]) => path === '/api/auth/sms/send')![1].signal!
    if (action === 'cancel') game.cancelLogin(); else if (action === 'expire') game.expire(); else scope.stop()
    expect(signal.aborted).toBe(true)
    response.resolve(json({ resend_after_seconds: 60 })); await first
    expect(game.smsResendAfterSeconds.value).toBe(0); expect(game.busy.value).toBe(false)
    expect(game.connected.value).toBe(false)
  })
  it.each(['/api/auth/sms/send', '/api/auth/sms/login'])('%s等待时复用官方人工验证，响应后清理', async path => {
    const response = deferred<Response>(); const { game, fetcher } = setup({ send: () => response.promise, login: () => response.promise })
    await game.initialize(); expect(typeof game.sendSms).toBe('function')
    vi.useFakeTimers()
    const action = path.endsWith('/send') ? game.sendSms(contact) : game.loginSms(credentials)
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === path)).toBe(true))
    await vi.advanceTimersByTimeAsync(600)
    expect(game.captchaChallenge.value?.challenge_id).toBe('c'.repeat(32))
    response.resolve(json(path.endsWith('/send') ? { resend_after_seconds: 60 } : connected)); await action
    expect(game.captchaChallenge.value).toBeNull(); expect(game.busy.value).toBe(false)
  })
  it('加密不可用时绝不明文回退，发码失败不开始冷却', async () => {
    const { game, fetcher } = setup(); await game.initialize(); expect(typeof game.sendSms).toBe('function')
    vi.stubGlobal('crypto', undefined); await game.sendSms(contact)
    expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/sms/send')).toBe(false)
    expect(game.error.value).toBe('登录加密准备失败，请重试'); expect(game.smsResendAfterSeconds.value).toBe(0)
  })
  it.each(['/api/auth/sms/send', '/api/auth/sms/login'])('%s的人工验证取消后保留安全错误且不启动冷却', async path => {
    const response = deferred<Response>(); const { game, fetcher } = setup({ send: () => response.promise, login: () => response.promise })
    const original = fetcher.getMockImplementation()!
    fetcher.mockImplementation((url, init) => {
      if (url === `/api/auth/captcha/${'c'.repeat(32)}` && init.method === 'DELETE') {
        response.resolve(json({ error: { code: 'AUTH_CAPTCHA_CANCELLED', message: '已取消人工验证' } }, 401))
        return Promise.resolve(json({ status: 'cancelled' }))
      }
      return original(url, init)
    })
    await game.initialize(); vi.useFakeTimers()
    const action = path.endsWith('/send') ? game.sendSms(contact) : game.loginSms(credentials)
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === path)).toBe(true)); await vi.advanceTimersByTimeAsync(600)
    await game.cancelCaptcha('c'.repeat(32)); await action
    expect(game.error.value).toBe('已取消人工验证'); expect(game.smsResendAfterSeconds.value).toBe(0)
    expect(game.captchaChallenge.value).toBeNull(); expect(game.busy.value).toBe(false)
  })
  it.each(['12345', '1234567', '１２３４５６'])('验证码 %s 不符合官方6位数字格式时不提交', async otp => {
    const { game, fetcher } = setup(); await game.initialize()
    await game.loginSms({ ...credentials, otp })
    expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/login-challenge')).toBe(false)
    expect(game.error.value).toBe('请输入有效的手机号和验证码')
  })
  it.each(['0', '086', '00'])('国家码 %s 被本地拒绝，不获取challenge或发送短信', async country_code => {
    const { game, fetcher } = setup(); await game.initialize()
    await game.sendSms({ ...contact, country_code }); await game.loginSms({ ...credentials, country_code })
    expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/login-challenge')).toBe(false)
  })
  it('视图取消暂停定时器但保留会话冷却，重入后按截止时间恢复', async () => {
    const { game } = setup(); await game.initialize(); vi.useFakeTimers(); await game.sendSms(contact)
    game.cancelLogin()
    expect(game.smsResendAfterSeconds.value).toBe(60)
    await vi.advanceTimersByTimeAsync(30_000); expect(game.smsResendAfterSeconds.value).toBe(60)
    expect(typeof game.resumeLogin).toBe('function'); game.resumeLogin()
    expect(game.smsResendAfterSeconds.value).toBe(30)
    await vi.advanceTimersByTimeAsync(30_000); expect(game.smsResendAfterSeconds.value).toBe(0)
  })
  it('短信登录成功进入角色选择后结束发码冷却', async () => {
    const { game } = setup({ login: () => Promise.resolve(json({ ...anonymous, status: 'role_selection', authenticated: true, roles: [{ id: 'synthetic-role', name: '', server_name: '' }] })) })
    await game.initialize(); vi.useFakeTimers(); await game.sendSms(contact)
    expect(game.smsResendAfterSeconds.value).toBe(60)
    await game.loginSms(credentials)
    expect(game.session.value.status).toBe('role_selection'); expect(game.smsResendAfterSeconds.value).toBe(0)
  })
  it('发码失败后若服务端仍在冷却，显示等待提示且不会自动重发', async () => {
    let attempts = 0
    const { game, fetcher } = setup({ send: () => Promise.resolve(++attempts === 1
      ? json({ error: { code: 'AUTH_UPSTREAM_FAILED', message: '游戏认证服务暂不可用' } }, 503)
      : json({ error: { code: 'AUTH_SMS_COOLDOWN', message: '请稍后再获取验证码' } }, 429)) })
    await game.initialize(); await game.sendSms(contact); await game.sendSms(contact)
    expect(game.error.value).toBe('请稍后再获取验证码')
    vi.useFakeTimers(); await vi.advanceTimersByTimeAsync(60_000)
    expect(fetcher.mock.calls.filter(([url]) => url === '/api/auth/sms/send')).toHaveLength(2)
  })
})

describe('登录页的短信入口', () => {
  it('发码后修改手机号及切换标签保留剩余冷却，不能立即重发', async () => {
    const { fetcher } = setup(); vi.useFakeTimers()
    const wrapper = mount(App, { global: { stubs: { PetBookView: { template: '<main />' }, ServerClock: { template: '<span />' } } } }); wrappers.push(wrapper)
    await flushPromises(); await wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === '我的')!.trigger('click'); await flushPromises()
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '验证码登录')!.trigger('click'); await flushPromises()
    await wrapper.get('input[name="mobile"]').setValue(contact.mobile); await wrapper.get('button[aria-label="获取验证码"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('60秒后重发'))
    await vi.advanceTimersByTimeAsync(10_000)
    await wrapper.get('input[name="mobile"]').setValue('12345678902')
    expect(wrapper.get('button[aria-label="获取验证码"]').text()).toBe('50秒后重发')
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '密码登录')!.trigger('click'); await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '验证码登录')!.trigger('click'); await flushPromises()
    const send = wrapper.get('button[aria-label="获取验证码"]')
    expect(send.text()).toBe('40秒后重发'); expect(send.attributes('disabled')).toBeDefined()
    await send.trigger('click'); expect(fetcher.mock.calls.filter(([url]) => url === '/api/auth/sms/send')).toHaveLength(1)
    await wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === '图鉴')!.trigger('click'); await flushPromises()
    expect(wrapper.findComponent(LoginView).exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(10_000)
    await wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === '我的')!.trigger('click'); await flushPromises()
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '验证码登录')!.trigger('click'); await flushPromises()
    expect(wrapper.get('button[aria-label="获取验证码"]').text()).toBe('30秒后重发')
  })
  it('隐藏国家码输入，发码和登录均默认使用86', async () => {
    const wrapper = mount(SmsLoginForm, { props: { disabled: false, busy: false, resendAfterSeconds: 0 } }); wrappers.push(wrapper)
    expect(wrapper.find('input[name="country_code"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('国家码')
    await wrapper.get('input[name="mobile"]').setValue(contact.mobile)
    await wrapper.get('button[aria-label="获取验证码"]').trigger('click')
    expect(wrapper.emitted('send')).toEqual([[contact]])
    await wrapper.get('input[name="otp"]').setValue(credentials.otp)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('login')).toEqual([[credentials]])
  })
  it('能力只提供短信时直接显示短信表单', async () => {
    const wrapper = mount(LoginView, { props: { capabilities: { ...capabilities, methods: ['sms'] }, session: anonymous, error: null, busy: false, qrBusy: false, qrChallenge: null, qrState: null } })
    wrappers.push(wrapper); await flushPromises()
    expect(wrapper.find('input[name="mobile"]').exists()).toBe(true)
    expect(wrapper.find('input[name="password"]').exists()).toBe(false)
    expect(wrapper.findAll('[role="tab"]')).toHaveLength(1)
  })
  it('只显示已提供的密码与短信标签，短信默认国家码86且用户点击才发码', async () => {
    const wrapper = mount(LoginView, { props: { capabilities: { ...capabilities, methods: ['account', 'sms'] }, session: anonymous, error: null, busy: false, qrBusy: false, qrChallenge: null, qrState: null } })
    wrappers.push(wrapper); await flushPromises()
    expect(wrapper.text()).toContain('密码登录'); expect(wrapper.text()).toContain('验证码登录'); expect(wrapper.text()).not.toContain('扫码登录')
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '验证码登录')!.trigger('click'); await flushPromises()
    expect(wrapper.find('input[name="country_code"]').exists()).toBe(false)
    expect(wrapper.emitted('smsSend')).toBeUndefined()
    await wrapper.get('input[name="mobile"]').setValue(contact.mobile)
    await wrapper.get('button[aria-label="获取验证码"]').trigger('click')
    expect(wrapper.emitted('smsSend')).toEqual([[contact]])
    await wrapper.get('input[name="otp"]').setValue('123456'); await wrapper.get('input[name="mobile"]').setValue('12345678902')
    expect((wrapper.get('input[name="otp"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.emitted('cancelLogin')).toBeDefined()
  })
  it('短信表单只提交精确6位数字，手机号变更清空验证码，冷却和繁忙时不能发码', async () => {
    const wrapper = mount(SmsLoginForm, { props: { disabled: false, busy: false, resendAfterSeconds: 0 } }); wrappers.push(wrapper)
    await wrapper.get('input[name="mobile"]').setValue(contact.mobile)
    expect(wrapper.findAllComponents(VanField).find(field => field.props('name') === 'otp')!.props('maxlength')).toBe('6')
    await wrapper.get('input[name="otp"]').setValue('1234567')
    expect((wrapper.get('input[name="otp"]').element as HTMLInputElement).value).toBe('123456')
    for (const otp of ['12345', '１２３４５６']) {
      await wrapper.get('input[name="otp"]').setValue(otp); await wrapper.get('form').trigger('submit')
      expect(wrapper.emitted('login')).toBeUndefined()
    }
    await wrapper.get('input[name="otp"]').setValue(credentials.otp); await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('login')).toEqual([[credentials]])
    await wrapper.get('input[name="mobile"]').setValue('12345678902')
    expect((wrapper.get('input[name="otp"]').element as HTMLInputElement).value).toBe('')
    await wrapper.setProps({ resendAfterSeconds: 60 })
    const send = wrapper.get('button[aria-label="获取验证码"]')
    expect(send.text()).toBe('60秒后重发'); expect(send.attributes('disabled')).toBeDefined()
    await send.trigger('click'); expect(wrapper.emitted('send')).toBeUndefined()
    await wrapper.setProps({ resendAfterSeconds: 0, busy: true }); await send.trigger('click')
    expect(wrapper.emitted('send')).toBeUndefined()
  })
  it('切标签销毁短信输入，并发出清理请求', async () => {
    const wrapper = mount(LoginView, { props: { capabilities: { ...capabilities, methods: ['account', 'sms'] }, session: anonymous, error: null, busy: false, qrBusy: false, qrChallenge: null, qrState: null } })
    wrappers.push(wrapper); await flushPromises()
    const tabs = wrapper.findAll('[role="tab"]')
    await tabs[1]!.trigger('click'); await flushPromises()
    await wrapper.get('input[name="mobile"]').setValue(contact.mobile); await wrapper.get('input[name="otp"]').setValue(credentials.otp)
    const before = wrapper.emitted('cancelLogin')!.length
    await tabs[0]!.trigger('click'); await flushPromises()
    expect(wrapper.emitted('cancelLogin')!.length).toBe(before + 1)
    await tabs[1]!.trigger('click'); await flushPromises()
    expect((wrapper.get('input[name="mobile"]').element as HTMLInputElement).value).toBe('')
    expect((wrapper.get('input[name="otp"]').element as HTMLInputElement).value).toBe('')
  })
  it('错误验证码后保留输入，可立即纠正并登录，不自动重发短信', async () => {
    const { fetcher } = setup(); const original = fetcher.getMockImplementation()!; let logins = 0
    fetcher.mockImplementation((path, init) => path === '/api/auth/sms/login'
      ? Promise.resolve(++logins === 1 ? json({ error: { code: 'AUTH_SMS_REJECTED', message: '验证码未通过，请检查输入' } }, 401) : json(connected)) : original(path, init))
    const wrapper = mount(App, { global: { stubs: { PetBookView: { template: '<main />' }, ServerClock: { template: '<span />' } } } }); wrappers.push(wrapper)
    await flushPromises(); await wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === '我的')!.trigger('click'); await flushPromises()
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '验证码登录')!.trigger('click'); await flushPromises()
    await wrapper.get('input[name="mobile"]').setValue(contact.mobile)
    await wrapper.get('button[aria-label="获取验证码"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('验证码已发送'))
    await wrapper.get('input[name="otp"]').setValue('111111'); await wrapper.get('form').trigger('submit')
    await vi.waitFor(() => expect(wrapper.text()).toContain('验证码未通过，请检查输入'))
    expect((wrapper.get('input[name="mobile"]').element as HTMLInputElement).value).toBe(contact.mobile)
    expect((wrapper.get('input[name="otp"]').element as HTMLInputElement).value).toBe('111111')
    await wrapper.get('input[name="otp"]').setValue('222222'); await wrapper.get('form').trigger('submit')
    await vi.waitFor(() => expect(wrapper.classes()).toContain('is-connected'))
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/auth/sms/send')).toHaveLength(1)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/auth/sms/login')).toHaveLength(2)
  })
  it('修改手机号取消旧发码，迟到成功不会污染新号码输入与发送状态', async () => {
    const response = deferred<Response>(); const { fetcher } = setup({ send: () => response.promise })
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    const wrapper = mount(App, { global: { stubs: { PetBookView: { template: '<main />' }, ServerClock: { template: '<span />' } } } }); wrappers.push(wrapper)
    await flushPromises(); await wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === '我的')!.trigger('click'); await flushPromises()
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '验证码登录')!.trigger('click'); await flushPromises()
    await wrapper.get('input[name="mobile"]').setValue(contact.mobile); await wrapper.get('input[name="otp"]').setValue(credentials.otp)
    await wrapper.get('button[aria-label="获取验证码"]').trigger('click')
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/sms/send')).toBe(true))
    const signal = fetcher.mock.calls.find(([path]) => path === '/api/auth/sms/send')![1].signal!
    await wrapper.get('input[name="mobile"]').setValue('12345678902'); expect(signal.aborted).toBe(true)
    response.resolve(json({ resend_after_seconds: 60 })); await flushPromises()
    expect((wrapper.get('input[name="mobile"]').element as HTMLInputElement).value).toBe('12345678902')
    expect((wrapper.get('input[name="otp"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).not.toContain('验证码已发送'); expect(wrapper.text()).not.toContain('60秒后重发')
    expect(storage).not.toHaveBeenCalled()
  })
})

describe('按challenge显式取消待处理认证', () => {
  it.each(['account', 'send', 'login'] as const)('%s取消只发DELETE pending/id，不退出整个会话', async operation => {
    const response = deferred<Response>(); const { game, fetcher } = setup({ send: () => response.promise, login: () => response.promise })
    await game.initialize()
    const path = operation === 'account' ? '/api/auth/login' : `/api/auth/sms/${operation}`
    const action = operation === 'account' ? game.login({ account: 'synthetic', password: 'synthetic' }) : operation === 'send' ? game.sendSms(contact) : game.loginSms(credentials)
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === path)).toBe(true))
    game.cancelLogin(); game.cancelLogin()
    const cancelled = fetcher.mock.calls.filter(([url]) => url.startsWith('/api/auth/pending/'))
    expect(cancelled).toHaveLength(1)
    expect(cancelled[0]![0]).toBe(`/api/auth/pending/${loginChallenge().challenge_id}`)
    expect(cancelled[0]![1]).toMatchObject({ method: 'DELETE', credentials: 'same-origin', cache: 'no-store' })
    expect(cancelled[0]![1].body).toBeUndefined(); expect(cancelled[0]![1].signal!.aborted).toBe(false)
    expect(fetcher.mock.calls.some(([url, init]) => url === '/api/session' && init.method === 'DELETE')).toBe(false)
    response.resolve(json(operation === 'send' ? { resend_after_seconds: 60 } : connected)); await action
    expect(game.connected.value).toBe(false)
  })
  it('challenge返回前取消不提交、不发送无目标取消请求', async () => {
    const response = deferred<Response>(); const { game, fetcher } = setup({ challenge: () => response.promise })
    await game.initialize(); const action = game.loginSms(credentials); game.cancelLogin()
    response.resolve(json(loginChallenge())); await action
    expect(fetcher.mock.calls.some(([url]) => url === '/api/auth/sms/login' || url.startsWith('/api/auth/pending/'))).toBe(false)
  })
  it.each(['success', 'failure'] as const)('%s响应后清除待取消id，后续卸载不取消已结束的认证', async ending => {
    const { game, fetcher, scope } = setup({ login: () => Promise.resolve(ending === 'success' ? json(connected) : json({ error: { code: 'AUTH_SMS_REJECTED', message: '验证码未通过验证' } }, 401)) })
    await game.initialize(); await game.loginSms(credentials); scope.stop()
    expect(fetcher.mock.calls.some(([url]) => url.startsWith('/api/auth/pending/'))).toBe(false)
    expect(game.connected.value).toBe(ending === 'success')
  })
  it('旧响应不能清除新认证的待取消id，旧取消响应不能覆盖新状态', async () => {
    const first = deferred<Response>(), second = deferred<Response>(), cancellation = deferred<Response>()
    const { game, fetcher } = setup(); const original = fetcher.getMockImplementation()!; let challenges = 0, logins = 0
    fetcher.mockImplementation((url, init) => url === '/api/auth/login-challenge'
      ? Promise.resolve(json({ ...loginChallenge(), challenge_id: (++challenges === 1 ? 'a' : 'b').repeat(32) }))
      : url === '/api/auth/sms/login' ? ++logins === 1 ? first.promise : second.promise
      : url.startsWith('/api/auth/pending/') ? cancellation.promise : original(url, init))
    await game.initialize(); const old = game.loginSms(credentials)
    await vi.waitFor(() => expect(logins).toBe(1)); game.cancelLogin()
    const current = game.loginSms(credentials); await vi.waitFor(() => expect(logins).toBe(2))
    first.resolve(json(connected)); await old
    cancellation.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '合成迟到取消错误' } }, 401)); await flushPromises()
    expect(game.busy.value).toBe(true); expect(game.error.value).toBeNull()
    game.cancelLogin()
    expect(fetcher.mock.calls.filter(([url]) => url.startsWith('/api/auth/pending/')).map(([url]) => url)).toEqual([`/api/auth/pending/${'a'.repeat(32)}`, `/api/auth/pending/${'b'.repeat(32)}`])
    second.resolve(json(connected)); await current; expect(game.connected.value).toBe(false)
  })
  it('真实界面切回密码标签时发出按id取消，旧结果不登录；成功卸载不取消新会话', async () => {
    const response = deferred<Response>(); const { fetcher } = setup({ login: () => response.promise })
    const wrapper = mount(App, { global: { stubs: { PetBookView: { template: '<main />' }, ServerClock: { template: '<span />' } } } }); wrappers.push(wrapper)
    await flushPromises(); await wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === '我的')!.trigger('click'); await flushPromises()
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '验证码登录')!.trigger('click'); await flushPromises()
    await wrapper.get('input[name="mobile"]').setValue(contact.mobile); await wrapper.get('input[name="otp"]').setValue(credentials.otp)
    await wrapper.get('form').trigger('submit')
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === '/api/auth/sms/login')).toBe(true))
    await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '密码登录')!.trigger('click'); await flushPromises()
    expect(fetcher.mock.calls.filter(([url]) => url.startsWith('/api/auth/pending/'))).toHaveLength(1)
    response.resolve(json(connected)); await flushPromises()
    expect(wrapper.classes()).not.toContain('is-connected')
    const original = fetcher.getMockImplementation()!
    fetcher.mockImplementation((url, init) => url === '/api/auth/login' ? Promise.resolve(json(connected)) : original(url, init))
    await wrapper.get('input[name="account"]').setValue('synthetic'); await wrapper.get('input[name="password"]').setValue('synthetic')
    await wrapper.get('form').trigger('submit')
    await vi.waitFor(() => expect(wrapper.classes()).toContain('is-connected'))
    expect(fetcher.mock.calls.filter(([url]) => url.startsWith('/api/auth/pending/'))).toHaveLength(1)
    expect(fetcher.mock.calls.some(([url, init]) => url === '/api/session' && init.method === 'DELETE')).toBe(false)
  })
})
