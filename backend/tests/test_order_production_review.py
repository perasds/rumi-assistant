"""Independent boundary checks for the conservative single-batch path."""
import asyncio
from types import SimpleNamespace

import pytest

import rumi_market.order_service as order_module

from rumi_market.order_replenishment import choose_replenishment
from rumi_market.order_service import OrderRun, order_revision
from test_order_replenishment import fixture
from test_order_service import ProductionFlowProvider, production_setup
from test_order_periodic_collection import FakeClock


def test_replenishment_does_not_start_a_worker_with_known_zero_energy():
    snapshot, catalog, reserves = fixture()
    snapshot['buildings'][0]['work_energy'] = 0
    assert choose_replenishment(snapshot, catalog, reserves, batch_limit=1) is None


def test_latest_zero_worker_energy_is_not_hidden_by_old_positive_work_baseline():
    service, session, provider = production_setup(True)
    assert choose_replenishment(service._snapshot(provider.raw), service.catalog, {}, batch_limit=1) is not None
    provider.raw['home']['workers'][0]['work_energy'] = 0
    assert provider.raw['home']['architectures'][0]['work_old']['lumi_work_energy'] > 0
    snapshot = service._snapshot(provider.raw)
    assert snapshot['buildings'][0]['worker_valid'] is True
    assert snapshot['buildings'][0]['work_energy'] == 0
    assert choose_replenishment(snapshot, service.catalog, {}, batch_limit=1) is None


class MissingClaimCreditProvider(ProductionFlowProvider):
    def __init__(self):
        super().__init__(True)
        self.claimed = asyncio.Event()

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if operation['action'] != 'claim_all' or not self.raw['home']['architectures'][0]['store_items']:
            return await super().apply_order_operation(operation,
                expected_revision=expected_revision, stop_requested=stop_requested)
        self.writes.append(operation)
        self.raw['home']['architectures'][0]['store_items'] = []
        self.raw['home']['sequence'] += 1
        # An unrelated item notification does not confirm receipt of item 20.
        self.change_inventory('9999', 1)
        self.claimed.set()
        return {'status': 'applied', 'rewards': []}


@pytest.mark.asyncio
async def test_material_batch_keeps_unknown_lock_until_claimed_product_is_in_inventory(monkeypatch):
    service, session, _ = production_setup(True)
    provider = MissingClaimCreditProvider()
    session.provider = provider
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))

    async def advance_when_ready(run, seconds=.25):
        if provider.raw['home']['architectures'][0]['store_items']:
            clock.now = 60
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', advance_when_ready)
    await service.start(session, 'synthetic-no-claim-credit-01', (await service.state(session))['revision'])
    await asyncio.wait_for(provider.started.wait(), 1)
    provider.produced()
    await asyncio.wait_for(provider.claimed.wait(), 1)
    await asyncio.sleep(.04)  # Past the synthetic notification timeout, not a production estimate.
    await service.stop(session)
    await asyncio.wait_for(session.order_task.task, 1)
    assert [row['action'] for row in provider.writes] == ['produce', 'claim_all']
    assert session.order_task.status == 'uncertain'
    assert session.order_task.uncertain is True
    assert service.roles[tuple(provider.raw['role_key'])] is session.order_task


class UpstreamStoreProvider(ProductionFlowProvider):
    def __init__(self):
        super().__init__(True)
        self.raw['inventory']['items'] = {}
        self.raw['home']['architectures'].append({
            'uid': 8, 'id': 10102, 'level': 1, 'capacity_remaining': 10,
            'store_items': [{'item_id': '10', 'count': 2}],
            'work_old': {'recipe_id': 7, 'lumi_uid': '98', 'lumi_work_energy': 1000}})

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        assert operation == {'action': 'claim_all'}
        self.writes.append(operation)
        self.raw['home']['architectures'][1]['store_items'] = []
        self.raw['home']['sequence'] += 1
        self.change_inventory('10', 2)
        return {'status': 'applied', 'rewards': []}


@pytest.mark.asyncio
async def test_replenishment_claims_ready_upstream_material_even_if_order_only_requests_its_product():
    service, session, _ = production_setup(True)
    provider = UpstreamStoreProvider()
    session.provider = provider
    service.catalog['recipes'].append({'building_id': '10102', 'recipe_id': '7', 'available_levels': [1],
        'volume': 1, 'work_amount': 10, 'materials': [], 'products': [{'item_id': '10', 'count': 1}]})
    before = await service.raw(session)
    plan = await service.plan(session, order_revision(before), session.order_settings)
    # Service.plan crosses JSON; the internal step consumes integer planner data.
    from rumi_market.order_planner import plan_orders
    internal_plan = plan_orders(service._snapshot(before), service.catalog,
        service._planner_settings(session.order_settings), now=before['server_time_seconds'])
    assert {row['item_id'] for row in plan['materials']} == {'20'}
    assert internal_plan['base_requirements'] == {'10': 2}
    run = OrderRun(tuple(before['role_key']), 'synthetic-upstream-claim-01', order_revision(before),
        session.order_settings, before['server_time_seconds'] + 3600)
    service.roles[run.role_key] = run
    changed = await service._collect_home_products(session, before, run)
    assert changed is True
    assert provider.writes == [{'action': 'claim_all'}]
    assert provider.raw['inventory']['items']['10'] == 2


class IntermediateStoreProvider(ProductionFlowProvider):
    def __init__(self):
        super().__init__(True)
        self.raw['inventory']['items'] = {}
        self.raw['orders']['orders'][0]['materials'] = [{'item_id': '30', 'count': 1}]
        self.raw['home']['architectures'][0]['work_old']['recipe_id'] = 1
        self.raw['home']['architectures'][0]['store_items'] = [{'item_id': '20', 'count': 1}]
        self.raw['home']['architectures'].append({
            'uid': 8, 'id': 10101, 'level': 1, 'capacity_remaining': 10, 'store_items': [],
            'work_old': {'recipe_id': 0, 'lumi_uid': '98', 'lumi_work_energy': 1000}})

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        assert operation == {'action': 'claim_all'}
        self.writes.append(operation)
        self.raw['home']['architectures'][0]['store_items'] = []
        self.raw['home']['sequence'] += 1
        self.change_inventory('20', 1)
        return {'status': 'applied', 'rewards': []}


@pytest.mark.asyncio
async def test_replenishment_claims_intermediate_bom_product_as_well_as_base_materials():
    service, session, _ = production_setup(True)
    provider = IntermediateStoreProvider()
    session.provider = provider
    service.catalog['recipes'].append({'building_id': '10101', 'recipe_id': '2', 'available_levels': [1],
        'volume': 1, 'work_amount': 10, 'materials': [{'item_id': '20', 'count': 1}],
        'products': [{'item_id': '30', 'count': 1}]})
    before = await service.raw(session)
    from rumi_market.order_planner import plan_orders
    plan = plan_orders(service._snapshot(before), service.catalog,
        service._planner_settings(session.order_settings), now=before['server_time_seconds'])
    assert {row['item_id'] for row in plan['materials']} == {'30'}
    assert plan['base_requirements'] == {'10': 2}
    run = OrderRun(tuple(before['role_key']), 'synthetic-intermediate-claim-01', order_revision(before),
        session.order_settings, before['server_time_seconds'] + 3600)
    service.roles[run.role_key] = run
    assert await service._collect_home_products(session, before, run) is True
    assert provider.writes == [{'action': 'claim_all'}]
    assert provider.raw['inventory']['items']['20'] == 1
