import { effectScope } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RushPurchaseState } from '../api/types'
import { useRushPurchase } from '../composables/useRushPurchase'

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const settings = { petIds: ['1000101', '1000201'], maxPriceGold: 123456, quantityPerPet: 1, minBalanceGold: 10000 }
const serverSettings = { pet_ids: [...settings.petIds], max_price_gold: settings.maxPriceGold, quantity_per_pet: 1, min_balance_gold: 10000 }
const idle = (): RushPurchaseState => ({ job_id: null, status: 'idle', settings: null, balance_gold: null,
  counts: {}, spent_gold: '0', message: null, attempts: 0, updated_at: null })
const running = (override: Partial<RushPurchaseState> = {}): RushPurchaseState => ({
  ...idle(), job_id: 'test-job', status: 'running', settings: serverSettings,
  balance_gold: '90071992547409930000', counts: { '1000101': 1, '1000201': 0 }, attempts: 1, ...override,
})
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function setup() {
  const scope = effectScope()
  scopes.push(scope)
  const expired = vi.fn()
  const rush = scope.run(() => useRushPurchase({ onUnauthorized: expired }))!
  return { rush, expired, scope }
}
function pending() {
  let resolve!: (response: Response) => void
  let signal!: AbortSignal
  const response = new Promise<Response>(accept => { resolve = accept })
  const fetcher = vi.fn((_path: string, init: RequestInit) => {
    signal = init.signal as AbortSignal
    return response
  })
  return { fetcher, resolve, signal: () => signal }
}

describe('自动抢购任务请求', () => {
  it('恢复显式总数量模式不再将指定池目标压成1', async () => {
    const selectedTotal = { ...serverSettings, quantity_mode: 'total' as const, quantity_per_pet: 7 }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json(running({ settings: selectedTotal })))))
    const { rush } = setup()
    await rush.loadState()
    expect(rush.error.value).toBeNull()
    expect(rush.state.value?.settings).toEqual(selectedTotal)
  })

  it('开始指定池总数量时提交显式模式和真实N，不附加不存在的自定义字段', async () => {
    const fetcher = vi.fn((path: string, _init: RequestInit) => Promise.resolve(json(path.endsWith('/start') ? running() : idle())))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    await rush.start({ ...settings, quantityMode: 'total', quantityPerPet: 7 })
    const body = JSON.parse(fetcher.mock.calls.find(([path]) => path.endsWith('/start'))![1].body as string)
    expect(body).toMatchObject({ pet_ids: settings.petIds, quantity_mode: 'total', quantity_per_pet: 7 })
    expect(body).not.toHaveProperty('pet_quantities')
  })

  it('开始自定义模式提交不同目标，恢复状态完整保留目标映射', async () => {
    const quantities = { '1000101': 2, '1000201': 3 }
    const custom = { ...serverSettings, quantity_mode: 'custom' as const, pet_quantities: quantities }
    const fetcher = vi.fn((path: string, _init: RequestInit) => Promise.resolve(json(path.endsWith('/start') ? running({ settings: custom }) : idle())))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    await rush.start({ ...settings, quantityMode: 'custom', petQuantities: quantities })
    expect(JSON.parse(fetcher.mock.calls.find(([path]) => path.endsWith('/start'))![1].body as string))
      .toMatchObject({ quantity_mode: 'custom', pet_quantities: quantities })
    expect(rush.state.value?.settings).toEqual(custom)
    quantities['1000101'] = 9
    expect(rush.state.value?.settings?.pet_quantities?.['1000101']).toBe(2)
  })

  it('显式每种一次模式仍将兼容数量归一为1', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json(running({ settings: {
      ...serverSettings, quantity_mode: 'each_once', quantity_per_pet: 7,
    } })))))
    const { rush } = setup()
    await rush.loadState()
    expect(rush.error.value).toBeNull()
    expect(rush.state.value?.settings?.quantity_per_pet).toBe(1)
  })

  it.each([
    { quantity_mode: 'unknown' },
    { quantity_mode: 'custom' },
    { quantity_mode: 'custom', pet_quantities: { '1000101': 1 } },
    { quantity_mode: 'custom', pet_quantities: { '1000101': 1, '1000201': 1, unknown: 1 } },
    { quantity_mode: 'custom', pet_quantities: { '1000101': 0, '1000201': 1 } },
    { quantity_mode: 'custom', pet_quantities: { '1000101': Number.MAX_SAFE_INTEGER, '1000201': 1 } },
  ])('拒绝未知模式或不完整/溢出的自定义状态 %j', override => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json({ ...running(), settings: { ...serverSettings, ...override } }))))
    const { rush } = setup()
    return rush.loadState().then(() => {
      expect(rush.state.value).toBeNull()
      expect(rush.canStart.value).toBe(false)
      expect(rush.error.value).toContain('无法读取抢购状态')
    })
  })

  it('不限噜咪按总数量恢复，服务端返回已完成后停止读取', async () => {
    vi.useFakeTimers()
    const allSettings = { ...serverSettings, pet_ids: [], quantity_per_pet: 2 }
    let current = running({ settings: allSettings, counts: {} })
    const fetcher = vi.fn(() => Promise.resolve(json(current)))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    expect(rush.error.value).toBeNull()
    expect(rush.state.value?.settings?.pet_ids).toEqual([])
    expect(rush.active.value).toBe(true)
    current = running({ settings: allSettings, counts: { '1000101': 2 }, status: 'completed' })
    await vi.advanceTimersByTimeAsync(1000)
    expect(rush.state.value?.counts['1000101']).toBe(2)
    expect(rush.state.value?.status).toBe('completed')
    expect(rush.canStart.value).toBe(true)
    expect(rush.canStop.value).toBe(false)
    await vi.advanceTimersByTimeAsync(2000)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('空选择开始按原样发送pet_ids空数组，不展开目录或自动改变数量', async () => {
    vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(json(path.endsWith('/start')
      ? running({ settings: { ...serverSettings, pet_ids: [], quantity_per_pet: 2 }, counts: {} }) : idle()))))
    const { rush } = setup()
    await rush.loadState()
    await rush.start({ ...settings, petIds: [], quantityPerPet: 2 })
    const fetcher = vi.mocked(fetch)
    expect(JSON.parse(fetcher.mock.calls.find(([path]) => path === '/api/rush-purchase/start')![1]!.body as string))
      .toMatchObject({ pet_ids: [], quantity_per_pet: 2 })
    expect(rush.error.value).toBeNull()
    expect(rush.state.value?.settings?.pet_ids).toEqual([])
  })

  it('旧指定模式恢复将兼容数量归一为1且不修改原响应', async () => {
    const legacy = running({ status: 'stopped', settings: { ...serverSettings, quantity_per_pet: 7 } })
    const fetcher = vi.fn((path: string) => Promise.resolve(json(path.endsWith('/start') ? running() : legacy)))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    expect(rush.state.value?.settings?.quantity_per_pet).toBe(1)
    expect(legacy.settings?.quantity_per_pet).toBe(7)
  })

  it('直接开始旧指定设置时也只提交每种1只', async () => {
    const fetcher = vi.fn((path: string, _init: RequestInit) => Promise.resolve(json(path.endsWith('/start') ? running() : idle())))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    await rush.start({ ...settings, quantityPerPet: 7 })
    const start = fetcher.mock.calls.find(([path]) => path === '/api/rush-purchase/start')!
    expect(JSON.parse(start[1].body as string).quantity_per_pet).toBe(1)
  })

  it('初始化只读取状态，未核实状态前不能开始', async () => {
    const fetcher = vi.fn().mockResolvedValue(json(idle()))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    expect(rush.canStart.value).toBe(false)
    await rush.start(settings)
    expect(fetcher).not.toHaveBeenCalled()
    await rush.loadState()
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]![0]).toBe('/api/rush-purchase')
    expect(fetcher.mock.calls[0]![1].method).toBe('GET')
    expect(rush.state.value).toEqual(idle())
    expect(rush.canStart.value).toBe(true)
  })

  it('用户开始时提交完整设置和随机请求ID，连续点击只提交一次', async () => {
    const uuid = vi.spyOn(crypto, 'randomUUID').mockReturnValue('00000000-0000-4000-8000-000000000001')
    const deferred = pending()
    const fetcher = vi.fn((path: string, init: RequestInit) => path.endsWith('/start')
      ? deferred.fetcher(path, init) : Promise.resolve(json(idle())))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    const first = rush.start(settings)
    await rush.start(settings)
    expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/start'))).toHaveLength(1)
    const start = fetcher.mock.calls.at(-1)!
    expect(start[1].method).toBe('POST')
    expect(JSON.parse(start[1].body as string)).toEqual({ request_id: '00000000-0000-4000-8000-000000000001', ...serverSettings })
    expect(uuid).toHaveBeenCalledOnce()
    expect(rush.busy.value).toBe(true)
    expect(rush.settingsLocked.value).toBe(true)
    deferred.resolve(json(running()))
    await first
    expect(rush.state.value).toEqual(running())
    expect(rush.canStart.value).toBe(false)
    expect(rush.canStop.value).toBe(true)
  })

  it('恢复运行任务每秒串行读取状态，结束后停止且不查询商品', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn().mockResolvedValueOnce(json(running())).mockResolvedValue(json(running({ status: 'completed', counts: { '1000101': 2, '1000201': 2 } })))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    expect(rush.state.value?.balance_gold).toBe('90071992547409930000')
    await vi.advanceTimersByTimeAsync(999)
    expect(fetcher).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(rush.state.value?.status).toBe('completed')
    expect(rush.canStart.value).toBe(true)
    await vi.advanceTimersByTimeAsync(3000)
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls.every(([path, init]) => path === '/api/rush-purchase' && init.method === 'GET')).toBe(true)
  })

  it('状态请求未返回时不重叠轮询，停止请求可中止旧读取', async () => {
    vi.useFakeTimers()
    const deferred = pending()
    let reads = 0
    const fetcher = vi.fn((path: string, init: RequestInit) => {
      if (path.endsWith('/stop')) return Promise.resolve(json(running({ status: 'stopped' })))
      return ++reads === 1 ? Promise.resolve(json(running())) : deferred.fetcher(path, init)
    })
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    await vi.advanceTimersByTimeAsync(1000)
    expect(reads).toBe(2)
    await vi.advanceTimersByTimeAsync(2000)
    expect(reads).toBe(2)
    await rush.stop()
    expect(deferred.signal().aborted).toBe(true)
    deferred.resolve(json(running()))
    await flushPromises()
    expect(rush.state.value?.status).toBe('stopped')
  })

  it('停止只提交当前job_id，重复点击只提交一次并等待停止状态', async () => {
    const deferred = pending()
    const fetcher = vi.fn((path: string, init: RequestInit) => path.endsWith('/stop')
      ? deferred.fetcher(path, init) : Promise.resolve(json(running())))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    const stop = rush.stop()
    await rush.stop()
    const posts = fetcher.mock.calls.filter(([path]) => path.endsWith('/stop'))
    expect(posts).toHaveLength(1)
    expect(JSON.parse(posts[0]![1].body as string)).toEqual({ job_id: 'test-job' })
    deferred.resolve(json(running({ status: 'stopping' })))
    await stop
    expect(rush.state.value?.status).toBe('stopping')
    expect(rush.canStop.value).toBe(false)
    expect(rush.settingsLocked.value).toBe(true)
  })

  it('开始网络失败后只读取核实状态，不把失败当未启动或重发POST', async () => {
    vi.useFakeTimers()
    let reads = 0
    const fetcher = vi.fn((path: string) => {
      if (path.endsWith('/start')) return Promise.reject(new Error('private raw transport error'))
      return Promise.resolve(json(++reads === 1 ? idle() : running()))
    })
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    await rush.start(settings)
    expect(rush.error.value).toContain('待核实')
    expect(rush.error.value).not.toContain('private raw')
    expect(rush.canStart.value).toBe(false)
    await rush.start(settings)
    await vi.advanceTimersByTimeAsync(1000)
    expect(rush.state.value?.status).toBe('running')
    expect(rush.error.value).toBeNull()
    expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/start'))).toHaveLength(1)
    expect(fetcher.mock.calls.every(([path]) => path.startsWith('/api/rush-purchase'))).toBe(true)
  })

  it('开始结果不明时读到旧idle也不允许新请求ID重复开始', async () => {
    const fetcher = vi.fn((path: string) => path.endsWith('/start')
      ? Promise.reject(new Error('test-only')) : Promise.resolve(json(idle())))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    await rush.start(settings)
    await rush.loadState()
    expect(rush.state.value?.status).toBe('idle')
    expect(rush.canStart.value).toBe(false)
    expect(rush.error.value).toContain('待核实')
    await rush.start(settings)
    expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/start'))).toHaveLength(1)
  })

  it('轮询网络失败保留当前进度和余额，下一次只GET恢复', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn().mockResolvedValueOnce(json(running()))
      .mockRejectedValueOnce(new Error('private raw'))
      .mockResolvedValue(json(running({ counts: { '1000101': 2, '1000201': 1 } })))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    await vi.advanceTimersByTimeAsync(1000)
    expect(rush.state.value).toEqual(running())
    expect(rush.error.value).toBeTruthy()
    expect(rush.error.value).not.toContain('private raw')
    await vi.advanceTimersByTimeAsync(1000)
    expect(rush.state.value?.counts['1000201']).toBe(1)
    expect(rush.error.value).toBeNull()
    expect(fetcher.mock.calls.every(([path, init]) => path === '/api/rush-purchase' && init.method === 'GET')).toBe(true)
  })

  it('uncertain结果在本次会话禁止重新开始', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json(running({ status: 'uncertain', message: '购买结果待核实' })))
      .mockResolvedValue(json(running({ status: 'stopped' })))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    expect(rush.canStart.value).toBe(false)
    await rush.loadState()
    await rush.start(settings)
    expect(rush.canStart.value).toBe(false)
    expect(fetcher.mock.calls.every(([path]) => path === '/api/rush-purchase')).toBe(true)
  })

  it('初始化网络错误或无效响应不能解锁开始，重新读取成功后可开始', async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new Error('test-only'))
      .mockResolvedValueOnce(json({ status: 'idle' })).mockResolvedValue(json(idle()))
    vi.stubGlobal('fetch', fetcher)
    const { rush } = setup()
    await rush.loadState()
    expect(rush.canStart.value).toBe(false)
    await rush.loadState()
    expect(rush.state.value).toBeNull()
    expect(rush.canStart.value).toBe(false)
    await rush.loadState()
    expect(rush.canStart.value).toBe(true)
  })

  it.each(['read', 'start', 'stop', 'poll'])('%s收到401时只通知一次并停止后续任务请求', async action => {
    vi.useFakeTimers()
    const denied = () => json({ error: { code: 'AUTH_REQUIRED', message: '登录已过期' } }, 401)
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(() => {
      calls++
      return Promise.resolve(action === 'read' || calls > 1 ? denied() : json(action === 'start' ? idle() : running()))
    }))
    const { rush, expired } = setup()
    await rush.loadState()
    if (action === 'start') await rush.start(settings)
    if (action === 'stop') await rush.stop()
    if (action === 'poll') await vi.advanceTimersByTimeAsync(1000)
    expect(expired).toHaveBeenCalledOnce()
    await rush.loadState()
    await rush.start(settings)
    await rush.stop()
    await vi.advanceTimersByTimeAsync(3000)
    expect(expired).toHaveBeenCalledOnce()
    expect(calls).toBe(action === 'read' ? 1 : 2)
  })

  it('销毁中止读取和计时器，迟到响应不更新状态或触发任务', async () => {
    vi.useFakeTimers()
    const deferred = pending()
    vi.stubGlobal('fetch', deferred.fetcher)
    const { rush, scope, expired } = setup()
    const read = rush.loadState()
    scope.stop()
    expect(deferred.signal().aborted).toBe(true)
    deferred.resolve(json(running()))
    await read
    await vi.advanceTimersByTimeAsync(3000)
    expect(rush.state.value).toBeNull()
    expect(expired).not.toHaveBeenCalled()
    expect(deferred.fetcher).toHaveBeenCalledOnce()
  })
})
