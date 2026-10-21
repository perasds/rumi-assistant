"""Native work updates advance server production without collecting or editing."""
import asyncio
from copy import deepcopy

import pytest

from rumi_market.game.client import GameClientError
from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.game.order_operations import GameOrderOperations
from rumi_market.order_service import order_revision
from test_order_operations import snapshot


class WorkClient:
    closed = False

    def __init__(self, raw, behavior='ack'):
        self.raw, self.behavior, self.calls = raw, behavior, []
        self.before = None

    async def request(self, command, response_command, payload, *, before_send):
        if self.before:
            self.before()
        if not before_send():
            raise GameClientError('GAME_REQUEST_NOT_SENT')
        assert (command, response_command, payload) == (3029, 3030, b'')
        self.calls.append((command, response_command, payload))
        if self.behavior == 'timeout':
            raise GameClientError('GAME_REQUEST_TIMEOUT')
        if self.behavior == 'cancel':
            raise asyncio.CancelledError()
        if self.behavior == 'rejected':
            raise GameClientError('GAME_REQUEST_REJECTED', response_code=7)
        return ResponseEnvelope(response_command, 1, 0, 0, 0,
                                b'\xff' if self.behavior == 'malformed' else b'')


def setup(behavior='ack'):
    raw, stop, allowed = snapshot(), asyncio.Event(), [True]
    client = WorkClient(raw, behavior)
    ops = GameOrderOperations(client, lambda: deepcopy(raw), lambda: allowed[0])
    return ops, client, raw, stop, allowed


@pytest.mark.asyncio
async def test_update_work_uses_exact_empty_native_messages_and_can_repeat_unchanged_state():
    codec = OrderProtoCodec()
    for command in (3029, 3030):
        assert not codec.message_class(command).DESCRIPTOR.fields
        assert codec.encode(command) == b''
    ops, client, raw, stop, _ = setup()
    raw['home']['architectures'][0]['store_items'] = []
    for _ in range(2):
        result = await ops.apply({'action': 'update_work'},
            expected_revision=order_revision(raw), stop_requested=stop.is_set)
        assert result == {'status': 'applied', 'rewards': []}
    assert client.calls == [(3029, 3030, b''), (3029, 3030, b'')]
    assert not ops.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['revision', 'stop', 'write', 'closed'])
async def test_update_work_rechecks_send_gate(mutation):
    ops, client, raw, stop, allowed = setup()
    revision = order_revision(raw)
    def change():
        if mutation == 'revision': raw['home']['sequence'] += 1
        elif mutation == 'stop': stop.set()
        elif mutation == 'write': allowed[0] = False
        else: client.closed = True
    client.before = change
    result = await ops.apply({'action': 'update_work'},
        expected_revision=revision, stop_requested=stop.is_set)
    assert result['status'] == 'rejected' and client.calls == [] and not ops.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior', ['timeout', 'malformed'])
async def test_unknown_work_update_retains_native_lock_and_never_retries(behavior):
    ops, client, raw, stop, _ = setup(behavior)
    for _ in range(2):
        result = await ops.apply({'action': 'update_work'},
            expected_revision=order_revision(raw), stop_requested=stop.is_set)
        assert result['status'] == 'uncertain'
    assert len(client.calls) == 1 and ops.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', [
    {'action': 'update_work', 'recipe_id': '1'},
    {'action': 'update_work', 'building_uid': '123'},
    {'action': 'update_work', 'lumi_uid': '123'},
])
async def test_work_update_cannot_accept_edit_or_worker_parameters(operation):
    ops, client, raw, stop, _ = setup()
    result = await ops.apply(operation,
        expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result['status'] == 'rejected' and client.calls == []
