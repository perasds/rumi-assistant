import { flushPromises, mount } from '@vue/test-utils'
import { closeToast } from 'vant'
import { afterEach, expect, it, vi } from 'vitest'
import type { MarketSnapshot, PetCatalog, RushPurchaseState, Session } from '../api/types'
import MarketView from '../views/MarketView.vue'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  closeToast(true)
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const session: Session = { status: 'connected', authenticated: true, role: null, roles: [], message: null }
const settings = { petIds: ['1000101', '1000201'], maxPriceGold: 3000000, quantityPerPet: 1, minBalanceGold: 10000 }
const apiSettings = { pet_ids: [...settings.petIds], max_price_gold: settings.maxPriceGold, quantity_per_pet: 1, min_balance_gold: 10000 }
const idle = (): RushPurchaseState => ({ job_id: null, status: 'idle', settings: null, balance_gold: null,
  counts: {}, spent_gold: '0', message: null, attempts: 0, updated_at: null })
const running = (override: Partial<RushPurchaseState> = {}): RushPurchaseState => ({ ...idle(),
  job_id: 'test-job', status: 'running', settings: apiSettings, balance_gold: '90071992547409930000',
  counts: { '1000101': 1, '1000201': 0 }, attempts: 1, ...override })
const catalog: PetCatalog = { items: [
  { pet_id: '1000101', name: '蜜桃', display_name: '蜜桃', name_verified: true, image_url: null },
  { pet_id: '1000201', name: '灰灰', display_name: '灰灰', name_verified: true, image_url: null },
], total: 2, named_count: 2, image_count: 0 }
const snapshot: MarketSnapshot = { refresh_id: 'test-refresh', scope: 'top10', status: 'complete', complete: true,
  items: [], loaded_count: 0, matched_count: 0, total_count: null, started_at: null, updated_at: null, message: null,
  name_coverage: { named: 2, total: 2 } }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function setup(current = idle(), stateRequest?: (path: string, init: RequestInit) => Promise<Response>) {
  const fetcher = vi.fn((path: string, init: RequestInit) => {
    if (path.startsWith('/api/pets')) return Promise.resolve(json(catalog))
    if (path === '/api/market/refresh') return Promise.resolve(json(snapshot))
    if (path.startsWith('/api/rush-purchase')) return stateRequest ? stateRequest(path, init) : Promise.resolve(json(current))
    throw new Error(`Unexpected test request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(MarketView, { props: { session }, attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}

it('没有设置时开始只打开设置，保存后仍等待用户主动开始', async () => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(fetcher.mock.calls.filter(([path, init]) => path === '/api/rush-purchase' && init.method === 'GET')).toHaveLength(1)
  await wrapper.get('button[aria-label="开始抢购"]').trigger('click')
  const dialog = wrapper.getComponent({ name: 'RushPurchaseSettingsDialog' })
  expect(dialog.props('show')).toBe(true)
  await flushPromises()
  dialog.vm.$emit('save', settings)
  await flushPromises()
  expect(dialog.props('show')).toBe(false)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')).toHaveLength(0)
})

it('点击开始后同位置显示取消，取消确认后恢复开始且不自动重启', async () => {
  let current = idle()
  const { wrapper, fetcher } = setup(current, async path => {
    if (path.endsWith('/start')) current = running()
    if (path.endsWith('/stop')) current = running({ status: 'stopped', message: '已停止' })
    return json(current)
  })
  await flushPromises()
  wrapper.getComponent({ name: 'RushPurchaseSettingsDialog' }).vm.$emit('save', settings)
  await flushPromises()
  await wrapper.get('button[aria-label="开始抢购"]').trigger('click')
  await flushPromises()
  const posts = fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')
  expect(posts).toHaveLength(1)
  expect(JSON.parse(posts[0]![1].body as string)).toMatchObject(apiSettings)
  expect(wrapper.find('button[aria-label="开始抢购"]').exists()).toBe(false)
  expect(wrapper.get('.rush-actions .rush-run-button').text()).toBe('取消抢购')
  expect(wrapper.get('button[aria-label="抢购设置"]').attributes('disabled')).toBeDefined()
  expect(wrapper.get('.rush-status').text()).toContain('监测中')
  expect(wrapper.get('.rush-status').text()).toContain('蜜桃')
  expect(wrapper.get('.rush-status').text()).toContain('1 / 1')
  expect(wrapper.get('.rush-status').text()).toContain('灰灰')
  expect(wrapper.get('.rush-status').text()).toContain('0 / 1')
  expect(wrapper.get('.rush-status').text()).toContain('已完成 1 / 2 种')
  expect(wrapper.get('.rush-status').text()).toContain('90,071,992,547,409,930,000')
  await wrapper.get('button[aria-label="取消抢购"]').trigger('click')
  await flushPromises()
  expect(JSON.parse(fetcher.mock.calls.find(([path]) => path === '/api/rush-purchase/stop')![1].body as string)).toEqual({ job_id: 'test-job' })
  expect(wrapper.get('.rush-status').text()).toContain('已停止')
  expect(wrapper.find('button[aria-label="取消抢购"]').exists()).toBe(false)
  expect(wrapper.get('.rush-actions .rush-run-button').text()).toBe('开始抢购')
  expect(wrapper.get('button[aria-label="开始抢购"]').attributes('disabled')).toBeUndefined()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')).toHaveLength(1)
  expect(wrapper.get('button[aria-label="抢购设置"]').attributes('disabled')).toBeUndefined()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
  expect(wrapper.find('.market-scroll[aria-label="一口价宠物列表"]').exists()).toBe(true)
})

it('取消请求和服务端取消处理中禁用按钮，只发一次取消并等待确认', async () => {
  vi.useFakeTimers()
  let current = running()
  let resolveStop!: (response: Response) => void
  const stopResponse = new Promise<Response>(resolve => { resolveStop = resolve })
  const { wrapper, fetcher } = setup(current, path => path.endsWith('/stop')
    ? stopResponse : Promise.resolve(json(current)))
  await flushPromises()

  await wrapper.get('button[aria-label="取消抢购"]').trigger('click')
  expect(wrapper.get('button[aria-label="取消抢购"]').attributes('disabled')).toBeDefined()
  expect(wrapper.get('.rush-actions .rush-run-button').text()).toBe('取消中')
  expect(wrapper.find('button[aria-label="开始抢购"]').exists()).toBe(false)
  await wrapper.get('button[aria-label="取消抢购"]').trigger('click')
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/stop')).toHaveLength(1)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase')).toHaveLength(1)

  current = running({ status: 'stopping', message: '正在取消' })
  resolveStop(json(current))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(1000)
  expect(wrapper.get('button[aria-label="取消抢购"]').attributes('disabled')).toBeDefined()
  expect(wrapper.get('.rush-actions .rush-run-button').text()).toBe('取消中')
  expect(wrapper.find('button[aria-label="开始抢购"]').exists()).toBe(false)
  expect(wrapper.get('button[aria-label="抢购设置"]').attributes('disabled')).toBeDefined()
  await wrapper.get('button[aria-label="取消抢购"]').trigger('click')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/stop')).toHaveLength(1)

  current = running({ status: 'stopped', message: '已停止' })
  await vi.advanceTimersByTimeAsync(1000)
  expect(wrapper.find('button[aria-label="取消抢购"]').exists()).toBe(false)
  expect(wrapper.get('button[aria-label="开始抢购"]').attributes('disabled')).toBeUndefined()
  expect(wrapper.get('.rush-status').text()).toContain('已完成 1 / 2 种')
  const requestsAfterConfirmation = fetcher.mock.calls.length
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetcher).toHaveBeenCalledTimes(requestsAfterConfirmation)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')).toHaveLength(0)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
})

it('进入页面恢复任务和设置，只轮询任务进度而不自动发送开始', async () => {
  vi.useFakeTimers()
  const { wrapper, fetcher } = setup(running())
  await flushPromises()
  expect(wrapper.getComponent({ name: 'RushPurchaseSettingsDialog' }).props('settings')).toEqual(settings)
  expect(wrapper.find('button[aria-label="开始抢购"]').exists()).toBe(false)
  expect(wrapper.find('button[aria-label="取消抢购"]').exists()).toBe(true)
  await vi.advanceTimersByTimeAsync(2000)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase')).toHaveLength(3)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')).toHaveLength(0)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
})

it('待核实结果显示提示，禁止本会话再次开始', async () => {
  const { wrapper, fetcher } = setup(running({ status: 'uncertain', message: '购买结果待核实' }))
  await flushPromises()
  expect(wrapper.get('.rush-status').text()).toContain('购买结果待核实')
  expect(wrapper.get('button[aria-label="开始抢购"]').attributes('disabled')).toBeDefined()
  await wrapper.get('button[aria-label="开始抢购"]').trigger('click')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')).toHaveLength(0)
})

it('读取失败保留错误和重查入口，重查成功后不会自动开始', async () => {
  let calls = 0
  const { wrapper, fetcher } = setup(idle(), async () => {
    if (++calls === 1) throw new Error('private raw')
    return json(idle())
  })
  await flushPromises()
  expect(wrapper.get('.rush-status [role="alert"]').text()).not.toContain('private raw')
  expect(wrapper.get('button[aria-label="开始抢购"]').attributes('disabled')).toBeDefined()
  await wrapper.get('button[aria-label="重新读取抢购状态"]').trigger('click')
  await flushPromises()
  expect(wrapper.find('.rush-status [role="alert"]').exists()).toBe(false)
  expect(wrapper.get('button[aria-label="开始抢购"]').attributes('disabled')).toBeUndefined()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')).toHaveLength(0)
})

it('任务401通知会话过期，卸载后清除任务轮询', async () => {
  vi.useFakeTimers()
  let calls = 0
  const { wrapper, fetcher } = setup(running(), async () => ++calls === 1 ? json(running())
    : json({ error: { code: 'AUTH_REQUIRED', message: '登录已过期' } }, 401))
  await flushPromises()
  await vi.advanceTimersByTimeAsync(1000)
  expect(wrapper.emitted('expired')).toHaveLength(1)
  wrapper.unmount()
  const count = fetcher.mock.calls.length
  await vi.advanceTimersByTimeAsync(3000)
  expect(fetcher).toHaveBeenCalledTimes(count)
})

it('不限噜咪保存不启动，开始后按同类可多只的成交总数显示进度', async () => {
  vi.useFakeTimers()
  let current = idle()
  const allSettings = { ...settings, petIds: [], quantityPerPet: 3 }
  const allApiSettings = { ...apiSettings, pet_ids: [], quantity_per_pet: 3 }
  const { wrapper, fetcher } = setup(current, async path => {
    if (path.endsWith('/start')) current = running({ settings: allApiSettings, counts: {} })
    return json(current)
  })
  await flushPromises()
  wrapper.getComponent({ name: 'RushPurchaseSettingsDialog' }).vm.$emit('save', allSettings)
  await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')).toHaveLength(0)
  await wrapper.get('button[aria-label="开始抢购"]').trigger('click')
  await flushPromises()
  expect(JSON.parse(fetcher.mock.calls.find(([path]) => path === '/api/rush-purchase/start')![1].body as string).pet_ids).toEqual([])
  expect(wrapper.get('.rush-status').text()).toContain('全部彩色噜咪')
  expect(wrapper.get('.rush-status').text()).toContain('已购买 0 / 3 只')
  expect(wrapper.find('.rush-status .pet-progress').exists()).toBe(false)
  expect(wrapper.get('.rush-status').text()).toContain('监测中')
  expect(wrapper.get('button[aria-label="抢购设置"]').attributes('disabled')).toBeDefined()
  current = running({ settings: allApiSettings, counts: { '1000101': 2 } })
  await vi.advanceTimersByTimeAsync(1000)
  expect(wrapper.get('.rush-status').text()).toContain('已购买 2 / 3 只')
  expect(wrapper.get('.rush-status').text()).not.toContain('每种')
  expect(wrapper.get('.rush-status').text()).not.toContain('已完成')
  expect(wrapper.find('button[aria-label="取消抢购"]').exists()).toBe(true)
  current = running({ settings: allApiSettings, counts: { '1000101': 2, '1000201': 1 }, status: 'completed' })
  await vi.advanceTimersByTimeAsync(1000)
  expect(wrapper.get('.rush-status').text()).toContain('已购买 3 / 3 只')
  expect(wrapper.get('button[aria-label="开始抢购"]').attributes('disabled')).toBeUndefined()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
})

it('保存和恢复独立数量时保留各自目标，开始只提交一次', async () => {
  const custom = { ...settings, quantityMode: 'custom' as const, petQuantities: { '1000101': 2, '1000201': 4 } }
  const customApi = { ...apiSettings, quantity_per_pet: 1, quantity_mode: 'custom' as const,
    pet_quantities: { '1000101': 2, '1000201': 4 } }
  const { wrapper, fetcher } = setup(idle(), async path => json(path.endsWith('/start') ? running({ settings: customApi }) : idle()))
  await flushPromises()
  const dialog = wrapper.getComponent({ name: 'RushPurchaseSettingsDialog' })
  dialog.vm.$emit('save', custom)
  custom.petQuantities['1000101'] = 99
  await flushPromises()
  await wrapper.get('button[aria-label="开始抢购"]').trigger('click')
  await flushPromises()
  const calls = fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase/start')
  expect(calls).toHaveLength(1)
  expect(JSON.parse(calls[0]![1].body as string)).toMatchObject({
    quantity_mode: 'custom', pet_quantities: { '1000101': 2, '1000201': 4 }, quantity_per_pet: 1,
  })
  expect(dialog.props('settings')).toMatchObject({ quantityMode: 'custom', petQuantities: { '1000101': 2, '1000201': 4 } })
})
