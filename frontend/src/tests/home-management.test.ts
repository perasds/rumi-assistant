import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { closeToast } from 'vant'
import HomeView from '../views/HomeView.vue'
import type { HomeAccountState } from '../api/types'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  closeToast(true)
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
const worker = '18446744073709551001'
const freeWorker = '18446744073709551002'
const occupiedWorker = '18446744073709551003'
const incompatibleWorker = '18446744073709551004'
const catalog = { scope: 'configuration', total: 1, recipe_count: 2, buildings: [
  { building_id: '101', name: '合成制卡台', island_id: 1, island_name: '测试岛', work_type: '手工', recipes: [
    { recipe_id: '11', min_building_level: 1, materials: [], products: [{ item_id: '201', name: '测试卡甲', count: 2, image_url: null }] },
    { recipe_id: '12', min_building_level: 2, materials: [], products: [{ item_id: '202', name: '测试卡乙', count: 1, image_url: null }] },
  ] },
] }
function state(overrides: Record<string, unknown> = {}): HomeAccountState {
  return { scope: 'account', ready: true, revision: 'revision-1', observed_at: '2026-10-15T01:00:00Z', message: null,
    buildings: [
      { building_uid: '71', building_id: '101', level: 3, current_recipe_id: '11', assigned_lumi_uids: [worker],
        production_count: 5, completed_count: 2, quantity_mode: 'batch', editable: true, reason: null,
        production_editable: true, production_reason: null,
        allowed_recipe_ids: ['11', '12'], assignment_limit: 1 },
      { building_uid: '72', building_id: '101', level: 1, current_recipe_id: '11', assigned_lumi_uids: [occupiedWorker],
        production_count: 1, completed_count: 0, quantity_mode: 'batch', editable: false, reason: '合成只读建筑',
        production_editable: false, production_reason: null,
        allowed_recipe_ids: ['11'], assignment_limit: 1 },
    ],
    roster: [
      { lumi_uid: worker, lumi_id: '10001', name: '合成蜜桃', image_url: null, work_aptitude: 80, work_energy: 200,
        assigned_building_uid: '71', eligible_building_uids: ['71'], selectable: false, reason: '当前工人' },
      { lumi_uid: freeWorker, lumi_id: '10001', name: '合成蜜桃', image_url: null, work_aptitude: 90, work_energy: 300,
        assigned_building_uid: null, eligible_building_uids: ['71'], selectable: true, reason: null },
      { lumi_uid: occupiedWorker, lumi_id: '10002', name: '合成灰灰', image_url: null, work_aptitude: 70, work_energy: 100,
        assigned_building_uid: '72', eligible_building_uids: ['71', '72'], selectable: false, reason: '已在其他建筑工作' },
      { lumi_uid: incompatibleWorker, lumi_id: '10003', name: '合成不匹配', image_url: null, work_aptitude: 70, work_energy: 100,
        assigned_building_uid: null, eligible_building_uids: ['72'], selectable: true, reason: null },
    ], last_apply: null, ...overrides }
}
function deferred() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>(done => { resolve = done })
  return { promise, resolve }
}
type Change = { building_uid: string; recipe_id?: string; lumi_uids?: string[]; production_count?: number }
type Apply = { request_id: string; expected_revision: string; changes: Change[] }
function applied(body: Apply, status = 'applied') {
  const result = state({ revision: 'revision-2', last_apply: { request_id: body.request_id, status,
    message: status === 'applied' ? '合成已确认应用' : '合成部分应用',
    results: body.changes.map(change => ({ building_uid: change.building_uid, status, message: '合成建筑结果' })) } })
  const change = body.changes[0]!
  result.buildings[0] = { ...result.buildings[0]!,
    current_recipe_id: status === 'applied' ? change.recipe_id ?? '11' : '11',
    assigned_lumi_uids: change.lumi_uids ?? [worker], production_count: change.production_count ?? 5 }
  return result
}
function setup(options: {
  read?: (init: RequestInit) => Promise<Response>
  write?: (body: Apply, init: RequestInit) => Promise<Response>
} = {}) {
  const fetcher = vi.fn((path: string, init: RequestInit = {}) => {
    if (path === '/api/home/production') return Promise.resolve(json(catalog))
    if (path === '/api/home/state') return options.read?.(init) ?? Promise.resolve(json(state()))
    if (path === '/api/home/settings') {
      const body = JSON.parse(init.body as string) as Apply
      return options.write?.(body, init) ?? Promise.resolve(json(applied(body)))
    }
    throw new Error(`Unexpected synthetic request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(HomeView, { props: { active: false }, attachTo: document.body, global: { stubs: { teleport: true } } })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}
async function openHome(wrapper: ReturnType<typeof mount>) {
  await flushPromises()
  await wrapper.setProps({ active: true })
  await flushPromises()
  expect(wrapper.find('.home-management').exists()).toBe(true)
  return wrapper.get('.home-management')
}
const writes = (fetcher: ReturnType<typeof setup>['fetcher']) => fetcher.mock.calls.filter(([path]) => path === '/api/home/settings')
async function chooseRecipe(wrapper: ReturnType<typeof mount>, count?: string) {
  await wrapper.get('[data-building-uid="71"] select').setValue('12')
  if (count !== undefined) await wrapper.get('[data-building-uid="71"] input[type="number"]').setValue(count)
}

it('管理默认展示账号建筑实例、当前产品和工作噜咪，目录仍独立可用', async () => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(fetcher.mock.calls.some(([path]) => path === '/api/home/state')).toBe(false)
  const home = await openHome(wrapper)
  expect(home.findAll('.home-instance-card')).toHaveLength(2)
  const card = home.get('[data-building-uid="71"]')
  expect(card.text()).toContain('合成制卡台')
  expect(card.text()).toContain('测试卡甲')
  expect(card.text()).toContain('合成蜜桃')
  expect(card.text()).toContain(worker)
  expect(card.text()).toContain('计划次数 5')
  expect(card.text()).toContain('已完成 2')
  expect(writes(fetcher)).toHaveLength(0)
  await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '配方目录')!.trigger('click')
  await flushPromises()
  expect(wrapper.get('.home-building-card').text()).toContain('测试卡乙')
})

it('选择产品只改草稿，新批次次数不默认1，明确应用才发一次且省略未改字段', async () => {
  const { wrapper, fetcher } = setup()
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper)
  const count = home.get('[data-building-uid="71"] input[type="number"]')
  expect((count.element as HTMLInputElement).value).toBe('')
  expect(home.get('button[aria-label="应用到游戏"]').attributes('disabled')).toBeDefined()
  expect(writes(fetcher)).toHaveLength(0)
  await count.setValue('3')
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(writes(fetcher)).toHaveLength(1)
  const [path, init] = writes(fetcher)[0]!
  const body = JSON.parse(init!.body as string)
  expect(path).toBe('/api/home/settings')
  expect(init?.method).toBe('PUT')
  expect(body.request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/)
  expect(body.expected_revision).toBe('revision-1')
  expect(body.changes).toEqual([{ building_uid: '71', recipe_id: '12', production_count: 3 }])
  expect(home.text()).toContain('合成已确认应用')
  expect(home.get('[data-building-uid="71"] .current-production').text()).toContain('测试卡乙')
})

it('连续生产不显示次数，不向接口猜填0或1', async () => {
  const source = state(); source.buildings[0] = { ...source.buildings[0]!, quantity_mode: 'continuous', production_count: null }
  const { wrapper, fetcher } = setup({ read: () => Promise.resolve(json(source)) })
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper)
  expect(home.get('[data-building-uid="71"]').find('input[type="number"]').exists()).toBe(false)
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(JSON.parse(writes(fetcher)[0]![1]!.body as string).changes).toEqual([{ building_uid: '71', recipe_id: '12' }])
})

it('选择工人区分同物种实例，只允许当前工人或匹配空闲噜咪，确认弹窗不写游戏', async () => {
  const { wrapper, fetcher } = setup()
  const home = await openHome(wrapper)
  await home.get('[data-building-uid="71"] button[aria-label="选择工作噜咪"]').trigger('click')
  const picker = wrapper.get('.home-worker-picker')
  expect(picker.get(`[data-lumi-uid="${occupiedWorker}"]`).attributes('disabled')).toBeDefined()
  expect(picker.get(`[data-lumi-uid="${incompatibleWorker}"]`).attributes('disabled')).toBeDefined()
  await picker.get(`[data-lumi-uid="${freeWorker}"]`).trigger('click')
  await picker.get('button[aria-label="保存工人选择"]').trigger('click')
  expect(writes(fetcher)).toHaveLength(0)
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(JSON.parse(writes(fetcher)[0]![1]!.body as string).changes).toEqual([{ building_uid: '71', lumi_uids: [freeWorker] }])
})

it('外部revision变化保留草稿并提示冲突，不能拿旧草稿直接应用', async () => {
  let reads = 0
  const { wrapper, fetcher } = setup({ read: () => Promise.resolve(json(state({ revision: ++reads === 1 ? 'revision-1' : 'revision-external' }))) })
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, '4')
  await home.get('button[aria-label="刷新家园状态"]').trigger('click')
  await flushPromises()
  expect((home.get('[data-building-uid="71"] select').element as HTMLSelectElement).value).toBe('12')
  expect(home.text()).toContain('家园状态已变化')
  expect(home.get('button[aria-label="应用到游戏"]').attributes('disabled')).toBeDefined()
  expect(writes(fetcher)).toHaveLength(0)
})

it('部分应用展示实际状态和结果，保留未完全应用的草稿且不自动补发', async () => {
  const { wrapper, fetcher } = setup({ write: body => Promise.resolve(json(applied(body, 'partial'))) })
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, '4')
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(home.text()).toContain('合成部分应用')
  expect((home.get('[data-building-uid="71"] select').element as HTMLSelectElement).value).toBe('12')
  expect(home.get('[data-building-uid="71"] .current-production').text()).toContain('测试卡甲')
  expect(writes(fetcher)).toHaveLength(1)
})

it.each(['applied', 'uncertain'])('应用响应丢失后只GET核实%s，不盲目重发或误清未知草稿', async result => {
  let requestBody: Apply | null = null
  const { wrapper, fetcher } = setup({
    write: body => { requestBody = body; return Promise.reject(new Error('synthetic-private-write-error')) },
    read: () => Promise.resolve(json(requestBody ? applied(requestBody, result) : state())),
  })
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, '2')
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(writes(fetcher)).toHaveLength(1)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/state')).toHaveLength(2)
  expect(home.text()).not.toContain('synthetic-private-write-error')
  expect(home.get('button[aria-label="应用到游戏"]').attributes('disabled')).toBeDefined()
  if (result === 'uncertain') {
    expect(home.text()).toContain('结果尚未确认')
    expect((home.get('[data-building-uid="71"] select').element as HTMLSelectElement).value).toBe('12')
  } else expect(home.text()).toContain('合成已确认应用')
})

it('应用中禁止重复发送，卸载会清理本机草稿并忽略迟到结果', async () => {
  const pending = deferred()
  let signal: AbortSignal | undefined
  const { wrapper, fetcher } = setup({ write: (_body, init) => { signal = init.signal as AbortSignal; return pending.promise } })
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, '2')
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(home.get('button[aria-label="应用到游戏"]').attributes('disabled')).toBeDefined()
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  expect(writes(fetcher)).toHaveLength(1)
  wrapper.unmount()
  wrappers.splice(wrappers.indexOf(wrapper), 1)
  await flushPromises()
  expect(signal?.aborted).toBe(true)
  expect(document.querySelector('.home-management')).toBeNull()
  pending.resolve(json(state()))
  await flushPromises()
  expect(document.querySelector('.home-management')).toBeNull()
  expect(writes(fetcher)).toHaveLength(1)
})

it('账号状态未就绪不把全配置目录伪装成账号建筑，401通知父组件会话过期', async () => {
  let unauthorized = false
  const { wrapper } = setup({ read: () => Promise.resolve(unauthorized
    ? json({ error: { code: 'AUTH_REQUIRED', message: '合成过期' } }, 401)
    : json(state({ ready: false, revision: null, buildings: [], message: '等待家园同步' }))) })
  const home = await openHome(wrapper)
  expect(home.text()).toContain('等待家园同步')
  expect(home.find('.home-instance-card').exists()).toBe(false)
  unauthorized = true
  await home.get('button[aria-label="刷新家园状态"]').trigger('click')
  await flushPromises()
  expect(wrapper.emitted('expired')).toHaveLength(1)
})

it.each(['applying', 'uncertain'])('首次读取已有%s结果时锁住编辑，不通过新请求绕过未确认操作', async status => {
  const { wrapper, fetcher } = setup({ read: () => Promise.resolve(json(state({ last_apply: {
    request_id: 'synthetic-existing-request', status, message: '上次操作仍待核实', results: [],
  } }))) })
  const home = await openHome(wrapper)
  expect(home.get('[data-building-uid="71"] select').attributes('disabled')).toBeDefined()
  expect(home.text()).toContain('结果尚未确认')
  expect(writes(fetcher)).toHaveLength(0)
})

it.each(['0', '-1', '1.5'])('批次次数%s不合法时不允许应用', async count => {
  const { wrapper, fetcher } = setup()
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, count)
  expect(home.get('button[aria-label="应用到游戏"]').attributes('disabled')).toBeDefined()
  expect(writes(fetcher)).toHaveLength(0)
})

it('刷新遇到其他应用的部分成功结果时，不清除不属于该请求的本机草稿', async () => {
  let reads = 0
  const { wrapper, fetcher } = setup({ read: () => {
    reads++
    return Promise.resolve(json(reads === 1 ? state() : state({ revision: `external-${reads}`, last_apply: {
      request_id: 'synthetic-external-request', status: reads === 2 ? 'uncertain' : 'partial', message: '合成外部结果',
      results: [{ building_uid: '71', status: reads === 2 ? 'uncertain' : 'applied', message: '合成外部建筑结果' }],
    } })))
  } })
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, '4')
  await home.get('button[aria-label="刷新家园状态"]').trigger('click')
  await flushPromises()
  await home.get('button[aria-label="刷新家园状态"]').trigger('click')
  await flushPromises()
  expect((home.get('[data-building-uid="71"] select').element as HTMLSelectElement).value).toBe('12')
  expect((home.get('[data-building-uid="71"] input[type="number"]').element as HTMLInputElement).value).toBe('4')
  expect(home.text()).toContain('家园状态已变化')
  expect(writes(fetcher)).toHaveLength(0)
})

it.each([
  [422, 'HOME_INVALID_SETTINGS'], [422, 'INVALID_INPUT'], [409, 'HOME_STATE_CONFLICT'],
  [409, 'HOME_REQUEST_CONFLICT'], [409, 'REQUEST_CONFLICT'], [403, 'ORIGIN_REJECTED'],
] as const)('写前明确拒绝%s/%s显示错误并解除本次等待，不自动重发', async (status, code) => {
  let reads = 0
  const { wrapper, fetcher } = setup({
    read: () => Promise.resolve(json(state({ revision: ++reads === 1 ? 'revision-1' : 'revision-new' }))),
    write: () => Promise.resolve(json({ error: { code, message: '合成明确拒绝' } }, status)),
  })
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, '2')
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(home.text()).toContain('合成明确拒绝')
  expect(home.text()).not.toContain('结果尚未确认')
  expect((home.get('[data-building-uid="71"] select').element as HTMLSelectElement).value).toBe('12')
  if (status === 409) expect(home.text()).toContain('家园状态已变化')
  else expect(home.get('button[aria-label="应用到游戏"]').attributes('disabled')).toBeUndefined()
  expect(writes(fetcher)).toHaveLength(1)
})

it('HOME_OUTCOME_UNKNOWN跟随后台既有请求核实，不清除本次未发送的草稿', async () => {
  let reads = 0
  const { wrapper, fetcher } = setup({
    read: () => {
      reads++
      return Promise.resolve(json(reads === 1 ? state() : state({ revision: `prior-${reads}`, last_apply: {
        request_id: 'synthetic-prior-request', status: reads === 2 ? 'uncertain' : 'applied', message: '合成前次操作已核实',
        results: [{ building_uid: '71', status: reads === 2 ? 'uncertain' : 'applied', message: '合成前次建筑结果' }],
      } })))
    },
    write: () => Promise.resolve(json({ error: { code: 'HOME_OUTCOME_UNKNOWN', message: '前次仍待核实' } }, 409)),
  })
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, '2')
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(home.text()).toContain('结果尚未确认')
  await home.get('button[aria-label="刷新家园状态"]').trigger('click')
  await flushPromises()
  expect(home.text()).not.toContain('结果尚未确认')
  expect((home.get('[data-building-uid="71"] select').element as HTMLSelectElement).value).toBe('12')
  expect((home.get('[data-building-uid="71"] input[type="number"]').element as HTMLInputElement).value).toBe('2')
  expect(writes(fetcher)).toHaveLength(1)
})

it('批次次数超出int32上限时在本机阻止应用并显示范围', async () => {
  const { wrapper, fetcher } = setup()
  const home = await openHome(wrapper)
  await chooseRecipe(wrapper, '2147483648')
  expect(home.get('button[aria-label="应用到游戏"]').attributes('disabled')).toBeDefined()
  expect(home.text()).toContain('2147483647')
  expect(writes(fetcher)).toHaveLength(0)
})

it('批次生产进行中只禁改物品和次数，仍可手动更换工人', async () => {
  const source = state({ buildings: [{ ...state().buildings[0], production_editable: false,
    production_reason: '生产进行中，请先在游戏停止当前生产再更换物品或次数' }, state().buildings[1]] })
  const { wrapper, fetcher } = setup({ read: () => Promise.resolve(json(source)) })
  const home = await openHome(wrapper)
  const card = home.get('[data-building-uid="71"]')
  expect(card.get('select').attributes('disabled')).toBeDefined()
  expect(card.get('input[type="number"]').attributes('disabled')).toBeDefined()
  expect(card.text()).toContain('生产进行中')
  expect(card.get('button[aria-label="选择工作噜咪"]').attributes('disabled')).toBeUndefined()
  await card.get('button[aria-label="选择工作噜咪"]').trigger('click')
  const picker = wrapper.get('.home-worker-picker')
  await picker.get(`[data-lumi-uid="${freeWorker}"]`).trigger('click')
  await picker.get('button[aria-label="保存工人选择"]').trigger('click')
  await home.get('button[aria-label="应用到游戏"]').trigger('click')
  await flushPromises()
  expect(JSON.parse(writes(fetcher)[0]![1]!.body as string).changes).toEqual([{ building_uid: '71', lumi_uids: [freeWorker] }])
})
