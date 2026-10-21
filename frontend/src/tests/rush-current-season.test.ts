import { effectScope } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { closeToast } from 'vant'
import type { RushPurchaseState, Session } from '../api/types'
import RushPurchaseSettingsDialog from '../components/market/RushPurchaseSettingsDialog.vue'
import RushPurchaseStatus from '../components/market/RushPurchaseStatus.vue'
import { useRushPurchase } from '../composables/useRushPurchase'
import { rushPurchaseDraft, validateRushPurchaseDraft } from '../utils/rushPurchaseSettings'
import MarketView from '../views/MarketView.vue'

const wrappers: ReturnType<typeof mount>[] = []
const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  scopes.splice(0).forEach(scope => scope.stop())
  closeToast(true)
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const catalog = { items: [
  { pet_id: '101', name: '蜜桃', display_name: '蜜桃', name_verified: true, image_url: null },
], total: 1, named_count: 1, image_count: 0 }
const settings = { petIds: ['101'], maxPriceGold: 120000, quantityPerPet: 4, minBalanceGold: 0 }
const seasonSettings = { pet_ids: [], max_price_gold: 120000, quantity_per_pet: 4, min_balance_gold: 0,
  quantity_mode: 'total' as const, current_season_only: true }
const idle = (): RushPurchaseState => ({ job_id: null, status: 'idle', settings: null, balance_gold: null,
  counts: {}, spent_gold: '0', message: null, attempts: 0, updated_at: null,
  season_ready: true, current_season_id: 2, season_message: null })
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
function rushScope() {
  const scope = effectScope()
  scopes.push(scope)
  return scope.run(() => useRushPurchase({ onUnauthorized: vi.fn() }))!
}

it('当前赛季模式清空遗留选择和独立目标，只保存购买总数量', () => {
  const result = validateRushPurchaseDraft({ petIds: ['unknown'], priceWan: '12', quantity: '4', minBalanceGold: '0',
    currentSeasonOnly: true, quantityMode: 'custom', petQuantities: { unknown: '9' } }, new Set())
  expect(result).toEqual({ settings: { petIds: [], maxPriceGold: 120000, quantityPerPet: 4, minBalanceGold: 0,
    currentSeasonOnly: true, quantityMode: 'total' }, errors: {} })
})

it('新表单默认不勾选，回填当前赛季时不保留隐藏选择', () => {
  expect(rushPurchaseDraft(null).currentSeasonOnly).toBe(false)
  expect(rushPurchaseDraft({ ...settings, currentSeasonOnly: true, quantityMode: 'custom', petQuantities: { '101': 9 } }))
    .toMatchObject({ currentSeasonOnly: true, petIds: [], quantityMode: 'total', quantity: '4', petQuantities: {} })
  expect(validateRushPurchaseDraft({ petIds: [], priceWan: '12', quantity: '4', minBalanceGold: '0' }, new Set()).settings)
    .not.toHaveProperty('currentSeasonOnly')
})

it('勾选后收起并禁用选择器，取消勾选不恢复已清空的自定义目标', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = mount(RushPurchaseSettingsDialog, { attachTo: document.body, props: { show: true,
    settings: { ...settings, quantityMode: 'custom', petQuantities: { '101': 9 } },
    seasonReady: true, currentSeasonId: 2,
  } })
  wrappers.push(wrapper)
  await flushPromises()
  await wrapper.get('button[aria-label="选择指定噜咪"]').trigger('click')
  expect(wrapper.find('.pet-picker').exists()).toBe(true)
  await wrapper.get('input[name="currentSeasonOnly"]').setValue(true)
  expect(wrapper.get('button[aria-label="选择指定噜咪"]').attributes('disabled')).toBeDefined()
  expect(wrapper.find('.pet-picker').exists()).toBe(false)
  expect(wrapper.find('input[name="quantityMode"]').exists()).toBe(false)
  expect(wrapper.find('input[name="petQuantity-101"]').exists()).toBe(false)
  expect(wrapper.find('input[name="quantity"]').exists()).toBe(true)
  await wrapper.get('input[name="quantity"]').setValue('4')
  expect(wrapper.text()).toContain('当前赛季 S2')
  await wrapper.get('input[name="currentSeasonOnly"]').setValue(false)
  await wrapper.get('button[aria-label="选择指定噜咪"]').trigger('click')
  expect((wrapper.get('input[data-pet-id="101"]').element as HTMLInputElement).checked).toBe(false)
  expect(wrapper.find('input[name="quantityMode"]').exists()).toBe(false)
  await wrapper.get('form').trigger('submit')
  expect(wrapper.emitted('save')?.[0]?.[0]).toEqual({ petIds: [], maxPriceGold: 120000, quantityPerPet: 4,
    minBalanceGold: 0, quantityMode: 'total' })
})

it('赛季未知时可以保存筛选，并明确显示尚未同步', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = mount(RushPurchaseSettingsDialog, { props: { show: true, settings: null, seasonReady: false, currentSeasonId: null } })
  wrappers.push(wrapper)
  await flushPromises()
  await wrapper.get('input[name="currentSeasonOnly"]').setValue(true)
  await wrapper.get('input[name="priceWan"]').setValue('12')
  await wrapper.get('input[name="quantity"]').setValue('4')
  expect(wrapper.text()).toContain('当前赛季尚未同步')
  expect(wrapper.text()).not.toContain('S1')
  await wrapper.get('form').trigger('submit')
  expect(wrapper.emitted('save')?.[0]?.[0]).toEqual({ petIds: [], maxPriceGold: 120000, quantityPerPet: 4,
    minBalanceGold: 0, currentSeasonOnly: true, quantityMode: 'total' })
})

it('当前赛季开始请求只携带总量，不泄漏旧选择和独立目标', async () => {
  const fetcher = vi.fn((path: string, _init: RequestInit) => Promise.resolve(json(path.endsWith('/start')
    ? { ...idle(), job_id: 'season-test', status: 'running', settings: seasonSettings } : idle())))
  vi.stubGlobal('fetch', fetcher)
  const rush = rushScope()
  await rush.loadState()
  await rush.start({ ...settings, currentSeasonOnly: true, quantityMode: 'custom', petQuantities: { '101': 9 } })
  const body = JSON.parse(fetcher.mock.calls.find(([path]) => path.endsWith('/start'))![1].body as string)
  expect(body).toMatchObject({ current_season_only: true, pet_ids: [], quantity_mode: 'total', quantity_per_pet: 4 })
  expect(body).not.toHaveProperty('pet_quantities')
  expect(rush.error.value).toBeNull()
})

it.each([
  { season_ready: false, current_season_id: null },
  { season_ready: undefined, current_season_id: undefined },
  { season_ready: true, current_season_id: null },
])('当前赛季未就绪时不发送开始请求 %j', async override => {
  const fetcher = vi.fn().mockResolvedValue(json({ ...idle(), ...override }))
  vi.stubGlobal('fetch', fetcher)
  const rush = rushScope()
  await rush.loadState()
  await rush.start({ ...settings, currentSeasonOnly: true })
  expect(fetcher).toHaveBeenCalledOnce()
  expect(rush.error.value).toContain('当前赛季尚未同步')
})

it.each([
  { pet_ids: ['101'] },
  { quantity_mode: 'custom', pet_quantities: { '101': 1 } },
  { quantity_mode: 'each_once' },
  { current_season_only: 'true' },
])('拒绝混合当前赛季与自定义目标的响应 %j', async override => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...idle(), status: 'running', settings: { ...seasonSettings, ...override } })))
  const rush = rushScope()
  await rush.loadState()
  expect(rush.state.value).toBeNull()
  expect(rush.canStart.value).toBe(false)
})

it('保存当前赛季设置但尚未同步时可刷新赛季状态，解禁后不自动开始', async () => {
  let seasonReady = false
  const fetcher = vi.fn((path: string) => Promise.resolve(json(path === '/api/pets' ? catalog : path === '/api/rush-purchase'
    ? { ...idle(), season_ready: seasonReady, current_season_id: seasonReady ? 2 : null,
      season_message: seasonReady ? null : '当前赛季尚未同步' }
    : { refresh_id: 'test', scope: 'top10', status: 'complete', complete: true, items: [], loaded_count: 0,
      matched_count: 0, total_count: 0, started_at: null, updated_at: null, message: null, name_coverage: { named: 1, total: 1 } })))
  vi.stubGlobal('fetch', fetcher)
  const session: Session = { status: 'connected', authenticated: true, role: null, roles: [], message: null }
  const wrapper = mount(MarketView, { props: { session } })
  wrappers.push(wrapper)
  await flushPromises()
  const dialog = wrapper.getComponent(RushPurchaseSettingsDialog)
  dialog.vm.$emit('save', { ...settings, petIds: [], currentSeasonOnly: true, quantityMode: 'total' })
  await flushPromises()
  expect(wrapper.get('button[aria-label="开始抢购"]').attributes('disabled')).toBeDefined()
  expect(wrapper.text()).toContain('当前赛季尚未同步')
  expect(dialog.props('currentSeasonId')).toBeNull()
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
  seasonReady = true
  await wrapper.get('button[aria-label="刷新赛季状态"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('button[aria-label="开始抢购"]').attributes('disabled')).toBeUndefined()
  expect(wrapper.text()).not.toContain('当前赛季尚未同步')
  expect(fetcher.mock.calls.filter(([path]) => path === '/api/rush-purchase')).toHaveLength(2)
  expect(fetcher.mock.calls.some(([path]) => path.endsWith('/start'))).toBe(false)
})

it('当前赛季进度显示总购买数量，不误称全部彩色噜咪', () => {
  const wrapper = mount(RushPurchaseStatus, { props: { state: { ...idle(), status: 'stopped', settings: seasonSettings, target_season_id: 1,
    counts: { '101': 3 } }, catalog, error: null, checking: false } })
  wrappers.push(wrapper)
  expect(wrapper.text()).toContain('仅当前赛季')
  expect(wrapper.text()).toContain('S1')
  expect(wrapper.text()).not.toContain('S2')
  expect(wrapper.text()).toContain('已购买 3 / 4 只')
  expect(wrapper.text()).not.toContain('全部彩色噜咪')
})
