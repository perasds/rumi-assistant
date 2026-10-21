import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import rumi_market.order_service as order_module

from rumi_market.errors import ApiError
from rumi_market.models import Role, SessionView
from rumi_market.order_models import OrderSettings
from rumi_market.order_service import OrderRun, OrderService
from rumi_market.order_replenishment import choose_replenishment
from rumi_market.sessions import Session, SessionStore


def raw():
    return {'role_key': ('7', '9007199254740993'), 'server_time_seconds': 1000,
        'orders': {'valid': True, 'sequence': 1, 'observed_at': None, 'orders': [
            {'slot_id': '1', 'is_unlock': True, 'order_type': 1,
             'materials': [{'item_id': '1', 'count': 10}],
             'rewards': [{'item_id': '54001', 'count': 100}], 'refresh_time': None, 'refresh_seconds': None},
            *[{'slot_id': str(i), 'is_unlock': True, 'order_type': 1,
               'materials': [{'item_id': '1', 'count': 5}],
               'rewards': [{'item_id': '54001', 'count': 40}], 'refresh_time': None, 'refresh_seconds': None}
              for i in (2, 3)]]},
        'inventory': {'valid': True, 'sequence': 1, 'items': {'1': 10, '9000': 9007199254740993},
                      'received_totals': {}, 'spent_totals': {}},
        'home': {'valid': True, 'sequence': 1, 'architectures': [], 'workers': []}}


class Provider:
    def __init__(self):
        self.raw = raw()
        self.writes = []
        self.outcome = 'applied'
        self.update_inventory = True

    async def order_snapshot(self):
        return deepcopy(self.raw)

    @property
    def order_identity(self):
        return tuple(self.raw['role_key'])

    def change_inventory(self, item_id, amount):
        inventory = self.raw['inventory']
        inventory['items'][item_id] = inventory['items'].get(item_id, 0) + amount
        ledger = inventory['received_totals' if amount > 0 else 'spent_totals']
        ledger[item_id] = ledger.get(item_id, 0) + abs(amount)
        inventory['sequence'] += 1

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if stop_requested():
            return {'status': 'rejected', 'rewards': []}
        if operation['action'] in {'update_work', 'claim_all'}:
            return {'status': 'applied', 'rewards': []}
        self.writes.append(operation)
        order = next(row for row in self.raw['orders']['orders'] if row['slot_id'] == operation['slot_id'])
        if self.outcome == 'applied':
            self.raw['orders']['orders'].remove(order)
            self.raw['orders']['sequence'] += 1
            if self.update_inventory:
                for cost in order['materials']:
                    self.change_inventory(cost['item_id'], -cost['count'])
        return {'status': self.outcome, 'rewards': order['rewards']}

    async def close(self):
        pass


def setup(provider=None):
    provider = provider or Provider()
    store = SessionStore(Provider)
    session = Session('synthetic-orders', provider, view=SessionView(status='connected', authenticated=True,
        role=Role(id='browser-key', name='', server_name='test')))
    store.sessions[session.id] = session
    service = OrderService(store, catalog={'recipes': [], 'names': {'1': '木头', '54001': '罐头'}}, sync_timeout=.02)
    return service, session, provider


@pytest.mark.asyncio
async def test_read_only_state_plan_precise_inventory_and_no_actions():
    service, session, provider = setup()
    state = await service.state(session)
    assert state['ready'] and state['execution_ready']
    assert state['settings']['max_orders'] == 1000
    assert state['settings']['allow_production'] is True
    plan = await service.plan(session, state['revision'], OrderSettings())
    assert plan['inventory_selection']['slot_ids'] == ['2', '3']
    assert plan['inventory_selection']['count'] == '2'
    assert provider.writes == []
    assert '9007199254740993' not in str(state)  # private real role key never leaves backend


@pytest.mark.asyncio
@pytest.mark.parametrize('max_orders,allow_production', [(100, False), (37, True)])
async def test_saved_order_settings_override_defaults(max_orders, allow_production):
    service, session, provider = setup()
    settings = OrderSettings(max_orders=max_orders, allow_production=allow_production)
    saved = await service.save_settings(session, settings)
    state = await service.state(session)
    assert saved['settings'] == settings.model_dump()
    assert state['settings'] == settings.model_dump()
    assert provider.writes == []


@pytest.mark.asyncio
async def test_start_replans_after_each_ack_then_stops_at_target():
    service, session, provider = setup()
    await service.save_settings(session, OrderSettings(max_orders=2))
    state = await service.state(session)
    await service.start(session, 'synthetic-request-001', state['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    result = await service.state(session)
    assert [row['slot_id'] for row in provider.writes] == ['2', '3']
    assert result['task']['status'] == 'completed'
    assert result['task']['completed_orders'] == 2
    assert result['task']['canned_received'] == '80'


@pytest.mark.asyncio
async def test_missing_inventory_not_zero_and_stale_revision_rejected():
    service, session, provider = setup()
    before = await service.state(session)
    provider.raw['inventory']['sequence'] += 1
    with pytest.raises(ApiError, match='ORDER_STATE_CHANGED'):
        await service.start(session, 'synthetic-request-001', before['revision'])
    provider.raw['inventory']['valid'] = False
    state = await service.state(session)
    assert not state['ready'] and not state['execution_ready']
    assert state['orders'][0]['materials'][0]['available'] is None


@pytest.mark.asyncio
async def test_unknown_or_unsettled_result_blocks_same_real_role_in_another_session():
    service, session, provider = setup()
    provider.outcome = 'uncertain'
    await service.start(session, 'synthetic-request-001', (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    assert (await service.state(session))['task']['status'] == 'uncertain'
    other = Session('second-browser', Provider(), view=session.view)
    with pytest.raises(ApiError, match='ORDER_ROLE_BUSY'):
        await service.start(other, 'synthetic-request-002', (await service.state(other))['revision'])
    assert len(provider.writes) == 1


@pytest.mark.asyncio
async def test_success_ack_without_inventory_settlement_never_double_spends():
    service, session, provider = setup()
    provider.update_inventory = False
    await service.start(session, 'synthetic-request-001', (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    assert (await service.state(session))['task']['status'] == 'uncertain'
    assert len(provider.writes) == 1


@pytest.mark.asyncio
async def test_cancel_waiting_task_and_idempotent_start_do_not_create_more_writes():
    service, session, provider = setup()
    provider.raw['inventory']['items']['1'] = 0
    state = await service.state(session)
    await service.start(session, 'synthetic-request-001', state['revision'])
    await asyncio.sleep(0)
    await service.start(session, 'synthetic-request-001', state['revision'])
    await service.stop(session)
    await asyncio.wait_for(session.order_task.task, 1)
    assert (await service.state(session))['task']['status'] == 'stopped'
    assert provider.writes == []


class InventoryBeforeSlotProvider(Provider):
    """Successful ACK and bag update, but the order-slot notification is late."""
    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if operation['action'] in {'update_work', 'claim_all'}:
            return await super().apply_order_operation(operation,
                expected_revision=expected_revision, stop_requested=stop_requested)
        assert operation['action'] == 'complete'
        self.writes.append(operation)
        order = next(row for row in self.raw['orders']['orders'] if row['slot_id'] == operation['slot_id'])
        for cost in order['materials']:
            self.change_inventory(cost['item_id'], -cost['count'])
        return {'status': 'applied', 'rewards': order['rewards']}


@pytest.mark.asyncio
async def test_inventory_notification_before_order_slot_never_submits_the_old_slot_twice():
    service, session, provider = setup(InventoryBeforeSlotProvider())
    session.order_settings = OrderSettings(max_orders=2)
    await service.start(session, 'synthetic-late-slot-01', (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    assert provider.writes == [{'action': 'complete', 'slot_id': '2'}]
    assert session.order_task.status == 'uncertain'
    assert session.order_task.uncertain is True
    assert service.roles[tuple(provider.raw['role_key'])] is session.order_task


class ClaimNotificationProvider(Provider):
    def __init__(self, *, block_ack=False):
        super().__init__()
        self.sent = asyncio.Event()
        self.acknowledged = asyncio.Event()
        self.release_ack = asyncio.Event()
        self.block_ack = block_ack
        self.raw['orders']['orders'] = [{
            'slot_id': '1', 'is_unlock': True, 'order_type': 1,
            'materials': [{'item_id': '20001', 'count': 1}],
            'rewards': [{'item_id': '54001', 'count': 1}],
            'refresh_time': None, 'refresh_seconds': None}]
        self.raw['inventory']['items'] = {'20001': 0}
        self.raw['home']['architectures'] = [{
            'uid': '1', 'id': '10102', 'level': 1,
            'work_old': {'lumi_uid': '1', 'recipe_id': 7, 'recipe_state': 1},
            'store_items': [{'item_id': '20001', 'count': 1}]}]

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if operation['action'] != 'claim':
            return await super().apply_order_operation(operation, expected_revision=expected_revision,
                                                       stop_requested=stop_requested)
        self.writes.append(operation)
        self.sent.set()
        if self.block_ack:
            await self.release_ack.wait()
        self.acknowledged.set()
        return {'status': 'applied', 'rewards': []}

    def notify_inventory(self):
        self.change_inventory('20001', 1)

    def notify_store(self):
        self.raw['home']['architectures'][0]['store_items'] = []
        self.raw['home']['sequence'] += 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_after_ack', [False, True])
async def test_cancelling_direct_single_claim_leaves_pending_confirmation_reserved(cancel_after_ack):
    service, session, provider = setup(ClaimNotificationProvider(block_ack=not cancel_after_ack))
    service.sync_timeout = .3
    session.order_settings = OrderSettings(max_orders=1, allow_production=True)
    run = OrderRun(tuple(provider.raw['role_key']), 'synthetic-claim-cancel-01', '', session.order_settings, 2000)
    service.roles[run.role_key] = run
    session.order_task = run
    run.task = asyncio.create_task(service._claim_building(session, provider.raw, run, '1', '10102'))
    await asyncio.wait_for(provider.acknowledged.wait() if cancel_after_ack else provider.sent.wait(), 1)
    run.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run.task
    assert [row['action'] for row in provider.writes] == ['claim']
    assert session.order_task.in_flight is True
    assert service.roles[tuple(provider.raw['role_key'])] is session.order_task


@pytest.mark.asyncio
@pytest.mark.parametrize('first_notification', ['inventory', 'store'])
async def test_direct_single_claim_still_waits_for_inventory_and_store(first_notification):
    service, session, provider = setup(ClaimNotificationProvider())
    service.sync_timeout = .3
    session.order_settings = OrderSettings(max_orders=1, allow_production=True)
    run = OrderRun(tuple(provider.raw['role_key']), 'synthetic-claim-notify-01', '', session.order_settings, 2000)
    service.roles[run.role_key] = run
    session.order_task = run
    run.task = asyncio.create_task(service._claim_building(session, deepcopy(provider.raw), run, '1', '10102'))
    await asyncio.wait_for(provider.acknowledged.wait(), 1)
    getattr(provider, f'notify_{first_notification}')()
    # The old fixed 0.1s wait would either re-claim or submit after only one notice.
    await asyncio.sleep(.12)
    assert [row['action'] for row in provider.writes] == ['claim']
    assert session.order_task.in_flight is True
    getattr(provider, 'notify_store' if first_notification == 'inventory' else 'notify_inventory')()
    assert await asyncio.wait_for(run.task, 1) is True
    assert [row['action'] for row in provider.writes] == ['claim']
    assert not run.in_flight


@pytest.mark.asyncio
async def test_cancel_after_complete_ack_before_slot_settlement_keeps_role_lock():
    provider = InventoryBeforeSlotProvider()
    service, session, provider = setup(provider)
    service.sync_timeout = .3
    await service.start(session, 'synthetic-complete-cancel-01', (await service.state(session))['revision'])
    # _run reaches the settlement await before yielding back to this test.
    await asyncio.sleep(0)
    assert len(provider.writes) == 1
    session.order_task.task.cancel()
    await asyncio.wait_for(session.order_task.task, 1)
    assert session.order_task.status == 'uncertain'
    assert session.order_task.uncertain is True
    assert service.roles[tuple(provider.raw['role_key'])] is session.order_task


@pytest.mark.asyncio
async def test_user_reconciliation_requires_current_complete_snapshot_and_never_retries():
    service, session, provider = setup()
    provider.outcome = 'uncertain'
    await service.start(session, 'synthetic-reconcile-01', (await service.state(session))['revision'])
    await session.order_task.task
    state = await service.state(session)
    provider.raw['inventory']['sequence'] += 1
    with pytest.raises(ApiError, match='ORDER_STATE_CHANGED'):
        await service.resolve(session, state['revision'])
    state = await service.state(session)
    result = await service.resolve(session, state['revision'])
    assert result['task']['status'] == 'stopped'
    assert not service.roles and len(provider.writes) == 1


@pytest.mark.asyncio
async def test_same_role_home_change_lease_excludes_order_task_start():
    service, session, provider = setup()
    state = await service.state(session)
    async with service.home_lease(session):
        with pytest.raises(ApiError, match='ORDER_ROLE_BUSY'):
            await service.start(session, 'synthetic-home-lease-01', state['revision'])
    assert provider.writes == []


class ProductionFlowProvider(Provider):
    def __init__(self, prepaid):
        super().__init__()
        self.prepaid = prepaid
        self.started = asyncio.Event()
        self.raw['orders']['orders'] = [{'slot_id': '1', 'is_unlock': True, 'order_type': 1,
            'materials': [{'item_id': '20', 'count': 1}], 'rewards': [{'item_id': '54001', 'count': 1}]}]
        self.raw['inventory']['items'] = {'10': 10}
        self.raw['home']['architectures'] = [{'uid': 7, 'id': 10101, 'level': 1,
            'capacity_remaining': 10, 'store_items': [],
            'work_old': {'recipe_id': 0, 'lumi_uid': '99', 'lumi_work_energy': 1000}}]
        self.raw['home'].update(roster_ready=True, dispatch_ready=True,
            workers=[{'uid': '99', 'id': 102601, 'raw_state': '0', 'work_energy': 1000,
                      'occupancy': [{'kind': 'work_old', 'architecture_uid': 7}]}])

    def produced(self):
        self.raw['home']['architectures'][0]['store_items'] = [{'item_id': '20', 'count': 1}]
        self.raw['home']['sequence'] += 1

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if operation['action'] == 'claim_all':
            stores = [row for row in self.raw['home']['architectures'] if row.get('store_items')]
            if not stores:
                return {'status': 'applied', 'rewards': []}
            self.writes.append(operation)
            for row in stores:
                for reward in row['store_items']:
                    self.change_inventory(reward['item_id'], reward['count'])
                row['store_items'] = []
                self.raw['home']['sequence'] += 1
            if self.started.is_set() and not self.prepaid and any(str(row['uid']) == '7' for row in stores):
                self.change_inventory('10', -2)
            return {'status': 'applied', 'rewards': []}
        if operation['action'] in {'complete', 'update_work'}:
            return await super().apply_order_operation(operation, expected_revision=expected_revision, stop_requested=stop_requested)
        self.writes.append(operation)
        if operation['action'] == 'produce':
            assert operation['recipe_times'] == 1
            self.raw['home']['architectures'][0]['work_old']['recipe_id'] = 1
            self.raw['home']['sequence'] += 1
            if self.prepaid:
                self.change_inventory('10', -2)
            self.started.set()
            return {'status': 'applied', 'rewards': []}
        assert operation['action'] == 'claim'
        self.raw['home']['architectures'][0]['store_items'] = []
        self.raw['home']['sequence'] += 1
        self.change_inventory('20', 1)
        if not self.prepaid:
            self.change_inventory('10', -2)
        return {'status': 'applied', 'rewards': [{'item_id': '20', 'count': 1}]}


def production_setup(prepaid):
    service, session, provider = setup(ProductionFlowProvider(prepaid))
    # These synthetic card-table recipes exercise settlement, not app defaults.
    service.auto_production_exclusions = frozenset()
    service.catalog['recipes'] = [{'building_id': '10101', 'recipe_id': '1', 'available_levels': [1],
        'volume': 1, 'work_amount': 30, 'materials': [{'item_id': '10', 'count': 2}],
        'products': [{'item_id': '20', 'count': 1}]}]
    session.order_settings = OrderSettings(allow_production=True, max_orders=1)
    return service, session, provider


@pytest.mark.asyncio
@pytest.mark.parametrize('prepaid', [True, False])
async def test_material_production_reserves_one_batch_until_claim_and_inventory_settlement(prepaid, monkeypatch):
    service, session, provider = production_setup(prepaid)
    from test_order_periodic_collection import FakeClock
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))
    collected = asyncio.Event()

    async def advance_when_ready(run, seconds=.25):
        if provider.raw['home']['architectures'][0]['store_items']:
            clock.now = 60
            collected.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', advance_when_ready)
    await service.start(session, 'synthetic-production-01', (await service.state(session))['revision'])
    await asyncio.wait_for(provider.started.wait(), 1)
    await asyncio.sleep(.04)  # Exceeds short synthetic settlement timeout, not a guessed production time.
    assert [row['action'] for row in provider.writes] == ['produce']
    assert session.order_task.in_flight
    provider.produced()
    await asyncio.wait_for(session.order_task.task, 1)
    assert collected.is_set()
    assert [row['action'] for row in provider.writes] == ['produce', 'claim_all', 'complete']
    assert provider.raw['inventory']['items']['10'] == 8
    assert session.order_task.status == 'completed'


@pytest.mark.asyncio
async def test_cancel_pending_production_stops_further_actions_and_keeps_reservation_uncertain():
    service, session, provider = production_setup(False)
    await service.start(session, 'synthetic-production-02', (await service.state(session))['revision'])
    await asyncio.wait_for(provider.started.wait(), 1)
    await service.stop(session)
    await asyncio.wait_for(session.order_task.task, 1)
    assert [row['action'] for row in provider.writes] == ['produce']
    assert session.order_task.status == 'uncertain'


def queued_production_setup(remaining, order_counts=(50,)):
    service, session, provider = production_setup(False)
    provider.raw['orders']['orders'] = [
        {'slot_id': str(index), 'is_unlock': True, 'order_type': 1,
         'materials': [{'item_id': '20', 'count': count}], 'rewards': []}
        for index, count in enumerate(order_counts, 1)]
    active = provider.raw['home']['architectures'][0]
    active['work_old'].update(recipe_id=1, recipe_state=1,
        recipe_intend_times=40 + remaining, recipe_complete_times=40)
    idle = deepcopy(active)
    idle.update(uid=8)
    idle['work_old'].update(recipe_id=0, recipe_state=0, lumi_uid='98',
        recipe_intend_times=0, recipe_complete_times=0)
    provider.raw['home']['architectures'].append(idle)
    worker = deepcopy(provider.raw['home']['workers'][0])
    worker.update(uid='98', occupancy=[{'kind': 'work_old', 'architecture_uid': 8}])
    provider.raw['home']['workers'].append(worker)
    return service, session, provider


@pytest.mark.parametrize('remaining,counts,needs_production', [
    (60, (50,), False), (50, (50,), False), (49, (50,), True),
    (20, (50,), True), (2, (2, 48), True), (50, (2, 48), False),
])
def test_raw_home_queue_covers_all_order_slots_before_using_idle_building(remaining, counts, needs_production):
    service, _, provider = queued_production_setup(remaining, counts)
    snapshot = service._snapshot(provider.raw)
    active, idle = snapshot['buildings']
    assert active['worker_valid'] and idle['worker_valid']
    proposal = choose_replenishment(snapshot, service.catalog, {}, batch_limit=1)
    if needs_production:
        assert proposal['operation']['building_uid'] == '8'
        assert proposal['operation']['recipe_times'] == 1
    else:
        assert proposal is None
    assert provider.writes == []


@pytest.mark.asyncio
async def test_production_step_waits_without_sending_when_current_queue_covers_shortage():
    service, session, provider = queued_production_setup(60)
    run = OrderRun(tuple(provider.raw['role_key']), 'synthetic-covered-queue',
        '', session.order_settings, 2000)
    async def unexpected_write(*args, **kwargs):
        pytest.fail('Existing production already covers this order; no operation may be sent')
    provider.apply_order_operation = unexpected_write
    changed = await service._production_step(session, provider.raw, run,
        {'materials': [{'item_id': '20', 'shortage': 50}]})
    assert changed is False
    assert not run.in_flight
