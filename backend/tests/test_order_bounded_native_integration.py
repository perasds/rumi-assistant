"""Bounded scheduling through native codecs and observed state, without a socket."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.handshake import GameHandshakeResult
from rumi_market.game.home_capacity_state import HomeCapacityState
from rumi_market.game.home_codec import HomeProtoCodec
from rumi_market.game.home_operations import GameHomeOperations
from rumi_market.game.home_state import HomeState
from rumi_market.game.inventory_state import InventoryState
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.game.order_operations import GameOrderOperations
from rumi_market.game.order_state import OrderState
from rumi_market.game.profile_sync import _load_schema
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.home_work_config import HomeWorkConfig
from rumi_market.order_catalog import load_order_catalog
from rumi_market.order_models import OrderSettings
from rumi_market.order_service import OrderService
from rumi_market.sessions import Session, SessionStore


WORKER_UID = 9007199254740993


class NativeBoundedPeer:
    """Only the transport peer is synthetic; all projections consume native bytes."""

    def __init__(self, *, prepaid=False, defer_receipts=False, replace_order=False,
                 wrong_start_ack=False):
        self.can_query, self.closed = True, False
        self.server_time_seconds = 1000
        self.result = GameHandshakeResult(role_id=22, zone_id=11, zone_name='synthetic-zone',
            server_time_ms=1000000, needs_activation=False, needs_initial_pet_selection=False)
        self.home_state, self.inventory_state = HomeState(True), InventoryState(True)
        self.order_state, self.home_capacity_state = OrderState(True), HomeCapacityState(True)
        self.home_codec, self.order_codec = HomeProtoCodec(), OrderProtoCodec()
        self.messages, self.item_message, _ = _load_schema()
        self.home_client = self
        self.prepaid, self.defer_receipts = prepaid, defer_receipts
        self.replace_order, self.wrong_start_ack = replace_order, wrong_start_ack
        self.catalog = load_order_catalog()
        self.calls, self.hand_ins, self.pending_receipts = [], [], {}
        self.bag = {20000: 100, 20001: 500, 20002: 100, 10031: 0, 10032: 0, 54001: 0}
        self.architecture = {'architecture_uid': 12, 'architecture_id': 10101,
            'architecture_level': 2, 'store_info': {},
            'work_old': {'lumi_uid': WORKER_UID, 'lumi_id': 102501,
                'lumi_work_energy': 10000, 'lumi_work_speed': 10000,
                'recipe_id': 0, 'recipe_intend_times': 0, 'recipe_complete_times': 0,
                'recipe_state': 0, 'recipe_work_amount': 0, 'recipe_update_time': 1000}}
        self.home_notice(2971, lumi_list=[{'lumi_uid': WORKER_UID, 'lumi_id': 102501,
                                          'lumi_state': 0}])
        self.home_notice(2913)
        self.home_notice(10093, info={'id': 77})
        self.home_notice(3001, architecture_list=[self.architecture],
            lumi_list=[{'lumi_uid': WORKER_UID, 'work_energy': 10000}],
            order_archi={'slot_list': [self.slot(1, 10031, 1 if replace_order else 3)]})
        self.observe(3036, self.messages[3036]().SerializeToString())
        self.observe(2941, self.messages[2941](item_list=[
            {'item_id': item, 'item_uid': 0, 'count': count}
            for item, count in self.bag.items()]).SerializeToString())

    @staticmethod
    def slot(slot_id, item, count):
        return {'slot_id': slot_id, 'is_unlock': True, 'order_info': {'order_type': 1,
            'item_need': [{'item_id': item, 'count': count}],
            'item_reward': [{'item_id': 54001, 'count': 5}]}}

    def observe(self, command, payload):
        response = ResponseEnvelope(command, 0, 1, 0, 0, payload)
        for state in (self.home_state, self.inventory_state, self.order_state,
                      self.home_capacity_state):
            state.observe(response)

    def home_notice(self, command, **fields):
        self.observe(command, self.home_codec.encode(command, **fields))

    def architecture_notice(self):
        self.home_notice(3003, change_list=[{'change_type': 5,
                                            'architecture_info': deepcopy(self.architecture)}])

    def move_inventory(self, amounts):
        changes = []
        for item, amount in amounts.items():
            self.bag[item] += amount
            assert self.bag[item] >= 0
            data = self.item_message(item_id=item, item_uid=0, count=self.bag[item]).SerializeToString()
            changes.append({'change_type': 3, 'data': data})
        self.observe(2942, self.messages[2942](change_list=changes).SerializeToString())

    def recipe(self):
        return next(row for row in self.catalog['recipes']
            if row['building_id'] == '10101'
            and row['recipe_id'] == str(self.architecture['work_old']['recipe_id']))

    def produce_one(self):
        work, recipe = self.architecture['work_old'], self.recipe()
        assert work['recipe_complete_times'] < work['recipe_intend_times']
        if not self.prepaid:
            self.move_inventory({int(row['item_id']): -row['count'] for row in recipe['materials']})
        rewards = self.architecture['store_info'].setdefault('base_rewards', [])
        for product in recipe['products']:
            item = int(product['item_id'])
            stored = next((row for row in rewards if row['item_id'] == item), None)
            if stored is None:
                rewards.append({'item_id': item, 'count': product['count']})
            else:
                stored['count'] += product['count']
        work['recipe_complete_times'] += 1
        work['recipe_work_amount'] = 0
        if work['recipe_complete_times'] == work['recipe_intend_times']:
            work.update(recipe_id=0, recipe_state=0)
        self.architecture_notice()

    def release_receipts(self):
        if self.pending_receipts:
            self.move_inventory(self.pending_receipts)
            self.pending_receipts = {}

    async def request(self, command, response_command, payload, *, before_send):
        assert before_send() is True
        codec = self.home_codec if command == 3403 else self.order_codec
        request = codec.decode(command, payload)
        fields = {field.name: value for field, value in request.ListFields()}
        self.calls.append((command, fields))
        if command == 3403:
            assert response_command == 3404
            assert request.archi_uid == 12 and request.archi_id == 10101
            ack = {'archi_uid': 12, 'archi_id': 10101, 'op': request.op}
            work = self.architecture['work_old']
            if request.op == 1:
                assert work['recipe_id'] == 0 and request.recipe_times > 1
                work.update(recipe_id=request.recipe_id, recipe_intend_times=request.recipe_times,
                    recipe_complete_times=0, recipe_state=1, recipe_work_amount=0)
                if self.prepaid:
                    self.move_inventory({int(row['item_id']): -row['count'] * request.recipe_times
                        for row in self.recipe()['materials']})
                if self.wrong_start_ack:
                    ack['archi_uid'] = 99
            else:
                assert request.op == 2 and request.recipe_id == work['recipe_id']
                assert request.recipe_times == 0
                work.update(recipe_id=0, recipe_intend_times=0, recipe_complete_times=0,
                            recipe_state=0, recipe_work_amount=0)
            # Real receive observers can publish configuration before its ACK.
            self.architecture_notice()
        elif command == 3027:
            assert response_command == 3028 and fields == {} and payload == b''
            for row in self.architecture['store_info'].get('base_rewards', []):
                item = row['item_id']
                self.pending_receipts[item] = self.pending_receipts.get(item, 0) + row['count']
            if self.architecture['store_info'].get('base_rewards'):
                self.architecture['store_info'] = {}
                self.architecture_notice()
            if not self.defer_receipts:
                self.release_receipts()
            ack = {}
        else:
            assert command == 3302 and response_command == 3303
            assert fields == {'slot_id': 1}
            required = 1 if self.replace_order else 3
            assert self.inventory_state.snapshot()['items']['10031'] >= required
            self.hand_ins.append(deepcopy(self.home_state.snapshot()['architectures'][0]['work_old']))
            self.move_inventory({10031: -required, 54001: 5})
            slots = [self.slot(2, 10032, 1)] if self.replace_order else []
            self.observe(3301, self.order_codec.encode(3301, order_archi={'slot_list': slots}))
            ack = {'reward': [{'item_id': 54001, 'count': 5}]}
        return ResponseEnvelope(response_command, 1, 0, 0, 0,
                                codec.encode(response_command, **ack))

    async def start(self, account, password):
        return self.result


async def native_setup(peer):
    provider = IndependentGameProvider(peer, SimpleNamespace(items=[]), login_verified=True)
    view = await provider.login('synthetic-account', 'synthetic-password')
    store = SessionStore(lambda: provider)
    session = Session('synthetic-native-bounded', provider, view=view)
    store.sessions[session.id] = session
    service = OrderService(store, sync_timeout=.2)
    # This fixture targets native queue confirmation rather than default policy.
    service.auto_production_exclusions = frozenset()
    session.order_settings = OrderSettings(allow_production=True,
        max_orders=2 if peer.replace_order else 1)
    return service, session, provider


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


async def stop_task(service, session):
    if session.order_task and not session.order_task.task.done():
        await service.stop(session)
        await asyncio.wait_for(session.order_task.task, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('prepaid', [False, True])
async def test_native_partial_receipts_hand_in_before_bounded_buffer_finishes(prepaid):
    peer = NativeBoundedPeer(prepaid=prepaid)
    service, session, provider = await native_setup(peer)
    raw = await provider.order_snapshot()
    assert raw['home']['capacity_state']['valid']
    assert raw['home']['architectures'][0]['capacity_remaining'] == HomeWorkConfig().level(10101, 2)['capacity']
    assert raw['inventory']['movement_epoch'] == 0
    assert raw['home']['roster_ready'] and raw['home']['dispatch_ready']
    assert service._buildings(raw)[0]['worker_valid']
    state = await service.state(session)
    assert state['execution_ready'] and not state['orders'][0]['can_complete']
    try:
        await service.start(session, 'synthetic-native-bounded-partial', state['revision'])
        await eventually(lambda: any(command == 3403 for command, _ in peer.calls))
        assert isinstance(provider._order_production_operations, GameHomeOperations)
        starts = [fields for command, fields in peer.calls if command == 3403]
        assert starts == [{'archi_id': 10101, 'archi_uid': 12, 'recipe_id': 1,
                           'recipe_times': 4, 'op': 1}]
        for completed in (1, 2):
            peer.produce_one()
            await eventually(lambda: peer.inventory_state.snapshot()['items']['10031'] == completed)
            assert not peer.hand_ins
        peer.produce_one()
        await asyncio.wait_for(session.order_task.task, 2)
        assert isinstance(provider._order_operations, GameOrderOperations)
        assert session.order_task.status == 'completed' and not session.order_task.uncertain
        assert session.order_task.completed == 1 and session.order_task.cans == 5
        assert peer.hand_ins[0]['recipe_id'] == 1
        assert peer.hand_ins[0]['recipe_complete_times'] == 3
        assert peer.hand_ins[0]['recipe_intend_times'] == 4
        assert sum(command == 3302 for command, _ in peer.calls) == 1
        assert sum(command == 3027 for command, _ in peer.calls) == 4
        inventory = peer.inventory_state.snapshot()
        assert inventory['received_totals']['10031'] == 3
        assert inventory['spent_totals']['10031'] == 3
        assert inventory['spent_totals']['20001'] == (20 if prepaid else 15)
        assert inventory['items']['10031'] == 0 and inventory['items']['54001'] == 5
    finally:
        await stop_task(service, session)


@pytest.mark.asyncio
async def test_native_empty_collection_ack_needs_real_inventory_receipt_before_hand_in():
    peer = NativeBoundedPeer(defer_receipts=True)
    service, session, _ = await native_setup(peer)
    try:
        await service.start(session, 'synthetic-native-bounded-late-bag',
                            (await service.state(session))['revision'])
        await eventually(lambda: any(command == 3403 for command, _ in peer.calls))
        for completed in (1, 2, 3):
            peer.produce_one()
            await eventually(lambda: peer.pending_receipts.get(10031) == completed)
        assert not peer.hand_ins and session.order_task.completed == 0
        assert not session.order_task.task.done() and not session.order_task.uncertain
        assert peer.inventory_state.snapshot()['items']['10031'] == 0
        assert peer.home_state.snapshot()['architectures'][0]['store_items'] == []
        peer.release_receipts()
        await asyncio.wait_for(session.order_task.task, 2)
        assert session.order_task.status == 'completed' and session.order_task.completed == 1
        assert peer.hand_ins[0]['recipe_complete_times'] == 3
        assert peer.hand_ins[0]['recipe_intend_times'] == 4
    finally:
        await stop_task(service, session)


@pytest.mark.asyncio
async def test_native_wrong_production_ack_freezes_after_one_start_even_with_matching_notice():
    peer = NativeBoundedPeer(wrong_start_ack=True)
    service, session, provider = await native_setup(peer)
    await service.start(session, 'synthetic-native-bounded-wrong-ack',
                        (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 2)
    assert session.order_task.status == 'uncertain'
    assert session.order_task.uncertain and session.order_task.completed == 0
    assert provider._order_production_operations.blocked
    assert [command for command, _ in peer.calls] == [3027, 3403]
    assert peer.home_state.snapshot()['architectures'][0]['work_old']['recipe_id'] == 1
    assert service.roles[provider.order_identity] is session.order_task


@pytest.mark.asyncio
async def test_native_cancel_op2_confirms_idle_before_replacement_recipe_start():
    peer = NativeBoundedPeer(replace_order=True)
    service, session, _ = await native_setup(peer)
    try:
        await service.start(session, 'synthetic-native-bounded-replace',
                            (await service.state(session))['revision'])
        await eventually(lambda: any(command == 3403 for command, _ in peer.calls))
        peer.produce_one()
        await eventually(lambda: any(command == 3403 and fields['recipe_id'] == 2
                                    for command, fields in peer.calls))
        production = [fields for command, fields in peer.calls if command == 3403]
        assert [(row['op'], row['recipe_id'], row['recipe_times']) for row in production] == [
            (1, 1, 2), (2, 1, 0), (1, 2, 2)]
        commands = [command for command, _ in peer.calls]
        assert commands.index(3302) < next(i for i, (command, fields) in enumerate(peer.calls)
                                         if command == 3403 and fields['op'] == 2)
        assert session.order_task.completed == 1 and not session.order_task.uncertain
        assert peer.inventory_state.snapshot()['items']['20001'] == 495
        assert peer.home_state.snapshot()['architectures'][0]['work_old']['recipe_id'] == 2
    finally:
        await stop_task(service, session)
