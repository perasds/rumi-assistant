import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import RushPurchaseSettingsDialog from '../components/market/RushPurchaseSettingsDialog.vue'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})
const catalog = {
  items: [
    { pet_id: '1000101', name: '蜜桃', display_name: '蜜桃', name_verified: true, image_url: '/test-peach.png' },
    { pet_id: '1000201', name: '灰灰', display_name: '灰灰', name_verified: true, image_url: '/test-gray.png' },
    { pet_id: '1003401', name: '小蜜蜂蜜桃', display_name: '小蜜蜂蜜桃', name_verified: true, image_url: null },
    { pet_id: '9999999', name: null, display_name: '未核实', name_verified: false, image_url: null },
  ], total: 4, named_count: 3, image_count: 2,
}
const saved = { petIds: ['1000101'], maxPriceGold: 125000, quantityPerPet: 2, minBalanceGold: 50000 }
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

function dialog(settings: typeof saved | null = null, show = true) {
  const wrapper = mount(RushPurchaseSettingsDialog, { attachTo: document.body, props: {
    settings, show, 'onUpdate:show': (value: boolean) => { void wrapper.setProps({ show: value }) },
  } })
  wrappers.push(wrapper)
  return wrapper
}
async function expand(wrapper: ReturnType<typeof dialog>) {
  await wrapper.get('button[aria-label="选择指定噜咪"]').trigger('click')
}
async function fillNumbers(wrapper: ReturnType<typeof dialog>) {
  await wrapper.get('input[name="priceWan"]').setValue('12.3456')
  if (wrapper.find('input[name="quantity"]').exists()) await wrapper.get('input[name="quantity"]').setValue('3')
  await wrapper.get('input[name="minBalanceGold"]').setValue('50000')
}

it('仅打开时独立读取全目录，只允许选择已核实噜咪并显示头像名称ID', async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(json(catalog)))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = dialog(null, false)
  await flushPromises()
  expect(fetcher).not.toHaveBeenCalled()
  await wrapper.setProps({ show: true })
  await flushPromises()
  expect(fetcher.mock.calls[0]?.[0]).toBe('/api/pets')
  await expand(wrapper)
  expect(wrapper.findAll('input[data-pet-id]')).toHaveLength(3)
  expect(wrapper.text()).toContain('蜜桃')
  expect(wrapper.text()).toContain('1000101')
  expect(wrapper.find('img[alt="蜜桃"]').attributes('src')).toBe('/test-peach.png')
  expect(wrapper.find('input[data-pet-id="9999999"]').exists()).toBe(false)
  expect(wrapper.text()).toContain('购买总数量')
  expect(wrapper.find('.van-cell--required').exists()).toBe(false)
})

it('名称与ID仅本地筛选，多选在切换筛选后保留', async () => {
  const fetcher = vi.fn().mockResolvedValue(json(catalog))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = dialog()
  await flushPromises()
  await expand(wrapper)
  await wrapper.get('input[aria-label="搜索噜咪名称或ID"]').setValue('蜜桃')
  expect(wrapper.findAll('input[data-pet-id]')).toHaveLength(2)
  await wrapper.get('input[data-pet-id="1000101"]').setValue(true)
  await wrapper.get('input[aria-label="搜索噜咪名称或ID"]').setValue('1000201')
  expect(wrapper.findAll('input[data-pet-id]')).toHaveLength(1)
  await wrapper.get('input[data-pet-id="1000201"]').setValue(true)
  await wrapper.get('input[aria-label="搜索噜咪名称或ID"]').setValue('')
  expect((wrapper.get('input[data-pet-id="1000101"]').element as HTMLInputElement).checked).toBe(true)
  expect((wrapper.get('input[data-pet-id="1000201"]').element as HTMLInputElement).checked).toBe(true)
  expect(fetcher).toHaveBeenCalledOnce()
})

it('选择器搜索按回车不会隐式保存设置', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog(saved)
  await flushPromises()
  await expand(wrapper)
  const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
  wrapper.get('input[aria-label="搜索噜咪名称或ID"]').element.dispatchEvent(event)
  expect(event.defaultPrevented).toBe(true)
  expect(wrapper.emitted('save')).toBeUndefined()
  expect(wrapper.emitted('update:show')).toBeUndefined()
})

it('指定多个噜咪后隐藏数量，每种固定1只并关闭窗口', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog()
  await flushPromises()
  await expand(wrapper)
  await wrapper.get('input[data-pet-id="1000101"]').setValue(true)
  await wrapper.get('input[data-pet-id="1000201"]').setValue(true)
  expect((wrapper.get('input[name="quantityMode"][value="each_once"]').element as HTMLInputElement).checked).toBe(true)
  expect(wrapper.find('input[name="quantity"]').exists()).toBe(false)
  expect(wrapper.text()).toContain('每种指定噜咪购买 1 只')
  await fillNumbers(wrapper)
  await wrapper.get('button[aria-label="保存抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')).toEqual([[{
    petIds: ['1000101', '1000201'], maxPriceGold: 123456, quantityPerPet: 1, minBalanceGold: 50000,
  }]])
  expect(wrapper.emitted('update:show')?.at(-1)).toEqual([false])
})

it('选择多个形态后可以设置选中范围的合计购买数量', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog()
  await flushPromises()
  await expand(wrapper)
  await wrapper.get('input[data-pet-id="1000101"]').setValue(true)
  await wrapper.get('input[data-pet-id="1000201"]').setValue(true)
  await wrapper.get('input[name="quantityMode"][value="total"]').setValue(true)
  await fillNumbers(wrapper)
  expect((wrapper.get('input[name="quantity"]').element as HTMLInputElement).value).toBe('3')
  await wrapper.get('button[aria-label="保存抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')?.[0]?.[0]).toEqual({
    petIds: ['1000101', '1000201'], maxPriceGold: 123456, quantityPerPet: 3,
    minBalanceGold: 50000, quantityMode: 'total',
  })
})

it('独立数量支持每个所选噜咪设置不同目标，并清理取消选择的目标', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog()
  await flushPromises()
  await expand(wrapper)
  await wrapper.get('input[data-pet-id="1000101"]').setValue(true)
  await wrapper.get('input[data-pet-id="1000201"]').setValue(true)
  await wrapper.get('input[name="quantityMode"][value="custom"]').setValue(true)
  await wrapper.get('input[name="petQuantity-1000101"]').setValue('2')
  await wrapper.get('input[name="petQuantity-1000201"]').setValue('5')
  await fillNumbers(wrapper)
  await wrapper.get('input[data-pet-id="1000201"]').setValue(false)
  await wrapper.get('button[aria-label="保存抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')?.[0]?.[0]).toEqual({
    petIds: ['1000101'], maxPriceGold: 123456, quantityPerPet: 1, minBalanceGold: 50000,
    quantityMode: 'custom', petQuantities: { '1000101': 2 },
  })
})

it('取消独立数量编辑不修改已保存目标，重新打开恢复原值', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const settings = { ...saved, quantityMode: 'custom' as const, petQuantities: { '1000101': 4 } }
  const wrapper = dialog(settings)
  await flushPromises()
  await wrapper.get('input[name="petQuantity-1000101"]').setValue('9')
  await wrapper.get('button[aria-label="取消抢购设置"]').trigger('click')
  expect(settings.petQuantities).toEqual({ '1000101': 4 })
  await wrapper.setProps({ show: true })
  await flushPromises()
  expect((wrapper.get('input[name="petQuantity-1000101"]').element as HTMLInputElement).value).toBe('4')
})

it('取消丢弃草稿，重新打开复制已保存值并复用本机目录', async () => {
  const fetcher = vi.fn().mockResolvedValue(json(catalog))
  vi.stubGlobal('fetch', fetcher)
  const settings = { ...saved, petIds: [...saved.petIds] }
  const wrapper = dialog(settings)
  await flushPromises()
  await fillNumbers(wrapper)
  await expand(wrapper)
  await wrapper.get('input[data-pet-id="1000201"]').setValue(true)
  await wrapper.get('button[aria-label="取消抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')).toBeUndefined()
  expect(settings).toEqual(saved)
  await wrapper.setProps({ show: true })
  await flushPromises()
  expect((wrapper.get('input[name="priceWan"]').element as HTMLInputElement).value).toBe('12.5')
  expect(wrapper.find('input[name="quantity"]').exists()).toBe(false)
  await expand(wrapper)
  expect((wrapper.get('input[data-pet-id="1000201"]').element as HTMLInputElement).checked).toBe(false)
  expect(fetcher).toHaveBeenCalledOnce()
})

it('遮罩关闭也不会保存改动', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog(saved)
  await flushPromises()
  await fillNumbers(wrapper)
  const overlay = document.querySelector('.van-overlay')!
  expect(overlay).not.toBeNull()
  overlay.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  await flushPromises()
  expect(wrapper.emitted('update:show')?.at(-1)).toEqual([false])
  expect(wrapper.emitted('save')).toBeUndefined()
})

it('不允许非正整数数量及非法金额保存', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog()
  await flushPromises()
  await wrapper.get('input[name="priceWan"]').setValue('0')
  await wrapper.get('input[name="quantity"]').setValue('1.5')
  await wrapper.get('input[name="minBalanceGold"]').setValue('-1')
  await wrapper.get('button[aria-label="保存抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')).toBeUndefined()
  expect(wrapper.findAll('[role="alert"]').length).toBeGreaterThanOrEqual(3)
  expect(wrapper.emitted('update:show')).toBeUndefined()
})

it('指定噜咪可不选择，保存全部彩色噜咪的购买总数量', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog()
  await flushPromises()
  expect(wrapper.text()).toContain('可选，不选则不限噜咪')
  await fillNumbers(wrapper)
  await wrapper.get('button[aria-label="保存抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')).toEqual([[{ petIds: [], maxPriceGold: 123456, quantityPerPet: 3, minBalanceGold: 50000 }]])
  expect(wrapper.emitted('update:show')?.at(-1)).toEqual([false])
})

it('目录仍在加载时，空选择不受目录请求阻止且保存后中止读取', async () => {
  let signal!: AbortSignal
  let resolve!: (value: Response) => void
  vi.stubGlobal('fetch', vi.fn((_path: string, init: RequestInit) => {
    signal = init.signal as AbortSignal
    return new Promise<Response>(accept => { resolve = accept })
  }))
  const wrapper = dialog()
  await fillNumbers(wrapper)
  expect(wrapper.text()).toContain('正在加载噜咪目录')
  expect(wrapper.get('button[aria-label="保存抢购设置"]').attributes('disabled')).toBeUndefined()
  await wrapper.get('button[aria-label="保存抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')?.[0]).toEqual([{ petIds: [], maxPriceGold: 123456, quantityPerPet: 3, minBalanceGold: 50000 }])
  expect(signal.aborted).toBe(true)
  resolve(json(catalog))
  await flushPromises()
  expect(wrapper.emitted('save')).toHaveLength(1)
})

it('目录读取失败时仍可保存空选择，不隐藏目录重试入口', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error: { code: 'TEMPORARY', message: '目录暂不可用' } }, 503)))
  const wrapper = dialog()
  await flushPromises()
  await fillNumbers(wrapper)
  expect(wrapper.text()).toContain('目录暂不可用')
  expect(wrapper.find('button[aria-label="重试加载噜咪目录"]').exists()).toBe(true)
  expect(wrapper.get('button[aria-label="保存抢购设置"]').attributes('disabled')).toBeUndefined()
  await wrapper.get('button[aria-label="保存抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')?.[0]?.[0]).toMatchObject({ petIds: [], quantityPerPet: 3 })
})

it('已有指定噜咪但目录失败时，可清空选择后保存为不限噜咪', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error: { code: 'TEMPORARY', message: '目录暂不可用' } }, 503)))
  const wrapper = dialog(saved)
  await flushPromises()
  expect(wrapper.get('button[aria-label="保存抢购设置"]').attributes('disabled')).toBeDefined()
  await wrapper.get('button[aria-label="清空指定噜咪"]').trigger('click')
  expect(wrapper.text()).toContain('可选，不选则不限噜咪')
  expect(wrapper.get('button[aria-label="保存抢购设置"]').attributes('disabled')).toBeUndefined()
  await wrapper.get('button[aria-label="保存抢购设置"]').trigger('click')
  expect(wrapper.emitted('save')).toEqual([[{ ...saved, petIds: [], quantityPerPet: 1 }]])
  expect(saved.petIds).toEqual(['1000101'])
})

it('总数量草稿切到指定模式后隐藏，清空指定后保留原总数量', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog()
  await flushPromises()
  await wrapper.get('input[name="quantity"]').setValue('7')
  await expand(wrapper)
  await wrapper.get('input[data-pet-id="1000101"]').setValue(true)
  expect(wrapper.find('input[name="quantity"]').exists()).toBe(false)
  await wrapper.get('button[aria-label="清空指定噜咪"]').trigger('click')
  expect((wrapper.get('input[name="quantity"]').element as HTMLInputElement).value).toBe('7')
})

it('旧指定模式清空选择后使用安全总数量1，不复用旧每种数量', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(catalog)))
  const wrapper = dialog({ ...saved, quantityPerPet: 9 })
  await flushPromises()
  expect(wrapper.find('input[name="quantity"]').exists()).toBe(false)
  await wrapper.get('button[aria-label="清空指定噜咪"]').trigger('click')
  expect((wrapper.get('input[name="quantity"]').element as HTMLInputElement).value).toBe('1')
})

it('目录读取失败时提供重试，成功后才能选择保存', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(json({ error: { code: 'TEMPORARY', message: '目录暂不可用' } }, 503))
    .mockResolvedValueOnce(json(catalog))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = dialog(saved)
  await flushPromises()
  expect(wrapper.text()).toContain('目录暂不可用')
  expect(wrapper.get('button[aria-label="保存抢购设置"]').attributes('disabled')).toBeDefined()
  await wrapper.get('button[aria-label="重试加载噜咪目录"]').trigger('click')
  await flushPromises()
  expect(wrapper.text()).not.toContain('目录暂不可用')
  expect(wrapper.get('button[aria-label="保存抢购设置"]').attributes('disabled')).toBeUndefined()
  expect(fetcher).toHaveBeenCalledTimes(2)
})

it('关闭中止目录请求，旧响应不能覆盖重新打开后的新目录', async () => {
  let resolveOld!: (value: Response) => void
  let oldSignal!: AbortSignal
  const oldResponse = new Promise<Response>(resolve => { resolveOld = resolve })
  const fetcher = vi.fn().mockImplementationOnce((_path: string, init: RequestInit) => {
    oldSignal = init.signal as AbortSignal
    return oldResponse
  }).mockResolvedValueOnce(json({ ...catalog, items: [catalog.items[1]] }))
  vi.stubGlobal('fetch', fetcher)
  const wrapper = dialog()
  await flushPromises()
  await wrapper.setProps({ show: false })
  expect(oldSignal.aborted).toBe(true)
  await wrapper.setProps({ show: true })
  await flushPromises()
  resolveOld(json(catalog))
  await flushPromises()
  await expand(wrapper)
  expect(wrapper.find('input[data-pet-id="1000101"]').exists()).toBe(false)
  expect(wrapper.find('input[data-pet-id="1000201"]').exists()).toBe(true)
  expect(wrapper.emitted('save')).toBeUndefined()
})

it('卸载中止目录请求，迟到响应不产生保存事件', async () => {
  let resolveOld!: (value: Response) => void
  let signal!: AbortSignal
  const pending = new Promise<Response>(resolve => { resolveOld = resolve })
  vi.stubGlobal('fetch', vi.fn((_path: string, init: RequestInit) => {
    signal = init.signal as AbortSignal
    return pending
  }))
  const wrapper = dialog()
  await flushPromises()
  wrapper.unmount()
  expect(signal.aborted).toBe(true)
  resolveOld(json(catalog))
  await flushPromises()
  expect(wrapper.emitted('save')).toBeUndefined()
})
