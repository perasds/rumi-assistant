"""Bounded native cancellation; all peers are synthetic and never use networking."""
import asyncio
from copy import deepcopy

import pytest

from rumi_market.game.client import GameClientError
from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.home_operations import GameHomeOperations
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.models import Role
from rumi_market.order_service import order_revision
from test_home_management_integration import SyntheticRuntime
from test_home_operations import Client, NEW, OLD, State, expected


class CancelState(State):
    def update(self, command, fields):
        if command == 3403 and fields['op'] == 2:
            # Native evidence does not promise that times, progress or state clear.
            self.data['architectures'][0]['work_old']['recipe_id'] = 0
            self.data['sequence'] += 1
        else:
            super().update(command, fields)


def setup(behaviors=()):
    state = CancelState()
    state.data['architectures'][0]['work_old'].update(
        recipe_id=1, recipe_intend_times=8, recipe_complete_times=2, recipe_state=1)
    client = Client(state, behaviors)
    operations = GameHomeOperations(client, state, confirmation_timeout=0.01)
    return operations, client, state


def change():
    return {'building_uid': 123, 'building_id': 10101, 'recipe_id': 1}


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior', ['notify', 'later'])
async def test_cancel_uses_current_recipe_op_two_and_confirms_idle_without_inventing_resets(behavior):
    operations, client, state = setup([behavior])
    result = await operations.cancel(change(), expected=expected(state.data))
    assert result['status'] == 'applied'
    assert client.calls == [(3403, 3404, {'archi_uid': 123, 'archi_id': 10101,
        'recipe_id': 1, 'recipe_times': 0, 'op': 2})]
    assert state.data['architectures'][0]['work_old'] == {
        'lumi_uid': OLD, 'lumi_id': 102601, 'recipe_id': 0,
        'recipe_intend_times': 8, 'recipe_complete_times': 2, 'recipe_state': 1}
    assert bool(state.waits) == (behavior == 'later')
    assert not operations.blocked and 'rewards' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior', ['ack_only', 'wrong_ack', 'empty_ack', 'timeout'])
async def test_unconfirmed_cancel_blocks_further_home_writes_without_replay(behavior):
    operations, client, state = setup([behavior])
    baseline = expected(state.data)
    assert (await operations.cancel(change(), expected=baseline))['status'] == 'uncertain'
    assert operations.blocked
    assert (await operations.cancel(change(), expected=baseline))['status'] == 'uncertain'
    assert (await operations.apply({**change(), 'recipe_times': 3}, expected=baseline))['status'] == 'uncertain'
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('archi_uid', 124), ('archi_id', 10102), ('op', 1)])
async def test_cancel_requires_every_ack_identity_field_to_match(field, value):
    operations, client, state = setup()
    original = client.request
    async def wrong_ack(*args, **kwargs):
        response = await original(*args, **kwargs)
        fields = {'archi_uid': 123, 'archi_id': 10101, 'op': 2, field: value}
        return ResponseEnvelope(3404, 1, 0, 0, 0, operations._codec.encode(3404, **fields))
    client.request = wrong_ack
    assert (await operations.cancel(change(), expected=expected(state.data)))['status'] == 'uncertain'
    assert operations.blocked and len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_notification', ['unchanged_sequence', 'invalid_state'])
async def test_cancel_requires_a_new_valid_notification_even_when_recipe_is_zero(invalid_notification):
    operations, client, state = setup()
    def after(snapshot, command):
        if invalid_notification == 'unchanged_sequence':
            snapshot['sequence'] -= 1
        else:
            snapshot['valid'] = False
    client.after = after
    assert (await operations.cancel(change(), expected=expected(state.data)))['status'] == 'uncertain'
    assert operations.blocked and len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('building_id', [10102, 10104, 10125])
async def test_cancel_rejects_continuous_and_farm_architecture_types(building_id):
    operations, client, state = setup()
    state.data['architectures'][0]['id'] = building_id
    result = await operations.cancel({**change(), 'building_id': building_id}, expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    lambda s: s['architectures'][0]['work_old'].update(recipe_id=0),
    lambda s: s['architectures'][0]['work_old'].update(recipe_intend_times=0),
    lambda s: s['architectures'][0]['work_old'].update(lumi_uid=None),
    lambda s: s['architectures'][0].update(cur_recipe={'recipe_id': 1}),
    lambda s: s['architectures'][0].update(has_farm_data=True),
])
async def test_cancel_requires_an_active_ordinary_work_old_queue(mutation):
    operations, client, state = setup()
    mutation(state.data)
    result = await operations.cancel(change(), expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_change', [
    {'building_uid': 123, 'building_id': 10101},
    {**change(), 'recipe_times': 0}, {**change(), 'lumi_uid': NEW},
    {**change(), 'recipe_id': True}, {**change(), 'recipe_id': 2},
    {**change(), 'building_uid': '123'}, {**change(), 'building_id': False},
])
async def test_cancel_accepts_only_exact_native_queue_identity(invalid_change):
    operations, client, state = setup()
    result = await operations.cancel(invalid_change, expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls


@pytest.mark.asyncio
async def test_cancel_requires_the_complete_original_configuration():
    operations, client, state = setup()
    baseline = expected(state.data)
    baseline.pop('recipe_times')
    assert (await operations.cancel(change(), expected=baseline))['status'] == 'rejected'
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('building_id', 10101.0), ('level', True), ('recipe_id', True), ('is_need_cut_ribbon', 0),
])
async def test_cancel_rejects_equal_looking_but_wrong_typed_baseline_fields(field, value):
    operations, client, state = setup()
    baseline = {**expected(state.data), field: value}
    assert (await operations.cancel(change(), expected=baseline))['status'] == 'rejected'
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    lambda s: s.update(valid=False),
    lambda s: s['architectures'][0].update(id=10102),
    lambda s: s['architectures'][0].update(level=2),
    lambda s: s['architectures'][0]['work_old'].update(recipe_id=2),
    lambda s: s['architectures'][0]['work_old'].update(recipe_intend_times=9),
    lambda s: s['architectures'][0]['work_old'].update(lumi_uid=NEW),
    lambda s: s['workers'][0].update(raw_state='128'),
    lambda s: s.update(dispatch_lumi_uids=[OLD]),
])
async def test_cancel_rechecks_building_recipe_and_worker_at_send_boundary(mutation):
    operations, client, state = setup()
    client.before = mutation
    result = await operations.cancel(change(), expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls and not operations.blocked


@pytest.mark.asyncio
async def test_explicit_server_rejection_allows_a_fresh_cancel_attempt():
    operations, client, state = setup(['rejected'])
    baseline = expected(state.data)
    assert (await operations.cancel(change(), expected=baseline))['status'] == 'rejected'
    assert not operations.blocked
    assert (await operations.cancel(change(), expected=baseline))['status'] == 'applied'
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_cancelled_coroutine_after_send_blocks_writes_and_strips_private_text():
    operations, client, state = setup(['cancel'])
    with pytest.raises(asyncio.CancelledError) as caught:
        await operations.cancel(change(), expected=expected(state.data))
    assert operations.blocked and len(client.calls) == 1 and not caught.value.args


class CancelRuntime(SyntheticRuntime):
    def __init__(self):
        super().__init__()
        self.before = None
        self.outcome = 'notify'
        self.architecture['work_old'].update(recipe_id=1, recipe_intend_times=8, recipe_complete_times=2)
        self.observe(3003, change_list=[{'change_type': 5, 'architecture_info': deepcopy(self.architecture)}])

    async def request(self, command, response_command, payload, *, before_send):
        if self.before:
            self.before()
        if not before_send():
            raise GameClientError('GAME_REQUEST_NOT_SENT')
        request = self.codec.decode(command, payload)
        assert command == 3403 and response_command == 3404
        assert request.archi_uid == 12 and request.archi_id == 10101
        assert request.recipe_id == 1 and request.recipe_times == 0 and request.op == 2
        self.calls.append(command)
        if self.outcome == 'timeout':
            raise GameClientError('GAME_REQUEST_TIMEOUT')
        self.architecture['work_old']['recipe_id'] = 0
        self.observe(3003, change_list=[{'change_type': 5, 'architecture_info': deepcopy(self.architecture)}])
        return ResponseEnvelope(3404, 1, 0, 0, 0, self.codec.encode(3404,
            archi_uid=12, archi_id=10101, op=2))


def adapter():
    runtime = CancelRuntime()
    provider = IndependentGameProvider(runtime, None, login_verified=True)
    provider._role = Role(id='browser', name='', server_name='test')
    def snapshot():
        return {'role_key': ('7', '123'), 'server_time_seconds': 1000,
            'home': runtime.home_state.snapshot(), 'orders': {'valid': True, 'sequence': 1, 'orders': []},
            'inventory': {'valid': True, 'sequence': 1, 'items': {}}}
    provider._order_snapshot_now = snapshot
    operation = {'action': 'cancel_production', 'building_uid': '12', 'building_id': '10101', 'recipe_id': '1'}
    return provider, runtime, snapshot, operation


@pytest.mark.asyncio
async def test_provider_cancellation_uses_real_codec_and_home_projection_without_reward_claims():
    provider, runtime, snapshot, operation = adapter()
    result = await provider.apply_order_operation(operation,
        expected_revision=order_revision(snapshot()), stop_requested=lambda: False)
    assert result == {'status': 'applied', 'rewards': []}
    work = runtime.home_state.snapshot()['architectures'][0]['work_old']
    assert work['recipe_id'] == 0 and work['recipe_intend_times'] == 8 and work['lumi_uid'] == OLD
    assert runtime.calls == [3403]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['stop', 'revision', 'recipe', 'building', 'extra', 'type'])
async def test_provider_rejects_unapproved_or_stale_cancel_identity(reason):
    provider, runtime, snapshot, operation = adapter()
    revision = order_revision(snapshot())
    if reason == 'revision': revision = '0' * 64
    if reason == 'recipe': operation['recipe_id'] = '2'
    if reason == 'building': operation['building_id'] = '10102'
    if reason == 'extra': operation['recipe_times'] = 0
    if reason == 'type': operation['recipe_id'] = 1
    result = await provider.apply_order_operation(operation,
        expected_revision=revision, stop_requested=lambda: reason == 'stop')
    assert result == {'status': 'rejected', 'rewards': []} and not runtime.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['stop', 'revision', 'session'])
async def test_provider_rechecks_stop_revision_and_session_immediately_before_cancel_send(reason):
    provider, runtime, snapshot, operation = adapter()
    revision = order_revision(snapshot())
    stopped = False
    def before():
        nonlocal stopped
        if reason == 'stop': stopped = True
        elif reason == 'session': runtime.can_query = False
        else:
            runtime.architecture['work_old']['recipe_id'] = 2
            runtime.observe(3003, change_list=[{'change_type': 5, 'architecture_info': deepcopy(runtime.architecture)}])
    runtime.before = before
    result = await provider.apply_order_operation(operation,
        expected_revision=revision, stop_requested=lambda: stopped)
    assert result == {'status': 'rejected', 'rewards': []} and not runtime.calls


@pytest.mark.asyncio
async def test_unknown_provider_cancel_blocks_cancel_and_produce_without_replay():
    provider, runtime, snapshot, operation = adapter()
    runtime.outcome = 'timeout'
    revision = order_revision(snapshot())
    assert (await provider.apply_order_operation(operation,
        expected_revision=revision, stop_requested=lambda: False))['status'] == 'uncertain'
    assert (await provider.apply_order_operation(operation,
        expected_revision=revision, stop_requested=lambda: False))['status'] == 'uncertain'
    produce = {**operation, 'action': 'produce', 'recipe_times': 3}
    assert (await provider.apply_order_operation(produce,
        expected_revision=revision, stop_requested=lambda: False))['status'] == 'uncertain'
    assert runtime.calls == [3403]
