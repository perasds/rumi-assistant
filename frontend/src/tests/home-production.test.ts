import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { closeToast } from 'vant'
import HomeView from '../views/HomeView.vue'

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
const catalog = {
  scope: 'configuration', total: 2, recipe_count: 3,
  buildings: [
    { building_id: '9007199254740993', name: '测试制卡台', island_id: 1, island_name: '测试主岛', work_type: '制卡',
      recipes: [
        { recipe_id: '11', min_building_level: 1,
          materials: [{ item_id: '101', name: '测试木材', count: 3, image_url: '/test/material.png' }],
          products: [{ item_id: '201', name: '测试噜咪卡', count: 2, image_url: '/test/product.png' }] },
        { recipe_id: '12', min_building_level: 4, materials: [],
          products: [{ item_id: '202', name: '测试精制卡', count: 1, image_url: null },
            { item_id: '203', name: '测试副产品', count: 5, image_url: null }] },
      ] },
    { building_id: '102', name: '建筑 #102', island_id: 2, island_name: '测试浮岛', work_type: null,
      recipes: [{ recipe_id: '13', min_building_level: 2, materials: [],
        products: [{ item_id: '204', name: '<b>测试物品</b>', count: 6, image_url: null }] }] },
  ],
}
function deferred() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>(done => { resolve = done })
  return { promise, resolve }
}
function setup(homeRequest: (init: RequestInit) => Promise<Response> = () => Promise.resolve(json(catalog))) {
  const fetcher = vi.fn((path: string, init: RequestInit = {}) => {
    if (path === '/api/home/production') return homeRequest(init)
    if (path === '/api/home/state') return Promise.resolve(json({ scope: 'account', ready: false, revision: null,
      observed_at: null, message: '合成账号状态未接入', buildings: [], roster: [], last_apply: null }))
    throw new Error(`Unexpected test request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mountHome()
  return { wrapper, fetcher }
}
function mountHome() {
  const wrapper = mount(HomeView, { props: { active: false }, attachTo: document.body })
  wrappers.push(wrapper)
  return wrapper
}
async function openHome(wrapper: ReturnType<typeof mount>) {
  await flushPromises()
  await wrapper.setProps({ active: true })
  await flushPromises()
  await wrapper.findAll('[role="tab"]').find(tab => tab.text() === '配方目录')!.trigger('click')
  await flushPromises()
  return wrapper.get('main[aria-label="家园生产"]')
}

it('家园首次进入才读取配置目录，展示全部建筑和配方，ID保持字符串', async () => {
  const { wrapper, fetcher } = setup()
  await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/production')).toHaveLength(0)
  const home = await openHome(wrapper)
  expect(home.get('h1').text()).toBe('家园生产')
  expect(home.text()).toContain('配置目录')
  expect(home.text()).not.toContain('已解锁')
  expect(home.text()).not.toContain('正在生产')
  expect(home.findAll('.home-building-card')).toHaveLength(2)
  const building = home.get('[data-building-id="9007199254740993"]')
  expect(building.text()).toContain('测试制卡台')
  expect(building.text()).toContain('测试主岛')
  expect(building.text()).toContain('制卡')
  expect(building.findAll('details')).toHaveLength(2)
  expect(building.get('[data-recipe-id="11"] summary').text()).toContain('测试噜咪卡 × 2')
  expect(building.get('[data-recipe-id="12"] summary').text()).toContain('测试精制卡 × 1')
  expect(building.get('[data-recipe-id="12"] summary').text()).toContain('测试副产品 × 5')
  expect(building.get('[data-recipe-id="12"] summary').text()).toContain('建筑 4 级')
  expect(home.text()).toContain('建筑 #102')
  const calls = fetcher.mock.calls.filter(([path]) => path === '/api/home/production')
  expect(calls).toHaveLength(1)
  expect(calls[0]![1]?.method).toBe('GET')
  expect(calls[0]![1]?.credentials).toBe('same-origin')
})

it('点击配方展开和收起材料，无材料的配方给出明确说明', async () => {
  const { wrapper } = setup()
  const home = await openHome(wrapper)
  const recipe = home.get('details[data-recipe-id="11"]')
  expect((recipe.element as HTMLDetailsElement).open).toBe(false)
  await recipe.get('summary').trigger('click')
  expect((recipe.element as HTMLDetailsElement).open).toBe(true)
  expect(recipe.get('.recipe-materials').text()).toContain('测试木材 × 3')
  expect(recipe.get('.recipe-materials img').attributes('src')).toBe('/test/material.png')
  await recipe.get('summary').trigger('click')
  expect((recipe.element as HTMLDetailsElement).open).toBe(false)
  const free = home.get('details[data-recipe-id="12"]')
  await free.get('summary').trigger('click')
  expect(free.get('.recipe-materials').text()).toContain('无需材料')
})

it('缺图仍显示名称与数量，不猜测图片路径，名称按纯文本显示', async () => {
  const { wrapper } = setup()
  const home = await openHome(wrapper)
  expect(home.get('[data-recipe-id="11"] summary img').attributes('src')).toBe('/test/product.png')
  expect(home.get('[data-recipe-id="12"] summary').find('img').exists()).toBe(false)
  const unnamedImage = home.get('[data-recipe-id="13"] summary')
  expect(unnamedImage.text()).toContain('<b>测试物品</b> × 6')
  expect(unnamedImage.find('b').exists()).toBe(false)
  expect(unnamedImage.find('img').exists()).toBe(false)
})

it('停用再启用保留目录和展开状态，不重复请求', async () => {
  const { wrapper, fetcher } = setup()
  const home = await openHome(wrapper)
  await home.get('[data-recipe-id="11"] summary').trigger('click')
  await wrapper.setProps({ active: false })
  await wrapper.setProps({ active: true })
  expect((home.get('[data-recipe-id="11"]').element as HTMLDetailsElement).open).toBe(true)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/production')).toHaveLength(1)
})

it('加载中停用再启用不会取消或重复请求，响应回来后直接显示', async () => {
  const pending = deferred()
  let signal: AbortSignal | undefined
  const { wrapper, fetcher } = setup(init => { signal = init.signal as AbortSignal; return pending.promise })
  const home = await openHome(wrapper)
  expect(home.text()).toContain('正在加载生产目录')
  await wrapper.setProps({ active: false })
  await wrapper.setProps({ active: true })
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/production')).toHaveLength(1)
  expect(signal?.aborted).toBe(false)
  pending.resolve(json(catalog))
  await flushPromises()
  expect(home.text()).toContain('测试制卡台')
  expect(home.text()).not.toContain('正在加载生产目录')
})

it('目录失败显示安全错误，用户可以重试后看到数据', async () => {
  let attempts = 0
  const { wrapper, fetcher } = setup(() => ++attempts === 1
    ? Promise.reject(new Error('synthetic-private-home-error')) : Promise.resolve(json(catalog)))
  const home = await openHome(wrapper)
  expect(home.get('[role="alert"]').text()).toContain('无法连接服务')
  expect(home.text()).not.toContain('synthetic-private-home-error')
  await wrapper.setProps({ active: false })
  await wrapper.setProps({ active: true })
  await flushPromises()
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/production')).toHaveLength(1)
  await home.get('button[aria-label="重试加载家园生产"]').trigger('click')
  await flushPromises()
  expect(home.text()).toContain('测试制卡台')
  expect(home.find('[role="alert"]').exists()).toBe(false)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/production')).toHaveLength(2)
})

it('空配置有空状态且停用再启用不会重复请求', async () => {
  const { wrapper, fetcher } = setup(() => Promise.resolve(json({ scope: 'configuration', buildings: [], total: 0, recipe_count: 0 })))
  const home = await openHome(wrapper)
  expect(home.text()).toContain('暂无生产建筑')
  expect(home.find('.home-building-card').exists()).toBe(false)
  await wrapper.setProps({ active: false })
  await wrapper.setProps({ active: true })
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/production')).toHaveLength(1)
})

it('家园401通知父组件结束会话且不会显示旧目录', async () => {
  const { wrapper } = setup(() => Promise.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '会话已失效' } }, 401)))
  await flushPromises()
  await wrapper.setProps({ active: true })
  await flushPromises()
  expect(wrapper.emitted('expired')).toHaveLength(1)
  expect(wrapper.find('.home-building-card').exists()).toBe(false)
})

it('卸载会取消目录请求，迟到旧401不会影响重新挂载的家园', async () => {
  const old = deferred()
  let oldSignal: AbortSignal | undefined
  let attempts = 0
  const { wrapper, fetcher } = setup(init => {
    if (++attempts === 1) { oldSignal = init.signal as AbortSignal; return old.promise }
    return Promise.resolve(json(catalog))
  })
  await openHome(wrapper)
  wrapper.unmount()
  wrappers.splice(wrappers.indexOf(wrapper), 1)
  await flushPromises()
  expect(oldSignal?.aborted).toBe(true)
  expect(document.querySelector('main[aria-label="家园生产"]')).toBeNull()
  const nextWrapper = mountHome()
  const home = await openHome(nextWrapper)
  expect(home.text()).toContain('测试制卡台')
  old.resolve(json({ error: { code: 'AUTH_REQUIRED', message: '迟到旧响应' } }, 401))
  await flushPromises()
  expect(wrapper.emitted('expired')).toBeUndefined()
  expect(nextWrapper.emitted('expired')).toBeUndefined()
  expect(home.text()).toContain('测试制卡台')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/production')).toHaveLength(2)
})

it('应用卸载会取消家园请求，晚到响应不会再触发页面更新', async () => {
  const pending = deferred()
  let signal: AbortSignal | undefined
  const { wrapper, fetcher } = setup(init => { signal = init.signal as AbortSignal; return pending.promise })
  await openHome(wrapper)
  wrapper.unmount()
  wrappers.splice(wrappers.indexOf(wrapper), 1)
  expect(signal?.aborted).toBe(true)
  const callCount = fetcher.mock.calls.length
  pending.resolve(json(catalog))
  await flushPromises()
  expect(fetcher.mock.calls.length).toBe(callCount)
  expect(document.querySelector('main[aria-label="家园生产"]')).toBeNull()
})
