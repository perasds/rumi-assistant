"""Observable order scheduling against a synthetic, notification-backed account."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from rumi_market.order_models import OrderSettings
from rumi_market.errors import ApiError
from rumi_market.order_production_queue import QueueBook
from test_order_service import Provider, setup


class BoundedProvider(Provider):
    def __init__(self, *, prepaid=False, independent=False, replace_order=False):
        super().__init__()
        self.snapshot_reads = 0
        self.prepaid, self.replace_order = prepaid, replace_order
        self.recipes = [
            {'building_id': '1', 'recipe_id': '1', 'available_levels': [1], 'volume': 1,
             'work_amount': 100, 'materials': [{'item_id': '10', 'count': 2}],
             'products': [{'item_id': '20', 'count': 1}]},
            {'building_id': '1', 'recipe_id': '2', 'available_levels': [1], 'volume': 1,
             'work_amount': 100, 'materials': [{'item_id': '10', 'count': 3}],
             'products': [{'item_id': '21', 'count': 1}]},
            {'building_id': '2', 'recipe_id': '3', 'available_levels': [1], 'volume': 1,
             'work_amount': 100, 'materials': [{'item_id': '11', 'count': 1}],
             'products': [{'item_id': '30', 'count': 1}]},
        ]
        materials = [{'item_id': '20', 'count': 1 if replace_order else 3}]
        if independent:
            materials.append({'item_id': '30', 'count': 2})
        self.raw['orders']['orders'] = [self.order('1', materials)]
        self.raw['inventory'].update(items={'10': 100, '11': 100}, movement_epoch=0)
        self.raw['home'].update(roster_ready=True, dispatch_ready=True,
            dispatch_lumi_uids=[], architectures=[self.building('7', '1', '99')],
            workers=[self.worker('7', '99')])
        if independent:
            self.raw['home']['architectures'].append(self.building('8', '2', '98'))
            self.raw['home']['workers'].append(self.worker('8', '98'))

    @staticmethod
    def order(slot, materials):
        return {'slot_id': slot, 'is_unlock': True, 'order_type': 1,
            'materials': materials, 'rewards': [{'item_id': '54001', 'count': 1}]}

    @staticmethod
    def worker(uid, worker):
        return {'uid': worker, 'id': 1, 'raw_state': '0', 'work_energy': 10000,
            'occupancy': [{'kind': 'work_old', 'architecture_uid': int(uid)}]}

    @staticmethod
    def building(uid, building, worker):
        return {'uid': int(uid), 'id': int(building), 'level': 1, 'capacity_remaining': 100,
            'store_items': [], 'work_old': {'recipe_id': 0, 'lumi_uid': worker,
                'lumi_work_energy': 10000, 'lumi_work_speed': 10000,
                'recipe_intend_times': 0, 'recipe_complete_times': 0, 'recipe_state': 0,
                'recipe_work_amount': 0, 'recipe_update_time': 1000}}

    def architecture(self, uid):
        return next(row for row in self.raw['home']['architectures'] if str(row['uid']) == uid)

    async def order_snapshot(self):
        self.snapshot_reads += 1
        return await super().order_snapshot()

    def recipe(self, uid):
        row = self.architecture(uid)
        return next(recipe for recipe in self.recipes
            if recipe['building_id'] == str(row['id'])
            and recipe['recipe_id'] == str(row['work_old']['recipe_id']))

    def produce_one(self, uid='7'):
        row, recipe = self.architecture(uid), self.recipe(uid)
        if not self.prepaid:
            for cost in recipe['materials']:
                self.change_inventory(cost['item_id'], -cost['count'])
        for product in recipe['products']:
            stored = next((item for item in row['store_items'] if item['item_id'] == product['item_id']), None)
            if stored is None:
                row['store_items'].append(deepcopy(product))
            else:
                stored['count'] += product['count']
        work = row['work_old']
        work['recipe_complete_times'] += 1
        work['recipe_work_amount'] = 0
        if work['recipe_complete_times'] == work['recipe_intend_times']:
            work.update(recipe_id=0, recipe_state=0)
        self.raw['home']['sequence'] += 1

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        if stop_requested():
            return {'status': 'rejected', 'rewards': []}
        action = operation['action']
        if action == 'update_work':
            return {'status': 'applied', 'rewards': []}
        if action == 'claim_all':
            stores = [row for row in self.raw['home']['architectures'] if row['store_items']]
            if stores:
                self.writes.append(deepcopy(operation))
            for row in stores:
                for product in row['store_items']:
                    self.change_inventory(product['item_id'], product['count'])
                row['store_items'] = []
                self.raw['home']['sequence'] += 1
            return {'status': 'applied', 'rewards': []}
        if action == 'complete':
            result = await super().apply_order_operation(operation,
                expected_revision=expected_revision, stop_requested=stop_requested)
            if self.replace_order and operation['slot_id'] == '1':
                self.raw['orders']['orders'] = [self.order('2', [{'item_id': '21', 'count': 1}])]
                self.raw['orders']['sequence'] += 1
            return result
        self.writes.append(deepcopy(operation))
        row = self.architecture(operation['building_uid'])
        if action == 'produce':
            assert row['work_old']['recipe_id'] == 0
            row['work_old'].update(recipe_id=int(operation['recipe_id']),
                recipe_intend_times=operation['recipe_times'], recipe_complete_times=0,
                recipe_state=1, recipe_work_amount=0)
            if self.prepaid:
                for cost in self.recipe(operation['building_uid'])['materials']:
                    self.change_inventory(cost['item_id'], -cost['count'] * operation['recipe_times'])
        else:
            assert action == 'cancel_production'
            assert str(row['work_old']['recipe_id']) == operation['recipe_id']
            # Cancellation proves idle only, never an invented refund.
            row['work_old'].update(recipe_id=0, recipe_state=0)
        self.raw['home']['sequence'] += 1
        return {'status': 'applied', 'rewards': []}


def bounded_setup(**options):
    provider = BoundedProvider(**options)
    service, session, _ = setup(provider)
    service.catalog = {'recipes': provider.recipes, 'names': {}}
    service.config = SimpleNamespace(building=lambda _: {'architecture_type': 3, 'work_type_id': 1},
        level=lambda *_: {'recipe_ids': ['1', '2', '3']}, work_types=lambda _: {1})
    session.order_settings = OrderSettings(allow_production=True,
        max_orders=2 if options.get('replace_order') else 1)
    return service, session, provider


async def eventually(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


async def start(service, session):
    state = await service.state(session)
    await service.start(session, 'synthetic-bounded-start-01', state['revision'])


async def close(service, session):
    if session.order_task and not session.order_task.task.done():
        await service.stop(session)
        await asyncio.wait_for(session.order_task.task, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('prepaid', [False, True])
async def test_partial_receipts_complete_order_without_waiting_for_buffer_batch(prepaid):
    service, session, provider = bounded_setup(prepaid=prepaid)
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        assert provider.writes[0]['recipe_times'] == 4
        provider.produce_one()
        await eventually(lambda: provider.raw['inventory']['items'].get('20') == 1)
        provider.produce_one()
        await eventually(lambda: provider.raw['inventory']['items'].get('20') == 2)
        provider.produce_one()
        await asyncio.wait_for(session.order_task.task, 1)
        assert session.order_task.status == 'completed'
        assert session.order_task.completed == 1
        assert provider.architecture('7')['work_old']['recipe_complete_times'] == 3
        assert provider.architecture('7')['work_old']['recipe_id'] == 1
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_unknown_external_production_cannot_share_owned_queue_materials():
    service, session, provider = bounded_setup()
    external = provider.building('9', '3', '97')
    external['cur_recipe'] = {'id': 4}
    external['work_old'].update(recipe_id=4, recipe_state=1, recipe_intend_times=1)
    provider.raw['home']['architectures'].append(external)
    provider.raw['home']['workers'].append(provider.worker('9', '97'))
    provider.recipes.append({'building_id': '3', 'recipe_id': '4', 'available_levels': [1],
        'volume': 1, 'work_amount': 100, 'materials': [{'item_id': '10', 'count': 2}],
        'products': [{'item_id': '40', 'count': 1}]})
    try:
        await start(service, session)
        observed = provider.snapshot_reads
        await eventually(lambda: provider.snapshot_reads > observed)
        assert not any(row['action'] == 'produce' for row in provider.writes)
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_independent_buildings_are_started_before_first_queue_finishes():
    service, session, provider = bounded_setup(independent=True)
    try:
        await start(service, session)
        await eventually(lambda: len([row for row in provider.writes if row['action'] == 'produce']) == 2)
        assert {row['building_uid'] for row in provider.writes if row['action'] == 'produce'} == {'7', '8'}
        assert all(row['work_old']['recipe_complete_times'] == 0 for row in provider.raw['home']['architectures'])
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_queued_material_reservation_prevents_hand_in_spending_future_inputs():
    service, session, provider = bounded_setup()
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        provider.raw['orders']['orders'].append(provider.order('2', [{'item_id': '10', 'count': 99}]))
        provider.raw['orders']['sequence'] += 1
        await asyncio.sleep(.04)
        assert not any(row['action'] == 'complete' for row in provider.writes)
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_confirmed_owned_queue_cancels_before_switching_to_new_order_product():
    service, session, provider = bounded_setup(replace_order=True)
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        assert provider.writes[0]['recipe_times'] == 2
        provider.produce_one()
        await eventually(lambda: any(row['action'] == 'cancel_production' for row in provider.writes))
        await eventually(lambda: any(row['action'] == 'produce' and row['recipe_id'] == '2' for row in provider.writes))
        actions = [row['action'] for row in provider.writes]
        assert actions.index('complete') < actions.index('cancel_production')
        assert provider.raw['inventory']['items']['10'] == 98
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_nearly_finished_batch_is_preserved_before_switching():
    service, session, provider = bounded_setup(replace_order=True)
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        provider.produce_one()
        provider.architecture('7')['work_old']['recipe_work_amount'] = 99 * 10000
        await eventually(lambda: any(row['action'] == 'complete' for row in provider.writes))
        await asyncio.sleep(.04)
        assert not any(row['action'] == 'cancel_production' for row in provider.writes)
        provider.produce_one()
        await eventually(lambda: any(row['action'] == 'produce' and row['recipe_id'] == '2' for row in provider.writes))
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_stopped_queue_keeps_its_material_reservation_in_the_next_run():
    service, session, provider = bounded_setup()
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        await close(service, session)
        assert session.order_task.status == 'stopped'
        provider.raw['orders']['orders'] = [provider.order('2', [{'item_id': '10', 'count': 99}])]
        provider.raw['orders']['sequence'] += 1
        before = len(provider.writes)
        state = await service.state(session)
        await service.start(session, 'synthetic-bounded-restart-02', state['revision'])
        await asyncio.sleep(.04)
        assert len(provider.writes) == before
        assert session.order_task.view()['running']
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_inventory_epoch_change_freezes_automation_before_next_write():
    service, session, provider = bounded_setup()
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        provider.raw['inventory']['movement_epoch'] += 1
        await asyncio.wait_for(session.order_task.task, 1)
        assert session.order_task.status == 'uncertain'
        assert [row['action'] for row in provider.writes] == ['produce']
        assert service.roles[tuple(provider.raw['role_key'])] is session.order_task
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_uncertain_cancellation_never_starts_the_replacement_recipe():
    service, session, provider = bounded_setup(replace_order=True)
    original = provider.apply_order_operation

    async def uncertain_cancel(operation, **kwargs):
        if operation['action'] == 'cancel_production':
            provider.writes.append(deepcopy(operation))
            return {'status': 'uncertain', 'rewards': []}
        return await original(operation, **kwargs)

    provider.apply_order_operation = uncertain_cancel
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        provider.produce_one()
        await asyncio.wait_for(session.order_task.task, 1)
        assert session.order_task.status == 'uncertain'
        assert not any(row['action'] == 'produce' and row['recipe_id'] == '2' for row in provider.writes)
        assert service.roles[tuple(provider.raw['role_key'])] is session.order_task
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_unchanged_cached_output_does_not_cause_a_collection_loop():
    service, session, provider = bounded_setup()
    original = provider.apply_order_operation

    async def delayed_inventory(operation, **kwargs):
        if operation['action'] == 'claim_all' and provider.architecture('7')['store_items']:
            provider.writes.append(deepcopy(operation))
            return {'status': 'applied', 'rewards': []}
        return await original(operation, **kwargs)

    provider.apply_order_operation = delayed_inventory
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        provider.produce_one()
        await eventually(lambda: any(row['action'] == 'claim_all' for row in provider.writes))
        await asyncio.sleep(.04)
        assert sum(row['action'] == 'claim_all' for row in provider.writes) == 1
        assert not any(row['action'] == 'complete' for row in provider.writes)
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_deadline_crossed_before_send_prevents_a_bounded_production_write():
    service, session, provider = bounded_setup()
    original = provider.apply_order_operation

    async def cross_deadline(operation, **kwargs):
        if operation['action'] == 'produce':
            provider.server_time_seconds = session.order_task.deadline
        return await original(operation, **kwargs)

    provider.apply_order_operation = cross_deadline
    try:
        await start(service, session)
        await asyncio.wait_for(session.order_task.task, 1)
        assert not any(row['action'] == 'produce' for row in provider.writes)
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_unsettled_cancelled_cost_pauses_explicitly_and_can_be_reconciled():
    service, session, provider = bounded_setup(replace_order=True)
    original = provider.apply_order_operation

    async def change_order_after_cancel(operation, **kwargs):
        result = await original(operation, **kwargs)
        if operation['action'] == 'cancel_production':
            provider.raw['orders']['orders'] = [provider.order('3', [{'item_id': '10', 'count': 97}])]
            provider.raw['orders']['sequence'] += 1
        return result

    provider.apply_order_operation = change_order_after_cancel
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        provider.produce_one()
        await eventually(lambda: session.order_task.task.done())
        assert session.order_task.status == 'uncertain'
        assert '取消' in session.order_task.message and '核对' in session.order_task.message
        assert provider.raw['inventory']['items']['10'] == 98
        state = await service.state(session)
        await service.resolve(session, state['revision'])
        state = await service.state(session)
        await service.start(session, 'synthetic-reconciled-start-03', state['revision'])
        await eventually(lambda: session.order_task.completed == 1)
        assert provider.raw['inventory']['items']['10'] == 1
    finally:
        await close(service, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('warm_book', [False, True])
async def test_mixed_unknown_queue_fields_keep_the_existing_single_batch_path(warm_book):
    service, session, provider = bounded_setup(independent=True)
    if warm_book:
        provider.raw['orders']['orders'] = [provider.order('0', [{'item_id': '11', 'count': 1}])]
        await start(service, session)
        await asyncio.wait_for(session.order_task.task, 1)
    provider.architecture('8')['work_old'].pop('recipe_complete_times')
    provider.raw['orders']['orders'] = [provider.order('1', [{'item_id': '30', 'count': 2}])]
    provider.raw['orders']['sequence'] += 1
    try:
        state = await service.state(session)
        await service.start(session, 'synthetic-mixed-next-run-02', state['revision'])
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        operation = next(row for row in provider.writes if row['action'] == 'produce')
        assert operation['building_uid'] == '8' and operation['recipe_times'] == 1
    finally:
        await close(service, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['inspection', 'home_change'])
async def test_stopped_queue_reservations_cannot_be_bypassed_by_other_home_tasks(operation):
    service, session, provider = bounded_setup()
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        await close(service, session)
        with pytest.raises(ApiError, match='ORDER_ROLE_BUSY'):
            if operation == 'inspection':
                state = await service.state(session)
                await service.start(session, 'synthetic-inspection-bypass-02', state['revision'], kind='inspection')
            else:
                async with service.home_lease(session):
                    pytest.fail('Material reservation was bypassed')
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_demand_above_one_queue_cap_does_not_idle_an_independent_building():
    service, session, provider = bounded_setup(independent=True)
    provider.raw['inventory']['items'].update({'10': 1000, '11': 1000})
    provider.raw['orders']['orders'][0]['materials'] = [
        {'item_id': '20', 'count': 100}, {'item_id': '30', 'count': 200}]
    try:
        await start(service, session)
        await eventually(lambda: len([row for row in provider.writes if row['action'] == 'produce']) == 2)
        assert all(row['recipe_times'] == 10 for row in provider.writes if row['action'] == 'produce')
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_home_lease_owns_the_role_before_awaiting_a_fresh_snapshot():
    service, session, provider = bounded_setup()
    session.order_settings = OrderSettings(allow_production=False)
    service.production_books[tuple(provider.raw['role_key'])] = (provider, QueueBook())
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.order_snapshot

    async def gated_snapshot():
        if asyncio.current_task().get_name() == 'bounded-home-race':
            entered.set()
            await release.wait()
        return await original()

    provider.order_snapshot = gated_snapshot

    async def home_request():
        async with service.home_lease(session):
            assert not session.order_task or not session.order_task.view()['running']

    home = asyncio.create_task(home_request(), name='bounded-home-race')
    try:
        await entered.wait()
        state = await service.state(session)
        with pytest.raises(ApiError, match='ORDER_ROLE_BUSY'):
            await service.start(session, 'synthetic-lease-racing-start-02', state['revision'])
    finally:
        release.set()
        await home
        await close(service, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('external_movement', ['spent', 'received'])
async def test_external_queue_debits_cannot_settle_an_owned_queue_cost(external_movement):
    service, session, provider = bounded_setup()
    external = provider.building('9', '3', '97')
    external['work_old'].update(recipe_id=4, recipe_state=1, recipe_intend_times=1)
    provider.raw['home']['architectures'].append(external)
    provider.raw['home']['workers'].append(provider.worker('9', '97'))
    provider.recipes.append({'building_id': '3', 'recipe_id': '4', 'available_levels': [1],
        'volume': 1, 'work_amount': 100,
        'materials': [{'item_id': '10' if external_movement == 'spent' else '12', 'count': 2}],
        'products': [{'item_id': '40' if external_movement == 'spent' else '20', 'count': 1}]})
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        # One side belongs to the external queue; its matching owned movement
        # has not arrived. A native completed count cannot attribute the source.
        provider.change_inventory('10', -2)
        external['work_old'].update(recipe_id=0, recipe_state=0, recipe_complete_times=1)
        provider.architecture('7')['work_old']['recipe_complete_times'] = 1
        provider.change_inventory('20', 1)
        provider.raw['home']['sequence'] += 1
        times = next(row['recipe_times'] for row in provider.writes if row['action'] == 'produce')
        provider.raw['orders']['orders'] = [provider.order('2', [{'item_id': '10', 'count': 100 - times * 2}])]
        provider.raw['orders']['sequence'] += 1
        observed = provider.snapshot_reads
        await eventually(lambda: provider.snapshot_reads > observed)
        assert not any(row['action'] == 'complete' for row in provider.writes)
    finally:
        await close(service, session)


@pytest.mark.asyncio
@pytest.mark.parametrize('continuous', [False, True])
async def test_confirmed_free_production_does_not_invalidate_an_owned_queue(continuous):
    service, session, provider = bounded_setup(independent=True)
    provider.recipes[2]['materials'] = []
    provider.raw['orders']['orders'][0]['materials'][1]['count'] = 200
    if continuous:
        service.config.building = lambda bid: {'architecture_type': 2 if str(bid) == '2' else 3,
            'work_type_id': 1}
    try:
        await start(service, session)
        await eventually(lambda: len([row for row in provider.writes if row['action'] == 'produce']) == 2
                         or session.order_task.task.done())
        observed = provider.snapshot_reads
        await eventually(lambda: provider.snapshot_reads > observed or session.order_task.task.done())
        assert len([row for row in provider.writes if row['action'] == 'produce']) == 2
        assert session.order_task.status != 'uncertain'
        assert session.order_task.production_book.refresh(provider.raw)
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_completed_external_queue_with_unknown_prior_recipe_cannot_share_owned_materials():
    service, session, provider = bounded_setup()
    external = provider.building('9', '3', '97')
    external['work_old'].update(recipe_intend_times=1, recipe_complete_times=1)
    provider.raw['home']['architectures'].append(external)
    provider.raw['home']['workers'].append(provider.worker('9', '97'))
    provider.recipes.append({'building_id': '3', 'recipe_id': '4', 'available_levels': [1],
        'volume': 1, 'work_amount': 100, 'materials': [{'item_id': '10', 'count': 2}],
        'products': [{'item_id': '40', 'count': 1}]})
    try:
        await start(service, session)
        observed = provider.snapshot_reads
        await eventually(lambda: provider.snapshot_reads > observed)
        assert not any(row['action'] == 'produce' for row in provider.writes)
    finally:
        await close(service, session)


@pytest.mark.asyncio
async def test_potential_external_refund_is_not_an_owned_product_receipt():
    service, session, provider = bounded_setup()
    provider.recipes[0].update(materials=[{'item_id': '11', 'count': 2}],
        products=[{'item_id': '10', 'count': 1}])
    provider.raw['inventory']['items']['10'] = 0
    provider.raw['orders']['orders'][0]['materials'] = [{'item_id': '10', 'count': 3}]
    external = provider.building('9', '3', '97')
    external['work_old'].update(recipe_id=4, recipe_state=1, recipe_intend_times=1)
    provider.raw['home']['architectures'].append(external)
    provider.raw['home']['workers'].append(provider.worker('9', '97'))
    provider.recipes.append({'building_id': '3', 'recipe_id': '4', 'available_levels': [1],
        'volume': 1, 'work_amount': 100, 'materials': [{'item_id': '10', 'count': 2}],
        'products': [{'item_id': '40', 'count': 1}]})
    try:
        await start(service, session)
        await eventually(lambda: any(row['action'] == 'produce' for row in provider.writes))
        provider.change_inventory('11', -2)
        provider.architecture('7')['work_old']['recipe_complete_times'] = 1
        # Synthetic possible cancellation refund, not a claim about real 3403
        # behavior. No owned product receipt has arrived in this scenario.
        external['work_old'].update(recipe_id=0, recipe_state=0,
            recipe_intend_times=0, recipe_complete_times=0)
        provider.change_inventory('10', 2)
        provider.raw['home']['sequence'] += 1
        observed = provider.snapshot_reads
        await eventually(lambda: provider.snapshot_reads > observed)
        assert session.order_task.production_book.active['7'].settled_batches == 0
    finally:
        await close(service, session)
