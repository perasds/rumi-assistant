import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import MyView from '../views/MyView.vue'

const wrappers: ReturnType<typeof mount>[] = []
const idle = {
  available: true, running: false, status: 'idle', message: '尚未启动', claimed_count: 0,
  skipped_count: 0, stamina_total: '0', recent: [], started_at: null, updated_at: null,
}
const running = { ...idle, running: true, status: 'running', message: '正在等待新红包' }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
const deferred = () => {
  let resolve!: (response: Response) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<Response>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
function setup(response: (path: string, init: RequestInit) => Promise<Response> = async () => json(idle)) {
  const fetcher = vi.fn(response)
  vi.stubGlobal('fetch', (path: string, init: RequestInit = {}) => path === '/api/me'
    ? Promise.resolve(json({ role_id: '1', nickname: '测试噜咪', server_name: '测试区', balance_gold: '0' }))
    : fetcher(path, init))
  const wrapper = mount(MyView, { attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}
const panel = (wrapper: ReturnType<typeof mount>) => wrapper.get('section[aria-label="自动抢红包"]')
const startButton = (wrapper: ReturnType<typeof mount>) => panel(wrapper).get('button[aria-label="开始自动抢红包"]')
const stopButton = (wrapper: ReturnType<typeof mount>) => panel(wrapper).get('button[aria-label="取消自动抢红包"]')
let hidden = false
async function setHidden(value: boolean) {
  hidden = value
  document.dispatchEvent(new Event('visibilitychange'))
  await flushPromises()
}
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  hidden = false
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('个人资料下默认关闭，只读取一次任务，闲置不轮询或自动开始', async () => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(panel(wrapper).text()).toContain('尚未启动')
  expect(panel(wrapper).text()).toContain('只抢启动后出现的新红包')
  expect(startButton(wrapper).attributes('disabled')).toBeUndefined()
  expect(wrapper.find('.personal-info-card + .auto-redbag-panel').exists()).toBe(true)
  await vi.advanceTimersByTimeAsync(30_000)
  expect(fetcher).toHaveBeenCalledOnce()
  expect(fetcher.mock.calls[0]?.[0]).toBe('/api/redbags/task')
})

it('开始和取消由用户触发，busy 防连点且停止请求没有 body', async () => {
  const start = deferred()
  const stop = deferred()
  const { wrapper, fetcher } = setup(async path => path.endsWith('/start') ? start.promise : path.endsWith('/stop') ? stop.promise : json(idle))
  await flushPromises()
  await startButton(wrapper).trigger('click')
  await startButton(wrapper).trigger('click')
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(startButton(wrapper).attributes('disabled')).toBeDefined()
  const startInit = fetcher.mock.calls[1]?.[1]
  expect(startInit?.method).toBe('POST')
  expect(JSON.parse(startInit?.body as string)).toEqual({ request_id: expect.stringMatching(/^[0-9a-f-]{36}$/) })
  start.resolve(json(running))
  await flushPromises()
  expect(panel(wrapper).text()).toContain('正在等待新红包')
  await stopButton(wrapper).trigger('click')
  await stopButton(wrapper).trigger('click')
  expect(fetcher).toHaveBeenCalledTimes(3)
  expect(fetcher.mock.calls[2]?.[0]).toBe('/api/redbags/task/stop')
  expect(fetcher.mock.calls[2]?.[1].method).toBe('POST')
  expect(fetcher.mock.calls[2]?.[1].body).toBeUndefined()
  expect(stopButton(wrapper).attributes('disabled')).toBeDefined()
  stop.resolve(json({ ...idle, status: 'stopped', message: '已手动取消' }))
  await flushPromises()
  expect(panel(wrapper).text()).toContain('已手动取消')
  expect(startButton(wrapper).attributes('disabled')).toBeUndefined()
  await vi.advanceTimersByTimeAsync(9000)
  expect(fetcher).toHaveBeenCalledTimes(3)
})

it('启动网络结果不明确时，手动重试使用同一个请求ID，确定结束后新开始才换ID', async () => {
  let starts = 0
  const { wrapper, fetcher } = setup(async path => {
    if (path.endsWith('/start')) {
      if (++starts === 1) throw new Error('synthetic-private-error')
      return json({ ...idle, status: 'daily_limit', message: '今日已达上限' })
    }
    return json(idle)
  })
  await flushPromises()
  await startButton(wrapper).trigger('click')
  await flushPromises()
  expect(panel(wrapper).text()).toContain('启动结果尚未确认')
  expect(panel(wrapper).text()).not.toContain('synthetic-private-error')
  await startButton(wrapper).trigger('click')
  await flushPromises()
  const firstId = JSON.parse(fetcher.mock.calls[1]?.[1].body as string).request_id
  expect(JSON.parse(fetcher.mock.calls[2]?.[1].body as string).request_id).toBe(firstId)
  await startButton(wrapper).trigger('click')
  await flushPromises()
  expect(JSON.parse(fetcher.mock.calls[3]?.[1].body as string).request_id).not.toBe(firstId)
})

it.each([
  ['daily_limit', '今日领取次数已达上限'],
  ['conditions_not_met', '当前条件不满足，已停止'],
  ['rate_limited', '请求过于频繁，已停止'],
  ['failed', '任务执行失败，已停止'],
])('展示 %s 停止原因并允许手动重新开始，不自动恢复', async (status, message) => {
  const { wrapper, fetcher } = setup(async () => json({ ...idle, status, message }))
  await flushPromises()
  expect(panel(wrapper).text()).toContain(message)
  expect(startButton(wrapper).attributes('disabled')).toBeUndefined()
  await vi.advanceTimersByTimeAsync(9000)
  expect(fetcher).toHaveBeenCalledOnce()
})

it('结果待核实禁止再次开始并提示核实后重新登录', async () => {
  const { wrapper, fetcher } = setup(async () => json({ ...idle, status: 'uncertain', message: '领取结果待核实' }))
  await flushPromises()
  expect(panel(wrapper).text()).toContain('核实后重新登录')
  expect(startButton(wrapper).attributes('disabled')).toBeDefined()
  await startButton(wrapper).trigger('click')
  expect(fetcher).toHaveBeenCalledOnce()
})

it('功能不可用时展示原因并禁用开始', async () => {
  const { wrapper } = setup(async () => json({ ...idle, available: false, message: '连接尚未就绪' }))
  await flushPromises()
  expect(panel(wrapper).text()).toContain('连接尚未就绪')
  expect(startButton(wrapper).attributes('disabled')).toBeDefined()
})

it('精确显示本次成功数和体力，仅展示最近20条纯文本记录与明确字段', async () => {
  const recent = Array.from({ length: 22 }, (_, index) => ({
    id: index + 1, at: '2026-10-20T08:00:00Z', status: index === 0 ? 'uncertain' : index === 1 ? 'skipped' : 'claimed',
    message: index === 0 ? '<img src=x onerror=alert(1)>' : `结果-${index + 1}`,
    stamina: index === 0 ? '9007199254740993' : '0', private_detail: 'synthetic-record-secret',
  }))
  const { wrapper } = setup(async () => json({ ...running, claimed_count: 8, skipped_count: 2, stamina_total: '9007199254740993',
    recent, access_token: 'synthetic-task-secret' }))
  await flushPromises()
  const card = panel(wrapper)
  expect(card.text()).toContain('本次成功 8')
  expect(card.text()).toContain('体力合计 9,007,199,254,740,993')
  expect(card.findAll('li')).toHaveLength(20)
  expect(card.text()).toContain('待核实')
  expect(card.text()).toContain('已跳过')
  expect(card.text()).toContain('<img src=x onerror=alert(1)>')
  expect(card.find('img').exists()).toBe(false)
  expect(card.text()).not.toContain('synthetic-task-secret')
  expect(card.text()).not.toContain('synthetic-record-secret')
  expect(card.text()).not.toContain('结果-21')
})

it('运行时每3秒刷新且请求不重叠，后台读取不闪按钮或整个页面', async () => {
  const next = deferred()
  let reads = 0
  const { wrapper, fetcher } = setup(async () => ++reads === 1 ? json(running) : next.promise)
  await flushPromises()
  await vi.advanceTimersByTimeAsync(2999)
  expect(fetcher).toHaveBeenCalledOnce()
  await vi.advanceTimersByTimeAsync(1)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(wrapper.get('main').attributes('aria-busy')).toBe('false')
  expect(stopButton(wrapper).attributes('disabled')).toBeUndefined()
  expect(stopButton(wrapper).find('.van-loading').exists()).toBe(false)
  await vi.advanceTimersByTimeAsync(6000)
  expect(fetcher).toHaveBeenCalledTimes(2)
  next.resolve(json({ ...running, claimed_count: 1, stamina_total: '12' }))
  await flushPromises()
  expect(panel(wrapper).text()).toContain('本次成功 1')
  await vi.advanceTimersByTimeAsync(2999)
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it('隐藏时停轮询，恢复可见立即读取并按最新状态停止轮询', async () => {
  let reads = 0
  const { wrapper, fetcher } = setup(async () => json(++reads === 1 ? running : { ...idle, status: 'daily_limit', message: '今日上限' }))
  await flushPromises()
  await setHidden(true)
  await vi.advanceTimersByTimeAsync(9000)
  expect(fetcher).toHaveBeenCalledOnce()
  await setHidden(false)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(panel(wrapper).text()).toContain('今日上限')
  await vi.advanceTimersByTimeAsync(9000)
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it('闲置页恢复可见也读取一次已有任务', async () => {
  let reads = 0
  const { wrapper, fetcher } = setup(async () => json(++reads === 1 ? idle : running))
  await flushPromises()
  await setHidden(true)
  await setHidden(false)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(stopButton(wrapper).text()).toBe('取消')
})

it('刷新错误保留已知运行状态和统计，不误显示未启动', async () => {
  let reads = 0
  const { wrapper, fetcher } = setup(async () => {
    if (++reads === 1) return json({ ...running, claimed_count: 3, stamina_total: '20' })
    throw new Error('synthetic-private-error')
  })
  await flushPromises()
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(panel(wrapper).text()).toContain('本次成功 3')
  expect(panel(wrapper).text()).toContain('正在等待新红包')
  expect(panel(wrapper).text()).toContain('无法连接服务')
  expect(panel(wrapper).text()).not.toContain('尚未启动')
  expect(panel(wrapper).text()).not.toContain('synthetic-private-error')
  expect(stopButton(wrapper).attributes('disabled')).toBeUndefined()
})

it('初次读取失败显示未知状态并保留读取重试入口', async () => {
  let reads = 0
  const { wrapper } = setup(async () => {
    if (++reads === 1) throw new Error('synthetic-private-error')
    return json(running)
  })
  await flushPromises()
  expect(panel(wrapper).text()).toContain('任务状态尚未读取')
  expect(startButton(wrapper).attributes('disabled')).toBeDefined()
  await panel(wrapper).get('button[aria-label="刷新红包任务"]').trigger('click')
  await flushPromises()
  expect(stopButton(wrapper).text()).toBe('取消')
})

it('取消可以中断刷新，晚到读取不能覆盖已取消状态', async () => {
  const next = deferred()
  let reads = 0
  const { wrapper, fetcher } = setup(async path => path.endsWith('/stop')
    ? json({ ...idle, status: 'stopped', message: '已取消' })
    : ++reads > 1 ? next.promise : json(running))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(3000)
  const signal = fetcher.mock.calls[1]?.[1].signal!
  await stopButton(wrapper).trigger('click')
  await flushPromises()
  expect(signal.aborted).toBe(true)
  next.resolve(json({ ...running, claimed_count: 99 }))
  await flushPromises()
  expect(panel(wrapper).text()).toContain('已取消')
  expect(panel(wrapper).text()).not.toContain('本次成功 99')
})

it('离页取消当前读取和timer，晚到401不影响新页面，且从不发送停止', async () => {
  const next = deferred()
  let reads = 0
  const { wrapper, fetcher } = setup(async () => ++reads === 1 ? json(running) : next.promise)
  await flushPromises()
  await vi.advanceTimersByTimeAsync(3000)
  const signal = fetcher.mock.calls[1]?.[1].signal!
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  next.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '登录过期' } }, 401))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(9000)
  expect(wrapper.emitted('expired')).toBeUndefined()
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(fetcher.mock.calls.every(([path]) => path === '/api/redbags/task')).toBe(true)
})

it('离页取消启动请求后，返回页面读取已有任务，不重复启动', async () => {
  const start = deferred()
  const { wrapper, fetcher } = setup(async path => path.endsWith('/start') ? start.promise : json(idle))
  await flushPromises()
  await startButton(wrapper).trigger('click')
  const signal = fetcher.mock.calls[1]?.[1].signal!
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  start.resolve(json(running))
  await flushPromises()
  const next = setup(async () => json(running))
  await flushPromises()
  expect(stopButton(next.wrapper).text()).toBe('取消')
  expect(next.fetcher).toHaveBeenCalledOnce()
  expect(next.fetcher.mock.calls[0]?.[0]).toBe('/api/redbags/task')
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/start'))).toHaveLength(1)
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/stop'))).toBe(false)
})

it('任务401仅通知上层一次并停止后续读取', async () => {
  const { wrapper, fetcher } = setup(async () => json({ error: { code: 'AUTH_REQUIRED', message: '登录过期' } }, 401))
  await flushPromises()
  expect(wrapper.emitted('expired')).toHaveLength(1)
  await setHidden(true)
  await setHidden(false)
  await vi.advanceTimersByTimeAsync(9000)
  expect(wrapper.emitted('expired')).toHaveLength(1)
  expect(fetcher).toHaveBeenCalledOnce()
})

it('启动中且服务暂不可用仍可取消，取消中仅等待后台结束', async () => {
  const { wrapper, fetcher } = setup(async path => json(path.endsWith('/stop')
    ? { ...running, status: 'stopping', message: '正在等待取消完成' }
    : { ...running, available: false, status: 'starting', message: '正在连接' }))
  await flushPromises()
  expect(stopButton(wrapper).attributes('disabled')).toBeUndefined()
  await stopButton(wrapper).trigger('click')
  await flushPromises()
  expect(panel(wrapper).text()).toContain('正在等待取消完成')
  expect(stopButton(wrapper).attributes('disabled')).toBeDefined()
  await stopButton(wrapper).trigger('click')
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it('个人资料与任务同时401时只通知上层一次', async () => {
  vi.stubGlobal('fetch', async () => json({ error: { code: 'AUTH_REQUIRED', message: '登录过期' } }, 401))
  const wrapper = mount(MyView, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  expect(wrapper.emitted('expired')).toHaveLength(1)
})
