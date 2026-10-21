import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from rumi_market.errors import ApiError
from rumi_market.order_service import order_revision
from test_order_service import production_setup


def setup_inspection():
    service, session, provider = production_setup(True)
    provider.raw['home']['architectures'][0]['work_old'].update(
        recipe_state=0, recipe_intend_times=0, recipe_complete_times=0)
    provider.server_time_seconds = provider.raw['server_time_seconds']
    service.catalog['names'] = {'20': '物资A'}
    return service, session, provider


async def stop(service, session):
    if session.order_task and not session.order_task.task.done():
        await service.stop(session)
        await asyncio.wait_for(session.order_task.task, 1)


async def test_read_only_inspection_reports_actual_idle_and_missing_worker_separately():
    service, session, provider = setup_inspection()
    missing = deepcopy(provider.raw['home']['architectures'][0])
    missing['uid'] = 8
    missing['work_old']['lumi_uid'] = '0'
    provider.raw['home']['architectures'].append(missing)
    value = await service.state(session)
    assert value['work_inspection']['ready']
    rows = value['work_inspection']['buildings']
    assert rows[0]['status'] == 'idle' and rows[0]['candidate']['item_id'] == '20'
    assert rows[1]['status'] == 'missing_worker'
    assert not provider.writes and session.order_task is None


async def test_inspection_resolves_worker_species_and_recipe_products_without_writes():
    service, session, provider = setup_inspection()
    worker_uid = '9007199254740993'
    provider.raw['home']['architectures'][0]['work_old'].update(lumi_uid=worker_uid, recipe_id=1)
    provider.raw['home']['workers'][0].update(uid=worker_uid, id=102601)
    looked_up = []

    def pet(pet_id):
        looked_up.append(pet_id)
        return SimpleNamespace(name='筋肉狗', image_url='/api/pets/102601/image')

    service.pet_catalog = SimpleNamespace(pet=pet)
    # A recipe ID is scoped to its building; an unrelated row must not win.
    service.catalog['recipes'].insert(0, {'building_id': '10102', 'recipe_id': '1',
        'products': [{'item_id': '99', 'count': 8}]})
    row = (await service.state(session))['work_inspection']['buildings'][0]
    assert row['worker_uid'] == worker_uid
    assert row['worker_name'] == '筋肉狗'
    assert row['worker_image_url'] == '/api/pets/102601/image'
    assert row['current_recipe_products'] == [{'item_id': '20', 'name': '物资A', 'count': '1'}]
    assert looked_up == ['102601']
    assert provider.writes == [] and session.order_task is None


@pytest.mark.parametrize('worker_uid,workers', [
    ('6566', []), ('6566', [{'uid': '6567', 'id': 102601}]), ('0', []),
    ('2147483647', []), (None, []),
])
async def test_inspection_never_uses_worker_instance_uid_as_species(worker_uid, workers):
    service, session, provider = setup_inspection()
    provider.raw['home']['architectures'][0]['work_old']['lumi_uid'] = worker_uid
    provider.raw['home']['workers'] = workers

    def forbidden_lookup(_):
        pytest.fail('Unmatched or missing instance UID must never be used for a catalog lookup')

    service.pet_catalog = SimpleNamespace(pet=forbidden_lookup)
    row = (await service.state(session))['work_inspection']['buildings'][0]
    assert row['worker_uid'] == worker_uid
    assert row['worker_name'] is None and row['worker_image_url'] is None
    assert provider.writes == []


@pytest.mark.parametrize('recipe_id,expected', [(0, []), (None, None), (999, None)])
async def test_inspection_keeps_unset_and_unknown_recipes_distinct(recipe_id, expected):
    service, session, provider = setup_inspection()
    provider.raw['home']['architectures'][0]['work_old']['recipe_id'] = recipe_id
    row = (await service.state(session))['work_inspection']['buildings'][0]
    assert row['current_recipe_products'] == expected
    assert row['worker_name'] is None and row['worker_image_url'] is None
    assert provider.writes == []


async def test_inspection_does_not_invent_a_name_for_an_unmapped_worker_species():
    service, session, provider = setup_inspection()
    service.pet_catalog = SimpleNamespace(pet=lambda _: SimpleNamespace(name=None, image_url=None))
    row = (await service.state(session))['work_inspection']['buildings'][0]
    assert row['worker_uid'] is not None
    assert row['worker_name'] is None and row['worker_image_url'] is None
    assert provider.writes == []


async def test_inspection_produces_one_batch_and_never_submits_an_order():
    service, session, provider = setup_inspection()
    before = await service.state(session)
    try:
        value = await service.start(session, 'synthetic-inspection-001', before['revision'], kind='inspection')
        assert value['task']['kind'] == 'inspection'
        await asyncio.wait_for(provider.started.wait(), 1)
        assert provider.writes[0]['recipe_times'] == 1
        provider.produced()
        for _ in range(100):
            if len(provider.writes) >= 2 and not session.order_task.in_flight:
                break
            await asyncio.sleep(.005)
        assert [row['action'] for row in provider.writes] == ['produce', 'claim']
        assert len(provider.raw['orders']['orders']) == 1
        assert (await service.state(session))['task']['inspected_actions'] == 1
    finally:
        await stop(service, session)


async def test_inspection_can_run_when_only_orders_are_not_synchronized():
    service, session, provider = setup_inspection()
    provider.raw['orders']['valid'] = False
    value = await service.state(session)
    assert not value['ready'] and value['work_inspection']['ready']
    try:
        await service.start(session, 'synthetic-inspection-002', value['revision'], kind='inspection')
        await asyncio.wait_for(provider.started.wait(), 1)
    finally:
        await stop(service, session)


@pytest.mark.parametrize('field', ['roster_ready', 'dispatch_ready'])
async def test_inspection_waits_for_work_baselines(field):
    service, session, provider = setup_inspection()
    provider.raw['home'][field] = False
    value = await service.state(session)
    assert not value['work_inspection']['ready']
    with pytest.raises(ApiError):
        await service.start(session, 'synthetic-inspection-003', value['revision'], kind='inspection')
    assert not provider.writes


async def test_unknown_inspection_operation_keeps_the_shared_role_lock():
    service, session, provider = setup_inspection()
    async def unknown(operation, **kwargs):
        provider.writes.append(operation)
        return {'status': 'uncertain', 'rewards': []}
    provider.apply_order_operation = unknown
    await service.start(session, 'synthetic-inspection-004', order_revision(provider.raw), kind='inspection')
    await asyncio.wait_for(session.order_task.task, 1)
    assert session.order_task.status == 'uncertain'
    with pytest.raises(ApiError, match='ORDER_ROLE_BUSY'):
        await service.start(session, 'synthetic-orders-after-unknown', order_revision(provider.raw))
    assert len(provider.writes) == 1


async def test_missing_worker_does_not_send_and_cancels_cleanly():
    service, session, provider = setup_inspection()
    provider.raw['home']['architectures'][0]['work_old']['lumi_uid'] = '0'
    await service.start(session, 'synthetic-inspection-005', order_revision(provider.raw), kind='inspection')
    await asyncio.sleep(0)
    await stop(service, session)
    assert not provider.writes and session.order_task.status == 'stopped'


async def test_server_deadline_is_rechecked_at_the_actual_operation_gate():
    service, session, provider = setup_inspection()
    async def delayed_send(operation, *, stop_requested, **kwargs):
        provider.raw['server_time_seconds'] = session.order_task.deadline
        provider.server_time_seconds = session.order_task.deadline
        assert stop_requested()
        return {'status': 'rejected', 'rewards': []}
    provider.apply_order_operation = delayed_send
    await service.start(session, 'synthetic-inspection-006', order_revision(provider.raw), kind='inspection')
    await asyncio.wait_for(session.order_task.task, 1)
    assert not provider.writes and session.order_task.status == 'completed'
