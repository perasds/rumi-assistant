import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ServerClock from '../components/layout/ServerClock.vue'
import { useServerClock } from '../composables/useServerClock'

const wrappers: Array<ReturnType<typeof mount>> = []
let monotonicMs = 1000
const serverMs = Date.UTC(2026, 9, 18, 4, 34, 56, 800)
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const serverTime = (unixMs = serverMs) => json({ unix_ms: unixMs, time_zone: 'Asia/Shanghai' })

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  vi.setSystemTime(new Date('2035-01-01T00:00:00Z'))
  monotonicMs = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => monotonicMs)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function clock() {
  const wrapper = mount(ServerClock)
  wrappers.push(wrapper)
  return wrapper
}
async function advance(milliseconds: number) {
  monotonicMs += milliseconds
  await vi.advanceTimersByTimeAsync(milliseconds)
}

it('首次挂载请求服务器时间，按往返耗时的一半补偿，与设备墙钟无关', async () => {
  let resolve!: (response: Response) => void
  const fetcher = vi.fn(() => new Promise<Response>(accept => { resolve = accept }))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = clock()
  expect(wrapper.text()).toBe('--:--:--')
  expect(fetcher).toHaveBeenCalledExactlyOnceWith('/api/time', expect.objectContaining({ method: 'GET', cache: 'no-store' }))
  monotonicMs += 400
  resolve(serverTime())
  await flushPromises()
  expect(wrapper.text()).toBe('12:34:57')
  expect(wrapper.get('[aria-label="服务器时间"]').attributes('title')).toBe('北京时间')
  expect(wrapper.find('button').exists()).toBe(false)
})

it('每秒更新，修改设备时间不会改变已同步基准，午夜显示00', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(serverTime(Date.UTC(2026, 9, 17, 16))))
  const wrapper = clock()
  await flushPromises()
  expect(wrapper.text()).toBe('00:00:00')
  await advance(1000)
  expect(wrapper.text()).toBe('00:00:01')
  vi.setSystemTime(new Date('1998-12-31T00:00:00Z'))
  await advance(1000)
  expect(wrapper.text()).toBe('00:00:02')
})

it('计时回调延迟后直接按单调时间差计算，不累计每次回调造成的漂移', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(serverTime(Date.UTC(2026, 9, 18, 4))))
  const wrapper = clock()
  await flushPromises()
  monotonicMs += 37_800
  await vi.advanceTimersByTimeAsync(1000)
  expect(wrapper.text()).toBe('12:00:37')
  monotonicMs += 6200
  await vi.advanceTimersByTimeAsync(1000)
  expect(wrapper.text()).toBe('12:00:44')
})

it('每60秒校准，页面恢复可见时重新校准并防止并发请求', async () => {
  let resolveSecond!: (response: Response) => void
  const fetcher = vi.fn().mockResolvedValueOnce(serverTime(Date.UTC(2026, 9, 18, 4)))
    .mockImplementationOnce(() => new Promise<Response>(accept => { resolveSecond = accept }))
    .mockResolvedValue(serverTime(Date.UTC(2026, 9, 18, 5)))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = clock()
  await flushPromises()
  await advance(59_000)
  expect(fetcher).toHaveBeenCalledTimes(1)
  await advance(1000)
  expect(fetcher).toHaveBeenCalledTimes(2)
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  document.dispatchEvent(new Event('visibilitychange'))
  await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(2)
  resolveSecond(serverTime(Date.UTC(2026, 9, 18, 4, 2)))
  await flushPromises()
  expect(wrapper.text()).toBe('12:02:00')
  document.dispatchEvent(new Event('visibilitychange'))
  await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(3)
  expect(wrapper.text()).toBe('13:00:00')
})

it.each([
  () => Promise.reject(new Error('offline')),
  () => Promise.resolve(json({ error: 'unavailable' }, 503)),
  () => Promise.resolve(json({ unix_ms: 1.5, time_zone: 'Asia/Shanghai' })),
  () => Promise.resolve(json({ unix_ms: serverMs, time_zone: 'UTC' })),
  () => Promise.resolve(json({ unix_ms: Number.MAX_SAFE_INTEGER, time_zone: 'Asia/Shanghai' })),
])('首次同步失败或返回无效数据时不回退设备时间', async response => {
  vi.stubGlobal('fetch', vi.fn(response))
  const wrapper = clock()
  await flushPromises()
  await advance(1000)
  expect(wrapper.text()).toBe('--:--:--')
})

it('后续校准失败仍按之前服务器基准走时，重新挂载会重新同步', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(serverTime(Date.UTC(2026, 9, 18, 4)))
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue(serverTime(Date.UTC(2026, 9, 18, 5)))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = clock()
  await flushPromises()
  await advance(60_000)
  expect(wrapper.text()).toBe('12:01:00')
  await advance(1000)
  expect(wrapper.text()).toBe('12:01:01')
  wrapper.unmount()
  const refreshed = clock()
  await flushPromises()
  expect(fetcher).toHaveBeenCalledTimes(3)
  expect(refreshed.text()).toBe('13:00:00')
})

it('卸载中止请求并移除计时器和可见性监听，迟到响应不更新状态', async () => {
  let resolve!: (response: Response) => void
  let signal!: AbortSignal
  const fetcher = vi.fn((_path: string, init: RequestInit) => {
    signal = init.signal as AbortSignal
    return new Promise<Response>(accept => { resolve = accept })
  })
  vi.stubGlobal('fetch', fetcher)
  let value!: ReturnType<typeof useServerClock>
  const Harness = defineComponent({
    setup() { value = useServerClock(); return { displayTime: value.displayTime } },
    template: '<span>{{ displayTime }}</span>',
  })
  const wrapper = mount(Harness)
  wrappers.push(wrapper)
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  resolve(serverTime())
  await flushPromises()
  expect(value.displayTime.value).toBe('--:--:--')
  expect(vi.getTimerCount()).toBe(0)
  document.dispatchEvent(new Event('visibilitychange'))
  await advance(120_000)
  expect(fetcher).toHaveBeenCalledTimes(1)
})
