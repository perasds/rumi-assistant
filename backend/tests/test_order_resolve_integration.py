"""Reconciliation crosses the service/provider/native-operation boundary."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from rumi_market.game.handshake import GameHandshakeResult
from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.order_models import OrderSettings
from test_order_operations import Client
from test_order_service import raw, setup


class SnapshotState:
    def __init__(self, getter):
        self.getter = getter

    def snapshot(self):
        return deepcopy(self.getter())


class ReconciliationClient(Client):
    async def request(self, command, response_command, payload, *, before_send):
        if command == 3027:
            assert response_command == 3028 and payload == b''
            assert before_send() is True
            self.calls.append((3027, 3028, {}))
            return ResponseEnvelope(3028, 1, 0, 0, 0, OrderProtoCodec().encode(3028))
        # Only handing in the order is allowed to time out in this scenario.
        assert command == 3302
        return await super().request(command, response_command, payload, before_send=before_send)


class Runtime:
    def __init__(self):
        self.raw = raw()
        self.can_query = True
        self.server_time_seconds = self.raw['server_time_seconds']
        self.result = GameHandshakeResult(role_id=22, zone_id=11, zone_name='synthetic-zone',
            server_time_ms=1000000, needs_activation=False, needs_initial_pet_selection=False)
        self.home_state = SnapshotState(lambda: self.raw['home'])
        self.inventory_state = SnapshotState(lambda: self.raw['inventory'])
        self.order_state = SnapshotState(lambda: self.raw['orders'])
        self.home_client = ReconciliationClient(self.raw, 'timeout')
        self.home_client.after = self.apply_confirmed_state

    async def start(self, account, password):
        return self.result

    async def apply_confirmed_state(self):
        if self.home_client.behavior == 'timeout':
            return
        _, _, fields = self.home_client.calls[-1]
        order = next(row for row in self.raw['orders']['orders'] if row['slot_id'] == str(fields['slot_id']))
        self.raw['orders']['orders'].remove(order)
        self.raw['orders']['sequence'] += 1
        for item in order['materials']:
            self.raw['inventory']['items'][item['item_id']] -= item['count']
        self.raw['inventory']['sequence'] += 1

    async def close(self):
        self.can_query = False


@pytest.mark.asyncio
async def test_manual_resolve_resets_native_unknown_lock_and_allows_only_a_new_explicit_task():
    runtime = Runtime()
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=[]), login_verified=True)
    await provider.login('synthetic-account', 'synthetic-password')
    service, session, _ = setup(provider)
    session.order_settings = OrderSettings(max_orders=1)
    initial = await service.state(session)
    await service.start(session, 'synthetic-native-first-01', initial['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    assert session.order_task.status == 'uncertain'
    assert provider._order_operations.blocked is True
    assert [command for command, _, _ in runtime.home_client.calls] == [3027, 3302]

    # A fresh complete state plus the explicit resolve action is the user ACK.
    current = await service.state(session)
    result = await service.resolve(session, current['revision'])
    assert result['task']['status'] == 'stopped'
    assert len(runtime.home_client.calls) == 2  # resolve itself never retries.
    await service.start(session, 'synthetic-native-first-01', initial['revision'])
    assert session.order_task.status == 'stopped'
    assert len(runtime.home_client.calls) == 2  # Replaying the old request is also inert.

    runtime.home_client.behavior = 'ack'
    await service.start(session, 'synthetic-native-next-01', (await service.state(session))['revision'])
    await asyncio.wait_for(session.order_task.task, 1)
    assert session.order_task.status == 'completed'
    assert session.order_task.completed == 1
    assert [command for command, _, _ in runtime.home_client.calls] == [3027, 3302, 3027, 3302]
