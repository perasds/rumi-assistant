import { computed, defineComponent, shallowRef } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { createMemoryHistory, createWebHashHistory, type Router, type RouterHistory } from 'vue-router'
import App from '../App.vue'
import OrderView from '../views/OrderView.vue'
import { createAppRouter, safeReturnPath } from '../router'
import type { Session } from '../api/types'
import { orderState } from './orderFixtures'

const mocked = vi.hoisted(() => ({ game: null as unknown }))
vi.mock('../composables/useGameSession', () => ({ useGameSession: () => mocked.game }))

const anonymous: Session = { status: 'anonymous', authenticated: false, role: null, roles: [], message: null }
const connected: Session = { status: 'connected', authenticated: true, roles: [], message: null,
  role: { id: 'synthetic-router-role', name: '', server_name: 'synthetic-zone' } }
const wrappers: ReturnType<typeof mount>[] = []
const routers: Router[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  routers.splice(0).forEach(router => router.options.history.destroy())
  window.history.replaceState(null, '', '/')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function gameSession(initial: Session | Promise<Session> = connected) {
  const session = shallowRef<Session>(anonymous)
  return {
    session, connected: computed(() => session.value.authenticated && session.value.status === 'connected'),
    capabilities: shallowRef({ integration_ready: true, methods: ['account'] }), error: shallowRef(null),
    busy: shallowRef(false), qrBusy: shallowRef(false), qrChallenge: shallowRef(null), qrState: shallowRef(null),
    captchaChallenge: shallowRef(null), captchaCancelling: shallowRef(false), captchaError: shallowRef(null),
    initialize: vi.fn(async () => { session.value = await initial }),
    login: vi.fn(async () => { session.value = connected }),
    logout: vi.fn(async () => { session.value = anonymous }),
    expire: () => { session.value = { ...anonymous, status: 'expired' } },
    createQr: vi.fn(), checkQr: vi.fn(), selectRole: vi.fn(), sendCaptchaInput: vi.fn(), cancelCaptcha: vi.fn(),
  }
}

const Book = defineComponent({ props: { active: Boolean }, template: '<main class="book-stub"><input aria-label="图鉴搜索"></main>' })
const Trade = defineComponent({ emits: ['expired', 'logout'], template: '<main class="trade-stub"><input aria-label="交易搜索"></main>' })
const Mine = defineComponent({ emits: ['expired', 'logout'], template: '<main class="mine-stub"><button @click="$emit(\'logout\')">退出登录</button></main>' })
const Login = defineComponent({ emits: ['login', 'reload', 'logout', 'select-role', 'qr-start', 'qr-check'],
  template: '<main class="login-panel"><button class="finish-login" @click="$emit(\'login\', {})">合成登录成功</button></main>' })
const stubs = { PetBookView: Book, MarketView: Trade, MyView: Mine, LoginView: Login,
  ServerClock: { template: '<span>合成时钟</span>' } }

async function setup(path: string | null = '/book', initial: Session | Promise<Session> = connected, history: RouterHistory = createMemoryHistory()) {
  const game = gameSession(initial)
  mocked.game = game
  const fetcher = vi.fn((path: string) => {
    if (path === '/api/home/orders/state') return Promise.resolve(new Response(JSON.stringify(orderState()), {
      headers: { 'Content-Type': 'application/json' },
    }))
    throw new Error(`Unexpected synthetic routing request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const router = createAppRouter(history)
  routers.push(router)
  await router.push(path ?? history.location)
  await router.isReady()
  const wrapper = mount(App, { global: { plugins: [router], stubs } })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, router, game, fetcher }
}

const menu = (wrapper: ReturnType<typeof mount>, label: string) =>
  wrapper.get('nav[aria-label="功能导航"]').findAll('button').find(button => button.text() === label)!

it.each(['/trade', '/orders', '/orders/inspection', '/mine'])('恢复已登录深链接 %s，页面不会回到图鉴', async path => {
  const { wrapper, router } = await setup(path)
  expect(router.currentRoute.value.path).toBe(path)
  expect(wrapper.get('.book-stub').isVisible()).toBe(false)
  if (path.startsWith('/orders')) {
    expect(wrapper.findComponent(OrderView).exists()).toBe(true)
    expect(wrapper.get('#work-inspection-tab').attributes('aria-selected')).toBe(path.endsWith('inspection') ? 'true' : 'false')
  } else expect(wrapper.find(path === '/trade' ? '.trade-stub' : '.mine-stub').exists()).toBe(true)
})

it('hash URL 刷新后重新创建路由仍留在工作巡检', async () => {
  window.history.replaceState(null, '', '/#/orders/inspection')
  const first = await setup(null, connected, createWebHashHistory())
  expect(window.location.hash).toBe('#/orders/inspection')
  first.wrapper.unmount()
  wrappers.splice(wrappers.indexOf(first.wrapper), 1)
  first.router.options.history.destroy()
  routers.splice(routers.indexOf(first.router), 1)
  const refreshed = await setup(null, connected, createWebHashHistory())
  expect(refreshed.router.currentRoute.value.path).toBe('/orders/inspection')
  expect(refreshed.wrapper.get('#work-inspection-tab').attributes('aria-selected')).toBe('true')
})

it('订单两标签使用URL且前后退复用同一组件，只读取一次状态', async () => {
  const { wrapper, router, fetcher } = await setup('/orders')
  const element = wrapper.getComponent(OrderView).element
  await wrapper.get('#work-inspection-tab').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/orders/inspection')
  expect(wrapper.getComponent(OrderView).element).toBe(element)
  router.back()
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/orders'))
  expect(wrapper.get('#order-task-tab').attributes('aria-selected')).toBe('true')
  router.forward()
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/orders/inspection'))
  expect(wrapper.getComponent(OrderView).element).toBe(element)
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/home/orders/state')).toHaveLength(1)
})

it('匿名初始化不清除深链接，登录完成后仍打开原工作巡检', async () => {
  let finish!: (session: Session) => void
  const pending = new Promise<Session>(resolve => { finish = resolve })
  const { wrapper, router, fetcher } = await setup('/orders/inspection', pending)
  expect(wrapper.find('.login-panel').exists()).toBe(true)
  expect(router.currentRoute.value.path).toBe('/orders/inspection')
  expect(fetcher).not.toHaveBeenCalled()
  finish(anonymous)
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/orders/inspection')
  await wrapper.get('.finish-login').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/orders/inspection')
  expect(wrapper.get('#work-inspection-tab').attributes('aria-selected')).toBe('true')
})

it('登录恢复前后保留交易目标，失效后再次登录也不跳图鉴', async () => {
  const { wrapper, router, game } = await setup('/trade')
  game.expire()
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/trade')
  expect(wrapper.find('.trade-stub').exists()).toBe(false)
  expect(wrapper.find('.login-panel').exists()).toBe(true)
  await game.login()
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/trade')
  expect(wrapper.find('.trade-stub').exists()).toBe(true)
})

it('显式登录路由使用白名单内部返回目标，成功后 replace 回原页', async () => {
  const { wrapper, router } = await setup('/login?redirect=/orders/inspection', anonymous)
  expect(router.currentRoute.value.path).toBe('/login')
  expect(router.currentRoute.value.query.redirect).toBe('/orders/inspection')
  await wrapper.get('.finish-login').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/orders/inspection')
  expect(wrapper.get('#work-inspection-tab').attributes('aria-selected')).toBe('true')
})

it.each(['https://other.invalid/path', '//other.invalid', '/login', '/orders?next=evil', ['x'], undefined])(
  '不接受外域或非白名单登录返回目标 %s', async redirect => {
    expect(safeReturnPath(redirect)).toBe('/book')
    const value = typeof redirect === 'string' ? encodeURIComponent(redirect) : ''
    const { router } = await setup(`/login?redirect=${value}`)
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/book')
  },
)

it.each(['/', '/not-found', '/orders/unknown'])('根路径或未知路径 %s 归一到图鉴', async path => {
  const { router, wrapper } = await setup(path)
  expect(router.currentRoute.value.path).toBe('/book')
  expect(wrapper.get('.book-stub').isVisible()).toBe(true)
})

it('导航保留图鉴和交易缓存，明确退出才卸载私有页并回图鉴', async () => {
  const { wrapper, router } = await setup('/book')
  const book = wrapper.get('.book-stub input')
  await book.setValue('合成图鉴搜索')
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  const trade = wrapper.get('.trade-stub input')
  await trade.setValue('合成交易搜索')
  await menu(wrapper, '订单').trigger('click')
  await flushPromises()
  expect(wrapper.get('.trade-stub').isVisible()).toBe(false)
  await menu(wrapper, '交易').trigger('click')
  await flushPromises()
  expect(wrapper.get('.trade-stub input').element).toBe(trade.element)
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  await wrapper.get('.mine-stub button').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/book')
  expect(wrapper.find('.trade-stub').exists()).toBe(false)
  expect((wrapper.get('.book-stub input').element as HTMLInputElement).value).toBe('合成图鉴搜索')
})

it('退出未确认时保持当前URL及私有页面', async () => {
  const { wrapper, router, game } = await setup('/mine')
  game.logout.mockImplementation(async () => {})
  await wrapper.get('.mine-stub button').trigger('click')
  await flushPromises()
  expect(router.currentRoute.value.path).toBe('/mine')
  expect(wrapper.find('.mine-stub').exists()).toBe(true)
})

it('旧的无router插件独立挂载仍可导航', async () => {
  mocked.game = gameSession(anonymous)
  const wrapper = mount(App, { global: { stubs } })
  wrappers.push(wrapper)
  await flushPromises()
  expect(wrapper.get('.book-stub').isVisible()).toBe(true)
  await menu(wrapper, '我的').trigger('click')
  await flushPromises()
  expect(wrapper.find('.login-panel').exists()).toBe(true)
})
