import { generateKeyPairSync } from 'node:crypto'
import { effectScope } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useGameSession } from '../composables/useGameSession'
import { decryptLogin, loginChallenge, testCrypto, type LoginEnvelope } from './loginEncryptionFixture'

const scopes: ReturnType<typeof effectScope>[] = []
const anonymous = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const connected = { ...anonymous, status: 'connected', authenticated: true }
const credentials = { account: 'synthetic-private-account', password: 'synthetic-private-password' }
const safeFailure = '登录加密准备失败，请重试'
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(accept => { resolve = accept })
  return { promise, resolve }
}
beforeEach(() => { vi.stubGlobal('crypto', testCrypto) })
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
async function setup(challenge: () => Promise<Response> = () => Promise.resolve(json(loginChallenge())),
  post: () => Promise<Response> = () => Promise.resolve(json(connected))) {
  const fetcher = vi.fn((path: string, _init: RequestInit) => {
    if (path === '/api/auth/login-challenge') return challenge()
    if (path === '/api/auth/login') return post()
    return Promise.resolve(json(path.includes('capabilities')
      ? { integration_ready: true, methods: ['account'] } : anonymous))
  })
  vi.stubGlobal('fetch', fetcher)
  const scope = effectScope()
  scopes.push(scope)
  const game = scope.run(useGameSession)!
  await game.initialize()
  return { game, scope, fetcher, posts: () => fetcher.mock.calls.filter(([path]) => path === '/api/auth/login') }
}

describe('账号登录混合加密', () => {
  it.each([credentials, { account: '合成账号🌙', password: '密'.repeat(4096) }])('请求体仅发送可由服务端私钥解密的信封（含Unicode与长密码）', async input => {
    const { game, fetcher, posts } = await setup()
    await game.login(input)
    expect(posts()).toHaveLength(1)
    const init = posts()[0]![1]
    const body = JSON.parse(init.body as string) as LoginEnvelope
    expect(Object.keys(body).sort()).toEqual(['challenge_id', 'ciphertext', 'encrypted_key', 'iv'])
    expect(init.body).not.toContain(input.account)
    expect(init.body).not.toContain(input.password)
    expect(Buffer.from(body.encrypted_key, 'base64')).toHaveLength(256)
    expect(Buffer.from(body.iv, 'base64')).toHaveLength(12)
    expect(await decryptLogin(body)).toEqual(input)
    expect(fetcher.mock.calls.find(([path]) => path === '/api/auth/login-challenge')![1]).toMatchObject({
      method: 'GET', credentials: 'same-origin', cache: 'no-store',
    })
    expect(init.credentials).toBe('same-origin')
    expect(game.connected.value).toBe(true)
    expect(game.busy.value).toBe(false)
  })

  it('相同输入的每次手动登录使用新AES密钥和IV', async () => {
    const { game, posts } = await setup()
    await game.login(credentials)
    await game.login(credentials)
    const [first, second] = posts().map(([, init]) => JSON.parse(init.body as string) as LoginEnvelope)
    expect(second!.encrypted_key).not.toBe(first!.encrypted_key)
    expect(second!.iv).not.toBe(first!.iv)
    expect(second!.ciphertext).not.toBe(first!.ciphertext)
    expect(await decryptLogin(second!)).toEqual(credentials)
  })

  const invalid = [
    ['缺少字段', () => ({})],
    ['错误算法', () => ({ ...loginChallenge(), algorithm: 'RSA-PKCS1' })],
    ['错误challenge ID', () => ({ ...loginChallenge(), challenge_id: 'short' })],
    ['无效过期时间', () => ({ ...loginChallenge(), expires_at: 'invalid' })],
    ['已过期', () => ({ ...loginChallenge(), expires_at: new Date(Date.now() - 1).toISOString() })],
    ['无效Base64公钥', () => ({ ...loginChallenge(), public_key: 'not!base64' })],
    ['无效DER公钥', () => ({ ...loginChallenge(), public_key: btoa('invalid-public-key') })],
    ['非RSA公钥', () => ({ ...loginChallenge(), public_key: generateKeyPairSync('ec', { namedCurve: 'prime256v1',
      publicKeyEncoding: { type: 'spki', format: 'der' }, privateKeyEncoding: { type: 'pkcs8', format: 'der' } }).publicKey.toString('base64') })],
    ['错误RSA长度', () => ({ ...loginChallenge(), public_key: generateKeyPairSync('rsa', { modulusLength: 1024,
      publicKeyEncoding: { type: 'spki', format: 'der' }, privateKeyEncoding: { type: 'pkcs8', format: 'der' } }).publicKey.toString('base64') })],
  ] as const
  it.each(invalid)('%s时显示固定安全错误，绝不POST或明文回退', async (_name, value) => {
    const { game, posts } = await setup(() => Promise.resolve(json(value())))
    await game.login(credentials)
    expect(posts()).toHaveLength(0)
    expect(game.error.value).toBe(safeFailure)
    expect(game.session.value.status).toBe('anonymous')
    expect(game.busy.value).toBe(false)
  })

  it('WebCrypto不可用时不提交账号和密码', async () => {
    vi.stubGlobal('crypto', undefined)
    const { game, posts } = await setup()
    await game.login(credentials)
    expect(posts()).toHaveLength(0)
    expect(game.error.value).toBe(safeFailure)
    expect(game.busy.value).toBe(false)
  })

  it('WebCrypto异常不显示原始敏感错误且不回退', async () => {
    vi.spyOn(testCrypto.subtle, 'encrypt').mockRejectedValue(new Error(credentials.password))
    const { game, posts } = await setup()
    await game.login(credentials)
    expect(posts()).toHaveLength(0)
    expect(game.error.value).toBe(safeFailure)
    expect(game.error.value).not.toContain(credentials.password)
  })

  it('准备登录期间重复点击不会获取第二个challenge或重复提交', async () => {
    const challenge = deferred<Response>()
    const { game, fetcher, posts } = await setup(() => challenge.promise)
    const first = game.login(credentials)
    const second = game.login(credentials)
    expect(game.busy.value).toBe(true)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/auth/login-challenge')).toHaveLength(1)
    challenge.resolve(json(loginChallenge()))
    await Promise.all([first, second])
    expect(posts()).toHaveLength(1)
  })

  it.each(['expire', 'unmount', 'new-generation'] as const)('%s后迟到challenge不能发送凭据或改变状态', async cancel => {
    const challenge = deferred<Response>()
    const { game, scope, fetcher, posts } = await setup(() => challenge.promise)
    const login = game.login(credentials)
    const challengeSignal = fetcher.mock.calls.find(([path]) => path === '/api/auth/login-challenge')![1].signal!
    if (cancel === 'expire') game.expire()
    else if (cancel === 'unmount') scope.stop()
    else await game.initialize()
    if (cancel !== 'new-generation') expect(challengeSignal.aborted).toBe(true)
    challenge.resolve(json(loginChallenge()))
    await login
    expect(posts()).toHaveLength(0)
    expect(game.connected.value).toBe(false)
    expect(game.error.value).toBeNull()
    if (cancel === 'expire') expect(game.session.value.status).toBe('expired')
  })

  it.each(['importKey', 'encrypt'] as const)('取消后迟到的%s结果不能继续加密或POST', async operation => {
    const entered = deferred<void>()
    const released = deferred<void>()
    if (operation === 'importKey') {
      const original = testCrypto.subtle.importKey.bind(testCrypto.subtle)
      vi.spyOn(testCrypto.subtle, 'importKey').mockImplementationOnce((...args: Parameters<SubtleCrypto['importKey']>) => {
        entered.resolve()
        return released.promise.then(() => original(...args))
      })
    } else {
      const original = testCrypto.subtle.encrypt.bind(testCrypto.subtle)
      vi.spyOn(testCrypto.subtle, 'encrypt').mockImplementationOnce((...args: Parameters<SubtleCrypto['encrypt']>) => {
        entered.resolve()
        return released.promise.then(() => original(...args))
      })
    }
    const { game, posts } = await setup()
    const login = game.login(credentials)
    await entered.promise
    game.expire()
    released.resolve()
    await login
    expect(posts()).toHaveLength(0)
    expect(game.session.value.status).toBe('expired')
    expect(game.busy.value).toBe(false)
    expect(game.error.value).toBeNull()
  })

  it('challenge获取保持15秒超时且不会自动重试或发送登录POST', async () => {
    vi.useFakeTimers()
    const { game, fetcher, posts } = await setup(() => new Promise((_resolve, reject) => {
      const signal = fetcher.mock.calls.at(-1)![1].signal!
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    }))
    const login = game.login(credentials)
    await vi.advanceTimersByTimeAsync(15_000)
    await login
    expect(posts()).toHaveLength(0)
    expect(game.error.value).toBe('请求超时，请稍后重试')
    expect(game.busy.value).toBe(false)
    await vi.advanceTimersByTimeAsync(300_000)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/auth/login-challenge')).toHaveLength(1)
  })
})
