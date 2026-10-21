"""Native bulk collection replaces work-update/read-store assumptions."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import rumi_market.order_service as order_module
from rumi_market.app import create_app
from rumi_market.auth import COOKIE_NAME
from rumi_market.order_models import OrderSettings
from rumi_market.order_service import order_revision
from test_order_periodic_collection import CollectionProvider, FakeClock, building, stop_at_first_wait
from test_order_service import ProductionFlowProvider, production_setup, setup


class WorkUpdateProvider(CollectionProvider):
    """The server can collect completed work absent from the last home snapshot."""

    def __init__(self, *, ready=True, outcome='applied'):
        super().__init__([building(1, '1', 10, store_items=[])])
        self.ready, self.claim_outcome = ready, outcome
        self.raw['orders']['orders'] = self.raw['orders']['orders'][:1]
        self.raw['home'].update(roster_ready=True, dispatch_ready=True)

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if operation['action'] != 'claim_all':
            return await super().apply_order_operation(operation,
                expected_revision=expected_revision, stop_requested=stop_requested)
        assert operation == {'action': 'claim_all'}
        assert expected_revision == order_revision(self.raw)
        if stop_requested():
            return {'status': 'rejected', 'rewards': []}
        self.writes.append(deepcopy(operation))
        if self.claim_outcome == 'applied' and self.ready:
            self.change_inventory('1', 10)
            self.ready = False
        return {'status': self.claim_outcome, 'rewards': []}


async def finish(service, session, *, kind='orders'):
    await service.start(session, 'synthetic-server-bulk-collection',
        (await service.state(session))['revision'], kind=kind)
    await asyncio.wait_for(session.order_task.task, 1)


@pytest.mark.asyncio
async def test_native_bulk_claim_funds_order_without_work_update_or_store_notice(monkeypatch):
    service, session, provider = setup(WorkUpdateProvider())
    session.order_settings = OrderSettings(max_orders=1, allow_production=False)
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await finish(service, session)
    assert [row['action'] for row in provider.writes] == ['claim_all', 'complete']
    assert session.order_task.status == 'completed'
    assert session.order_task.completed == 1
    assert provider.raw['home']['architectures'][0]['store_items'] == []
    assert provider.raw['inventory']['items']['1'] == 0
    assert not session.order_task.in_flight


class ProductionWorkUpdateProvider(ProductionFlowProvider):
    def __init__(self, clock):
        super().__init__(False)
        self.clock, self.collection_times = clock, []
        self.delivered_product = False

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if operation['action'] != 'claim_all':
            return await super().apply_order_operation(operation,
                expected_revision=expected_revision, stop_requested=stop_requested)
        assert operation == {'action': 'claim_all'}
        assert expected_revision == order_revision(self.raw)
        if stop_requested():
            return {'status': 'rejected', 'rewards': []}
        self.collection_times.append(self.clock.now)
        if self.started.is_set() and self.clock.now >= 120 and not self.delivered_product:
            self.produced()
            self.delivered_product = True
        before = len(self.writes)
        result = await super().apply_order_operation(operation,
            expected_revision=expected_revision, stop_requested=stop_requested)
        if len(self.writes) == before:
            self.writes.append(deepcopy(operation))
        return result


@pytest.mark.asyncio
async def test_material_batch_collects_at_60_and_120_seconds_while_preserving_reservation(monkeypatch):
    service, session, _ = production_setup(False)
    clock, observations = FakeClock(), []
    provider = ProductionWorkUpdateProvider(clock)
    session.provider = provider
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))
    ticks = iter((59, 60, 119, 120))

    async def advance(run, seconds=.25):
        observations.append((clock.now, run.in_flight))
        try:
            clock.now = next(ticks)
        except StopIteration:
            run.stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', advance)
    await finish(service, session)
    assert provider.collection_times == [0, 60, 120]
    assert observations == [(0, True), (59, True), (60, True), (119, True)]
    assert [row['action'] for row in provider.writes] == [
        'claim_all', 'produce', 'claim_all', 'claim_all', 'complete']
    assert provider.raw['inventory']['items']['10'] == 8
    assert session.order_task.status == 'completed'
    assert not session.order_task.in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['deadline', 'stop', 'role-change'])
async def test_batch_bulk_ack_revalidates_task_before_using_received_product(monkeypatch, ending):
    service, session, _ = production_setup(False)
    clock = FakeClock()
    provider = ProductionWorkUpdateProvider(clock)
    session.provider = provider
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))
    apply = provider.apply_order_operation

    async def end_during_collection(operation, *, expected_revision, stop_requested):
        result = await apply(operation, expected_revision=expected_revision, stop_requested=stop_requested)
        if operation['action'] == 'claim_all' and provider.delivered_product:
            if ending == 'deadline':
                provider.raw['server_time_seconds'] = session.order_task.deadline
            elif ending == 'stop':
                session.order_task.stop.set()
            else:
                provider.raw['role_key'] = ('8', '9007199254740993')
        return result

    async def advance(run, seconds=.25):
        if clock.now == 0:
            clock.now = 120
        else:
            run.stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(provider, 'apply_order_operation', end_during_collection)
    monkeypatch.setattr(service, '_wait', advance)
    await finish(service, session)
    assert [row['action'] for row in provider.writes] == ['claim_all', 'produce', 'claim_all']
    assert provider.delivered_product
    assert session.order_task.status == 'uncertain'
    assert session.order_task.uncertain and session.order_task.in_flight
    assert session.order_task.completed == 0
    assert service.roles[session.order_task.role_key] is session.order_task


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['stop', 'deadline'])
async def test_stop_or_deadline_before_first_loop_prevents_bulk_claim(ending):
    service, session, provider = setup(WorkUpdateProvider())
    await service.start(session, 'synthetic-collection-already-ended',
        (await service.state(session))['revision'])
    if ending == 'stop':
        session.order_task.stop.set()
    else:
        provider.raw['server_time_seconds'] = session.order_task.deadline
    await asyncio.wait_for(session.order_task.task, 1)
    assert provider.writes == []
    assert not session.order_task.uncertain


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['stop', 'deadline'])
async def test_stop_or_deadline_at_next_due_tick_prevents_another_bulk_claim(monkeypatch, ending):
    service, session, provider = setup(WorkUpdateProvider(ready=False))
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))

    async def end_at_next_tick(run, seconds=.25):
        clock.now = 60
        if ending == 'stop':
            await service.stop(session)
        else:
            provider.raw['server_time_seconds'] = run.deadline
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', end_at_next_tick)
    await finish(service, session)
    assert provider.writes == [{'action': 'claim_all'}]
    assert session.order_task.status == ('stopped' if ending == 'stop' else 'completed')
    assert not session.order_task.uncertain and not service.roles


@pytest.mark.asyncio
async def test_unknown_bulk_ack_stops_without_complete_and_keeps_role_protection(monkeypatch):
    service, session, provider = setup(WorkUpdateProvider(outcome='uncertain'))
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await finish(service, session)
    assert provider.writes == [{'action': 'claim_all'}]
    assert session.order_task.status == 'uncertain' and session.order_task.uncertain
    assert service.roles[tuple(provider.raw['role_key'])] is session.order_task
    assert provider.raw['inventory']['items']['1'] == 0


def test_state_and_plan_routes_do_not_collect_server_work():
    _, session, provider = setup(WorkUpdateProvider())
    app = create_app()
    app.state.sessions.sessions[session.id] = session
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, session.id)
        response = client.get('/api/home/orders/state')
        assert response.status_code == 200
        state = response.json()
        preview = client.post('/api/home/orders/plan', json={
            'expected_revision': state['revision'], 'settings': state['settings']})
        assert preview.status_code == 200
    assert provider.writes == []
    assert provider.raw['home']['architectures'][0]['store_items'] == []


@pytest.mark.asyncio
async def test_inspection_does_not_gain_bulk_collection(monkeypatch):
    service, session, provider = setup(WorkUpdateProvider())
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await finish(service, session, kind='inspection')
    assert provider.writes == []
    assert session.order_task.status == 'stopped'
