"""Default automatic exclusions never alter native work or manual home edits."""
from copy import deepcopy

import pytest

from rumi_market.home_management import HomeManagementService
from rumi_market.order_production_queue import QueueBook
from rumi_market.order_replenishment import _pending_products
from rumi_market.order_service import OrderRun
from test_order_bounded_scheduler import bounded_setup, close, eventually, start


EXCLUDED = ('10101', '10110', '10120')


def excluded_setup(building_id, *, bounded=True):
    service, session, provider = bounded_setup()
    provider.architecture('7')['id'] = int(building_id)
    for recipe in provider.recipes:
        if recipe['building_id'] == '1':
            recipe['building_id'] = building_id
    if not bounded:
        provider.raw['inventory'].pop('movement_epoch')
    return service, session, provider


@pytest.mark.asyncio
@pytest.mark.parametrize('building_id', EXCLUDED)
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('kind', ['orders', 'inspection'])
async def test_default_excluded_buildings_never_start_order_or_inspection_production(building_id, bounded, kind):
    service, session, provider = excluded_setup(building_id, bounded=bounded)
    try:
        state = await service.state(session)
        card = state['work_inspection']['buildings'][0]
        assert card['candidate'] is None
        assert card['message'] == '默认不安排自动生产'
        if kind == 'inspection':
            await service.start(session, 'synthetic-default-inspection', state['revision'], kind=kind)
        else:
            await start(service, session)
        observed = provider.snapshot_reads
        await eventually(lambda: provider.snapshot_reads > observed)
        assert not any(row['action'] in {'produce', 'cancel_production'} for row in provider.writes)
        if kind == 'orders':
            assert bool(session.order_task.production_book) is bounded
    finally:
        await close(service, session)


@pytest.mark.parametrize('building_id,enabled', [*[(value, False) for value in EXCLUDED], ('10102', True)])
def test_default_flag_is_separate_from_native_editability_and_manual_settings(building_id, enabled):
    service, _, provider = excluded_setup(building_id)
    original = deepcopy(provider.raw)
    building = service._buildings(provider.raw)[0]
    assert building['auto_production_enabled'] is enabled
    assert building['editable'] is True and building['reason'] is None
    assert building['blocked_reason'] is None
    manual = HomeManagementService(None, service.sessions)._building(provider.architecture('7'))
    assert manual.editable is True
    assert provider.raw == original


@pytest.mark.asyncio
@pytest.mark.parametrize('building_id', EXCLUDED)
async def test_excluded_running_work_keeps_pending_output_reservations_and_collection(building_id):
    service, session, provider = excluded_setup(building_id)
    building = provider.architecture('7')
    building['work_old'].update(recipe_id=1, recipe_state=1,
        recipe_intend_times=3, recipe_complete_times=1)
    building['store_items'] = [{'item_id': '20', 'count': 1}]
    state = await service.state(session)
    card = state['work_inspection']['buildings'][0]
    assert card['status'] == 'working' and card['current_recipe_products'][0]['item_id'] == '20'
    assert _pending_products(service._snapshot(provider.raw), service.catalog) == {'20': 3}
    run = OrderRun(tuple(provider.raw['role_key']), 'synthetic-default-existing', '', session.order_settings, 2000)
    run.production_book = QueueBook()
    assert run.production_book.refresh(provider.raw)
    assert service._queue_reserves(provider.raw, run) == {'10': 4}
    spent, received, blocked = service._external_queue_movements(provider.raw, run)
    assert spent == {'10': 6} and received == {'20': 4, '10': 6} and not blocked
    service.roles[run.role_key] = run
    try:
        assert await service._collect_home_products(session, provider.raw, run)
        assert provider.raw['inventory']['items']['20'] == 1
        assert building['store_items'] == []
        assert [row['action'] for row in provider.writes] == ['claim_all']
    finally:
        service.roles.pop(run.role_key, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('building_id', EXCLUDED)
async def test_orders_still_use_actual_stock_from_excluded_buildings(building_id):
    service, session, provider = excluded_setup(building_id)
    provider.raw['inventory']['items']['20'] = 3
    await start(service, session)
    await session.order_task.task
    assert session.order_task.status == 'completed'
    assert [row['action'] for row in provider.writes] == ['complete']


@pytest.mark.asyncio
async def test_excluded_idle_building_does_not_block_other_building_materials():
    service, session, provider = bounded_setup(independent=True)
    provider.architecture('7')['id'] = 10120
    for recipe in provider.recipes:
        if recipe['building_id'] == '1':
            recipe['building_id'] = '10120'
    provider.raw['orders']['orders'][0]['materials'] = [{'item_id': '30', 'count': 2}]
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        assert {row['building_uid'] for row in provider.writes if row['action'] == 'produce'} == {'8'}
        assert provider.architecture('7')['work_old']['recipe_id'] == 0
    finally:
        await close(service, session)
