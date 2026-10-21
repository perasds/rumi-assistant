import { flushPromises, mount, type DOMWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { closeToast, Popup as VanPopup } from 'vant'
import App from '../App.vue'
import MarketView from '../views/MarketView.vue'
import type { Pet, PetCatalog, RushPurchaseState, Session } from '../api/types'
import { loginChallenge, testCrypto } from './loginEncryptionFixture'
import { orderState } from './orderFixtures'

const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => { vi.stubGlobal('crypto', testCrypto) })
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  closeToast(true)
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
const connected: Session = { status: 'connected', authenticated: true, role: null, roles: [], message: null }
const anonymous: Session = { ...connected, status: 'anonymous', authenticated: false }
const pets: Pet[] = [
  { pet_id: '1000101', name: '蜜桃', display_name: '蜜桃', name_verified: true, image_url: '/test-peach.png' },
  { pet_id: '9000001', name: null, display_name: '噜咪 #9000001', name_verified: false, image_url: null },
]
const catalogOf = (items = pets): PetCatalog => ({ items, total: items.length,
  named_count: items.filter(pet => pet.name_verified).length, image_count: items.filter(pet => pet.image_url).length })
const idle: RushPurchaseState = { job_id: null, status: 'idle', settings: null, balance_gold: null,
  counts: {}, spent_gold: '0', message: null, attempts: 0, updated_at: null }
const skill = (skill_id: string, name: string) => ({ skill_id, name, description: `${name}说明`,
  element: { id: 1, name: '草' }, cost_text: '3', power: 80, image_url: null })
function detail(id: string, overrides: Record<string, unknown> = {}) {
  return { ...(pets.find(pet => pet.pet_id === id) ?? pets[0]!), available: true, pokedex_id: '001',
    elements: [{ id: 1, name: '草' }], stats: [{ key: 'hp', label: '体力', min: 70, max: 100 }],
    normal_attack: { element: { id: 1, name: '草' }, power: 20, energy_gain: 5, interval_seconds: 1.5 },
    traits: [{ trait_id: 'battle-1', kind: 'battle', name: '战斗特性', description: '<b>原样说明</b>' },
      { trait_id: 'home-1', kind: 'home', name: '家园特性', description: null }],
    fixed_skill: skill('fixed-1', '固定藤鞭'), replaceable_skills: [skill('pool-1', '飞叶'), skill('pool-2', '花雨')],
    missing_fields: [], ...overrides }
}
function deferred() {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>(done => { resolve = done })
  return { promise, resolve }
}
function setup(options: {
  catalog?: PetCatalog
  catalogRequest?: () => Promise<Response>
  detailRequest?: (id: string, init: RequestInit) => Promise<Response>
  rush?: RushPurchaseState
} = {}) {
  let authenticated = true
  const fetcher = vi.fn((path: string, init: RequestInit = {}) => {
    if (path === '/api/time') return Promise.resolve(json({ unix_ms: 1_800_000_000_123, time_zone: 'Asia/Shanghai' }))
    if (path === '/api/auth/capabilities') return Promise.resolve(json({ integration_ready: true, methods: ['account'] }))
    if (path === '/api/auth/login-challenge') return Promise.resolve(json(loginChallenge()))
    if (path === '/api/session' && init.method === 'DELETE') {
      authenticated = false
      return Promise.resolve(json(anonymous))
    }
    if (path === '/api/auth/login') {
      authenticated = true
      return Promise.resolve(json(connected))
    }
    if (path === '/api/session') return Promise.resolve(json(authenticated ? connected : anonymous))
    if (path === '/api/me') return Promise.resolve(json({
      role_id: null, nickname: null, server_name: null, balance_gold: null,
    }))
    if (path === '/api/home/orders/state') return Promise.resolve(json(orderState()))
    if (path === '/api/pets') return options.catalogRequest?.() ?? Promise.resolve(json(options.catalog ?? catalogOf()))
    if (path.startsWith('/api/pets?')) return Promise.resolve(json(catalogOf([pets[0]!])) )
    if (path.startsWith('/api/pets/')) {
      const id = path.slice('/api/pets/'.length)
      return options.detailRequest?.(id, init) ?? Promise.resolve(json(detail(id)))
    }
    if (path === '/api/rush-purchase') return Promise.resolve(json(options.rush ?? idle))
    if (path === '/api/market/refresh') return Promise.resolve(json({ refresh_id: 'test', scope: 'top10',
      status: 'complete', complete: true, items: [], loaded_count: 0, matched_count: 0, total_count: 0,
      started_at: null, updated_at: null, message: null, name_coverage: { named: 1, total: 1 } }))
    throw new Error(`Unexpected test request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(App, { attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}
function menu(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.get('nav').findAll('button').find(item => item.text() === label)
  expect(button, `缺少菜单 ${label}`).toBeDefined()
  return button!
}
async function openBook(wrapper: ReturnType<typeof mount>) {
  await flushPromises()
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  return wrapper.get('.pet-book-view')
}
async function openPet(wrapper: ReturnType<typeof mount>, id = '1000101') {
  await wrapper.get(`.pet-book-grid button[data-pet-id="${id}"]`).trigger('click')
  await flushPromises()
  return wrapper.get('.pet-detail-popup')
}
async function tab(popup: Pick<DOMWrapper<Element>, 'findAll'>, label: string) {
  const target = popup.findAll('[role="tab"]').find(item => item.text() === label)
  expect(target).toBeDefined()
  await target!.trigger('click')
  await flushPromises()
}

it('默认图鉴独立加载完整306条目录，包含未核验与缺图记录', async () => {
  const all = [...pets, ...Array.from({ length: 304 }, (_, index) => ({ ...pets[0]!,
    pet_id: String(2000000 + index), name: `测试${index}`, display_name: `测试${index}` }))]
  const { wrapper, fetcher } = setup({ catalog: catalogOf(all) })
  await flushPromises()
  expect(wrapper.get('nav').findAll('button').map(button => button.text())).toEqual(['交易', '图鉴', '订单', '我的'])
  expect(fetcher.mock.calls.some(([path]) => path === '/api/me')).toBe(false)
  expect(menu(wrapper, '图鉴').find('svg').exists()).toBe(true)
  expect(menu(wrapper, '图鉴').attributes('aria-current')).toBe('page')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(1)
  const book = await openBook(wrapper)
  expect(wrapper.find('.feature-placeholder').exists()).toBe(false)
  expect(book.findAll('.pet-book-grid button')).toHaveLength(306)
  expect(book.get('button[data-pet-id="9000001"]').text()).toContain('9000001')
  expect(book.get('button[data-pet-id="1000101"] img').attributes('src')).toBe('/test-peach.png')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(1)
  await book.get('input[type="search"]').setValue('9000001')
  expect(book.findAll('.pet-book-grid button')).toHaveLength(1)
  await book.get('input[type="search"]').setValue('蜜桃')
  expect(book.findAll('.pet-book-grid button')).toHaveLength(1)
  await book.get('input[type="search"]').setValue('不存在')
  expect(book.text()).toContain('没有匹配的噜咪')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(1)
})

it('图鉴倒序展示，名称筛选与清空后保持相同倒序', async () => {
  const items = [
    { ...pets[0]!, pet_id: '110', name: '蜜桃甲', display_name: '蜜桃甲' },
    { ...pets[0]!, pet_id: '220', name: '灰灰', display_name: '灰灰' },
    { ...pets[0]!, pet_id: '330', name: '蜜桃乙', display_name: '蜜桃乙' },
  ]
  const { wrapper } = setup({ catalog: catalogOf(items) })
  const book = await openBook(wrapper)
  const ids = () => book.findAll('.pet-book-grid button').map(button => button.attributes('data-pet-id'))
  const search = book.get('input[type="search"]')
  expect(ids()).toEqual(['330', '220', '110'])
  await search.setValue('蜜桃')
  expect(ids()).toEqual(['330', '110'])
  await search.setValue('')
  expect(ids()).toEqual(['330', '220', '110'])
  await search.setValue('蜜桃')
  expect(ids()).toEqual(['330', '110'])
})

it('目录加载失败可重试，错误不显示原始异常', async () => {
  let attempts = 0
  const { wrapper, fetcher } = setup({ catalogRequest: () => ++attempts === 1
    ? Promise.reject(new Error('private-test-error')) : Promise.resolve(json(catalogOf())) })
  const book = await openBook(wrapper)
  expect(book.get('[role="alert"]').text()).not.toContain('private-test-error')
  await book.get('button[aria-label="重试加载图鉴"]').trigger('click')
  await flushPromises()
  expect(book.findAll('.pet-book-grid button')).toHaveLength(2)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(2)
})

it('详情展示基本、特性与完整技能池，点击技能仅切换说明并按普通文本显示', async () => {
  const { wrapper, fetcher } = setup()
  await openBook(wrapper)
  const popup = await openPet(wrapper)
  expect(popup.text()).toContain('蜜桃')
  expect(popup.text()).toContain('70–100')
  expect(popup.text()).toContain('1.5 秒')
  await tab(popup, '特性')
  expect(popup.text()).toContain('战斗特性')
  expect(popup.text()).toContain('家园特性')
  expect(popup.text()).toContain('<b>原样说明</b>')
  expect(popup.find('b').exists()).toBe(false)
  await tab(popup, '技能')
  expect(popup.text()).toContain('固定技能')
  expect(popup.text()).toContain('可替换技能池')
  const fixed = popup.get('button[data-skill-id="fixed-1"]')
  expect(fixed.text()).toContain('固定藤鞭')
  expect(popup.findAll('.replaceable-skills button')).toHaveLength(2)
  await popup.get('button[data-skill-id="pool-2"]').trigger('click')
  expect(popup.get('.skill-detail').text()).toContain('花雨说明')
  expect(popup.get('button[data-skill-id="pool-2"]').attributes('aria-pressed')).toBe('true')
  expect(popup.find('select').exists()).toBe(false)
  expect(fetcher.mock.calls.every(([path, init]) => init?.method === 'GET' || path === '/api/market/refresh')).toBe(true)
})

it('缺失字段显示未知，缺整份配置显示未收录且不沿用上一只资料', async () => {
  const { wrapper } = setup({ detailRequest: id => Promise.resolve(json(id === '1000101'
    ? detail(id, { pokedex_id: null, elements: [], stats: [], normal_attack: null, traits: [], fixed_skill: null,
      replaceable_skills: [], missing_fields: ['stats', 'normal_attack'] })
    : detail(id, { available: false, pokedex_id: null, elements: [], stats: [], normal_attack: null,
      traits: [], fixed_skill: null, replaceable_skills: [] }))) })
  await openBook(wrapper)
  let popup = await openPet(wrapper)
  expect(popup.text()).toContain('—')
  expect(popup.text()).not.toContain('70–100')
  await tab(popup, '特性')
  expect(popup.text()).toContain('未收录')
  await tab(popup, '技能')
  expect(popup.text()).toContain('未收录')
  await popup.get('button[aria-label="关闭详情"]').trigger('click')
  popup = await openPet(wrapper, '9000001')
  expect(popup.text()).toContain('详细资料暂未收录')
  expect(popup.text()).not.toContain('固定藤鞭')
})

it('详情失败可重试，成功详情按ID缓存', async () => {
  let attempts = 0
  const { wrapper, fetcher } = setup({ detailRequest: id => ++attempts === 1
    ? Promise.reject(new Error('private-detail-error')) : Promise.resolve(json(detail(id))) })
  await openBook(wrapper)
  let popup = await openPet(wrapper)
  expect(popup.get('[role="alert"]').text()).not.toContain('private-detail-error')
  await popup.get('button[aria-label="重试加载噜咪详情"]').trigger('click')
  await flushPromises()
  expect(popup.text()).toContain('70–100')
  await popup.get('button[aria-label="关闭详情"]').trigger('click')
  popup = await openPet(wrapper)
  expect(popup.text()).toContain('70–100')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets/1000101')).toHaveLength(2)
})

it('快速切换噜咪会取消旧请求，忽略仍然迟到的旧响应', async () => {
  const old = deferred()
  let oldSignal: AbortSignal | null = null
  const { wrapper } = setup({ detailRequest: (id, init) => {
    if (id === '1000101') { oldSignal = init.signal as AbortSignal; return old.promise }
    return Promise.resolve(json(detail(id, { pokedex_id: '另一只编号' })))
  } })
  await openBook(wrapper)
  await openPet(wrapper)
  const popup = await openPet(wrapper, '9000001')
  expect(oldSignal!.aborted).toBe(true)
  expect(popup.text()).toContain('另一只编号')
  old.resolve(json(detail('1000101')))
  await flushPromises()
  expect(popup.text()).toContain('另一只编号')
  expect(popup.get('.basic-values dd').text()).toBe('另一只编号')
})

it('关闭详情取消请求，迟到响应不重新打开且下次可重新读取', async () => {
  const old = deferred()
  let oldSignal: AbortSignal | null = null
  let calls = 0
  const { wrapper } = setup({ detailRequest: (id, init) => {
    if (++calls === 1) { oldSignal = init.signal as AbortSignal; return old.promise }
    return Promise.resolve(json(detail(id)))
  } })
  await openBook(wrapper)
  const popup = await openPet(wrapper)
  await popup.get('button[aria-label="关闭详情"]').trigger('click')
  expect(oldSignal!.aborted).toBe(true)
  old.resolve(json(detail('1000101')))
  await flushPromises()
  expect(wrapper.getComponent({ name: 'PetDetailPopup' }).props('show')).toBe(false)
  expect((await openPet(wrapper)).text()).toContain('70–100')
  expect(calls).toBe(2)
})

it.each(['基本', '特性', '技能'])('关闭%s页签时保留完整内容，动画结束后才清理，缓存仍能重开', async label => {
  const { wrapper, fetcher } = setup()
  await openBook(wrapper)
  const popup = await openPet(wrapper)
  await tab(popup, label)
  const component = wrapper.getComponent({ name: 'PetDetailPopup' })
  const nativePopup = component.getComponent(VanPopup)
  const before = { pet: component.props('pet'), detail: component.props('detail'), text: popup.text() }
  await popup.get('button[aria-label="关闭详情"]').trigger('click')
  expect(component.props('show')).toBe(false)
  expect(component.props('pet')).toBe(before.pet)
  expect(component.props('detail')).toBe(before.detail)
  expect(popup.text()).toBe(before.text)
  expect(popup.findAll('[role="tab"]').find(item => item.text() === label)?.attributes('aria-selected')).toBe('true')
  nativePopup.vm.$emit('closed')
  await flushPromises()
  expect(component.props('pet')).toBeNull()
  expect(component.props('detail')).toBeNull()
  expect(popup.isVisible()).toBe(false)
  expect((await openPet(wrapper)).text()).toContain('70–100')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets/1000101')).toHaveLength(1)
})

it('遮罩关闭保留内容到closed，快速打开另一只时旧closed不会清掉新资料', async () => {
  const { wrapper } = setup()
  await openBook(wrapper)
  await openPet(wrapper)
  const component = wrapper.getComponent({ name: 'PetDetailPopup' })
  const nativePopup = component.getComponent(VanPopup)
  nativePopup.vm.$emit('update:show', false)
  await flushPromises()
  expect(component.props('show')).toBe(false)
  expect(component.props('pet')?.pet_id).toBe('1000101')
  await openPet(wrapper, '9000001')
  nativePopup.vm.$emit('closed')
  await flushPromises()
  expect(component.props('show')).toBe(true)
  expect(component.props('pet')?.pet_id).toBe('9000001')
  expect(component.props('detail')?.pet_id).toBe('9000001')
})

it('加载中关闭立即取消请求，保留加载画面到closed且忽略迟到响应', async () => {
  const pending = deferred()
  let signal!: AbortSignal
  const { wrapper } = setup({ detailRequest: (_id, init) => {
    signal = init.signal as AbortSignal
    return pending.promise
  } })
  await openBook(wrapper)
  const popup = await openPet(wrapper)
  const component = wrapper.getComponent({ name: 'PetDetailPopup' })
  await popup.get('button[aria-label="关闭详情"]').trigger('click')
  expect(signal.aborted).toBe(true)
  expect(component.props('show')).toBe(false)
  expect(component.props('loading')).toBe(true)
  expect(popup.text()).toContain('正在加载详情')
  pending.resolve(json(detail('1000101')))
  await flushPromises()
  expect(component.props('detail')).toBeNull()
  expect(component.props('loading')).toBe(true)
  component.getComponent(VanPopup).vm.$emit('closed')
  await flushPromises()
  expect(component.props('pet')).toBeNull()
  expect(component.props('loading')).toBe(false)
})

it('错误状态离场保留提示且重试不再请求，打开已缓存噜咪不残留旧错误', async () => {
  const { wrapper, fetcher } = setup({ detailRequest: id => id === '9000001'
    ? Promise.reject(new Error('synthetic-detail-error')) : Promise.resolve(json(detail(id))) })
  await openBook(wrapper)
  await openPet(wrapper)
  const popup = await openPet(wrapper, '9000001')
  const component = wrapper.getComponent({ name: 'PetDetailPopup' })
  const error = component.props('error')
  expect(error).toBeTruthy()
  await popup.get('button[aria-label="关闭详情"]').trigger('click')
  expect(component.props('error')).toBe(error)
  const before = fetcher.mock.calls.length
  await popup.get('button[aria-label="重试加载噜咪详情"]').trigger('click')
  await flushPromises()
  expect(fetcher.mock.calls).toHaveLength(before)
  await openPet(wrapper)
  expect(component.props('show')).toBe(true)
  expect(component.props('error')).toBeNull()
  expect(component.props('loading')).toBe(false)
  expect(component.props('detail')?.pet_id).toBe('1000101')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets/1000101')).toHaveLength(1)
})

it('切页关闭详情并取消请求，保留图鉴搜索、滚动和目录', async () => {
  const pending = deferred()
  let signal: AbortSignal | null = null
  const { wrapper, fetcher } = setup({ detailRequest: (_id, init) => {
    signal = init.signal as AbortSignal
    return pending.promise
  } })
  const book = await openBook(wrapper)
  await book.get('input[type="search"]').setValue('蜜桃')
  const scroll = book.get('.pet-book-scroll').element
  scroll.scrollTop = 128
  await openPet(wrapper)
  await menu(wrapper, '订单').trigger('click')
  await flushPromises()
  expect(signal!.aborted).toBe(true)
  expect(wrapper.getComponent({ name: 'PetDetailPopup' }).props('show')).toBe(false)
  expect(book.isVisible()).toBe(false)
  pending.resolve(json(detail('1000101')))
  await flushPromises()
  await menu(wrapper, '图鉴').trigger('click')
  await flushPromises()
  expect(book.isVisible()).toBe(true)
  expect((book.get('input[type="search"]').element as HTMLInputElement).value).toBe('蜜桃')
  expect(scroll.scrollTop).toBe(128)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(1)
})

it('图鉴期间交易实例与抢购轮询继续，切页不发额外刷新、开始或取消', async () => {
  vi.useFakeTimers()
  const { wrapper, fetcher } = setup({ rush: { ...idle, job_id: 'test-job', status: 'running', settings: {
    pet_ids: [], max_price_gold: 1000, quantity_per_pet: 2, min_balance_gold: 0,
  } } })
  await flushPromises()
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  const market = wrapper.getComponent(MarketView).element
  await openBook(wrapper)
  await vi.advanceTimersByTimeAsync(2000)
  await flushPromises()
  expect(wrapper.getComponent(MarketView).element).toBe(market)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase')).toHaveLength(3)
  await menu(wrapper, '交易').trigger('click')
  expect(wrapper.getComponent(MarketView).element).toBe(market)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/market/refresh')).toHaveLength(1)
  expect(fetcher.mock.calls.filter(([path]) => /rush-purchase\/(start|stop)$/.test(path))).toHaveLength(0)
})

it('从个人中心退出和重新登录保留公开图鉴详情缓存，不重复加载物种资料', async () => {
  const pending = deferred()
  let signal: AbortSignal | null = null
  let calls = 0
  const { wrapper, fetcher } = setup({ detailRequest: (id, init) => {
    if (++calls === 1) { signal = init.signal as AbortSignal; return pending.promise }
    return Promise.resolve(json(detail(id)))
  } })
  await openBook(wrapper)
  await openPet(wrapper)
  pending.resolve(json(detail('1000101', { pokedex_id: '公开物种编号' })))
  await flushPromises()
  expect(wrapper.get('.pet-detail-popup').text()).toContain('公开物种编号')
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  await wrapper.get('main[aria-label="个人信息"] button[aria-label="退出登录"]').trigger('click')
  await flushPromises()
  expect(signal!.aborted).toBe(false)
  expect(wrapper.get('.pet-book-view').isVisible()).toBe(true)
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  await wrapper.get('input[name="account"]').setValue('test-account')
  await wrapper.get('input[name="password"]').setValue('test-password')
  await wrapper.get('form').trigger('submit')
  await vi.waitFor(() => expect(wrapper.find('.login-panel').exists()).toBe(false))
  await openBook(wrapper)
  const popup = await openPet(wrapper)
  await flushPromises()
  expect(popup.text()).toContain('公开物种编号')
  expect(calls).toBe(1)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/pets')).toHaveLength(1)
})
