import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import OrderView from '../views/OrderView.vue'
import { orderState } from './orderFixtures'

const wrappers: ReturnType<typeof mount>[] = []
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const workplace = (changes: Record<string, unknown> = {}) => ({ building_uid: '18446744073709551614', building_id: '100',
  name: '木材工坊', level: 2, status: 'idle', message: '当前可以安排生产', worker_uid: '9007199254740993', current_recipe_id: '0',
  candidate: { recipe_id: '200', item_id: '54002', name: '合成木头', stock: '9007199254740993', output_count: '1', recipe_times: '1' }, ...changes })
const inspectionState = (changes: Record<string, unknown> = {}) => orderState({ work_inspection: { ready: true, reason: null,
  buildings: [workplace(), workplace({ building_uid: 'missing', name: '空缺工坊', status: 'missing_worker', worker_uid: null,
    candidate: null, message: '尚未安排工作噜咪' }), workplace({ building_uid: 'unknown', name: '待同步工坊', status: 'unknown',
    worker_uid: null, current_recipe_id: null, candidate: null, message: '当前工位数据尚未同步' })] }, ...changes })
function setup(response: (path: string, init: RequestInit) => Promise<Response> = async () => json(inspectionState())) {
  const fetcher = vi.fn(response)
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(OrderView, { attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}
function tab(wrapper: ReturnType<typeof mount>, name: string) {
  const button = wrapper.findAll('[role="tab"]').find(item => item.text() === name)
  expect(button, `缺少页签 ${name}`).toBeDefined()
  return button!
}
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('默认订单任务，无任务时切换工作巡检只读且不启动轮询', async () => {
  vi.useFakeTimers()
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(tab(wrapper, '订单任务').attributes('aria-selected')).toBe('true')
  expect(fetcher).toHaveBeenCalledOnce()
  await tab(wrapper, '工作巡检').trigger('click')
  expect(wrapper.get('#work-inspection-panel').isVisible()).toBe(true)
  expect(wrapper.get('#order-task-panel').isVisible()).toBe(false)
  await tab(wrapper, '订单任务').trigger('click')
  await tab(wrapper, '工作巡检').trigger('click')
  expect(fetcher).toHaveBeenCalledOnce()
  await vi.advanceTimersByTimeAsync(2100)
  expect(fetcher).toHaveBeenCalledOnce()
  expect(fetcher.mock.calls.every(([path, init]) => path === '/api/home/orders/state' && (init?.method ?? 'GET') === 'GET')).toBe(true)
})

it('巡检展示精确库存、批次和工位状态，明确缺工标红且未知不误报缺工', async () => {
  const { wrapper } = setup()
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  const panel = wrapper.get('#work-inspection-panel')
  expect(panel.text()).toContain('合成木头')
  expect(panel.text()).toContain('9007199254740993')
  expect(panel.text()).toContain('默认 1 批')
  expect(panel.text()).toContain('持续类保持持续生产')
  expect(panel.text()).toContain('逐批确认')
  expect(panel.get('[data-building-uid="missing"] .workplace-status').text()).toBe('缺少工作噜咪')
  expect(panel.get('[data-building-uid="missing"] .workplace-status').classes()).toContain('missing-worker')
  expect(panel.get('[data-building-uid="unknown"]').text()).toContain('状态未同步')
  expect(panel.get('[data-building-uid="unknown"]').text()).not.toContain('缺少工作噜咪')
  expect(panel.get('[data-building-uid="unknown"] .workplace-status').classes()).not.toContain('missing-worker')
  expect(panel.find('input[name="parallel_count"]').exists()).toBe(false)
})

it('持续配方使用面向用户的说明，不显示接口约定数值', async () => {
  const { wrapper } = setup(async () => json(inspectionState({ work_inspection: { ready: true, reason: null,
    buildings: [workplace({ candidate: { recipe_id: '201', item_id: '54002', name: '持续产物', stock: '0',
      output_count: '1', recipe_times: '0' } })] } })))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  const panel = wrapper.get('#work-inspection-panel')
  expect(panel.get('.candidate').text()).toContain('持续类保持持续生产')
  expect(panel.get('.candidate').text()).toContain('当前库存 0')
  expect(panel.text()).not.toContain('持续生产使用 0')
  expect(panel.text()).not.toContain('持续生产（0）')
})

it('巡检卡片显示工作噜咪名称头像和当前配方产物，保留精确实例编号', async () => {
  const { wrapper, fetcher } = setup(async () => json(inspectionState({ work_inspection: { ready: true, reason: null,
    buildings: [workplace({ building_uid: '69', name: '紫晶矿', status: 'blocked', candidate: null,
      worker_uid: '9007199254740993', worker_name: '合成矿工噜咪', worker_image_url: '/synthetic-worker.png',
      current_recipe_id: '24', current_recipe_products: [{ item_id: '20004', name: '紫晶砂', count: '1' },
        { item_id: '20005', name: '合成副产物', count: '9007199254740993' }] })] } })))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  const card = wrapper.get('[data-building-uid="69"]')
  expect(card.text()).toContain('合成矿工噜咪')
  expect(card.text()).toContain('实例 9007199254740993')
  expect(card.get('img').attributes('src')).toBe('/synthetic-worker.png')
  expect(card.text()).toContain('紫晶砂 × 1')
  expect(card.text()).toContain('合成副产物 × 9007199254740993')
  expect(card.text()).not.toContain('当前配方 24')
  expect(fetcher).toHaveBeenCalledOnce()
})

it('名称缺失时明确未同步，不把实例号当作噜咪名称或空配方', async () => {
  const { wrapper } = setup(async () => json(inspectionState({ work_inspection: { ready: true, reason: null,
    buildings: [workplace({ building_uid: '69', worker_uid: '6566', current_recipe_id: '24', candidate: null }),
      workplace({ building_uid: 'missing', status: 'missing_worker', worker_uid: '0', current_recipe_id: '0', candidate: null })] } })))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  const card = wrapper.get('[data-building-uid="69"]')
  expect(card.text()).toContain('噜咪名称未同步')
  expect(card.text()).toContain('实例 6566')
  expect(card.text()).toContain('配方 #24（名称未同步）')
  expect(card.find('img').exists()).toBe(false)
  const missing = wrapper.get('[data-building-uid="missing"]')
  expect(missing.text()).toContain('工作噜咪 未配置')
  expect(missing.text()).toContain('当前配方 未设置')
  expect(missing.text()).not.toContain('实例 0')
})

it.each(['0', '2147483647'])('不可安排建筑的空位标记%s也显示未配置，不显示头像和实例', async workerUid => {
  const { wrapper } = setup(async () => json(inspectionState({ work_inspection: { ready: true, reason: null,
    buildings: [workplace({ building_uid: '69', status: 'blocked', worker_uid: workerUid,
      worker_name: '旧名称', worker_image_url: '/synthetic-worker.png', candidate: null })] } })))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  const card = wrapper.get('[data-building-uid="69"]')
  expect(card.text()).toContain('工作噜咪 未配置')
  expect(card.text()).not.toContain(`实例 ${workerUid}`)
  expect(card.text()).not.toContain('旧名称')
  expect(card.find('img').exists()).toBe(false)
})

it('旧状态没有巡检字段时明确未同步，不能开始', async () => {
  const { wrapper, fetcher } = setup(async () => json(orderState()))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeDefined()
  expect(wrapper.get('#work-inspection-panel').text()).toContain('工作巡检数据尚未同步')
  expect(fetcher).toHaveBeenCalledOnce()
})

it('巡检不依赖订单通知与普通订单执行门槛，订单仍禁止开始', async () => {
  const { wrapper } = setup(async () => json(inspectionState({ ready: false, execution_ready: false, message: '订单通知尚未同步' })))
  await flushPromises()
  expect(wrapper.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeDefined()
  await tab(wrapper, '工作巡检').trigger('click')
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeUndefined()
})

it.each(['inventory_ready', 'production_ready'])('%s未同步时后端巡检就绪也不放宽本地门槛', async field => {
  const { wrapper } = setup(async () => json(inspectionState({ [field]: false })))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeDefined()
})

it('页签支持方向键和首尾键，键盘切换同样不发请求', async () => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  await tab(wrapper, '订单任务').trigger('keydown', { key: 'ArrowRight' })
  expect(tab(wrapper, '工作巡检').attributes('aria-selected')).toBe('true')
  expect(document.activeElement).toBe(tab(wrapper, '工作巡检').element)
  await tab(wrapper, '工作巡检').trigger('keydown', { key: 'Home' })
  expect(tab(wrapper, '订单任务').attributes('aria-selected')).toBe('true')
  await tab(wrapper, '订单任务').trigger('keydown', { key: 'End' })
  expect(tab(wrapper, '工作巡检').attributes('aria-selected')).toBe('true')
  expect(fetcher).toHaveBeenCalledOnce()
})

it('显式开始巡检只提交一次，复用任务停止且不展示订单进度', async () => {
  let resolve!: (value: Response) => void
  const started = new Promise<Response>(done => { resolve = done })
  const task = { kind: 'inspection', status: 'running', running: true, completed_orders: 0, canned_received: '0', inspected_actions: 2, message: '正在巡检' }
  const { wrapper, fetcher } = setup(async path => path.endsWith('/inspection/start') ? started : json(inspectionState(path.endsWith('/task/stop')
    ? { task: { ...task, status: 'stopped', running: false, message: '已停止后续安排' } } : {})))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  const start = wrapper.get('button[aria-label="开始巡检"]')
  await start.trigger('click')
  await start.trigger('click')
  const requests = fetcher.mock.calls.filter(([path]) => path.endsWith('/inspection/start'))
  expect(requests).toHaveLength(1)
  expect(requests[0]![1].method).toBe('POST')
  expect(JSON.parse(requests[0]![1].body as string)).toEqual({ expected_revision: 'synthetic-revision-1', request_id: expect.any(String) })
  expect(JSON.parse(requests[0]![1].body as string).request_id).toMatch(/^[A-Za-z0-9_-]{16,64}$/)
  resolve(json(inspectionState({ task, execution_ready: false, work_inspection: { ready: false, reason: '工位状态更新中', buildings: [] } })))
  await flushPromises()
  expect(wrapper.get('#work-inspection-panel').text()).toContain('已确认安排 2 次')
  expect(wrapper.get('#work-inspection-panel').text()).not.toContain('完成 0 单')
  await wrapper.get('button[aria-label="停止巡检"]').trigger('click')
  await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/task/stop'))).toHaveLength(1)
  expect(wrapper.get('#work-inspection-panel').text()).toContain('已停止后续安排')
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/task/start'))).toBe(false)
})

it('订单运行时不能启动巡检', async () => {
  const { wrapper, fetcher } = setup(async () => json(inspectionState({ task: { kind: 'orders', status: 'running', running: true,
    completed_orders: 1, canned_received: '40', message: null } })))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeDefined()
  expect(wrapper.get('#work-inspection-panel').text()).toContain('订单任务正在运行')
  expect(fetcher).toHaveBeenCalledOnce()
})

it('巡检运行时订单页提示并禁止开始，不把巡检次数展示成订单完成数', async () => {
  const { wrapper, fetcher } = setup(async () => json(inspectionState({ task: { kind: 'inspection', status: 'running', running: true,
    completed_orders: 0, canned_received: '0', inspected_actions: 4, message: null } })))
  await flushPromises()
  const panel = wrapper.get('#order-task-panel')
  expect(panel.text()).toContain('工作巡检正在运行')
  expect(panel.text()).not.toContain('已确认完成 0 单')
  expect(panel.get('button[aria-label="开始订单任务"]').attributes('disabled')).toBeDefined()
  expect(fetcher).toHaveBeenCalledOnce()
})

it('巡检结果待核实须先勾选游戏核对，复用解除暂停且不自动开始', async () => {
  const task = { kind: 'inspection', status: 'uncertain', running: false, completed_orders: 0, canned_received: '0', inspected_actions: 1, message: '请核对生产结果' }
  const { wrapper, fetcher } = setup(async path => json(inspectionState(path.endsWith('/task/resolve') ? {}
    : { ready: false, execution_ready: false, task })))
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  const resolve = wrapper.get('button[aria-label="解除巡检任务暂停"]')
  expect(resolve.attributes('disabled')).toBeDefined()
  await wrapper.get('input[name="inspection_task_acknowledged"]').setValue(true)
  expect(resolve.attributes('disabled')).toBeUndefined()
  await resolve.trigger('click')
  await flushPromises()
  const request = fetcher.mock.calls.find(([path]) => path.endsWith('/task/resolve'))!
  expect(JSON.parse(request[1].body as string)).toEqual({ expected_revision: 'synthetic-revision-1', acknowledged: true })
  expect(wrapper.text()).toContain('已解除暂停，任务尚未启动')
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/inspection/start') || path.endsWith('/task/start'))).toBe(false)
})

it('订单设置未保存时巡检也不能开始，切换页签保留编辑', async () => {
  const { wrapper } = setup()
  await flushPromises()
  await wrapper.get('input[name="horizon_hours"]').setValue('48')
  await tab(wrapper, '工作巡检').trigger('click')
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeDefined()
  expect(wrapper.get('#work-inspection-panel').text()).toContain('请先保存')
  await tab(wrapper, '订单任务').trigger('click')
  expect((wrapper.get('input[name="horizon_hours"]').element as HTMLInputElement).value).toBe('48')
})

it('巡检开始网络结果未知时保持锁定且不自动轮询，手动刷新不自动重试', async () => {
  vi.useFakeTimers()
  const { wrapper, fetcher } = setup(async path => {
    if (path.endsWith('/inspection/start')) throw new Error('synthetic-private')
    return json(inspectionState())
  })
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  await wrapper.get('button[aria-label="开始巡检"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeDefined()
  await vi.advanceTimersByTimeAsync(1100)
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeDefined()
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/state'))).toHaveLength(1)
  await wrapper.get('button[aria-label="刷新订单状态"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeUndefined()
  expect(wrapper.text()).not.toContain('synthetic-private')
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/inspection/start'))).toHaveLength(1)
})

it.each(['stopped', 'completed', 'failed'])('巡检开始结果未知后不自动读取旧%s任务或重试开始', async status => {
  vi.useFakeTimers()
  const { wrapper, fetcher } = setup(async path => {
    if (path.endsWith('/inspection/start')) throw new Error('synthetic-private')
    return json(inspectionState({ task: { kind: 'inspection', status, running: false, inspected_actions: 1,
      completed_orders: 0, canned_received: '0', message: '上次巡检记录' } }))
  })
  await flushPromises()
  await tab(wrapper, '工作巡检').trigger('click')
  await wrapper.get('button[aria-label="开始巡检"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeDefined()
  await vi.advanceTimersByTimeAsync(1100)
  expect(wrapper.get('button[aria-label="开始巡检"]').attributes('disabled')).toBeDefined()
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/state'))).toHaveLength(1)
  expect(wrapper.get('#work-inspection-panel').text()).toContain('操作结果待核实')
  expect(fetcher.mock.calls.filter(([path]) => path.endsWith('/inspection/start'))).toHaveLength(1)
})
