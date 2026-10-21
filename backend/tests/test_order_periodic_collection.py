"""Orders use native bulk collection; inventory notices alone fund orders."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import rumi_market.order_service as order_module
from rumi_market.errors import ApiError
from rumi_market.order_models import OrderSettings
from rumi_market.order_service import order_revision
from rumi_market.sessions import Session
from test_order_service import Provider, production_setup, setup


def building(uid, item='20001', count=2, **changes):
    row = {'uid': uid, 'id': 10101, 'level': 1,
           'work_old': {'recipe_id': 0, 'lumi_uid': '0'},
           'store_items': [{'item_id': item, 'count': count}]}
    row.update(changes)
    return row


class CollectionProvider(Provider):
    def __init__(self, buildings=None):
        super().__init__()
        self.raw['inventory']['items']['1'] = 0
        self.raw['home']['architectures'] = [building(1)] if buildings is None else buildings
        self.claim_outcome = 'applied'
        self.after_claim = self.after_collect = self.after_complete = None
        self.inventory_notice = self.store_notice = self.credit_rewards = True
        self.empty_ack = False

    def collect_row(self, row):
        rewards = deepcopy((row.get('store_items') or []) + (row.get('store_extra_items') or []))
        if self.inventory_notice and self.credit_rewards:
            for reward in rewards:
                self.change_inventory(reward['item_id'], reward['count'])
        if self.store_notice:
            row['store_items'], row['store_extra_items'] = [], []
            self.raw['home']['sequence'] += 1
        return rewards

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        assert expected_revision == order_revision(self.raw)
        if operation['action'] not in {'claim', 'claim_all'}:
            result = await super().apply_order_operation(operation,
                expected_revision=expected_revision, stop_requested=stop_requested)
            if operation['action'] == 'complete' and self.after_complete:
                self.after_complete()
            return result
        if stop_requested():
            return {'status': 'rejected', 'rewards': []}
        self.writes.append(deepcopy(operation))
        if self.claim_outcome != 'applied':
            return {'status': self.claim_outcome, 'rewards': []}
        if operation['action'] == 'claim_all':
            assert operation == {'action': 'claim_all'}
            for row in self.raw['home']['architectures']:
                self.collect_row(row)
            if self.after_collect:
                self.after_collect()
            return {'status': 'applied', 'rewards': []}  # Native 3028 has no rewards.
        row = next(row for row in self.raw['home']['architectures']
                   if str(row['uid']) == operation['building_uid'])
        rewards = self.collect_row(row)
        if self.after_claim:
            self.after_claim(row)
        return {'status': 'applied', 'rewards': [] if self.empty_ack else rewards}


async def stop_at_first_wait(run, seconds=.25):
    run.stop.set()
    await asyncio.sleep(0)


async def start_and_finish(service, session, *, kind='orders'):
    state = await service.state(session)
    await service.start(session, 'synthetic-periodic-collection', state['revision'], kind=kind)
    await asyncio.wait_for(session.order_task.task, 1)


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


def restock(provider, uid=1):
    row = next(row for row in provider.raw['home']['architectures'] if row['uid'] == uid)
    row['store_items'] = [{'item_id': '20001', 'count': 2}]
    provider.raw['home']['sequence'] += 1


@pytest.mark.asyncio
@pytest.mark.parametrize('allow_production', [False, True])
async def test_start_uses_one_native_bulk_claim_for_all_stores(monkeypatch, allow_production):
    service, session, provider = setup(CollectionProvider([
        building(1, '20001', 2), building(2, '20002', 3)]))
    session.order_settings = OrderSettings(allow_production=allow_production)
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await start_and_finish(service, session)
    assert provider.writes == [{'action': 'claim_all'}]
    assert provider.raw['inventory']['items']['20001'] == 2
    assert provider.raw['inventory']['items']['20002'] == 3
    assert session.order_task.status == 'stopped'
    assert not session.order_task.in_flight


@pytest.mark.asyncio
async def test_collection_waits_60_seconds_and_does_not_catch_up_missed_rounds(monkeypatch):
    service, session, provider = setup(CollectionProvider())
    clock, observed = FakeClock(), []
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))

    async def advance(run, seconds=.25):
        observed.append((clock.now, len(provider.writes)))
        step = len(observed)
        if step == 1:
            restock(provider)
            clock.now = 59
        elif step == 2:
            clock.now = 60
        elif step == 3:
            restock(provider)
            clock.now = 240
        elif step == 4:
            restock(provider)
        else:
            run.stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', advance)
    await start_and_finish(service, session)
    assert observed == [(0, 1), (59, 1), (60, 2), (240, 3), (240, 3)]
    assert provider.writes == [{'action': 'claim_all'}] * 3
    assert session.order_task.status == 'stopped'


@pytest.mark.asyncio
async def test_long_collection_round_allows_funded_order_before_collecting_again(monkeypatch):
    service, session, provider = setup(CollectionProvider())
    session.order_settings = OrderSettings(max_orders=1)
    provider.raw['inventory']['items']['1'] = 10
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))

    def slow_round():
        clock.now += 70
        restock(provider)

    provider.after_collect = slow_round
    await start_and_finish(service, session)
    assert [row['action'] for row in provider.writes] == ['claim_all', 'complete']
    assert provider.raw['home']['architectures'][0]['store_items']
    assert session.order_task.status == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'store_items': None}, {'store_items': []},
    {'store_items': [{'item_id': '20001', 'count': 0}]},
    {'store_items': [], 'store_extra_items': [{'item_id': '20001', 'count': 2}]},
    {'has_farm_data': True}, {'cur_recipe': {'recipe_id': 7}}, {'work_old': None},
])
async def test_bulk_claim_does_not_require_local_store_or_work_branch_readiness(monkeypatch, changes):
    service, session, provider = setup(CollectionProvider([building(1, **changes)]))
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await start_and_finish(service, session)
    assert provider.writes == [{'action': 'claim_all'}]
    assert session.order_task.status == 'stopped'
    assert not session.order_task.uncertain


@pytest.mark.asyncio
async def test_state_and_plan_reads_never_collect_stores():
    service, session, provider = setup(CollectionProvider())
    for _ in range(2):
        state = await service.state(session)
        await service.plan(session, state['revision'], OrderSettings(allow_production=True))
    assert provider.writes == []
    assert session.order_task is None


@pytest.mark.asyncio
async def test_inspection_does_not_gain_periodic_bulk_collection(monkeypatch):
    service, session, provider = setup(CollectionProvider())
    provider.raw['home'].update(roster_ready=True, dispatch_ready=True)
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await start_and_finish(service, session, kind='inspection')
    assert provider.writes == []
    assert session.order_task.status == 'stopped'


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['stop', 'deadline', 'session-exit', 'role-change'])
async def test_task_end_prevents_later_periodic_collection(monkeypatch, ending):
    service, session, provider = setup(CollectionProvider())
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))

    async def end_task(run, seconds=.25):
        restock(provider)
        clock.now = 60
        if ending == 'stop':
            await service.stop(session)
        elif ending == 'deadline':
            provider.raw['server_time_seconds'] = run.deadline
        elif ending == 'session-exit':
            session.active = False
        else:
            provider.raw['role_key'] = ('8', '9007199254740993')
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', end_task)
    await start_and_finish(service, session)
    assert provider.writes == [{'action': 'claim_all'}]
    assert provider.raw['home']['architectures'][0]['store_items']
    assert session.order_task.status == {'stop': 'stopped', 'deadline': 'completed',
        'session-exit': 'failed', 'role-change': 'failed'}[ending]
    assert not service.roles


@pytest.mark.asyncio
async def test_reaching_order_target_prevents_another_due_collection(monkeypatch):
    service, session, provider = setup(CollectionProvider())
    session.order_settings = OrderSettings(max_orders=1)
    provider.raw['inventory']['items']['1'] = 10
    clock = FakeClock()
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))

    def after_complete():
        restock(provider)
        clock.now = 180

    provider.after_complete = after_complete
    await start_and_finish(service, session)
    assert [row['action'] for row in provider.writes] == ['claim_all', 'complete']
    assert provider.raw['home']['architectures'][0]['store_items']
    assert session.order_task.status == 'completed'
    assert not service.roles


@pytest.mark.asyncio
async def test_unknown_bulk_claim_keeps_role_lock_and_prevents_other_actions(monkeypatch):
    service, session, provider = setup(CollectionProvider())
    provider.claim_outcome = 'uncertain'
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await start_and_finish(service, session)
    assert provider.writes == [{'action': 'claim_all'}]
    assert session.order_task.status == 'uncertain' and session.order_task.uncertain
    assert service.roles[tuple(provider.raw['role_key'])] is session.order_task
    other = Session('second-periodic-browser', CollectionProvider(), view=session.view)
    with pytest.raises(ApiError, match='ORDER_ROLE_BUSY'):
        await service.start(other, 'synthetic-periodic-other', (await service.state(other))['revision'])


@pytest.mark.asyncio
@pytest.mark.parametrize('ack_received', [False, True])
async def test_cancelling_bulk_request_distinguishes_pending_ack_from_empty_success(ack_received, monkeypatch):
    service, session, provider = setup(CollectionProvider([]))
    sent, waiting, release_ack = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def apply(operation, *, expected_revision, stop_requested):
        assert operation == {'action': 'claim_all'}
        assert not stop_requested()
        provider.writes.append(deepcopy(operation))
        sent.set()
        if not ack_received:
            await release_ack.wait()
        return {'status': 'applied', 'rewards': []}

    async def wait_for_cancellation(run, seconds=.25):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, 'apply_order_operation', apply)
    monkeypatch.setattr(service, '_wait', wait_for_cancellation)
    await service.start(session, 'synthetic-cancel-native-bulk', (await service.state(session))['revision'])
    await asyncio.wait_for(waiting.wait() if ack_received else sent.wait(), 1)
    session.order_task.task.cancel()
    await asyncio.wait_for(session.order_task.task, 1)
    assert provider.writes == [{'action': 'claim_all'}]
    assert session.order_task.status == ('stopped' if ack_received else 'uncertain')
    assert session.order_task.uncertain is (not ack_received)
    assert bool(service.roles) is (not ack_received)


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['deadline', 'role-change'])
async def test_collection_revalidates_at_actual_send_gate(monkeypatch, ending):
    service, session, provider = setup(CollectionProvider())

    async def change_before_send(operation, *, expected_revision, stop_requested):
        if ending == 'deadline':
            provider.raw['server_time_seconds'] = session.order_task.deadline
            provider.server_time_seconds = session.order_task.deadline
        else:
            provider.raw['role_key'] = ('8', '9007199254740993')
        assert stop_requested()
        return {'status': 'rejected', 'rewards': []}

    monkeypatch.setattr(provider, 'apply_order_operation', change_before_send)
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await start_and_finish(service, session)
    assert provider.writes == []
    assert not session.order_task.uncertain
    assert not service.roles


@pytest.mark.asyncio
@pytest.mark.parametrize('notice', ['none', 'inventory-only'])
async def test_empty_bulk_ack_does_not_require_store_notification_or_invent_inventory(monkeypatch, notice):
    service, session, provider = setup(CollectionProvider([building(1, '1', 10)]))
    session.order_settings = OrderSettings(max_orders=1)
    provider.store_notice = False
    provider.inventory_notice = notice == 'inventory-only'
    monkeypatch.setattr(service, '_wait', stop_at_first_wait)
    await start_and_finish(service, session)
    assert [row['action'] for row in provider.writes] == (
        ['claim_all', 'complete'] if notice == 'inventory-only' else ['claim_all'])
    assert session.order_task.status == ('completed' if notice == 'inventory-only' else 'stopped')
    assert not session.order_task.uncertain
    assert provider.raw['home']['architectures'][0]['store_items']


@pytest.mark.asyncio
async def test_periodic_collection_continues_while_material_batch_is_reserved(monkeypatch):
    service, session, provider = production_setup(False)
    provider.raw['home']['architectures'].append(building(8, store_items=[]))
    clock, observed = FakeClock(), []
    monkeypatch.setattr(order_module, 'time', SimpleNamespace(monotonic=clock))

    async def advance(run, seconds=.25):
        observed.append((clock.now, run.in_flight, provider.raw['inventory']['items'].get('20001', 0)))
        if len(observed) == 1:
            restock(provider, 8)
            clock.now = 60
        elif len(observed) == 2:
            provider.produced()
            clock.now = 120
        else:
            run.stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(service, '_wait', advance)
    await start_and_finish(service, session)
    assert observed[:2] == [(0, True, 0), (60, True, 2)]
    assert [row['action'] for row in provider.writes] == ['produce', 'claim_all', 'claim_all', 'complete']
    assert provider.raw['inventory']['items']['10'] == 8
    assert session.order_task.status == 'completed' and not session.order_task.in_flight
