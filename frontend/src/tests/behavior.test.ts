import { effectScope } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request } from '../api/client'
import type { MarketSnapshot } from '../api/types'
import { useGameSession } from '../composables/useGameSession'
import { useMarketListings } from '../composables/useMarketListings'
import RefreshStatus from '../components/market/RefreshStatus.vue'
import { loginChallenge, testCrypto } from './loginEncryptionFixture'

const scopes: ReturnType<typeof effectScope>[] = []
beforeEach(() => { vi.stubGlobal('crypto', testCrypto) })
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.useRealTimers()
})
function inScope<T>(fn: () => T): T {
  const scope = effectScope()
  scopes.push(scope)
  return scope.run(fn)!
}
const anonymous = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const unavailable = { provider: 'bilibili_game', integration_ready: false, methods: [], reason_code: 'AUTH_PROTOCOL_UNVERIFIED', message: '游戏登录协议尚未接通' }
const snapshot = (id: string, status: MarketSnapshot['status'] = 'complete'): MarketSnapshot => ({
  refresh_id: id, status, complete: status === 'complete', items: [], loaded_count: 9, matched_count: 0, scope: 'all',
  total_count: null, started_at: null, updated_at: null, message: null, name_coverage: { named: 0, total: 306 },
})
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function deferredResponse() {
  let resolve!: (response: Response) => void
  let signal!: AbortSignal
  let started!: () => void
  const waiting = new Promise<void>(accept => { started = accept })
  const fetcher = vi.fn((_path: string, init: RequestInit) => new Promise<Response>((accept, reject) => {
    resolve = accept
    signal = init.signal as AbortSignal
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    started()
  }))
  return { fetcher, resolve: (body: unknown) => resolve(json(body)), signal: () => signal, started: waiting }
}
async function readyAccountLogin() {
  const pending = deferredResponse()
  const available = { ...unavailable, integration_ready: true, methods: ['account'], message: null }
  vi.stubGlobal('fetch', vi.fn((path: string, init: RequestInit) => path === '/api/auth/login'
    ? pending.fetcher(path, init) : Promise.resolve(json(path === '/api/auth/login-challenge'
      ? loginChallenge() : path.includes('capabilities') ? available : anonymous))))
  const game = inScope(useGameSession)
  await game.initialize()
  return { game, pending }
}

describe('同源 API 和认证', () => {
  it('请求携带同源 Cookie，金额保留整数', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ direct_buy_price: 2946872 }))
    vi.stubGlobal('fetch', fetcher)
    expect(await request('/api/example')).toEqual({ direct_buy_price: 2946872 })
    expect(fetcher.mock.calls[0]![1].credentials).toBe('same-origin')
  })
  it('仅显示合同里的安全错误消息，不显示上游原始正文', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('upstream-private-body', { status: 502 })))
    await expect(request('/api/example')).rejects.toMatchObject({ message: '服务请求失败，请稍后重试' })
  })
  it('人工验证期间账号登录超过 15 秒仍等待，并可接收正常完成结果', async () => {
    vi.useFakeTimers()
    const { game, pending } = await readyAccountLogin()
    const login = game.login({ account: 'test-only', password: 'test-only' })
    await pending.started
    await vi.advanceTimersByTimeAsync(16_000)
    expect(game.busy.value).toBe(true)
    expect(pending.signal().aborted).toBe(false)
    expect(game.error.value).toBeNull()
    pending.resolve({ ...anonymous, status: 'connected', authenticated: true })
    await login
    expect(game.connected.value).toBe(true)
    expect(game.busy.value).toBe(false)
  })
  it('账号登录的人工验证等待最多 300 秒', async () => {
    vi.useFakeTimers()
    const { game, pending } = await readyAccountLogin()
    const login = game.login({ account: 'test-only', password: 'test-only' })
    await pending.started
    await vi.advanceTimersByTimeAsync(299_999)
    expect(game.busy.value).toBe(true)
    expect(pending.signal().aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await login
    expect(pending.signal().aborted).toBe(true)
    expect(game.busy.value).toBe(false)
    expect(game.error.value).toBe('请求超时，请稍后重试')
  })
  it('普通请求仍在 15 秒超时', async () => {
    vi.useFakeTimers()
    const pending = deferredResponse()
    vi.stubGlobal('fetch', pending.fetcher)
    const response = request('/api/example')
    const rejected = expect(response).rejects.toMatchObject({ code: 'TIMEOUT' })
    await vi.advanceTimersByTimeAsync(14_999)
    expect(pending.signal().aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await rejected
    expect(pending.signal().aborted).toBe(true)
  })
  it('延长等待的请求仍立即响应调用方取消', async () => {
    vi.useFakeTimers()
    const pending = deferredResponse()
    vi.stubGlobal('fetch', pending.fetcher)
    const controller = new AbortController()
    const response = request('/api/example', { signal: controller.signal }, { timeoutMs: 300_000 })
    const rejected = expect(response).rejects.toMatchObject({ name: 'AbortError' })
    controller.abort()
    await rejected
    expect(pending.signal().aborted).toBe(true)
  })
  it.each(['AUTH_REJECTED', 'AUTH_CAPTCHA_CANCELLED'])('首次登录 %s 显示安全文案而不是会话过期', async code => {
    const message = code === 'AUTH_REJECTED' ? '账号登录未通过，请检查输入' : '已取消人工验证'
    const available = { ...unavailable, integration_ready: true, methods: ['account'], message: null }
    vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(path === '/api/auth/login'
      ? json({ error: { code, message, retryable: false } }, 401)
      : json(path === '/api/auth/login-challenge' ? loginChallenge() : path.includes('capabilities') ? available : anonymous))))
    const game = inScope(useGameSession)
    await game.initialize()
    await game.login({ account: 'test-only', password: 'test-only' })
    expect(game.session.value.status).toBe('anonymous')
    expect(game.session.value.authenticated).toBe(false)
    expect(game.error.value).toBe(message)
  })
  it('能力为空时不提交账号和密码', async () => {
    const fetcher = vi.fn((path: string) => Promise.resolve(json(path.includes('capabilities') ? unavailable : anonymous)))
    vi.stubGlobal('fetch', fetcher)
    const game = inScope(useGameSession)
    await game.initialize()
    await game.login({ account: 'test-only', password: 'test-only' })
    expect(game.error.value).toBe('游戏登录协议尚未接通')
    expect(fetcher.mock.calls.some(([path]) => path === '/api/auth/login')).toBe(false)
  })
  it('扫码过期显示真实状态，不升级为已登录', async () => {
    const available = { ...unavailable, integration_ready: true, methods: ['qr'], message: null }
    const fetcher = vi.fn((path: string) => Promise.resolve(json(
      path.includes('capabilities') ? available : path === '/api/auth/qr'
        ? { challenge_id: 'test-qr', qr_payload: 'test-only-payload', expires_at: '2026-10-13T12:00:00Z' }
        : path.includes('/api/auth/qr/') ? { status: 'expired', session: null, message: '二维码已过期' } : anonymous,
    )))
    vi.stubGlobal('fetch', fetcher)
    const game = inScope(useGameSession)
    await game.initialize()
    await game.createQr()
    await game.checkQr()
    expect(game.qrState.value?.status).toBe('expired')
    expect(game.session.value.authenticated).toBe(false)
  })
})

describe('商品筛选与刷新', () => {
  it('搜索等待同个POST返回结果，超过15秒仍等待且不自动读取快照', async () => {
    vi.useFakeTimers()
    const pending = deferredResponse()
    const fetcher = vi.fn((path: string, init: RequestInit) => path.startsWith('/api/pets')
      ? Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
      : pending.fetcher(path, init))
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    const refreshing = market.refresh()
    const polls = () => fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))
    try {
      await vi.advanceTimersByTimeAsync(16_000)
      expect(market.busy.value).toBe(true)
      expect(market.snapshot.value).toBeNull()
      expect(pending.signal().aborted).toBe(false)
      expect(polls()).toHaveLength(0)
    } finally {
      pending.resolve(snapshot('direct'))
      await refreshing
    }
    expect(market.snapshot.value?.refresh_id).toBe('direct')
    expect(market.snapshot.value?.complete).toBe(true)
    expect(market.busy.value).toBe(false)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(polls()).toHaveLength(0)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
  })

  it('搜索35秒超时后结束等待并显示错误，不自动重试或读取快照', async () => {
    vi.useFakeTimers()
    const pending = deferredResponse()
    const fetcher = vi.fn((path: string, init: RequestInit) => path.startsWith('/api/pets')
      ? Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
      : pending.fetcher(path, init))
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    const refreshing = market.refresh()
    await vi.advanceTimersByTimeAsync(34_999)
    expect(market.busy.value).toBe(true)
    expect(pending.signal().aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await refreshing
    expect(pending.signal().aborted).toBe(true)
    expect(market.busy.value).toBe(false)
    expect(market.error.value).toBe('请求超时，请稍后重试')
    expect(market.snapshot.value).toBeNull()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))).toHaveLength(0)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
  })

  it('显式读取loading快照也不会启动自动轮询', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn((path: string) => Promise.resolve(json(path.startsWith('/api/pets')
      ? { items: [], total: 0, named_count: 0, image_count: 0 }
      : snapshot('loading', 'loading'))))
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    await market.load()
    const polls = () => fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))
    expect(market.snapshot.value?.status).toBe('loading')
    await vi.advanceTimersByTimeAsync(30_000)
    expect(polls()).toHaveLength(1)
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(0)
  })

  it('退出或卸载时取消仍在等待的搜索，清空结果且不显示取消错误', async () => {
    const pending = deferredResponse()
    const expired = vi.fn()
    vi.stubGlobal('fetch', vi.fn((path: string, init: RequestInit) => path.startsWith('/api/pets')
      ? Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
      : pending.fetcher(path, init)))
    const market = inScope(() => useMarketListings({ onUnauthorized: expired }))
    const refreshing = market.refresh()
    expect(market.busy.value).toBe(true)
    scopes.at(-1)!.stop()
    await refreshing
    expect(pending.signal().aborted).toBe(true)
    expect(market.snapshot.value).toBeNull()
    expect(market.busy.value).toBe(false)
    expect(market.error.value).toBeNull()
    expect(expired).not.toHaveBeenCalled()
  })

  it.each(['success', 'failed'] as const)('带条件刷新直接等待有界POST%s，不过滤旧的前十快照', async result => {
    let resolveFiltered!: (value: Response) => void
    const pendingFiltered = new Promise<Response>(resolve => { resolveFiltered = resolve })
    const fetcher = vi.fn((path: string, _init?: RequestInit) => path.startsWith('/api/pets')
      ? Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
      : path === '/api/market/refresh' ? pendingFiltered
        : Promise.reject(new Error('不得GET过滤旧前十')))
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    const refreshing = market.refresh({ query: '蜜桃', category: 'color', pet_ids: ['1000101'] })
    await flushPromises()
    try {
      expect(market.snapshot.value).toBeNull()
      expect(market.busy.value).toBe(true)
      const refresh = fetcher.mock.calls.find(([path]) => path === '/api/market/refresh')!
      expect(JSON.parse(refresh[1]?.body as string)).toEqual({ category: 'color', query: '蜜桃', pet_ids: ['1000101'], limit: 10 })
    } finally {
      resolveFiltered(result === 'success' ? json(snapshot('filtered'))
        : json({ error: { code: 'TIMEOUT', message: '查询超时', retryable: true } }, 503))
      await refreshing
    }
    if (result === 'success') expect(market.snapshot.value?.refresh_id).toBe('filtered')
    else {
      expect(market.snapshot.value).toBeNull()
      expect(market.error.value).toBe('查询超时')
    }
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))).toHaveLength(0)
  })
  it('初始读取和无筛选刷新均固定彩色类别', async () => {
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(json(snapshot('color'))))
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    await market.load()
    await market.refresh()
    const [firstPath] = fetcher.mock.calls[0]!
    expect(new URL(firstPath, 'http://localhost').searchParams.get('category')).toBe('color')
    const post = fetcher.mock.calls.find(([path]) => path === '/api/market/refresh')!
    expect(JSON.parse(post[1].body)).toEqual({ category: 'color', query: '', pet_ids: [], limit: 10 })
  })
  it.each(['all', 'normal', 'item'] as const)('旧调用类别%s不会改变彩色查询范围', async category => {
    const fetcher = vi.fn().mockImplementation((path: string) => Promise.resolve(json(
      path.startsWith('/api/pets') ? { items: [], total: 0, named_count: 0, image_count: 0 } : snapshot('color'),
    )))
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    const filters = { query: ' 蜜桃 ', category, pet_ids: ['1000101'] }
    await market.setFilters(filters)
    await market.refresh(filters)
    const posts = fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')
    expect(posts).toHaveLength(2)
    for (const post of posts) expect(JSON.parse((post[1] as RequestInit).body as string)).toEqual({
      category: 'color', query: '蜜桃', pet_ids: ['1000101'], limit: 10,
    })
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))).toHaveLength(0)
  })
  it('中文名称和形态 ID 正确进入重建查询的POST，目录只加载一次', async () => {
    const fetcher = vi.fn((path: string, _init?: RequestInit) => Promise.resolve(json(path.startsWith('/api/pets')
      ? { items: [], total: 0, named_count: 0, image_count: 0 } : snapshot('r1'))))
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    await market.setFilters({ query: ' 蜜桃 ', category: 'color', pet_ids: ['1000101', '1000102'] })
    expect(fetcher).toHaveBeenCalled()
    const post = fetcher.mock.calls.find(([path]) => path === '/api/market/refresh')!
    expect(JSON.parse(post[1]?.body as string)).toEqual({ category: 'color', query: '蜜桃', pet_ids: ['1000101', '1000102'], limit: 10 })
    const catalogs = fetcher.mock.calls.filter(([path]) => path.startsWith('/api/pets'))
    expect(catalogs).toHaveLength(1)
    expect(new URL(catalogs[0]![0], 'http://localhost').searchParams.get('query')).toBe('蜜桃')
  })
  it('401 清除商品并通知会话失效', async () => {
    const expired = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error: { code: 'SESSION_EXPIRED', message: '登录已过期', retryable: false } }, 401)))
    const market = inScope(() => useMarketListings({ onUnauthorized: expired }))
    await market.load()
    expect(expired).toHaveBeenCalledOnce()
    expect(market.snapshot.value).toBeNull()
  })
  it('新刷新开始后，旧列表响应不能覆盖新轮次', async () => {
    let resolveOld!: (value: Response) => void
    const oldResponse = new Promise<Response>(resolve => { resolveOld = resolve })
    vi.stubGlobal('fetch', vi.fn((path: string, init: RequestInit) => path.startsWith('/api/pets')
      ? Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
      : init.method === 'POST' ? Promise.resolve(json(snapshot('new'))) : oldResponse))
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    const oldLoad = market.load()
    await market.refresh()
    resolveOld(json(snapshot('old')))
    await oldLoad
    expect(market.snapshot.value?.refresh_id).toBe('new')
  })
  it('新筛选后迟到的旧POST及401不会清空新结果或退出会话', async () => {
    let resolveOld!: (value: Response) => void
    let oldSignal!: AbortSignal
    const pending = new Promise<Response>(resolve => { resolveOld = resolve })
    const expired = vi.fn()
    const fetcher = vi.fn((path: string, init?: RequestInit) => {
      if (path.startsWith('/api/pets')) return Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
      if (path === '/api/market/refresh' && JSON.parse(init?.body as string).query === '蜜桃') {
        oldSignal = init?.signal as AbortSignal
        return pending
      }
      return Promise.resolve(json(snapshot('new')))
    })
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: expired }))
    const oldRefresh = market.setFilters({ query: '蜜桃', category: 'color', pet_ids: [] })
    await market.setFilters({ query: '灰灰', category: 'color', pet_ids: [] })
    try {
      expect(oldSignal.aborted).toBe(true)
      expect(market.snapshot.value?.refresh_id).toBe('new')
    } finally {
      resolveOld(json({ error: { code: 'AUTH_REQUIRED', message: '旧会话错误', retryable: false } }, 401))
      await oldRefresh
    }
    expect(expired).not.toHaveBeenCalled()
    expect(market.snapshot.value?.refresh_id).toBe('new')
    expect(market.error.value).toBeNull()
    expect(market.busy.value).toBe(false)
  })
  it('新筛选取消旧的显式读取，迟到响应不能覆盖结果或启动轮询', async () => {
    vi.useFakeTimers()
    let resolvePoll!: (value: Response) => void
    let pollSignal!: AbortSignal
    const oldPoll = new Promise<Response>(resolve => { resolvePoll = resolve })
    const fetcher = vi.fn((path: string, init?: RequestInit) => {
      if (path.startsWith('/api/pets')) return Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
      if (path === '/api/market/refresh') return Promise.resolve(json(snapshot('filtered')))
      pollSignal = init?.signal as AbortSignal
      return oldPoll
    })
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    const oldLoad = market.load()
    await market.setFilters({ query: '蜜桃', category: 'color', pet_ids: [] })
    try {
      expect(pollSignal.aborted).toBe(true)
      expect(market.snapshot.value?.refresh_id).toBe('filtered')
    } finally {
      resolvePoll(json(snapshot('old-poll', 'loading')))
      await oldLoad
    }
    await vi.advanceTimersByTimeAsync(2_000)
    expect(market.snapshot.value?.refresh_id).toBe('filtered')
    expect(fetcher.mock.calls.filter(([path]) => path.startsWith('/api/market/listings'))).toHaveLength(1)
  })
  it('服务暂不可用时显示后端的安全消息', async () => {
    vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(path.startsWith('/api/pets')
      ? json({ items: [], total: 0, named_count: 0, image_count: 0 })
      : json({ error: { code: 'MARKET_PROTOCOL_UNVERIFIED', message: '一口价查询协议尚未接通', retryable: false } }, 503))))
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    await market.refresh()
    expect(market.error.value).toBe('一口价查询协议尚未接通')
  })
  it('筛选失败不会把上一次筛选结果继续展示为当前结果', async () => {
    const fetcher = vi.fn((path: string) => Promise.resolve(path.startsWith('/api/pets')
      ? json({ items: [], total: 0, named_count: 0, image_count: 0 })
      : path.startsWith('/api/market/listings') ? json(snapshot('previous'))
        : json({ error: { code: 'TIMEOUT', message: '查询超时', retryable: true } }, 503)))
    vi.stubGlobal('fetch', fetcher)
    const market = inScope(() => useMarketListings({ onUnauthorized: vi.fn() }))
    await market.load()
    expect(market.snapshot.value?.refresh_id).toBe('previous')
    await market.setFilters({ query: '蜜桃', category: 'all', pet_ids: [] })
    expect(market.snapshot.value).toBeNull()
    expect(market.error.value).toBe('查询超时')
  })
})

describe('完整与部分结果', () => {
  it('尚未完成时明确显示部分状态和名称覆盖率', () => {
    const wrapper = mount(RefreshStatus, { props: { snapshot: snapshot('partial', 'partial') } })
    expect(wrapper.text()).toContain('本轮未完成')
    expect(wrapper.text()).toContain('0 / 306')
    expect(wrapper.text()).not.toContain('本轮已加载完')
  })
  it('只有明确 complete=true 才显示已加载完', async () => {
    const wrapper = mount(RefreshStatus, { props: { snapshot: snapshot('confirmed') } })
    expect(wrapper.text()).toContain('本轮已加载完')
    await wrapper.setProps({ snapshot: { ...snapshot('uncertain'), complete: false } })
    expect(wrapper.text()).not.toContain('本轮已加载完')
  })
  it('前十完成与全量完成采用不同文案，不声称全市场已加载完', async () => {
    const wrapper = mount(RefreshStatus, { props: { snapshot: { ...snapshot('top10'), scope: 'top10' } } })
    expect(wrapper.text()).toContain('本次前十查询完成')
    expect(wrapper.text()).not.toContain('本轮已加载完')
    await wrapper.setProps({ snapshot: { ...snapshot('top10', 'loading'), scope: 'top10' } })
    expect(wrapper.text()).toContain('按游戏一口价升序读取')
    expect(wrapper.text()).not.toContain('本次前十查询完成')
    await wrapper.setProps({ snapshot: { ...snapshot('top10', 'partial'), scope: 'top10' } })
    expect(wrapper.text()).toContain('请刷新重试')
    expect(wrapper.text()).not.toContain('本次前十查询完成')
  })
})
