export function orderState(overrides: Record<string, unknown> = {}) {
  return {
    ready: true, revision: 'synthetic-revision-1', observed_at: '2026-10-18T10:00:00Z',
    server_time_seconds: 1800000000, message: null,
    orders: [{ slot_id: '18446744073709551614', is_unlock: true, order_type: 1,
      materials: [{ item_id: '9007199254740993', name: '合成木头', count: '9007199254740993', available: '9007199254740992', shortage: '1' }],
      rewards: [{ item_id: '301', name: '罐头', count: '100' }], refresh_time: null, refresh_seconds: null, can_complete: false }],
    inventory_ready: true, production_ready: true, buildings: [],
    settings: { horizon_hours: 24, max_orders: 100, reserves: {}, allow_production: false, allow_refresh: false },
    task: { status: 'idle', running: false, completed_orders: 0, canned_received: '0', message: null },
    execution_ready: true, execution_reason: null, ...overrides,
  }
}
export function orderPlan(overrides: Record<string, unknown> = {}) {
  return {
    revision: 'synthetic-revision-1',
    inventory_selection: { slot_ids: ['18446744073709551614'], count: '1', canned_count: '100', optimal: true, reason: null },
    materials: [{ item_id: '9007199254740993', name: '合成木头', required: '9007199254740993', available: '9007199254740992', shortage: '1' }],
    production: [{ building_uid: '18446744073709551613', building_id: '100', recipe_id: '200', item_id: '9007199254740993',
      name: '合成木头', count: '1', time_source: 'unknown', duration_seconds: null, ready_at_server_seconds: null, blocked_reason: '建筑速度未同步' }],
    predicted_count: '1', predicted_canned_count: '100', warnings: ['合成预测，不代表实得'], ...overrides,
  }
}
