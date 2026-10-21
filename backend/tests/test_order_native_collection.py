"""Native collect-all must not depend on a locally cached production store."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import rumi_market.order_service as order_module
from rumi_market.order_models import OrderSettings
from rumi_market.order_service import order_revision
from test_order_periodic_collection import FakeClock, building, stop_at_first_wait
from test_order_service import Provider, production_setup, setup


class NativeCollectionProvider(Provider):
    def __init__(self, *, outcome='applied', credit=True):
        super().__init__()
        self.raw['inventory']['items'] = {'1': 0}
        self.raw['home']['architectures'] = [building(1, store_items=[])]
        self.outcome, self.credit = outcome, credit

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if operation['action'] != 'claim_all':
            return await super().apply_order_operation(operation,
                expected_revision=expected_revision, stop_requested=stop_requested)
        assert operation == {'action': 'claim_all'}
        assert expected_revision == order_revision(self.raw)
        if stop_requested():
            return {'status': 'rejected', 'rewards': []}
        self.writes.append(deepcopy(operation))
        if self.outcome == 'applied' and self.credit:
            self.raw['inventory']['items']['1'] = 10
            self.raw['inventory']['sequence'] += 1
            self.credit = False
        # This is the actual 3028 contract: no reward or quantity fields.
        return {'status': self.outcome, 'rewards': []}


@pytest.mark.asyncio
async def test_empty_cached_store_still_collects_and_actual_inventory_funds_order(monkeypatch):
    service, session, provider = setup(NativeCollectionProvider())
    session.order_settings = OrderSettings(max_orders=1)
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await service.start(session, 'native-empty-cached-store', (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)

    assert [row['action'] for row in provider.writes] == ['claim_all', 'complete']
    assert session.order_task.completed == 1
    assert session.order_task.status == 'completed'
    # No invented store-clear notification was needed.
    assert provider.raw['home']['sequence'] == 1


@pytest.mark.asyncio
async def test_empty_ack_without_inventory_does_not_invent_items_or_fail(monkeypatch):
    service, session, provider = setup(NativeCollectionProvider(credit=False))
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await service.start(session, 'native-empty-no-notice', (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)

    assert provider.writes == [{'action': 'claim_all'}]
    assert session.order_task.status == 'stopped'
    assert not session.order_task.uncertain and not session.order_task.in_flight
    assert session.order_task.completed == 0


@pytest.mark.asyncio
async def test_collect_all_runs_at_60_seconds_even_when_revision_is_unchanged(monkeypatch):
    service, session, provider = setup(NativeCollectionProvider(credit=False))
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))
    ticks = iter((59, 60, 240))

    async def advance(run, seconds=.25):
        try:
            clock.now = next(ticks)
        except StopIteration:
            run.stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', advance)
    await service.start(session, 'native-collection-ticks', (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    assert provider.writes == [{'action': 'claim_all'}] * 3
    assert not session.order_task.uncertain


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['uncertain', 'rejected'])
async def test_unconfirmed_collect_all_does_not_submit_orders(monkeypatch, outcome):
    service, session, provider = setup(NativeCollectionProvider(outcome=outcome))
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await service.start(session, 'native-unconfirmed-collection', (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    assert provider.writes == [{'action': 'claim_all'}]
    assert session.order_task.status == ('uncertain' if outcome == 'uncertain' else 'failed')
    assert session.order_task.completed == 0


class ReservedBatchProvider(NativeCollectionProvider):
    def __init__(self):
        super().__init__(credit=False)
        self.raw['inventory'].update(items={'10': 10}, received_totals={}, spent_totals={})
        self.raw['home']['architectures'] = [building(7, store_items=[])]


@pytest.mark.asyncio
async def test_reserved_batch_collects_other_buildings_without_releasing_inputs(monkeypatch):
    service, session, _ = production_setup(False)
    provider = ReservedBatchProvider()
    session.provider = provider
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))
    state = await service.state(session)
    from rumi_market.order_service import OrderRun
    run = OrderRun(tuple(provider.raw['role_key']), 'native-batch-reserved', state['revision'],
        session.order_settings, 2000, in_flight=True)
    before = deepcopy(provider.raw)
    service.roles[run.role_key] = run
    proposal = {'operation': {'building_uid': '7', 'building_id': '10101', 'recipe_id': '1'},
                'costs': [{'item_id': '10', 'count': 2}]}
    observations = []

    async def advance(run, seconds=.25):
        observations.append(run.in_flight)
        if clock.now == 0:
            clock.now = 60
        else:
            run.stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', advance)
    assert not await service._finish_material_batch(session, before, proposal, run)
    assert provider.writes == [{'action': 'claim_all'}]
    assert all(observations) and run.in_flight


@pytest.mark.asyncio
async def test_batch_accounts_for_input_replenished_while_cost_and_output_arrive(monkeypatch):
    service, session, _ = production_setup(False)
    provider = ReservedBatchProvider()
    session.provider = provider
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))
    from rumi_market.order_service import OrderRun
    run = OrderRun(tuple(provider.raw['role_key']), 'native-batch-movements', '',
        session.order_settings, 2000, in_flight=True)
    before = deepcopy(provider.raw)
    service.roles[run.role_key] = run
    proposal = {'operation': {'building_uid': '7', 'building_id': '10101', 'recipe_id': '1'},
                'costs': [{'item_id': '10', 'count': 2}]}

    async def notify_actual_movements(run, seconds=.25):
        if clock.now == 0:
            clock.now = 60
            # 2 inputs consumed, another building contributes 5 of that input.
            provider.raw['inventory'].update(sequence=4, items={'10': 13, '20': 1},
                received_totals={'10': 5, '20': 1}, spent_totals={'10': 2})
        else:
            run.stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', notify_actual_movements)
    assert await service._finish_material_batch(session, before, proposal, run)
    assert provider.raw['inventory']['items']['10'] == 13


@pytest.mark.asyncio
async def test_net_inventory_and_empty_ack_cannot_release_unconfirmed_material_cost(monkeypatch):
    service, session, _ = production_setup(False)
    provider = ReservedBatchProvider()
    session.provider = provider
    from rumi_market.order_service import OrderRun
    run = OrderRun(tuple(provider.raw['role_key']), 'native-batch-no-debit', '',
        session.order_settings, 2000, in_flight=True)
    before = deepcopy(provider.raw)
    service.roles[run.role_key] = run
    proposal = {'operation': {'building_uid': '7', 'building_id': '10101', 'recipe_id': '1'},
                'costs': [{'item_id': '10', 'count': 2}]}
    provider.raw['inventory'].update(sequence=2, items={'10': 8, '20': 1},
        received_totals={'20': 1}, spent_totals={})
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    assert not await service._finish_material_batch(session, before, proposal, run)
    assert run.in_flight


@pytest.mark.asyncio
async def test_reserved_batch_completion_does_not_collect_again_in_the_same_interval(monkeypatch):
    service, session, provider = production_setup(False)
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))
    observed = []
    original_apply = provider.apply_order_operation

    async def record(operation, **kwargs):
        # Record even an empty collect-all: an empty ACK is still a sent request.
        observed.append((clock.now, operation['action']))
        return await original_apply(operation, **kwargs)

    async def finish_at_next_collection(run, seconds=.25):
        clock.now = 60
        provider.produced()
        await asyncio.sleep(0)

    monkeypatch.setattr(provider, 'apply_order_operation', record)
    monkeypatch.setattr(service, '_wait', finish_at_next_collection)
    await service.start(session, 'native-batch-shared-collection-clock',
        (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)

    assert observed == [(0, 'claim_all'), (0, 'produce'),
                        (60, 'claim_all'), (60, 'complete')]
    assert session.order_task.status == 'completed'
    assert not session.order_task.in_flight


def test_inventory_rebaseline_cannot_reuse_movements_for_an_old_batch():
    from rumi_market.order_service import OrderService
    before = {'inventory': {'valid': True, 'sequence': 1, 'movement_epoch': 0,
        'received_totals': {}, 'spent_totals': {}}}
    current = {'inventory': {'valid': True, 'sequence': 4, 'movement_epoch': 1,
        'received_totals': {'20': 1}, 'spent_totals': {'10': 2}}}
    assert not OrderService._batch_movements_confirmed(before, current,
        [{'item_id': '10', 'count': 2}], [{'item_id': '20', 'count': 1}])
