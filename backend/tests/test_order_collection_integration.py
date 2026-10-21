"""Order tasks collect into the native inventory projection before handing in."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.handshake import GameHandshakeResult
from rumi_market.game.home_codec import HomeProtoCodec
from rumi_market.game.home_state import HomeState
from rumi_market.game.inventory_state import InventoryState
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.game.order_operations import GameOrderOperations
from rumi_market.game.order_state import OrderState
from rumi_market.game.profile_sync import _load_schema
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.order_models import OrderSettings
from test_order_service import setup


class CollectionRuntime:
    """Synthetic peer; all writes still cross the real native operation codec."""

    def __init__(self, first_notice='none', *, production_notice=False, empty_store=False):
        self.can_query, self.closed = True, False
        self.server_time_seconds = 1000
        self.result = GameHandshakeResult(role_id=22, zone_id=11, zone_name='synthetic-zone',
            server_time_ms=1000000, needs_activation=False, needs_initial_pet_selection=False)
        self.home_state = HomeState(verified_version=True)
        self.inventory_state = InventoryState(True)
        self.order_state = OrderState(True)
        self.home_codec, self.order_codec = HomeProtoCodec(), OrderProtoCodec()
        self.inventory_messages, _, _ = _load_schema()
        self.home_client = self
        self.calls, self.delivered = [], []
        self.first_notice = first_notice
        self.production_notice = production_notice
        self.production_notifications = 0
        self.claim_acked = asyncio.Event()
        self.architecture = {'architecture_uid': 12, 'architecture_id': 10101,
            'architecture_level': 1, 'work_old': {'lumi_uid': 0, 'recipe_id': 0},
            'store_info': {'base_rewards': [{'item_id': 20001, 'count': 3}]}}
        slot = {'slot_id': 1, 'is_unlock': True, 'order_info': {'order_type': 1,
            'item_need': [{'item_id': 20001, 'count': 3}],
            'item_reward': [{'item_id': 54001, 'count': 5}]}}
        initial_architecture = deepcopy(self.architecture)
        if production_notice or empty_store:
            initial_architecture['store_info'] = {}
        self.observe(3001, self.home_codec.encode(3001, architecture_list=[initial_architecture],
            order_archi={'slot_list': [slot]}))
        self.inventory(0)

    def observe(self, command, payload):
        notice = ResponseEnvelope(command, 0, 1, 0, 0, payload)
        for state in (self.home_state, self.inventory_state, self.order_state):
            state.observe(notice)

    def inventory(self, material_count, canned_count=0):
        self.observe(2941, self.inventory_messages[2941](item_list=[
            {'item_id': 20001, 'item_uid': 0, 'count': material_count},
            {'item_id': 54001, 'item_uid': 0, 'count': canned_count}]).SerializeToString())

    def deliver(self, kind):
        self.delivered.append(kind)
        if kind == 'inventory':
            self.inventory(3)
        elif kind == 'store':
            architecture = deepcopy(self.architecture)
            architecture['store_info'] = {}
            self.observe(3003, self.home_codec.encode(3003, change_list=[
                {'change_type': 5, 'architecture_info': architecture}]))

    async def request(self, command, response_command, payload, *, before_send):
        assert before_send() is True
        request = self.order_codec.decode(command, payload)
        fields = {field.name: value for field, value in request.ListFields()}
        self.calls.append((command, response_command, fields))
        if command == 3027:
            assert response_command == 3028 and fields == {} and payload == b''
            if self.production_notice:
                self.observe(3003, self.home_codec.encode(3003, change_list=[
                    {'change_type': 5, 'architecture_info': self.architecture}]))
                self.production_notice = False
                self.production_notifications += 1
            self.deliver(self.first_notice)
            self.claim_acked.set()
            ack = {}
        else:
            assert command == 3302 and fields == {'slot_id': 1}
            assert self.inventory_state.snapshot()['items']['20001'] == 3
            self.inventory(0, 5)
            self.observe(3301, self.order_codec.encode(3301, order_archi={'slot_list': []}))
            ack = {'reward': [{'item_id': 54001, 'count': 5}]}
        return ResponseEnvelope(response_command, 1, 0, 0, 0,
                                self.order_codec.encode(response_command, **ack))

    async def start(self, account, password):
        return self.result

async def native_setup(runtime):
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=[]), login_verified=True)
    view = await provider.login('synthetic-account', 'synthetic-password')
    service, session, _ = setup(provider)
    session.view = view
    session.order_settings = OrderSettings(max_orders=1, allow_production=False)
    return service, session, provider


@pytest.mark.asyncio
@pytest.mark.parametrize('empty_store', [False, True])
async def test_native_bulk_ack_then_inventory_without_home_store_notice_funds_order(empty_store):
    runtime = CollectionRuntime(empty_store=empty_store)
    service, session, provider = await native_setup(runtime)
    before_home = runtime.home_state.snapshot()
    state = await service.state(session)
    assert state['execution_ready'] and not state['orders'][0]['can_complete']

    await service.start(session, 'synthetic-native-bulk-late-inventory', state['revision'])
    try:
        await asyncio.wait_for(runtime.claim_acked.wait(), 1)
        await asyncio.sleep(.03)
        assert isinstance(provider._order_operations, GameOrderOperations)
        assert runtime.calls == [(3027, 3028, {})]
        assert not session.order_task.task.done()
        assert not session.order_task.uncertain and session.order_task.completed == 0
        assert runtime.inventory_state.snapshot()['items']['20001'] == 0
        assert runtime.home_state.snapshot() == before_home

        runtime.deliver('inventory')
        await asyncio.wait_for(session.order_task.task, 1)

        assert runtime.calls == [(3027, 3028, {}), (3302, 3303, {'slot_id': 1})]
        assert session.order_task.status == 'completed'
        assert session.order_task.completed == 1 and session.order_task.cans == 5
        assert runtime.home_state.snapshot() == before_home
        assert runtime.inventory_state.snapshot()['items'] == {'20001': 0, '54001': 5}
        assert runtime.inventory_state.snapshot()['received_totals']['20001'] == 3
        assert runtime.inventory_state.snapshot()['spent_totals']['20001'] == 3
    finally:
        if not session.order_task.task.done():
            await service.stop(session)
            await asyncio.wait_for(session.order_task.task, 1)


@pytest.mark.asyncio
async def test_native_bulk_ack_without_inventory_keeps_waiting_and_never_hands_in():
    runtime = CollectionRuntime('store')
    service, session, _ = await native_setup(runtime)
    await service.start(session, 'synthetic-native-bulk-no-inventory',
        (await service.state(session))['revision'])
    try:
        await asyncio.wait_for(runtime.claim_acked.wait(), 1)
        await asyncio.sleep(.03)
        assert runtime.calls == [(3027, 3028, {})]
        assert runtime.home_state.snapshot()['architectures'][0]['store_items'] == []
        assert runtime.inventory_state.snapshot()['items']['20001'] == 0
        assert session.order_task.completed == 0
        assert session.order_task.status == 'waiting_materials'
        assert not session.order_task.uncertain and not session.order_task.in_flight
        assert not session.order_task.task.done()
    finally:
        await service.stop(session)
        await asyncio.wait_for(session.order_task.task, 1)
    assert session.order_task.status == 'stopped'
    assert runtime.calls == [(3027, 3028, {})]


@pytest.mark.asyncio
async def test_native_inventory_before_empty_bulk_ack_needs_no_store_notification():
    runtime = CollectionRuntime('inventory')
    service, session, _ = await native_setup(runtime)
    before_home = runtime.home_state.snapshot()
    await service.start(session, 'synthetic-native-bulk-early-inventory',
        (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    assert runtime.calls == [(3027, 3028, {}), (3302, 3303, {'slot_id': 1})]
    assert session.order_task.status == 'completed'
    assert runtime.home_state.snapshot() == before_home
    assert runtime.inventory_state.snapshot()['items'] == {'20001': 0, '54001': 5}
