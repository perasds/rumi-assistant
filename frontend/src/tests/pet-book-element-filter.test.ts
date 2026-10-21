import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import PetBookView from '../views/PetBookView.vue'
import type { Pet, PetCatalog } from '../api/types'

const fire = { id: 3, name: '火' }
const water = { id: 2, name: '水' }
const grass = { id: 4, name: '草' }
const dragon = { id: 9, name: '龙' }
const items = [
  { pet_id: '1001', name: '火龙甲', display_name: '火龙甲', name_verified: true, image_url: null, elements: [fire, dragon] },
  { pet_id: '1002', name: '草兔', display_name: '草兔', name_verified: true, image_url: null, elements: [grass] },
  { pet_id: '1003', name: '水龙乙', display_name: '水龙乙', name_verified: true, image_url: null, elements: [water, dragon] },
  { pet_id: '1004', name: null, display_name: '未知记录', name_verified: false, image_url: null, elements: [] },
  { pet_id: '1005', name: null, display_name: '旧目录记录', name_verified: false, image_url: null },
]
const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.unstubAllGlobals()
})

async function setup(pets: Pet[] = items) {
  const catalog: PetCatalog = { items: pets, total: pets.length, named_count: 3, image_count: 0 }
  const fetcher = vi.fn((path: string) => {
    if (path !== '/api/pets') throw new Error(`Unexpected synthetic request: ${path}`)
    return Promise.resolve(new Response(JSON.stringify(catalog), { headers: { 'Content-Type': 'application/json' } }))
  })
  vi.stubGlobal('fetch', fetcher)
  const wrapper = mount(PetBookView, { props: { active: true } })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, fetcher, ids: () => wrapper.findAll('.pet-card').map(card => card.attributes('data-pet-id')) }
}

it('默认全部并按目录倒序展示，元素去重按游戏ID排序且只加载一次目录', async () => {
  const { wrapper, fetcher, ids } = await setup()
  const select = wrapper.get('select[aria-label="筛选噜咪属性"]')
  expect(select.findAll('option').map(option => option.text())).toEqual(['全部属性', '水', '火', '草', '龙'])
  expect((select.element as HTMLSelectElement).value).toBe('')
  expect(ids()).toEqual(['1005', '1004', '1003', '1002', '1001'])
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(fetcher).toHaveBeenCalledWith('/api/pets', expect.anything())
})

it('一次只筛选一个元素，双属性包含该元素即可匹配，恢复全部后保留缺失属性记录', async () => {
  const { wrapper, ids, fetcher } = await setup()
  const select = wrapper.get('select[aria-label="筛选噜咪属性"]')
  await select.setValue('3')
  expect(ids()).toEqual(['1001'])
  await select.setValue('9')
  expect(ids()).toEqual(['1003', '1001'])
  expect((select.element as HTMLSelectElement).selectedOptions).toHaveLength(1)
  await select.setValue('')
  expect(ids()).toEqual(['1005', '1004', '1003', '1002', '1001'])
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('属性和名称或ID共同筛选，无匹配显示空态，离开再返回保留选择而不重新查询', async () => {
  const { wrapper, ids, fetcher } = await setup()
  const select = wrapper.get('select[aria-label="筛选噜咪属性"]')
  const search = wrapper.get('input[type="search"]')
  await select.setValue('3')
  await search.setValue('水')
  expect(ids()).toEqual([])
  expect(wrapper.text()).toContain('没有匹配的噜咪')
  await select.setValue('9')
  expect(ids()).toEqual(['1003'])
  await search.setValue('1001')
  expect(ids()).toEqual(['1001'])
  await search.setValue('')
  expect(ids()).toEqual(['1003', '1001'])
  await wrapper.setProps({ active: false })
  await wrapper.setProps({ active: true })
  await flushPromises()
  expect((select.element as HTMLSelectElement).value).toBe('9')
  expect(ids()).toEqual(['1003', '1001'])
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('旧目录没有元素字段时仍展示全部，不猜测属性也不请求每只宠物的详情', async () => {
  const { wrapper, ids, fetcher } = await setup(items.map(({ elements: _elements, ...pet }) => pet))
  const select = wrapper.get('select[aria-label="筛选噜咪属性"]')
  expect(select.findAll('option').map(option => option.text())).toEqual(['全部属性'])
  expect(select.attributes('disabled')).toBeDefined()
  expect(ids()).toEqual(['1005', '1004', '1003', '1002', '1001'])
  expect(fetcher).toHaveBeenCalledTimes(1)
})
