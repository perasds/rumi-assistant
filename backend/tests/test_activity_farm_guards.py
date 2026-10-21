"""Activity farm type stays read-only even without an actFarm submessage."""

import pytest

from rumi_market.home_management import HomeManagementService
from rumi_market.order_replenishment import choose_replenishment
from rumi_market.order_service import OrderService
from rumi_market.work_inspection import inspect_workplaces
from test_home_operations import expected, setup as native_setup
from test_order_service import raw as order_raw


WORKERS = {10101: 102501, 10125: 100501, 10130: 114301}


def fixture(building_id, farm_flag):
    operations, client, state = native_setup()
    service = OrderService(None)
    # Isolate architecture-type guards from the separate automatic defaults.
    service.auto_production_exclusions = frozenset()
    config = service.config.building(building_id)
    level = service.config.level(building_id, 1)
    row = state.data['architectures'][0]
    row.update(id=building_id, capacity_remaining=level['capacity'], store_items=[])
    row['work_old'].update(lumi_id=WORKERS[building_id], recipe_id=0,
        recipe_state=0, lumi_work_energy=1000)
    if farm_flag == 'absent':
        row.pop('has_farm_data', None)
    else:
        row['has_farm_data'] = False
    state.data['workers'][0].update(id=WORKERS[building_id], raw_state='0', work_energy=1000)
    assert config['work_type_id'] in service.config.work_types(WORKERS[building_id])
    recipe = next(value for value in service.catalog['recipes']
                  if value['building_id'] == str(building_id) and value['recipe_id'] == level['recipe_ids'][0])
    raw = order_raw()
    raw['home'] = state.snapshot()
    raw['inventory']['items'] = {item['item_id']: item['count'] for item in recipe['materials']}
    raw['orders']['orders'] = [{'slot_id': '1', 'is_unlock': True, 'order_type': 1,
        'materials': recipe['products'], 'rewards': []}]
    return operations, client, state, service, raw, recipe


@pytest.mark.parametrize('building_id', [10101, 10125, 10130])
@pytest.mark.parametrize('farm_flag', ['absent', False])
def test_order_and_inspection_never_schedule_activity_farms_without_farm_submessage(building_id, farm_flag):
    _, _, _, service, raw, _ = fixture(building_id, farm_flag)
    snapshot = service._snapshot(raw)
    building, = snapshot['buildings']
    # The worker and inputs are valid; the building type is the deciding guard.
    assert building['worker_valid'] is True
    allowed = building_id == 10101
    assert building['editable'] is allowed
    assert building['production_editable'] is allowed
    assert building['reason'] == (None if allowed else 'production_branch_unverified')
    proposal = choose_replenishment(snapshot, service.catalog, {}, batch_limit=1)
    assert (proposal is not None) is allowed
    inspected, = inspect_workplaces(snapshot, service.catalog, {})
    assert (inspected['proposal'] is not None) is allowed


@pytest.mark.parametrize('building_id', [10101, 10125, 10130])
@pytest.mark.parametrize('farm_flag', ['absent', False])
def test_home_management_marks_activity_farm_type_read_only(building_id, farm_flag):
    _, _, state, _, _, _ = fixture(building_id, farm_flag)
    service = HomeManagementService(None, None)
    view = service._building(state.data['architectures'][0])
    allowed = building_id == 10101
    assert view.editable is allowed and view.production_editable is allowed
    assert view.reason == (None if allowed else '活动农田暂只支持查看')
    assert view.production_reason == view.reason


@pytest.mark.asyncio
@pytest.mark.parametrize('building_id', [10101, 10125, 10130])
@pytest.mark.parametrize('farm_flag', ['absent', False])
async def test_native_home_operations_reject_activity_farm_type_before_any_write(building_id, farm_flag):
    operations, client, state, _, _, recipe = fixture(building_id, farm_flag)
    result = await operations.apply({'building_uid': 123, 'building_id': building_id,
        'recipe_id': int(recipe['recipe_id']), 'recipe_times': 1}, expected=expected(state.data))
    if building_id == 10101:
        assert result['status'] == 'applied'
        assert client.calls == [(3403, 3404, {'archi_uid': 123, 'archi_id': 10101,
            'recipe_id': 1, 'recipe_times': 1, 'op': 1})]
    else:
        assert result['status'] == 'rejected'
        assert client.calls == []
        assert not operations.blocked
