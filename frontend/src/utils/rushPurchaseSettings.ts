import type { RushQuantityMode } from '../api/types'
export type { RushQuantityMode } from '../api/types'

export interface RushPurchaseSettings {
  petIds: string[]
  currentSeasonOnly?: boolean
  maxPriceGold: number
  // 兼容旧字段名：total 为总数，each_once/custom 为兼容占位1。
  quantityPerPet: number
  quantityMode?: RushQuantityMode
  petQuantities?: Record<string, number>
  minBalanceGold: number
}

export interface RushPurchaseDraft {
  petIds: string[]
  currentSeasonOnly?: boolean
  priceWan: string
  quantity: string
  quantityMode?: RushQuantityMode
  petQuantities?: Record<string, string>
  minBalanceGold: string
}

export type RushPurchaseErrors = Partial<Record<keyof RushPurchaseDraft, string>>

export function effectiveRushQuantityMode(value: { petIds: readonly string[]; quantityMode?: RushQuantityMode | null; currentSeasonOnly?: boolean }): RushQuantityMode {
  if (value.currentSeasonOnly) return 'total'
  return value.quantityMode ?? (value.petIds.length ? 'each_once' : 'total')
}

function integerInput(value: string, minimum: number): number | null {
  const text = value.trim()
  if (!/^\d+$/.test(text)) return null
  const number = Number(text)
  return Number.isSafeInteger(number) && number >= minimum ? number : null
}

function priceGold(value: string): number | null {
  const match = /^(\d+)(?:\.(\d{1,4}))?$/.exec(value.trim())
  if (!match) return null
  // 拼接整数位与小数位，避免以浮点乘法将“万”换算成金币。
  return integerInput(`${match[1]}${(match[2] ?? '').padEnd(4, '0')}`, 1)
}

export function validateRushPurchaseDraft(
  draft: RushPurchaseDraft,
  allowedPetIds: ReadonlySet<string>,
): { settings: RushPurchaseSettings | null; errors: RushPurchaseErrors } {
  const errors: RushPurchaseErrors = {}
  const petIds = draft.currentSeasonOnly ? [] : [...new Set(draft.petIds)]
  if (petIds.some(id => !allowedPetIds.has(id))) errors.petIds = '请选择已核实名称的噜咪'
  const mode = effectiveRushQuantityMode(draft)
  if (!['each_once', 'total', 'custom'].includes(mode)) errors.quantityMode = '请选择有效的购买数量模式'
  if (mode !== 'total' && !petIds.length) errors.petIds = '请先选择指定噜咪'

  const maxPriceGold = priceGold(draft.priceWan)
  if (maxPriceGold === null) errors.priceWan = '请输入大于 0 的价格，最多 4 位小数，金额不能过大'
  const quantityPerPet = mode === 'total' ? integerInput(draft.quantity, 1) : 1
  if (quantityPerPet === null) errors.quantity = '请输入大于 0 的整数数量'
  let petQuantities: Record<string, number> | undefined
  if (mode === 'custom') {
    const values = draft.petQuantities
    const entries = values && typeof values === 'object' && !Array.isArray(values) ? Object.entries(values) : []
    let total = 0
    petQuantities = {}
    if (!values || entries.length !== petIds.length || entries.some(([id]) => !petIds.includes(id))) {
      errors.petQuantities = '请为每个指定噜咪设置有效数量，不得包含其他噜咪'
    }
    for (const id of petIds) {
      const text = values?.[id]
      const count = typeof text === 'string' ? integerInput(text, 1) : null
      if (count === null || !Number.isSafeInteger(total + count)) {
        errors.petQuantities = '每个噜咪的数量须为大于 0 的整数，合计数量不能过大'
      } else {
        petQuantities[id] = count
        total += count
      }
    }
  }
  const minBalanceGold = integerInput(draft.minBalanceGold, 0)
  if (minBalanceGold === null) errors.minBalanceGold = '请输入有效的非负整数金币'

  if (Object.keys(errors).length || maxPriceGold === null || quantityPerPet === null || minBalanceGold === null) {
    return { settings: null, errors }
  }
  return { settings: { petIds, maxPriceGold, quantityPerPet, minBalanceGold,
    ...(draft.currentSeasonOnly ? { currentSeasonOnly: true, quantityMode: 'total' as const }
      : draft.quantityMode != null ? { quantityMode: draft.quantityMode } : {}),
    ...(petQuantities ? { petQuantities } : {}),
  }, errors }
}

export function rushPurchaseDraft(settings: RushPurchaseSettings | null): RushPurchaseDraft {
  if (!settings) return { petIds: [], currentSeasonOnly: false, priceWan: '', quantity: '1', minBalanceGold: '0' }
  const gold = BigInt(settings.maxPriceGold)
  const fraction = (gold % 10000n).toString().padStart(4, '0').replace(/0+$/, '')
  return {
    petIds: settings.currentSeasonOnly ? [] : [...settings.petIds],
    currentSeasonOnly: settings.currentSeasonOnly ?? false,
    priceWan: `${gold / 10000n}${fraction ? `.${fraction}` : ''}`,
    quantity: effectiveRushQuantityMode(settings) === 'total' ? String(settings.quantityPerPet) : '1',
    minBalanceGold: String(settings.minBalanceGold),
    ...(settings.currentSeasonOnly ? { quantityMode: 'total' as const, petQuantities: {} }
      : { ...(settings.quantityMode != null ? { quantityMode: settings.quantityMode } : {}),
        ...(settings.petQuantities ? { petQuantities: Object.fromEntries(Object.entries(settings.petQuantities).map(([id, count]) => [id, String(count)])) } : {}) }),
  }
}
