import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import MyView from '../views/MyView.vue'

const wrappers: ReturnType<typeof mount>[] = []
const info = { role_id: '18446744073709551614', nickname: '测试噜咪', server_name: '测试区服', balance_gold: '9007199254740993' }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
function setup(response: () => Promise<Response> = async () => json(info)) {
  const fetcher = vi.fn((_path: string, _init: RequestInit = {}) => response())
  vi.stubGlobal('fetch', (path: string, init: RequestInit = {}) => path === '/api/me' ? fetcher(path, init) : Promise.resolve(json({
    available: true, running: false, status: 'idle', message: '尚未启动', claimed_count: 0, skipped_count: 0,
    stamina_total: '0', recent: [], started_at: null, updated_at: null,
  })))
  const wrapper = mount(MyView, { attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, fetcher }
}
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('进入个人页读取当前资料，ID和金币大整数不丢失精度，额外字段不展示', async () => {
  const { wrapper, fetcher } = setup(async () => json({ ...info, access_token: 'synthetic-private-token' }))
  await flushPromises()
  expect(fetcher).toHaveBeenCalledOnce()
  expect(fetcher.mock.calls[0]?.[0]).toBe('/api/me')
  expect(wrapper.get('main[aria-label="个人信息"]').text()).toContain('测试噜咪')
  expect(wrapper.text()).toContain('18446744073709551614')
  expect(wrapper.text()).toContain('9,007,199,254,740,993')
  expect(wrapper.text()).toContain('测试区服')
  expect(wrapper.text()).not.toContain('synthetic-private-token')
})

it('同步不到的数据不伪装成零余额或真实昵称', async () => {
  const { wrapper } = setup(async () => json({ role_id: null, nickname: null, server_name: null, balance_gold: null }))
  await flushPromises()
  expect(wrapper.findAll('dd').map(row => row.text())).toEqual(['未同步', '未同步', '未同步', '未同步'])
})

it('资料加载期间和失败后仍能从个人中心退出登录', async () => {
  let reject!: (reason: Error) => void
  const { wrapper } = setup(() => new Promise<Response>((_resolve, fail) => { reject = fail }))
  const logout = wrapper.get('button[aria-label="退出登录"]')
  expect(logout.attributes('disabled')).toBeUndefined()
  await logout.trigger('click')
  expect(wrapper.emitted('logout')).toHaveLength(1)
  reject(new Error('synthetic-private-error'))
  await flushPromises()
  await wrapper.get('button[aria-label="退出登录"]').trigger('click')
  expect(wrapper.emitted('logout')).toHaveLength(2)
})

it('退出处理中禁用个人中心的退出入口，避免重复退出', async () => {
  const { wrapper } = setup()
  await flushPromises()
  await wrapper.setProps({ logoutBusy: true })
  const logout = wrapper.get('button[aria-label="退出登录"]')
  expect(logout.attributes('disabled')).toBeDefined()
  await logout.trigger('click')
  expect(wrapper.emitted('logout')).toBeUndefined()
  await wrapper.setProps({ logoutBusy: false })
  await logout.trigger('click')
  expect(wrapper.emitted('logout')).toHaveLength(1)
})

it('合法零余额展示为0金币，昵称使用纯文本', async () => {
  const { wrapper } = setup(async () => json({ ...info, nickname: '<img src=x onerror=alert(1)>', balance_gold: '0' }))
  await flushPromises()
  expect(wrapper.findAll('dd').at(-1)?.text()).toBe('0 金币')
  expect(wrapper.text()).toContain('<img src=x onerror=alert(1)>')
  expect(wrapper.find('img[src="x"]').exists()).toBe(false)
})

it('刷新等待期间不能重复请求，完成后展示新余额', async () => {
  let resolve!: (response: Response) => void
  const pending = new Promise<Response>(done => { resolve = done })
  let calls = 0
  const { wrapper, fetcher } = setup(async () => ++calls === 1 ? json(info) : pending)
  await flushPromises()
  const refresh = wrapper.get('button[aria-label="刷新个人信息"]')
  await refresh.trigger('click')
  await refresh.trigger('click')
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(refresh.attributes('disabled')).toBeDefined()
  resolve(json({ ...info, balance_gold: '12000' }))
  await flushPromises()
  expect(wrapper.text()).toContain('12,000')
  expect(refresh.attributes('disabled')).toBeUndefined()
})

it('401清除旧资料并通知上层登录过期', async () => {
  let calls = 0
  const { wrapper } = setup(async () => ++calls === 1 ? json(info) : json({ error: { code: 'AUTH_REQUIRED', message: '登录过期' } }, 401))
  await flushPromises()
  await wrapper.get('button[aria-label="刷新个人信息"]').trigger('click')
  await flushPromises()
  expect(wrapper.emitted('expired')).toHaveLength(1)
  expect(wrapper.text()).not.toContain(info.role_id)
})

it('网络失败清旧资料并保留重试入口，不暴露异常文本', async () => {
  let calls = 0
  const { wrapper } = setup(async () => {
    if (++calls === 1) return json(info)
    throw new Error('synthetic-private-error')
  })
  await flushPromises()
  await wrapper.get('button[aria-label="刷新个人信息"]').trigger('click')
  await flushPromises()
  expect(wrapper.text()).not.toContain(info.role_id)
  expect(wrapper.text()).not.toContain('synthetic-private-error')
  expect(wrapper.get('[role="alert"]').text()).toContain('无法连接服务')
  expect(wrapper.get('button[aria-label="刷新个人信息"]').attributes('disabled')).toBeUndefined()
})

it('离开页面取消请求，晚到401不影响之后的会话', async () => {
  let resolve!: (response: Response) => void
  const pending = new Promise<Response>(done => { resolve = done })
  const { wrapper, fetcher } = setup(() => pending)
  await flushPromises()
  const signal = (fetcher.mock.calls[0]?.[1] as RequestInit).signal!
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  resolve(json({ error: { code: 'AUTH_REQUIRED', message: '过期' } }, 401))
  await flushPromises()
  expect(wrapper.emitted('expired')).toBeUndefined()
})
