import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import BookApp from '../BookApp.vue'
import type { Pet, PetCatalog, PetDetail } from '../api/types'

const wrappers: ReturnType<typeof mount>[] = []
const fire = { id: 3, name: '火' }
const water = { id: 2, name: '水' }
const dragon = { id: 9, name: '龙' }
const pets: Pet[] = [
  { pet_id: '1000101', name: '火龙甲', display_name: '火龙甲', name_verified: true, image_url: '/api/pets/images/1000101.png', elements: [fire, dragon] },
  { pet_id: '1000102', name: '水龙乙', display_name: '水龙乙', name_verified: true, image_url: null, elements: [water, dragon] },
  { pet_id: '9000001', name: null, display_name: '噜咪 #9000001', name_verified: false, image_url: null, elements: [] },
]
const catalog: PetCatalog = { items: pets, total: 3, named_count: 2, image_count: 1 }
const detail: PetDetail = {
  ...pets[0]!, available: true, pokedex_id: '001', elements: [fire, dragon],
  stats: [{ key: 'hp', label: '体力', min: 70, max: 100 }],
  normal_attack: { element: fire, power: 20, energy_gain: 5, interval_seconds: 1.5 },
  traits: [{ trait_id: 'battle-1', kind: 'battle', name: '测试战斗特性', description: '特性说明' }],
  fixed_skill: { skill_id: 'fixed-1', name: '测试火焰', description: '固定技能说明', element: fire, cost_text: '3', power: 80, image_url: '/api/pets/skill-images/fixed-1.png' },
  replaceable_skills: [{ skill_id: 'pool-1', name: '测试龙息', description: '可替换技能说明', element: dragon, cost_text: '4', power: 90, image_url: null }],
  missing_fields: [],
}
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })

afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  window.history.replaceState({}, '', '/')
  vi.unstubAllGlobals()
})

async function setup(hash = '') {
  window.history.replaceState({}, '', `/${hash}`)
  const fetcher = vi.fn((path: string, init: RequestInit = {}) => {
    if (init.method !== 'GET') throw new Error(`Unexpected book request method: ${init.method}`)
    if (path === '/api/pets') return Promise.resolve(json(catalog))
    if (path === '/api/pets/1000101') return Promise.resolve(json(detail))
    throw new Error(`Unexpected book request: ${path}`)
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(BookApp, { attachTo: document.body })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, fetcher, ids: () => wrapper.findAll('.pet-card').map(card => card.attributes('data-pet-id')) }
}

it('未安装路由也能启动真实图鉴，仅请求公开目录并保留品牌', async () => {
  const { wrapper, fetcher, ids } = await setup()
  expect(wrapper.get('.brand').text()).toBe('噜咪小助手')
  expect(wrapper.get('.brand-icon').attributes('src')).toBe('/app-icon.svg')
  expect(wrapper.get('main[aria-label="噜咪图鉴"]').isVisible()).toBe(true)
  expect(ids()).toEqual(['9000001', '1000102', '1000101'])
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/pets'])
  expect(wrapper.find('nav').exists()).toBe(false)
  expect(wrapper.find('form, .login-panel, input[type="password"]').exists()).toBe(false)
  expect(wrapper.findAll('button, a').some(control => /^(登录|交易|订单|我的|红包|管理后台)$/.test(control.text()))).toBe(false)
})

it.each(['#/login', '#/trade', '#/orders/inspection', '#/mine', '#/admin', '#/unknown'])('旧地址 %s 始终只展示图鉴', async hash => {
  const { wrapper, fetcher } = await setup(hash)
  window.location.hash = '#/login?redirect=/trade'
  window.dispatchEvent(new HashChangeEvent('hashchange'))
  await flushPromises()
  expect(wrapper.get('main[aria-label="噜咪图鉴"]').isVisible()).toBe(true)
  expect(wrapper.find('nav, form, .login-panel').exists()).toBe(false)
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/pets'])
})

it('元素保持单选并与名称和ID共同筛选，无需请求单只详情', async () => {
  const { wrapper, fetcher, ids } = await setup()
  const select = wrapper.get('select[aria-label="筛选噜咪属性"]')
  const search = wrapper.get('input[type="search"]')
  expect(select.findAll('option').map(option => option.text())).toEqual(['全部属性', '水', '火', '龙'])
  await select.setValue('9')
  await search.setValue('水龙')
  expect(ids()).toEqual(['1000102'])
  await search.setValue('1000101')
  expect(ids()).toEqual(['1000101'])
  await select.setValue('2')
  expect((select.element as HTMLSelectElement).selectedOptions).toHaveLength(1)
  expect(ids()).toEqual([])
  expect(wrapper.text()).toContain('没有匹配的噜咪')
  await select.setValue('')
  await search.setValue('')
  expect(ids()).toEqual(['9000001', '1000102', '1000101'])
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/pets'])
})

it('保留目录内部滚动容器和原生图片懒加载', async () => {
  const { wrapper, fetcher } = await setup()
  const scroll = wrapper.get('section[aria-label="图鉴目录"]')
  expect(scroll.classes()).toContain('pet-book-scroll')
  expect(scroll.attributes('tabindex')).toBe('0')
  scroll.element.scrollTop = 128
  await wrapper.get('input[type="search"]').setValue('火龙')
  expect(scroll.element.scrollTop).toBe(128)
  const image = wrapper.get('.pet-card img')
  expect(image.attributes('loading')).toBe('lazy')
  expect(image.attributes('decoding')).toBe('async')
  expect(image.attributes('width')).toBe('100')
  expect(image.attributes('height')).toBe('100')
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/pets'])
})

it('点击详情和技能继续可用，仅按需读取单只公开详情', async () => {
  const { wrapper, fetcher } = await setup()
  await wrapper.get('.pet-card[data-pet-id="1000101"]').trigger('click')
  await flushPromises()
  const popup = wrapper.get('.pet-detail-popup')
  expect(popup.text()).toContain('70–100')
  const skillsTab = popup.findAll('[role="tab"]').find(tab => tab.text() === '技能')
  expect(skillsTab).toBeDefined()
  await skillsTab!.trigger('click')
  await flushPromises()
  expect(popup.get('.skill-detail').text()).toContain('固定技能说明')
  await popup.get('button[data-skill-id="pool-1"]').trigger('click')
  expect(popup.get('.skill-detail').text()).toContain('可替换技能说明')
  await popup.get('button[aria-label="关闭详情"]').trigger('click')
  expect(wrapper.getComponent({ name: 'PetDetailPopup' }).props('show')).toBe(false)
  expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/pets', '/api/pets/1000101'])
})
