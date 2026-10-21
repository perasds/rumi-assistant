import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { closeToast } from 'vant'
import App from '../App.vue'
import { orderPlan, orderState } from './orderFixtures'

const wrappers: ReturnType<typeof mount>[] = []
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const session = { status: 'connected', authenticated: true, role: null, roles: [], message: null }
function deferred() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>(done => { resolve = done })
  return { promise, resolve }
}
function setup(options: { authenticated?: boolean; response?: (path: string, init: RequestInit) => Promise<Response> } = {}) {
  const fetcher = vi.fn((path: string, init: RequestInit = {}) => {
    if (path === '/api/time') return Promise.resolve(json({ unix_ms: 1800000000000, time_zone: 'Asia/Shanghai' }))
    if (path === '/api/session') return Promise.resolve(json(options.authenticated === false ? { ...session, status: 'anonymous', authenticated: false } : session))
    if (path === '/api/auth/capabilities') return Promise.resolve(json({ integration_ready: true, methods: ['account'] }))
    if (path === '/api/pets') return Promise.resolve(json({ items: [], total: 0, named_count: 0, image_count: 0 }))
    if (path.startsWith('/api/home/orders/')) return options.response?.(path, init) ?? Promise.resolve(json(path.endsWith('/plan') ? orderPlan() : orderState()))
    throw new Error('Unexpected synthetic request')
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(App, { attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}
function menu(wrapper: ReturnType<typeof mount>, text: string) {
  const button = wrapper.get('nav').findAll('button').find(item => item.text() === text)
  expect(button, `缺少菜单 ${text}`).toBeDefined()
  return button!
}
async function open(wrapper: ReturnType<typeof mount>) {
  await flushPromises()
  await menu(wrapper, '订单').trigger('click')
  await flushPromises()
  return wrapper.get('main[aria-label="订单助手"]')
}
const calls = (fetcher: ReturnType<typeof setup>['fetcher']) => fetcher.mock.calls.filter(([path]) => path.startsWith('/api/home/orders/')).map(([path, init]) => [path, init ?? {}] as const)
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  closeToast(true)
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('第三项是订单，匿名点击只显示登录，图鉴仍可返回', async () => {
  const { wrapper, fetcher } = setup({ authenticated: false })
  await flushPromises()
  expect(wrapper.get('nav').findAll('button').map(button => button.text())).toEqual(['交易', '图鉴', '订单', '我的'])
  await menu(wrapper, '订单').trigger('click')
  await flushPromises()
  expect(wrapper.find('.login-panel').exists()).toBe(true)
  expect(wrapper.find('main[aria-label="订单助手"]').exists()).toBe(false)
  expect(calls(fetcher)).toHaveLength(0)
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
})

it('已登录也仅进入订单时GET，不自动保存、计划或开始，大整数完整显示', async () => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(calls(fetcher)).toHaveLength(0)
  const page = await open(wrapper)
  expect(calls(fetcher)).toHaveLength(1)
  expect(calls(fetcher)[0]?.[0]).toBe('/api/home/orders/state')
  expect(calls(fetcher)[0]?.[1].method ?? 'GET').toBe('GET')
  expect(page.text()).toContain('18446744073709551614')
  expect(page.text()).toContain('9007199254740993')
  expect(page.text()).toContain('9007199254740992')
  expect(page.text()).not.toContain('冒险')
})

it('保存设置只PUT，预览才POST计划，保留量大整数按字符串发送', async () => {
  const { wrapper, fetcher } = setup()
  const page = await open(wrapper)
  await page.get('input[name="horizon_hours"]').setValue('48')
  await page.get('input[name="max_orders"]').setValue('300')
  await page.get('button[aria-label="添加保留物资"]').trigger('click')
  await page.get('input[name="reserve_item_id"]').setValue('54002')
  await page.get('input[name="reserve_count"]').setValue('9007199254740993')
  await page.get('button[aria-label="保存订单设置"]').trigger('click')
  await flushPromises()
  const saved = calls(fetcher).find(([path]) => path.endsWith('/settings'))!
  expect(saved[1].method).toBe('PUT')
  expect(JSON.parse(saved[1].body as string)).toEqual({ horizon_hours: 48, max_orders: 300,
    reserves: { '54002': '9007199254740993' }, allow_production: false, allow_refresh: false })
  expect(calls(fetcher).some(([path]) => path.endsWith('/task/start') || path.endsWith('/plan'))).toBe(false)
  await page.get('button[aria-label="预览订单计划"]').trigger('click')
  await flushPromises()
  const preview = calls(fetcher).find(([path]) => path.endsWith('/plan'))!
  expect(JSON.parse(preview[1].body as string).expected_revision).toBe('synthetic-revision-1')
  expect(page.get('[aria-label="缺料清单"]').text()).toContain('合成木头')
  expect(page.get('[aria-label="生产计划"]').text()).toContain('建筑速度未同步')
  expect(page.get('[aria-label="生产计划"]').text()).not.toContain('0 秒')
  expect(page.text()).toContain('预计完成')
})

it.each([['horizon_hours', '0'], ['horizon_hours', '169'], ['max_orders', '1.5'], ['max_orders', '10001']])('无效%s=%s不能保存或预览', async (name, value) => {
  const { wrapper, fetcher } = setup()
  const page = await open(wrapper)
  await page.get(`input[name="${name}"]`).setValue(value)
  expect(page.get('button[aria-label="保存订单设置"]').attributes('disabled')).toBeDefined()
  expect(page.get('button[aria-label="预览订单计划"]').attributes('disabled')).toBeDefined()
  expect(page.get('[role="alert"]').text()).toContain('整数')
  expect(calls(fetcher)).toHaveLength(1)
})

it('保留物资拒绝重复ID、负数和指数形式，不丢失合法大整数', async () => {
  const { wrapper, fetcher } = setup()
  const page = await open(wrapper)
  await page.get('button[aria-label="添加保留物资"]').trigger('click')
  await page.get('input[name="reserve_item_id"]').setValue('300')
  await page.get('input[name="reserve_count"]').setValue('1e3')
  expect(page.get('button[aria-label="保存订单设置"]').attributes('disabled')).toBeDefined()
  await page.get('input[name="reserve_count"]').setValue('-1')
  expect(page.get('button[aria-label="保存订单设置"]').attributes('disabled')).toBeDefined()
  await page.get('input[name="reserve_count"]').setValue('9007199254740993')
  await page.get('button[aria-label="添加保留物资"]').trigger('click')
  await page.findAll('input[name="reserve_item_id"]')[1]!.setValue('300')
  await page.findAll('input[name="reserve_count"]')[1]!.setValue('2')
  expect(page.get('[role="alert"]').text()).toContain('重复')
  expect(calls(fetcher)).toHaveLength(1)
})

it('显式开始只发一次，运行中按钮变取消，取消不依赖执行能力', async () => {
  const pending = deferred()
  const { wrapper, fetcher } = setup({ response: async (path) => {
    if (path.endsWith('/task/start')) return pending.promise
    if (path.endsWith('/task/stop')) return json(orderState({ task: { status: 'stopped', running: false, completed_orders: 2, canned_received: '9007199254740993', message: '已停止后续操作' } }))
    return json(orderState())
  } })
  const page = await open(wrapper)
  const start = page.get('button[aria-label="开始订单任务"]')
  await start.trigger('click')
  await start.trigger('click')
  expect(calls(fetcher).filter(([path]) => path.endsWith('/task/start'))).toHaveLength(1)
  const body = JSON.parse(calls(fetcher).find(([path]) => path.endsWith('/task/start'))![1].body as string)
  expect(body.expected_revision).toBe('synthetic-revision-1')
  expect(body.request_id).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
  pending.resolve(json(orderState({ execution_ready: false, task: { status: 'running', running: true, completed_orders: 0, canned_received: '0', message: null } })))
  await flushPromises()
  await page.get('button[aria-label="取消订单任务"]').trigger('click')
  await flushPromises()
  expect(calls(fetcher).filter(([path]) => path.endsWith('/task/stop'))).toHaveLength(1)
  expect(page.text()).toContain('已停止')
  expect(page.text()).toContain('9007199254740993')
})

it.each(['unavailable', 'uncertain'])('%s状态明确说明并禁止开始', async mode => {
  const { wrapper, fetcher } = setup({ response: async () => json(orderState(mode === 'unavailable'
    ? { execution_ready: false, execution_reason: '当前连接未开放订单执行' }
    : { task: { status: 'uncertain', running: false, completed_orders: 0, canned_received: '0', message: '等待核对游戏结果' } })) })
  const page = await open(wrapper)
  expect(page.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeDefined()
  expect(page.text()).toContain(mode === 'unavailable' ? '当前连接未开放订单执行' : '等待核对游戏结果')
  expect(calls(fetcher)).toHaveLength(1)
})

it('离开页面abort全部等待请求，不向后端自动停止任务，迟到401不能使新会话过期', async () => {
  const pending = deferred()
  const { wrapper, fetcher } = setup({ response: async path => path.endsWith('/plan') ? pending.promise : json(orderState()) })
  const page = await open(wrapper)
  const view = wrapper.getComponent({ name: 'OrderView' })
  await page.get('button[aria-label="预览订单计划"]').trigger('click')
  const signal = calls(fetcher).at(-1)![1].signal!
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(signal.aborted).toBe(true)
  expect(wrapper.find('main[aria-label="订单助手"]').exists()).toBe(false)
  pending.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '过期' } }, 401))
  await flushPromises()
  expect(view.emitted('expired')).toBeUndefined()
  expect(wrapper.find('header button').exists()).toBe(false)
  expect(calls(fetcher).some(([path]) => path.endsWith('/task/stop'))).toBe(false)
})

it('当前页面401清除订单与计划并通知上层，返回订单需要登录', async () => {
  let reads = 0
  const { wrapper } = setup({ response: async path => path.endsWith('/state') && ++reads > 1
    ? json({ error: { code: 'AUTH_REQUIRED', message: '过期' } }, 401) : json(orderState()) })
  const page = await open(wrapper)
  await page.get('button[aria-label="刷新订单状态"]').trigger('click')
  await flushPromises()
  expect(wrapper.find('main[aria-label="订单助手"]').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('18446744073709551614')
  expect(wrapper.find('.login-panel').exists()).toBe(true)
})

it('运行任务仅轮询HTTP状态，隐藏和离开停止轮询；网络异常不暴露原文', async () => {
  vi.useFakeTimers()
  const { wrapper, fetcher } = setup({ response: async () => json(orderState({ task: { status: 'running', running: true, completed_orders: 0, canned_received: '0', message: null } })) })
  await open(wrapper)
  await vi.advanceTimersByTimeAsync(2100)
  expect(calls(fetcher).length).toBeGreaterThanOrEqual(3)
  expect(calls(fetcher).every(([path, init]) => path.endsWith('/state') && (init.method ?? 'GET') === 'GET')).toBe(true)
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  document.dispatchEvent(new Event('visibilitychange'))
  const count = calls(fetcher).length
  await vi.advanceTimersByTimeAsync(3000)
  expect(calls(fetcher)).toHaveLength(count)
  await menu(wrapper, '图鉴').trigger('click')
  await vi.advanceTimersByTimeAsync(3000)
  expect(calls(fetcher)).toHaveLength(count)
})

it('后台轮询不切换刷新按钮的文字、禁用状态或页面忙碌提示', async () => {
  vi.useFakeTimers()
  const pending = deferred()
  let reads = 0
  const current = orderState({ task: { status: 'running', running: true, completed_orders: 0, canned_received: '0', message: null } })
  const { wrapper, fetcher } = setup({ response: async () => ++reads === 2 ? pending.promise : json(current) })
  const page = await open(wrapper)
  const refresh = page.get('button[aria-label="刷新订单状态"]')
  await vi.advanceTimersByTimeAsync(1000)
  expect(calls(fetcher)).toHaveLength(2)
  expect(refresh.text()).toBe('刷新状态')
  expect(refresh.attributes('disabled')).toBeUndefined()
  expect(page.attributes('aria-busy')).toBe('false')
  pending.resolve(json({ ...current, task: { ...current.task, completed_orders: 1 } }))
  await flushPromises()
  expect(page.text()).toContain('已确认完成 1 单')
  expect(refresh.text()).toBe('刷新状态')
  expect(refresh.attributes('disabled')).toBeUndefined()
})

it('手动刷新保留进度提示，完成后恢复按钮', async () => {
  const pending = deferred()
  let reads = 0
  const { wrapper } = setup({ response: async () => ++reads === 2 ? pending.promise : json(orderState()) })
  const page = await open(wrapper)
  const refresh = page.get('button[aria-label="刷新订单状态"]')
  await refresh.trigger('click')
  expect(refresh.text()).toBe('同步中…')
  expect(refresh.attributes('disabled')).toBeDefined()
  expect(page.attributes('aria-busy')).toBe('true')
  pending.resolve(json(orderState()))
  await flushPromises()
  expect(refresh.text()).toBe('刷新状态')
  expect(refresh.attributes('disabled')).toBeUndefined()
  expect(page.attributes('aria-busy')).toBe('false')
})

it('后台读取期间点击刷新复用当前请求并显示进度，不重复请求', async () => {
  vi.useFakeTimers()
  const pending = deferred()
  let reads = 0
  const current = orderState({ task: { status: 'running', running: true, completed_orders: 0, canned_received: '0', message: null } })
  const { wrapper, fetcher } = setup({ response: async () => ++reads === 2 ? pending.promise : json(current) })
  const page = await open(wrapper)
  await vi.advanceTimersByTimeAsync(1000)
  const refresh = page.get('button[aria-label="刷新订单状态"]')
  expect(refresh.attributes('disabled')).toBeUndefined()
  await refresh.trigger('click')
  expect(refresh.text()).toBe('同步中…')
  expect(refresh.attributes('disabled')).toBeDefined()
  expect(calls(fetcher)).toHaveLength(2)
  pending.resolve(json(current))
  await flushPromises()
  expect(refresh.text()).toBe('刷新状态')
  expect(refresh.attributes('disabled')).toBeUndefined()
  await vi.advanceTimersByTimeAsync(1000)
  expect(calls(fetcher)).toHaveLength(3)
})

it('失败信息只显示安全文本，HTML名称按文本展示', async () => {
  let reads = 0
  const { wrapper } = setup({ response: async () => {
    if (++reads > 1) throw new Error('synthetic-private-secret')
    const state = orderState()
    state.orders[0]!.materials[0]!.name = '<img src=x onerror=alert(1)>'
    return json(state)
  } })
  const page = await open(wrapper)
  expect(page.text()).toContain('<img src=x onerror=alert(1)>')
  expect(page.find('img[src="x"]').exists()).toBe(false)
  await page.get('button[aria-label="刷新订单状态"]').trigger('click')
  await flushPromises()
  expect(page.text()).not.toContain('synthetic-private-secret')
  expect(page.get('[role="alert"]').text()).toContain('无法连接服务')
})

it.each(['inventory_ready', 'production_ready'])('%s未齐即使执行能力开放也禁止开始', async field => {
  const { wrapper } = setup({ response: async () => json(orderState({ [field]: false })) })
  const page = await open(wrapper)
  expect(page.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeDefined()
})

it('后端设置字段顺序不影响已保存判断，修改后未保存禁止开始', async () => {
  const { wrapper } = setup({ response: async () => json(orderState({ settings: {
    allow_refresh: false, allow_production: false, reserves: {}, max_orders: 100, horizon_hours: 24,
  } })) })
  const page = await open(wrapper)
  expect(page.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeUndefined()
  await page.get('input[name="max_orders"]').setValue('99')
  expect(page.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeDefined()
  expect(page.text()).toContain('请先保存')
})

it('空闲手动刷新保留编辑中的草稿，版本变化清除旧预览', async () => {
  let revision = 'synthetic-revision-1'
  const { wrapper } = setup({ response: async path => json(path.endsWith('/plan') ? orderPlan() : orderState({ revision })) })
  const page = await open(wrapper)
  await page.get('button[aria-label="预览订单计划"]').trigger('click')
  await flushPromises()
  expect(page.find('[aria-label="订单计划摘要"]').exists()).toBe(true)
  revision = 'synthetic-revision-2'
  await page.get('button[aria-label="刷新订单状态"]').trigger('click')
  await flushPromises()
  expect(page.find('[aria-label="订单计划摘要"]').exists()).toBe(false)
  await page.get('input[name="max_orders"]').setValue('88')
  await page.get('button[aria-label="刷新订单状态"]').trigger('click')
  await flushPromises()
  expect((page.get('input[name="max_orders"]').element as HTMLInputElement).value).toBe('88')
})

it('开始结果未知时不能再次开始，不泄露服务端异常字段', async () => {
  vi.useFakeTimers()
  const { wrapper, fetcher } = setup({ response: async path => path.endsWith('/task/start')
    ? json({ error: { code: 'INTERNAL_ERROR', message: 'synthetic-private-backend-secret' } }, 500) : json(orderState()) })
  const page = await open(wrapper)
  await page.get('button[aria-label="开始订单任务"]').trigger('click')
  await flushPromises()
  expect(page.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeDefined()
  expect(page.text()).toContain('结果待核实')
  expect(page.text()).not.toContain('synthetic-private-backend-secret')
  expect(calls(fetcher).filter(([path]) => path.endsWith('/task/start'))).toHaveLength(1)
  await vi.advanceTimersByTimeAsync(1000)
  expect(page.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeDefined()
})

it('状态轮询等待时仍可取消任务，中止读取后只发送一次取消', async () => {
  vi.useFakeTimers()
  const pending = deferred()
  let reads = 0
  const { wrapper, fetcher } = setup({ response: async path => {
    if (path.endsWith('/task/stop')) return json(orderState({ task: { status: 'stopped', running: false, completed_orders: 0, canned_received: '0', message: null } }))
    if (++reads > 1) return pending.promise
    return json(orderState({ task: { status: 'running', running: true, completed_orders: 0, canned_received: '0', message: null } }))
  } })
  const page = await open(wrapper)
  await vi.advanceTimersByTimeAsync(1000)
  const read = calls(fetcher).at(-1)![1].signal!
  await page.get('button[aria-label="刷新订单状态"]').trigger('click')
  expect(page.get('button[aria-label="刷新订单状态"]').text()).toBe('同步中…')
  expect(page.get('button[aria-label="取消订单任务"]').attributes('disabled')).toBeUndefined()
  await page.get('button[aria-label="取消订单任务"]').trigger('click')
  await flushPromises()
  expect(read.aborted).toBe(true)
  expect(page.get('button[aria-label="刷新订单状态"]').text()).toBe('刷新状态')
  expect(calls(fetcher).filter(([path]) => path.endsWith('/task/stop'))).toHaveLength(1)
  pending.resolve(json(orderState({ task: { status: 'running', running: true, completed_orders: 0, canned_received: '0', message: null } })))
  await flushPromises()
  expect(page.text()).toContain('已停止')
})

it('未预览也展示账号建筑工况，生产时间来源分别说明', async () => {
  const building = { building_uid: '18446744073709551613', building_id: '100', level: 2, current_recipe_id: '200',
    assigned_lumi_uids: ['9007199254740993'], production_count: 5, completed_count: 2, quantity_mode: 'batch',
    editable: false, reason: null, production_editable: false, production_reason: '已有生产中', allowed_recipe_ids: ['200'], assignment_limit: 1 }
  const production = orderPlan().production[0]!
  const { wrapper } = setup({ response: async path => json(path.endsWith('/plan') ? orderPlan({ production: [
    { ...production, time_source: 'calculated', duration_seconds: 72, blocked_reason: null },
    { ...production, time_source: 'server', ready_at_server_seconds: 1800000300, blocked_reason: null },
  ] }) : orderState({ buildings: [building] })) })
  const page = await open(wrapper)
  expect(page.get('[aria-label="账号建筑"]').text()).toContain('18446744073709551613')
  expect(page.get('[aria-label="账号建筑"]').text()).toContain('9007199254740993')
  expect(page.get('[aria-label="账号建筑"]').text()).toContain('已有生产中')
  await page.get('button[aria-label="预览订单计划"]').trigger('click')
  await flushPromises()
  expect(page.get('[aria-label="生产计划"]').text()).toContain('按当前速度推算 72 秒')
  expect(page.get('[aria-label="生产计划"]').text()).toContain('服务器完成时刻 1800000300')
})

it.each([['2147483648', '1'], ['54002', '9223372036854775808']])('保留物资%s和数量%s超过后端范围时禁止提交', async (id, count) => {
  const { wrapper, fetcher } = setup()
  const page = await open(wrapper)
  await page.get('button[aria-label="添加保留物资"]').trigger('click')
  await page.get('input[name="reserve_item_id"]').setValue(id)
  await page.get('input[name="reserve_count"]').setValue(count)
  expect(page.get('button[aria-label="保存订单设置"]').attributes('disabled')).toBeDefined()
  expect(page.get('[role="alert"]').text()).toContain('不能超过')
  expect(calls(fetcher)).toHaveLength(1)
})

it('保留物资最多256项，合法整数边界可原样保存', async () => {
  const reserves = Object.fromEntries(Array.from({ length: 255 }, (_, index) => [String(index + 1), '0']))
  reserves['2147483647'] = '9223372036854775807'
  const { wrapper, fetcher } = setup({ response: async () => json(orderState({ settings: {
    horizon_hours: 24, max_orders: 100, reserves, allow_production: false, allow_refresh: false,
  } })) })
  const page = await open(wrapper)
  expect(page.get('button[aria-label="添加保留物资"]').attributes('disabled')).toBeDefined()
  expect(page.get('button[aria-label="保存订单设置"]').attributes('disabled')).toBeUndefined()
  await page.get('button[aria-label="保存订单设置"]').trigger('click')
  await flushPromises()
  const body = JSON.parse(calls(fetcher).find(([path]) => path.endsWith('/settings'))![1].body as string)
  expect(body.reserves['2147483647']).toBe('9223372036854775807')
})

it('换单暂未开放，开关禁用；旧开启设置可保存为关闭后再开始', async () => {
  const { wrapper, fetcher } = setup({ response: async () => json(orderState({ settings: {
    horizon_hours: 24, max_orders: 100, reserves: {}, allow_production: false, allow_refresh: true,
  } })) })
  const page = await open(wrapper)
  const refresh = page.get('input[name="allow_refresh"]')
  expect(refresh.attributes('disabled')).toBeDefined()
  expect((refresh.element as HTMLInputElement).checked).toBe(false)
  expect(page.text()).toContain('自动换单暂未开放')
  expect(page.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeDefined()
  await page.get('button[aria-label="保存订单设置"]').trigger('click')
  await flushPromises()
  expect(JSON.parse(calls(fetcher).find(([path]) => path.endsWith('/settings'))![1].body as string).allow_refresh).toBe(false)
})

it('真实计划DTO允许缺料名和库存空值、无建筑的未知生产，使用中文原因和物品ID兜底', async () => {
  const { wrapper } = setup({ response: async path => json(path.endsWith('/plan') ? orderPlan({
    inventory_selection: { slot_ids: [], count: '0', canned_count: '0', optimal: false, reason: 'inventory_unknown' },
    materials: [{ item_id: '54002', name: null, required: '9007199254740993', available: null, shortage: null }],
    production: [{ building_uid: null, building_id: null, recipe_id: null, item_id: '54002', name: '合成物资', count: '0', time_source: 'unknown',
      duration_seconds: null, ready_at_server_seconds: null, blocked_reason: 'production_unavailable' }],
    predicted_count: '0', warnings: ['inventory_unknown'],
  }) : orderState()) })
  const page = await open(wrapper)
  await page.get('button[aria-label="预览订单计划"]').trigger('click')
  await flushPromises()
  expect(page.get('[aria-label="缺料清单"]').text()).toContain('物品 54002')
  expect(page.get('[aria-label="缺料清单"]').text()).toContain('可用 未同步')
  expect(page.get('[aria-label="缺料清单"]').text()).toContain('缺少 待计算')
  expect(page.get('[aria-label="生产计划"]').text()).toContain('暂无可用生产条件')
  expect(page.get('[aria-label="生产计划"]').text()).toContain('建筑 未找到')
  expect(page.get('[aria-label="订单计划摘要"]').text()).toContain('库存未同步完整')
  expect(page.text()).not.toContain('production_unavailable')
})

it('上游需求不可计算时不把依赖异常误报成库存未同步', async () => {
  const { wrapper } = setup({ response: async path => json(path.endsWith('/plan') ? orderPlan({
    base_requirements: null, warnings: ['material_cycle'],
  }) : orderState()) })
  const page = await open(wrapper)
  await page.get('button[aria-label="预览订单计划"]').trigger('click')
  await flushPromises()
  expect(page.get('[aria-label="缺料清单"]').text()).toContain('上游需求暂不可计算')
  expect(page.get('[aria-label="缺料清单"]').text()).not.toContain('库存尚未同步完整')
})

it('默认跳过的生产在预览中说明原因，不显示为数据未核实', async () => {
  const blocked = { ...orderPlan().production[0]!, count: '0', time_source: 'unknown',
    duration_seconds: null, blocked_reason: 'automatic_production_disabled' }
  const { wrapper } = setup({ response: async path => json(path.endsWith('/plan')
    ? orderPlan({ production: [blocked] }) : orderState()) })
  const page = await open(wrapper)
  await page.get('button[aria-label="预览订单计划"]').trigger('click')
  await flushPromises()
  const production = page.get('[aria-label="生产计划"]')
  expect(production.text()).toContain('默认不安排自动生产')
  expect(production.text()).not.toContain('automatic_production_disabled')
  expect(production.text()).not.toContain('该条件尚未核实')
})

it('明确的任务409拒绝展示安全原因，不伪装成已发送结果未知', async () => {
  const { wrapper } = setup({ response: async path => path.endsWith('/task/start')
    ? json({ error: { code: 'ORDER_ROLE_BUSY', message: 'synthetic-untrusted-error' } }, 409) : json(orderState()) })
  const page = await open(wrapper)
  await page.get('button[aria-label="开始订单任务"]').trigger('click')
  await flushPromises()
  expect(page.get('[role="alert"]').text()).toContain('该角色已有订单任务')
  expect(page.text()).not.toContain('synthetic-untrusted-error')
  expect(page.text()).not.toContain('操作结果待核实，请先刷新状态')
})

it('待核实任务只能勾选游戏核对后显式解除暂停，等待期间不自动读取', async () => {
  vi.useFakeTimers()
  const uncertain = orderState({ task: { status: 'uncertain', running: false, completed_orders: 1, canned_received: '40', message: '请在游戏核对' } })
  const { wrapper, fetcher } = setup({ response: async path => json(path.endsWith('/task/resolve') ? orderState() : uncertain) })
  const page = await open(wrapper)
  expect(calls(fetcher).some(([path]) => path.endsWith('/task/resolve'))).toBe(false)
  const resolve = page.get('button[aria-label="解除订单任务暂停"]')
  expect(resolve.attributes('disabled')).toBeDefined()
  expect(page.get('button[aria-label="保存订单设置"]').attributes('disabled')).toBeDefined()
  await page.get('input[name="order_task_acknowledged"]').setValue(true)
  await vi.advanceTimersByTimeAsync(1000)
  expect(calls(fetcher).filter(([path]) => path.endsWith('/state'))).toHaveLength(1)
  expect((page.get('input[name="order_task_acknowledged"]').element as HTMLInputElement).checked).toBe(true)
  await resolve.trigger('click')
  await flushPromises()
  const operation = calls(fetcher).find(([path]) => path.endsWith('/task/resolve'))!
  expect(operation[1].method).toBe('POST')
  expect(JSON.parse(operation[1].body as string)).toEqual({ expected_revision: 'synthetic-revision-1', acknowledged: true })
  expect(page.find('input[name="order_task_acknowledged"]').exists()).toBe(false)
  expect(page.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeUndefined()
  expect(calls(fetcher).some(([path]) => path.endsWith('/task/start'))).toBe(false)
})

it.each([['running', true], ['uncertain', true], ['idle', false]])('任务%s/running=%s不展示解除暂停核对入口', async (status, running) => {
  const { wrapper } = setup({ response: async () => json(orderState({ task: { status, running, completed_orders: 0, canned_received: '0', message: null } })) })
  const page = await open(wrapper)
  expect(page.find('input[name="order_task_acknowledged"]').exists()).toBe(false)
})

it('解除暂停请求等待中禁止重复发送，离开abort，迟到401不影响当前会话', async () => {
  const pending = deferred()
  const { wrapper, fetcher } = setup({ response: async path => path.endsWith('/task/resolve') ? pending.promise
    : json(orderState({ task: { status: 'uncertain', running: false, completed_orders: 0, canned_received: '0', message: null } })) })
  const page = await open(wrapper)
  await page.get('input[name="order_task_acknowledged"]').setValue(true)
  const resolve = page.get('button[aria-label="解除订单任务暂停"]')
  await resolve.trigger('click')
  await resolve.trigger('click')
  expect(calls(fetcher).filter(([path]) => path.endsWith('/task/resolve'))).toHaveLength(1)
  const signal = calls(fetcher).at(-1)![1].signal!
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(signal.aborted).toBe(true)
  pending.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '过期' } }, 401))
  await flushPromises()
  expect(wrapper.find('header button').exists()).toBe(false)
})
