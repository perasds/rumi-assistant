import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import RushPurchaseStatus from '../components/market/RushPurchaseStatus.vue'

const baseSettings = { pet_ids: ['101', '102'], max_price_gold: 3000000, quantity_per_pet: 5, min_balance_gold: 0 }
const baseState = { job_id: 'test-only', status: 'running' as const, settings: baseSettings, balance_gold: '10000000',
  counts: { '101': 2, '102': 1 }, spent_gold: '300', attempts: 3, updated_at: null, message: null }
const catalog = { items: [
  { pet_id: '101', name: '形态A', display_name: '形态A', name_verified: true, image_url: null },
  { pet_id: '102', name: '形态B', display_name: '形态B', name_verified: true, image_url: null },
], total: 2, named_count: 2, image_count: 0 }

it('选中池合计模式显示共同总目标，不误显示每种各一只', () => {
  const wrapper = mount(RushPurchaseStatus, { props: { state: { ...baseState,
    settings: { ...baseSettings, quantity_mode: 'total' as const } }, catalog, error: null, checking: false } })
  expect(wrapper.text()).toContain('指定 2 种')
  expect(wrapper.text()).toContain('已购买 3 / 5 只')
  expect(wrapper.text()).not.toContain('已完成')
  expect(wrapper.get('.pet-progress').text()).not.toContain('/ 1')
  wrapper.unmount()
})

it('独立目标分别显示实际配额和完成种数', () => {
  const wrapper = mount(RushPurchaseStatus, { props: { state: { ...baseState,
    settings: { ...baseSettings, quantity_mode: 'custom' as const, pet_quantities: { '101': 2, '102': 3 } } },
    catalog, error: null, checking: false } })
  expect(wrapper.text()).toContain('已完成 1 / 2 种')
  expect(wrapper.findAll('.pet-progress li').map(row => row.text())).toEqual(['形态A2 / 2', '形态B1 / 3'])
  wrapper.unmount()
})
