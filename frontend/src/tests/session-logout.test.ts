import { effectScope } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Session } from '../api/types'
import { useGameSession } from '../composables/useGameSession'

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const anonymous: Session = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const connected: Session = { status: 'connected', authenticated: true, roles: [], message: null,
  role: { id: 'synthetic-role', name: '', server_name: 'synthetic-zone' } }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
const unconfirmed = '退出未确认，请重试，自动任务可能仍在运行'
async function setup(logoutRequest: (signal: AbortSignal) => Promise<Response>) {
  const fetcher = vi.fn((path: string, init: RequestInit) => {
    if (path === '/api/session' && init.method === 'DELETE') return logoutRequest(init.signal as AbortSignal)
    return Promise.resolve(json(path.includes('capabilities')
      ? { integration_ready: true, methods: ['account'] } : connected))
  })
  vi.stubGlobal('fetch', fetcher)
  const scope = effectScope()
  scopes.push(scope)
  const game = scope.run(useGameSession)!
  await game.initialize()
  return { game, scope, fetcher }
}

describe('退出确认与自动任务控制', () => {
  it('等待DELETE确认期间保留连接，成功响应后才清空会话', async () => {
    let resolve!: (value: Response) => void
    const response = new Promise<Response>(accept => { resolve = accept })
    const { game } = await setup(() => response)
    const previous = game.session.value
    const logout = game.logout()
    expect(game.connected.value).toBe(true)
    expect(game.session.value).toBe(previous)
    expect(game.busy.value).toBe(true)
    resolve(json(anonymous))
    await logout
    expect(game.connected.value).toBe(false)
    expect(game.session.value).toEqual(anonymous)
    expect(game.busy.value).toBe(false)
    expect(game.error.value).toBeNull()
  })

  it.each(['network', '503', '403', 'invalid-response'])('%s失败保留当前角色及连接并明确提示退出未确认', async failure => {
    vi.useFakeTimers()
    const { game, fetcher } = await setup(() => {
      if (failure === 'network') return Promise.reject(new Error('synthetic-private-transport-error'))
      if (failure === 'invalid-response') return Promise.resolve(new Response('unreadable', { status: 200 }))
      return Promise.resolve(json({ error: { code: 'TEST_ONLY', message: 'synthetic-private-server-error' } }, Number(failure)))
    })
    const previous = game.session.value
    await game.logout()
    expect(game.connected.value).toBe(true)
    expect(game.session.value).toBe(previous)
    expect(game.busy.value).toBe(false)
    expect(game.error.value).toBe(unconfirmed)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fetcher.mock.calls.filter(([path, init]) => path === '/api/session' && init.method === 'DELETE')).toHaveLength(1)
  })

  it('明确401失效时退出连接并正确复位busy', async () => {
    const { game, fetcher } = await setup(() => Promise.resolve(json({
      error: { code: 'AUTH_REQUIRED', message: 'synthetic-expired' },
    }, 401)))
    await game.logout()
    expect(game.connected.value).toBe(false)
    expect(game.session.value.authenticated).toBe(false)
    expect(game.session.value.status).toBe('expired')
    expect(game.busy.value).toBe(false)
    expect(game.error.value).toBeNull()
    expect(fetcher.mock.calls.filter(([path, init]) => path === '/api/session' && init.method === 'DELETE')).toHaveLength(1)
  })

  it('失败后仅用户再次点击才重试，成功确认后退出', async () => {
    let calls = 0
    const { game, fetcher } = await setup(() => ++calls === 1
      ? Promise.reject(new Error('synthetic-network')) : Promise.resolve(json(anonymous)))
    await game.logout()
    expect(game.connected.value).toBe(true)
    expect(game.error.value).toBe(unconfirmed)
    await game.logout()
    expect(game.session.value).toEqual(anonymous)
    expect(game.error.value).toBeNull()
    expect(fetcher.mock.calls.filter(([path, init]) => path === '/api/session' && init.method === 'DELETE')).toHaveLength(2)
  })

  it('等待退出时连续点击不会取消并重发DELETE', async () => {
    let resolve!: (value: Response) => void
    const response = new Promise<Response>(accept => { resolve = accept })
    const { game, fetcher } = await setup(() => response)
    const first = game.logout()
    const second = game.logout()
    expect(fetcher.mock.calls.filter(([path, init]) => path === '/api/session' && init.method === 'DELETE')).toHaveLength(1)
    resolve(json(anonymous))
    await Promise.all([first, second])
    expect(game.busy.value).toBe(false)
  })

  it('退出等待期间收到会话失效，迟到成功响应不能覆盖expired状态', async () => {
    let resolve!: (value: Response) => void
    const response = new Promise<Response>(accept => { resolve = accept })
    const { game } = await setup(() => response)
    const logout = game.logout()
    game.expire()
    resolve(json(anonymous))
    await logout
    expect(game.session.value.status).toBe('expired')
    expect(game.busy.value).toBe(false)
    expect(game.error.value).toBeNull()
  })

  it('DELETE超时保持连接和停止入口所需会话，不自动重发', async () => {
    vi.useFakeTimers()
    const { game, fetcher } = await setup(signal => new Promise<Response>((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    }))
    const logout = game.logout()
    await vi.advanceTimersByTimeAsync(15_000)
    await logout
    expect(game.connected.value).toBe(true)
    expect(game.busy.value).toBe(false)
    expect(game.error.value).toBe(unconfirmed)
    expect(fetcher.mock.calls.filter(([path, init]) => path === '/api/session' && init.method === 'DELETE')).toHaveLength(1)
  })
})
