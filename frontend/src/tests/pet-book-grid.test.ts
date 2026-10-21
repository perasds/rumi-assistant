import { mount } from '@vue/test-utils'
import { afterEach, expect, it } from 'vitest'
import PetBookGrid from '../components/pet-book/PetBookGrid.vue'
import type { Pet } from '../api/types'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))

const pet: Pet = {
  pet_id: '1000101', name: '蜜桃', display_name: '蜜桃', name_verified: true, image_url: '/test-peach.png',
}

function setup(imageUrl: string | null = pet.image_url) {
  const value = { ...pet, image_url: imageUrl }
  const wrapper = mount(PetBookGrid, { props: { pets: [value] } })
  wrappers.push(wrapper)
  return { wrapper, value }
}

it('图鉴头像使用原生懒加载和异步解码，并预留正方形尺寸', () => {
  const { wrapper } = setup()
  const image = wrapper.get('img')
  expect(image.attributes('loading')).toBe('lazy')
  expect(image.attributes('decoding')).toBe('async')
  expect(image.attributes('src')).toBe('/test-peach.png')
  expect(image.attributes('alt')).toBe('蜜桃')
  expect(image.attributes('width')).toBe('100')
  expect(image.attributes('height')).toBe('100')
  expect(wrapper.find('.pet-image').exists()).toBe(true)
})

it.each([null, ''])('缺少头像地址 %s 时只显示占位，不创建图片请求', imageUrl => {
  const { wrapper } = setup(imageUrl)
  expect(wrapper.find('img').exists()).toBe(false)
  expect(wrapper.get('.missing-image').text()).toBe('?')
  expect(wrapper.get('.missing-image').attributes('aria-label')).toBe('蜜桃暂无图片')
})

it('头像加载失败后保留占位，卡片仍可打开详情', async () => {
  const { wrapper, value } = setup()
  await wrapper.get('img').trigger('error')
  expect(wrapper.find('img').exists()).toBe(false)
  expect(wrapper.get('.missing-image').text()).toBe('?')
  await wrapper.get('button').trigger('click')
  expect(wrapper.emitted('select')).toEqual([[value]])
})

it('同一只噜咪换成新头像地址后可以从加载失败恢复', async () => {
  const { wrapper } = setup()
  await wrapper.get('img').trigger('error')
  await wrapper.setProps({ pets: [{ ...pet, image_url: '/replacement-peach.png' }] })
  expect(wrapper.find('.missing-image').exists()).toBe(false)
  expect(wrapper.get('img').attributes('src')).toBe('/replacement-peach.png')
  expect(wrapper.get('img').attributes('loading')).toBe('lazy')
})

it('从缺图记录更新到有效地址时恢复头像', async () => {
  const { wrapper } = setup(null)
  await wrapper.setProps({ pets: [pet] })
  expect(wrapper.find('.missing-image').exists()).toBe(false)
  expect(wrapper.get('img').attributes('src')).toBe('/test-peach.png')
})

it('懒加载头像不改变目录顺序、名称和卡片选择事件', async () => {
  const second = { ...pet, pet_id: '9000001', display_name: '噜咪 #9000001', image_url: null }
  const wrapper = mount(PetBookGrid, { props: { pets: [second, pet] } })
  wrappers.push(wrapper)
  const cards = wrapper.findAll('button')
  expect(cards.map(card => card.attributes('data-pet-id'))).toEqual(['9000001', '1000101'])
  expect(cards[1]!.text()).toContain('蜜桃')
  await cards[1]!.trigger('click')
  expect(wrapper.emitted('select')).toEqual([[pet]])
})
