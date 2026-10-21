"""Native one-key collection uses an empty ACK and never infers rewards."""
import asyncio
from copy import deepcopy

import pytest

from rumi_market.game.client import GameClientError
from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.game.order_operations import GameOrderOperations
from rumi_market.order_service import order_revision
from test_order_operations import snapshot


class ClaimAllClient:
    closed = False

    def __init__(self, behavior='ack'):
        self.behavior, self.calls = behavior, []
        self.before = self.after = None

    async def request(self, command, response_command, payload, *, before_send):
        if self.before:
            self.before()
        if not before_send():
            raise GameClientError('GAME_REQUEST_NOT_SENT')
        self.calls.append((command, response_command, payload))
        if command == 3302:
            return ResponseEnvelope(3303, 1, 0, 0, 0, OrderProtoCodec().encode(3303))
        assert (command, response_command, payload) == (3027, 3028, b'')
        if self.after:
            await self.after()
        if self.behavior == 'timeout':
            raise GameClientError('GAME_REQUEST_TIMEOUT')
        if self.behavior == 'cancel':
            raise asyncio.CancelledError('synthetic-private-cancellation')
        if self.behavior == 'rejected':
            raise GameClientError('GAME_REQUEST_REJECTED', response_code=7)
        return ResponseEnvelope(3026 if self.behavior == 'wrong_ack' else 3028,
            1, 0, 7 if self.behavior == 'error_code' else 0,
            1 if self.behavior == 'fragment' else 0,
            b'\xff' if self.behavior == 'malformed' else b'')


def setup(behavior='ack'):
    raw, stop, allowed = snapshot(), asyncio.Event(), [True]
    client = ClaimAllClient(behavior)
    ops = GameOrderOperations(client, lambda: deepcopy(raw), lambda: allowed[0])
    return ops, client, raw, stop, allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('stored', [None, [], [{'item_id': '101', 'count': 0}]])
async def test_claim_all_sends_with_empty_raw_store_and_does_not_infer_inventory(stored):
    ops, client, raw, stop, _ = setup()
    raw['orders']['valid'] = False  # Collection needs home and inventory, not orders.
    raw['home']['architectures'][0]['store_items'] = stored
    before = deepcopy(raw)
    result = await ops.apply({'action': 'claim_all'},
        expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result == {'status': 'applied', 'rewards': []}
    assert client.calls == [(3027, 3028, b'')]
    assert raw == before and not ops.blocked


@pytest.mark.asyncio
async def test_claim_all_can_repeat_unchanged_revision_after_a_confirmed_operation():
    ops, client, raw, stop, _ = setup()
    kwargs = {'expected_revision': order_revision(raw), 'stop_requested': stop.is_set}
    assert (await ops.apply({'action': 'complete', 'slot_id': '1'}, **kwargs))['status'] == 'applied'
    for _ in range(2):
        assert await ops.apply({'action': 'claim_all'}, **kwargs) == {'status': 'applied', 'rewards': []}
    assert [row[0] for row in client.calls] == [3302, 3027, 3027]


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['revision', 'role', 'home', 'inventory', 'stop', 'write', 'closed'])
async def test_claim_all_rechecks_send_gate(mutation):
    ops, client, raw, stop, allowed = setup()
    revision = order_revision(raw)
    def change():
        if mutation == 'revision': raw['home']['sequence'] += 1
        elif mutation == 'role': raw['role_key'] = (1, 3)
        elif mutation == 'home': raw['home']['valid'] = False
        elif mutation == 'inventory': raw['inventory']['valid'] = False
        elif mutation == 'stop': stop.set()
        elif mutation == 'write': allowed[0] = False
        else: client.closed = True
    client.before = change
    result = await ops.apply({'action': 'claim_all'},
        expected_revision=revision, stop_requested=stop.is_set)
    assert result == {'status': 'rejected', 'rewards': []}
    assert client.calls == [] and not ops.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize('baseline', ['home', 'inventory'])
async def test_claim_all_requires_valid_resource_baselines(baseline):
    ops, client, raw, stop, _ = setup()
    raw[baseline]['valid'] = False
    result = await ops.apply({'action': 'claim_all'},
        expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result['status'] == 'rejected' and client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior', ['timeout', 'malformed', 'wrong_ack', 'fragment'])
async def test_unknown_claim_all_blocks_further_sends_without_retry(behavior):
    ops, client, raw, stop, _ = setup(behavior)
    for _ in range(2):
        result = await ops.apply({'action': 'claim_all'},
            expected_revision=order_revision(raw), stop_requested=stop.is_set)
        assert result == {'status': 'uncertain', 'rewards': []}
    assert client.calls == [(3027, 3028, b'')] and ops.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior', ['rejected', 'error_code'])
async def test_explicit_claim_all_rejection_does_not_block_or_retry(behavior):
    ops, client, raw, stop, _ = setup(behavior)
    result = await ops.apply({'action': 'claim_all'},
        expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result == {'status': 'rejected', 'rewards': []}
    assert len(client.calls) == 1 and not ops.blocked


@pytest.mark.asyncio
async def test_cancelled_claim_all_blocks_and_sanitizes_exception():
    ops, client, raw, stop, _ = setup('cancel')
    with pytest.raises(asyncio.CancelledError) as captured:
        await ops.apply({'action': 'claim_all'},
            expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert ops.blocked and len(client.calls) == 1
    assert 'synthetic-private' not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['building_uid', 'building_id', 'recipe_id', 'lumi_uid'])
async def test_claim_all_cannot_accept_edit_or_selection_parameters(field):
    ops, client, raw, stop, _ = setup()
    result = await ops.apply({'action': 'claim_all', field: '123'},
        expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result['status'] == 'rejected' and client.calls == []


@pytest.mark.asyncio
async def test_explicit_claim_all_calls_are_serialized_and_do_not_resend_automatically():
    ops, client, raw, stop, _ = setup()
    entered, release = asyncio.Event(), asyncio.Event()
    async def after():
        entered.set()
        await release.wait()
    client.after = after
    kwargs = {'expected_revision': order_revision(raw), 'stop_requested': stop.is_set}
    first = asyncio.create_task(ops.apply({'action': 'claim_all'}, **kwargs))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(ops.apply({'action': 'claim_all'}, **kwargs))
    await asyncio.sleep(0)
    assert client.calls == [(3027, 3028, b'')]
    release.set()
    assert (await first)['status'] == (await second)['status'] == 'applied'
    assert client.calls == [(3027, 3028, b''), (3027, 3028, b'')]


def test_constructing_operations_and_reading_snapshots_never_collects():
    ops, client, raw, _, _ = setup()
    for _ in range(3):
        deepcopy(raw)
        order_revision(raw)
    assert client.calls == [] and not ops.blocked
