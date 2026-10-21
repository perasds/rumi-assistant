import { describe, expect, it } from 'vitest'
import {
  effectiveRushQuantityMode,
  rushPurchaseDraft,
  validateRushPurchaseDraft,
  type RushPurchaseDraft,
  type RushPurchaseSettings,
} from '../utils/rushPurchaseSettings'

const allowedPetIds = new Set(['1000101', '1000201'])
const validDraft = (): RushPurchaseDraft => ({
  petIds: [], priceWan: '12.3456', quantity: '2', minBalanceGold: '10000',
})

describe('抢购设置校验', () => {
  it('缺省模式延续旧规则，显式总数量不因已选噜咪改变', () => {
    expect(effectiveRushQuantityMode({ petIds: [] })).toBe('total')
    expect(effectiveRushQuantityMode({ petIds: ['1000101'] })).toBe('each_once')
    expect(effectiveRushQuantityMode({ petIds: ['1000101'], quantityMode: 'total' })).toBe('total')
  })

  it('显式总数量模式支持在指定噜咪范围内合计购买N只', () => {
    expect(validateRushPurchaseDraft({ ...validDraft(), petIds: ['1000101', '1000201'], quantityMode: 'total', quantity: '3' }, allowedPetIds)).toEqual({
      settings: { petIds: ['1000101', '1000201'], maxPriceGold: 123456, quantityMode: 'total', quantityPerPet: 3, minBalanceGold: 10000 }, errors: {},
    })
  })

  it('自定义模式保存每只不同目标并复制映射，不读取隐藏总数量', () => {
    const draft: RushPurchaseDraft = { ...validDraft(), petIds: ['1000101', '1000201'], quantityMode: 'custom', quantity: 'invalid',
      petQuantities: { '1000101': '2', '1000201': '3' } }
    const result = validateRushPurchaseDraft(draft, allowedPetIds)
    expect(result).toEqual({ settings: { petIds: ['1000101', '1000201'], maxPriceGold: 123456, quantityMode: 'custom',
      quantityPerPet: 1, petQuantities: { '1000101': 2, '1000201': 3 }, minBalanceGold: 10000 }, errors: {} })
    draft.petQuantities!['1000101'] = '9'
    expect(result.settings?.petQuantities?.['1000101']).toBe(2)
  })

  it('显式每种一次固定为1，不带切换模式前的自定义草稿', () => {
    const result = validateRushPurchaseDraft({ ...validDraft(), petIds: ['1000101'], quantityMode: 'each_once', quantity: '7',
      petQuantities: { '1000101': '3' } }, allowedPetIds)
    expect(result.settings).toEqual({ petIds: ['1000101'], maxPriceGold: 123456, quantityMode: 'each_once', quantityPerPet: 1, minBalanceGold: 10000 })
  })

  it.each(['each_once', 'custom'] as const)('%s需要指定噜咪，不能意外放宽到所有噜咪', quantityMode => {
    const result = validateRushPurchaseDraft({ ...validDraft(), quantityMode, petQuantities: {} }, allowedPetIds)
    expect(result.settings).toBeNull()
    expect(result.errors.petIds).toBeTruthy()
  })

  it.each([
    undefined, {}, { '1000101': '2' }, { '1000101': '2', '1000201': '3', unknown: '1' },
    { '1000101': '0', '1000201': '1' }, { '1000101': '1.5', '1000201': '1' },
    { '1000101': '9007199254740992', '1000201': '1' },
    { '1000101': String(Number.MAX_SAFE_INTEGER), '1000201': '1' },
  ])('自定义目标缺失、未知、多余、非正整数或合计溢出时拒绝 %j', petQuantities => {
    const result = validateRushPurchaseDraft({ ...validDraft(), petIds: ['1000101', '1000201'], quantityMode: 'custom',
      petQuantities: petQuantities as Record<string, string> | undefined }, allowedPetIds)
    expect(result.settings).toBeNull()
    expect(result.errors.petQuantities).toBeTruthy()
  })

  it('拒绝未知数量模式', () => {
    const result = validateRushPurchaseDraft({ ...validDraft(), quantityMode: 'unknown' } as unknown as RushPurchaseDraft, allowedPetIds)
    expect(result.settings).toBeNull()
    expect(result.errors.quantityMode).toBeTruthy()
  })

  it('将万精确换为整数金币，无指定时保存购买总数量', () => {
    expect(validateRushPurchaseDraft(validDraft(), allowedPetIds)).toEqual({
      settings: { petIds: [], maxPriceGold: 123456, quantityPerPet: 2, minBalanceGold: 10000 },
      errors: {},
    })
  })

  it('多选去重并保持顺序，保存副本不受表单修改影响', () => {
    const draft = { ...validDraft(), petIds: ['1000201', '1000101', '1000201'] }
    const result = validateRushPurchaseDraft(draft, allowedPetIds)
    expect(result.settings?.petIds).toEqual(['1000201', '1000101'])
    expect(result.settings?.quantityPerPet).toBe(1)
    expect(draft.petIds).toEqual(['1000201', '1000101', '1000201'])
    draft.petIds.push('unverified')
    expect(result.settings?.petIds).toEqual(['1000201', '1000101'])
    expect([...allowedPetIds]).toEqual(['1000101', '1000201'])
  })

  it('空选择表示全部彩色噜咪，不依赖已核实目录且保存独立副本', () => {
    const draft = { ...validDraft(), petIds: [] as string[] }
    const result = validateRushPurchaseDraft(draft, new Set())
    expect(result).toEqual({ settings: { petIds: [], maxPriceGold: 123456, quantityPerPet: 2, minBalanceGold: 10000 }, errors: {} })
    draft.petIds.push('1000101')
    expect(result.settings?.petIds).toEqual([])
  })

  it.each([
    { petIds: ['unknown'] }, { petIds: ['1000101', 'unknown'] }, { petIds: [''] },
  ])('非空选择拒绝未核实的噜咪 $petIds', ({ petIds }) => {
    const result = validateRushPurchaseDraft({ ...validDraft(), petIds }, allowedPetIds)
    expect(result.settings).toBeNull()
    expect(result.errors.petIds).toBeTruthy()
  })

  it.each([
    ['0.0001', 1], ['0.3', 3000], ['12.3400', 123400], [' 0001.0001 ', 10001],
    ['900719925474.0991', Number.MAX_SAFE_INTEGER],
  ])('精确接受价格 %s 万', (priceWan, maxPriceGold) => {
    const result = validateRushPurchaseDraft({ ...validDraft(), priceWan }, allowedPetIds)
    expect(result.errors).toEqual({})
    expect(result.settings?.maxPriceGold).toBe(maxPriceGold)
  })

  it.each(['', '0', '0.0000', '-1', '+1', '1e3', 'NaN', 'Infinity', '1.00001', '1,000', '1.2.3', '900719925474.0992'])('拒绝无效或超范围价格 %s', priceWan => {
    const result = validateRushPurchaseDraft({ ...validDraft(), priceWan }, allowedPetIds)
    expect(result.settings).toBeNull()
    expect(result.errors.priceWan).toBeTruthy()
  })

  it.each(['1', ' 0002 ', String(Number.MAX_SAFE_INTEGER)])('无指定时接受正安全整数总数量 %s', quantity => {
    const result = validateRushPurchaseDraft({ ...validDraft(), quantity }, allowedPetIds)
    expect(result.errors).toEqual({})
    expect(result.settings?.quantityPerPet).toBe(Number(quantity))
  })

  it.each(['', '0', '-1', '1.5', '+1', '1e2', 'NaN', 'Infinity', '9007199254740992'])('拒绝无效数量 %s', quantity => {
    const result = validateRushPurchaseDraft({ ...validDraft(), quantity }, allowedPetIds)
    expect(result.settings).toBeNull()
    expect(result.errors.quantity).toBeTruthy()
  })

  it.each(['0', ' 0002 ', String(Number.MAX_SAFE_INTEGER)])('接受非负安全整数金币下限 %s', minBalanceGold => {
    const result = validateRushPurchaseDraft({ ...validDraft(), minBalanceGold }, allowedPetIds)
    expect(result.errors).toEqual({})
    expect(result.settings?.minBalanceGold).toBe(Number(minBalanceGold))
  })

  it.each(['', '-1', '1.5', '+1', '1e2', 'NaN', 'Infinity', '9007199254740992'])('拒绝无效金币下限 %s', minBalanceGold => {
    const result = validateRushPurchaseDraft({ ...validDraft(), minBalanceGold }, allowedPetIds)
    expect(result.settings).toBeNull()
    expect(result.errors.minBalanceGold).toBeTruthy()
  })

  it.each(['', '0', '2', '999', '1.5', 'NaN'])('指定噜咪时忽略隐藏数量草稿%s并固定每种1只', quantity => {
    const result = validateRushPurchaseDraft({ ...validDraft(), petIds: ['1000101', '1000201'], quantity }, allowedPetIds)
    expect(result.errors).toEqual({})
    expect(result.settings?.quantityPerPet).toBe(1)
  })

  it('一次返回本模式全部字段错误，不产生部分可用设置', () => {
    const draft = { petIds: [], priceWan: '0', quantity: '0', minBalanceGold: '-1' }
    const result = validateRushPurchaseDraft(draft, allowedPetIds)
    expect(result.settings).toBeNull()
    expect(Object.keys(result.errors).sort()).toEqual(['minBalanceGold', 'priceWan', 'quantity'])
  })
})

describe('抢购设置表单回填', () => {
  it('回填指定池总数量时保留目标数量和显式模式', () => {
    const settings: RushPurchaseSettings = { petIds: ['1000101'], maxPriceGold: 123456, quantityMode: 'total', quantityPerPet: 7, minBalanceGold: 10000 }
    const draft = rushPurchaseDraft(settings)
    expect(draft.quantity).toBe('7')
    expect(draft.quantityMode).toBe('total')
    expect(validateRushPurchaseDraft(draft, allowedPetIds).settings).toEqual(settings)
  })

  it('回填自定义数量为独立字符串映射', () => {
    const settings: RushPurchaseSettings = { petIds: ['1000101', '1000201'], maxPriceGold: 123456, quantityMode: 'custom', quantityPerPet: 1,
      petQuantities: { '1000101': 2, '1000201': 3 }, minBalanceGold: 10000 }
    const draft = rushPurchaseDraft(settings)
    expect(draft.petQuantities).toEqual({ '1000101': '2', '1000201': '3' })
    expect(validateRushPurchaseDraft(draft, allowedPetIds).settings).toEqual(settings)
    draft.petQuantities!['1000101'] = '9'
    expect(settings.petQuantities?.['1000101']).toBe(2)
  })

  it('无设置时使用独立默认草稿', () => {
    const draft = rushPurchaseDraft(null)
    expect(draft).toEqual({ petIds: [], currentSeasonOnly: false, priceWan: '', quantity: '1', minBalanceGold: '0' })
    draft.petIds.push('test-only')
    expect(rushPurchaseDraft(null).petIds).toEqual([])
  })

  it.each([
    [1, '0.0001'], [3000, '0.3'], [123400, '12.34'], [10000, '1'], [Number.MAX_SAFE_INTEGER, '900719925474.0991'],
  ])('金币 %s 回填并重新校验无精度损失', (maxPriceGold, priceWan) => {
    const settings: RushPurchaseSettings = { petIds: [], maxPriceGold, quantityPerPet: 3, minBalanceGold: Number.MAX_SAFE_INTEGER }
    const draft = rushPurchaseDraft(settings)
    expect(draft).toEqual({ petIds: [], currentSeasonOnly: false, priceWan, quantity: '3', minBalanceGold: String(Number.MAX_SAFE_INTEGER) })
    expect(validateRushPurchaseDraft(draft, allowedPetIds).settings).toEqual(settings)
    draft.petIds.push('1000201')
    expect(settings.petIds).toEqual([])
  })

  it('恢复旧指定模式时将旧每种数量归一为1，且不修改原设置', () => {
    const settings = { petIds: ['1000101'], maxPriceGold: 123456, quantityPerPet: 7, minBalanceGold: 10000 }
    const draft = rushPurchaseDraft(settings)
    expect(draft.quantity).toBe('1')
    expect(validateRushPurchaseDraft(draft, allowedPetIds).settings?.quantityPerPet).toBe(1)
    expect(settings.quantityPerPet).toBe(7)
  })
})
