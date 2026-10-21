import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useOrderAssistant } from '../composables/useOrderAssistant'
import { orderPlan, orderState } from './orderFixtures'

const wrappers: ReturnType<typeof mount>[] = []
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
const state = (task: Record<string, unknown> = {}) => orderState({ work_inspection: { ready: true, reason: null, buildings: [] },
  task: { ...orderState().task, ...task } })
const running = (kind = 'orders') => state({ kind, status: 'running', running: true })
let hidden = false
beforeEach(() => {
  vi.useFakeTimers()
  hidden = false
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
function setup(response: (path: string, init: RequestInit) => Promise<Response> = async () => json(state())) {
  const fetcher = vi.fn(response)
  vi.stubGlobal('fetch', fetcher)
  const expired = vi.fn()
  let assistant!: ReturnType<typeof useOrderAssistant>
  const wrapper = mount(defineComponent({ setup() {
    assistant = useOrderAssistant(expired)
    return () => null
  } }))
  wrappers.push(wrapper)
  return { assistant, fetcher, wrapper, expired }
}
function visibility(value: boolean) {
  hidden = value
  document.dispatchEvent(new Event('visibilitychange'))
}
function deferred() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>(done => { resolve = done })
  return { promise, resolve }
}

it('无任务进入只读一次，空闲恢复可见不额外读取，手动刷新仍可用', async () => {
  const { assistant, fetcher } = setup()
  await flushPromises()
  await vi.advanceTimersByTimeAsync(5000)
  expect(fetcher).toHaveBeenCalledOnce()
  visibility(true)
  visibility(false)
  await flushPromises()
  await vi.advanceTimersByTimeAsync(2000)
  expect(fetcher).toHaveBeenCalledOnce()
  await assistant.load(true)
  expect(fetcher).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it.each(['settled', 'quick'])('首次读取被隐藏中止后可恢复（%s），迟到响应不覆盖空闲结果', async timing => {
  const pending = deferred()
  let reads = 0
  const recovered = { ...state(), revision: 'recovered-revision' }
  const { assistant, fetcher } = setup(async () => ++reads === 1 ? pending.promise : json(recovered))
  const signal = fetcher.mock.calls[0]![1].signal!
  visibility(true)
  expect(signal.aborted).toBe(true)
  if (timing === 'settled') {
    pending.resolve(json(running()))
    await flushPromises()
  }
  visibility(false)
  await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(assistant.state.value?.revision).toBe('recovered-revision')
  if (timing === 'quick') {
    pending.resolve(json(running()))
    await flushPromises()
  }
  expect(assistant.state.value?.task.running).toBe(false)
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it('隐藏时挂载尚未开始首次读取，可见后补读一次，空闲后不轮询', async () => {
  hidden = true
  const { fetcher } = setup()
  await flushPromises()
  expect(fetcher).not.toHaveBeenCalled()
  visibility(false)
  await flushPromises()
  expect(fetcher).toHaveBeenCalledOnce()
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetcher).toHaveBeenCalledOnce()
})

it('首次读取已失败时不会因可见变化反复重试，仍可手动刷新', async () => {
  let reads = 0
  const { assistant, fetcher } = setup(async () => {
    if (++reads === 1) throw new Error('synthetic-network-error')
    return json(state())
  })
  await flushPromises()
  expect(assistant.error.value).toContain('无法连接服务')
  visibility(true)
  visibility(false)
  await flushPromises()
  expect(fetcher).toHaveBeenCalledOnce()
  await assistant.load(true)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(assistant.state.value?.task.running).toBe(false)
})

it.each(['orders', 'inspection'])('进入发现已有%s运行任务时恢复每秒一次读取', async kind => {
  const { fetcher } = setup(async () => json(running(kind)))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(2100)
  expect(fetcher).toHaveBeenCalledTimes(3)
  expect(fetcher.mock.calls.every(([path, init]) => path.endsWith('/state') && init.method === 'GET')).toBe(true)
})

it.each(['orders', 'inspection'])('显式开始%s成功才启动轮询，停止确认后停止读取', async kind => {
  let current = state()
  const { assistant, fetcher } = setup(async path => {
    if (path.endsWith('/start')) current = running(kind)
    if (path.endsWith('/stop')) current = state({ kind, status: 'stopped' })
    return json(current)
  })
  await flushPromises()
  await vi.advanceTimersByTimeAsync(2100)
  expect(fetcher).toHaveBeenCalledOnce()
  await (kind === 'inspection' ? assistant.startInspection() : assistant.start())
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/start'))).toHaveLength(1)
  await vi.advanceTimersByTimeAsync(2100)
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/state'))).toHaveLength(3)
  await assistant.stop()
  const count = fetcher.mock.calls.length
  await vi.advanceTimersByTimeAsync(4000)
  expect(fetcher).toHaveBeenCalledTimes(count)
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/stop'))).toHaveLength(1)
})

it.each(['idle', 'stopped', 'completed', 'failed', 'uncertain', 'confirming'])('轮询读到%s且running=false后停止，恢复可见也不多读', async status => {
  let current = running()
  const { fetcher } = setup(async () => json(current))
  await flushPromises()
  current = state({ status })
  await vi.advanceTimersByTimeAsync(1000)
  expect(fetcher).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(4000)
  visibility(true)
  visibility(false)
  await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it.each(['save', 'preview', 'resolve'] as const)('%s完成未运行任务时不安排轮询', async operation => {
  const initial = operation === 'resolve' ? state({ status: 'uncertain' }) : state()
  const { assistant, fetcher } = setup(async path => json(path.endsWith('/plan') ? orderPlan() : path.endsWith('/state') ? initial : state()))
  await flushPromises()
  await assistant[operation]()
  expect(fetcher).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(4000)
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it('运行时隐藏暂停，恢复可见立即读取并继续唯一轮询', async () => {
  const { fetcher } = setup(async () => json(running()))
  await flushPromises()
  visibility(true)
  await vi.advanceTimersByTimeAsync(4000)
  expect(fetcher).toHaveBeenCalledOnce()
  visibility(false)
  await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(2100)
  expect(fetcher).toHaveBeenCalledTimes(4)
})

it('已知运行任务读取网络错误后保留状态并继续重试，只发送GET', async () => {
  let reads = 0
  const { assistant, fetcher } = setup(async () => {
    if (++reads === 2) throw new Error('synthetic-network-error')
    return json(running())
  })
  await flushPromises()
  await vi.advanceTimersByTimeAsync(1000)
  expect(assistant.state.value?.task.running).toBe(true)
  expect(assistant.error.value).toContain('无法连接服务')
  await vi.advanceTimersByTimeAsync(1000)
  expect(fetcher).toHaveBeenCalledTimes(3)
  expect(assistant.error.value).toBeNull()
  expect(fetcher.mock.calls.every(([path, init]) => path.endsWith('/state') && init.method === 'GET')).toBe(true)
})

it.each(['orders', 'inspection'])('%s停止请求结果未知时只继续核实已知运行任务，不重发停止', async kind => {
  let current = running(kind)
  const { assistant, fetcher } = setup(async path => {
    if (path.endsWith('/stop')) throw new Error('synthetic-network-error')
    return json(current)
  })
  await flushPromises()
  await assistant.stop()
  expect(assistant.state.value?.task.running).toBe(true)
  expect(assistant.canStart.value).toBe(false)
  current = state({ kind, status: 'stopped' })
  await vi.advanceTimersByTimeAsync(1000)
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/state'))).toHaveLength(2)
  expect(assistant.state.value?.task.running).toBe(false)
  expect(assistant.canStart.value).toBe(false)
  const count = fetcher.mock.calls.length
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetcher).toHaveBeenCalledTimes(count)
  await assistant.load(true)
  expect(assistant.canStart.value).toBe(true)
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/stop'))).toHaveLength(1)
})

it.each(['orders', 'inspection'])('%s开始结果未知时不自动读空闲状态或重发开始，保护保持到手动核实', async kind => {
  const { assistant, fetcher } = setup(async path => {
    if (path.endsWith('/start')) throw new Error('synthetic-network-error')
    return json(state())
  })
  await flushPromises()
  await (kind === 'inspection' ? assistant.startInspection() : assistant.start())
  await vi.advanceTimersByTimeAsync(4000)
  visibility(true)
  visibility(false)
  await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(assistant.canStart.value).toBe(false)
  expect(assistant.canStartInspection.value).toBe(false)
  await assistant.start()
  await assistant.startInspection()
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/start'))).toHaveLength(1)
  await assistant.load(true)
  expect(assistant.canStart.value).toBe(true)
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/start'))).toHaveLength(1)
})

it('慢读取不重叠，返回运行状态后从完成时刻安排下一次', async () => {
  const pending = deferred()
  let reads = 0
  const { fetcher } = setup(async () => ++reads === 2 ? pending.promise : json(running()))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(5000)
  expect(fetcher).toHaveBeenCalledTimes(2)
  pending.resolve(json(running()))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(999)
  expect(fetcher).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(1)
  expect(fetcher).toHaveBeenCalledTimes(3)
})

it('隐藏中止运行中的读取，迟到响应不更新；可见后重新核实', async () => {
  const pending = deferred()
  let reads = 0
  const { assistant, fetcher } = setup(async () => ++reads === 2 ? pending.promise : json(running()))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(1000)
  const signal = fetcher.mock.calls[1]![1].signal!
  visibility(true)
  expect(signal.aborted).toBe(true)
  pending.resolve(json(state({ status: 'completed' })))
  await flushPromises()
  expect(assistant.state.value?.task.running).toBe(true)
  visibility(false)
  await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(3)
})

it('卸载中止读取和计时器，迟到401不通知账号过期', async () => {
  const pending = deferred()
  let reads = 0
  const { assistant, fetcher, wrapper, expired } = setup(async () => ++reads === 2 ? pending.promise : json(running()))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(1000)
  const signal = fetcher.mock.calls[1]![1].signal!
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  pending.resolve(new Response(JSON.stringify({ error: { code: 'AUTH_REQUIRED', message: '过期' } }), { status: 401 }))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(4000)
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(expired).not.toHaveBeenCalled()
  expect(assistant.state.value).toBeNull()
})
