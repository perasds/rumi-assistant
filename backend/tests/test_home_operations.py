"""Synthetic home writes: verified wire fields, fresh state and event confirmation."""
import asyncio
from copy import deepcopy
import importlib

import pytest

from rumi_market.game.client import GameClientError
from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.home_codec import HomeProtoCodec


OLD, NEW = '9007199254740993', '9007199254740995'


def view():
    return {'valid': True, 'sequence': 1, 'roster_ready': True,
            'dispatch_ready': True, 'dispatch_lumi_uids': [],
            'architectures': [{'uid': 123, 'id': 10101, 'level': 1,
                'level_up_complete_time': '0', 'is_need_cut_ribbon': False,
                'work_old': {'lumi_uid': OLD, 'lumi_id': 102601, 'recipe_id': 0,
                             'recipe_intend_times': 0, 'recipe_complete_times': 0},
                'cur_recipe': None, 'dorm': None, 'travel': None, 'interaction_uids': []}],
            'workers': [{'uid': OLD, 'id': 102601, 'raw_state': '2', 'occupancy': [{'kind': 'work_old', 'architecture_uid': 123}]},
                        {'uid': NEW, 'id': 102601, 'raw_state': '0', 'occupancy': []}]}


def expected(snapshot):
    row = snapshot['architectures'][0]
    work = row['work_old']
    return {'building_id': row['id'], 'level': row['level'],
            'lumi_uid': work['lumi_uid'], 'recipe_id': work['recipe_id'],
            'recipe_times': work['recipe_intend_times'],
            'level_up_complete_time': row['level_up_complete_time'],
            'is_need_cut_ribbon': row['is_need_cut_ribbon']}


class State:
    def __init__(self):
        self.data = view()
        self.pending = None
        self.waits = []

    def snapshot(self):
        return deepcopy(self.data)

    def update(self, command, fields):
        work = self.data['architectures'][0]['work_old']
        if command == 3401:
            work['lumi_uid'] = str(fields['lumi_uid'])
        else:
            work.update(recipe_id=fields['recipe_id'], recipe_intend_times=fields['recipe_times'])
        self.data['sequence'] += 1

    async def wait_for_update(self, sequence, timeout):
        self.waits.append(sequence)
        if self.pending:
            command, fields = self.pending
            self.pending = None
            self.update(command, fields)
            return True
        return False


class Client:
    def __init__(self, state, behaviors=()):
        self.state, self.behaviors = state, list(behaviors)
        self.calls = []
        self.before = None
        self.after = None
        self.closed = False

    async def request(self, command, response_command, payload, *, before_send):
        if self.before:
            self.before(self.state.data)
        if not before_send():
            raise GameClientError('GAME_REQUEST_NOT_SENT')
        codec = HomeProtoCodec()
        message = codec.decode(command, payload)
        fields = {field.name: value for field, value in message.ListFields()}
        self.calls.append((command, response_command, fields))
        behavior = self.behaviors.pop(0) if self.behaviors else 'notify'
        if behavior == 'rejected':
            raise GameClientError('GAME_REQUEST_REJECTED', response_code=17)
        if behavior == 'timeout':
            raise GameClientError('GAME_REQUEST_TIMEOUT')
        if behavior == 'cancel':
            raise asyncio.CancelledError('synthetic-private-cancel')
        if behavior == 'notify':
            self.state.update(command, fields)
        elif behavior == 'later':
            self.state.pending = command, fields
        if self.after:
            self.after(self.state.data, command)
        ack = {key: fields[key] for key in ('archi_uid', 'archi_id', 'op')}
        if command == 3401:
            ack['lumi_uid'] = fields['lumi_uid']
        if behavior == 'wrong_ack':
            ack['archi_uid'] += 1
        if behavior == 'empty_ack':
            ack = {}
        return ResponseEnvelope(response_command, 1, 0, 0, 0, codec.encode(response_command, **ack))


def setup(behaviors=()):
    module = importlib.import_module('rumi_market.game.home_operations')
    state = State()
    client = Client(state, behaviors)
    return module.GameHomeOperations(client, state, confirmation_timeout=0.05), client, state


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior', ['notify', 'later'])
async def test_recipe_start_encodes_native_fields_and_requires_live_confirmation(behavior):
    operations, client, state = setup([behavior])
    result = await operations.apply({'building_uid': 123, 'building_id': 10101,
                                     'recipe_id': 1, 'recipe_times': 5}, expected=expected(state.data))
    assert result['status'] == 'applied'
    assert client.calls == [(3403, 3404, {'archi_uid': 123, 'archi_id': 10101,
                                        'recipe_id': 1, 'recipe_times': 5, 'op': 1})]
    assert bool(state.waits) == (behavior == 'later')


@pytest.mark.asyncio
async def test_worker_and_recipe_are_separate_confirmed_steps_without_automatic_transfer():
    operations, client, state = setup()
    result = await operations.apply({'building_uid': 123, 'building_id': 10101,
                                     'lumi_uid': NEW, 'recipe_id': 1, 'recipe_times': 5},
                                    expected=expected(state.data))
    assert result['status'] == 'applied'
    assert client.calls == [
        (3401, 3402, {'archi_uid': 123, 'archi_id': 10101, 'lumi_uid': int(NEW), 'op': 1}),
        (3403, 3404, {'archi_uid': 123, 'archi_id': 10101, 'recipe_id': 1, 'recipe_times': 5, 'op': 1})]


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior', ['ack_only', 'wrong_ack', 'empty_ack', 'timeout'])
async def test_unconfirmed_write_blocks_later_writes_without_replay(behavior):
    operations, client, state = setup([behavior])
    change = {'building_uid': 123, 'building_id': 10101, 'recipe_id': 1, 'recipe_times': 5}
    baseline = expected(state.data)
    assert (await operations.apply(change, expected=baseline))['status'] == 'uncertain'
    assert operations.blocked
    assert (await operations.apply(change, expected=baseline))['status'] == 'uncertain'
    assert len(client.calls) == 1 and state.snapshot()['valid']


@pytest.mark.asyncio
async def test_confirmed_worker_then_rejected_recipe_reports_partial_without_blocking():
    operations, client, state = setup(['notify', 'rejected'])
    result = await operations.apply({'building_uid': 123, 'building_id': 10101,
                                     'lumi_uid': NEW, 'recipe_id': 1, 'recipe_times': 5}, expected=expected(state.data))
    assert result['status'] == 'partial' and '噜咪' in result['message']
    assert not operations.blocked and len(client.calls) == 2


@pytest.mark.asyncio
async def test_unknown_second_step_takes_precedence_over_a_confirmed_first_step():
    operations, client, state = setup(['notify', 'timeout'])
    result = await operations.apply({'building_uid': 123, 'building_id': 10101,
                                     'lumi_uid': NEW, 'recipe_id': 1, 'recipe_times': 5}, expected=expected(state.data))
    assert result['status'] == 'uncertain' and 'uncertain' not in result
    assert '工作噜咪已确认' in result['message'] and '生产设置结果待核实' in result['message']
    assert operations.blocked and len(client.calls) == 2


@pytest.mark.asyncio
async def test_running_normal_batch_cannot_be_reconfigured_without_a_separate_stop():
    operations, client, state = setup()
    state.data['architectures'][0]['work_old'].update(recipe_id=1, recipe_intend_times=10, recipe_complete_times=2)
    result = await operations.apply({'building_uid': 123, 'building_id': 10101,
                                     'recipe_id': 1, 'recipe_times': 5}, expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls


@pytest.mark.asyncio
async def test_running_batch_does_not_globally_disable_the_separate_worker_selection_path():
    operations, client, state = setup()
    state.data['architectures'][0]['work_old'].update(recipe_id=1, recipe_intend_times=10)
    result = await operations.apply({'building_uid': 123, 'building_id': 10101, 'lumi_uid': NEW},
                                    expected=expected(state.data))
    assert result['status'] == 'applied' and [row[0] for row in client.calls] == [3401]


@pytest.mark.asyncio
@pytest.mark.parametrize('change_after_worker', ['level', 'worker_state'])
async def test_second_step_rechecks_original_configuration_and_worker_eligibility(change_after_worker):
    operations, client, state = setup()
    def after(snapshot, command):
        if command == 3401:
            if change_after_worker == 'level': snapshot['architectures'][0]['level'] = 2
            else: snapshot['workers'][1]['raw_state'] = '128'
    client.after = after
    result = await operations.apply({'building_uid': 123, 'building_id': 10101,
                                     'lumi_uid': NEW, 'recipe_id': 1, 'recipe_times': 5}, expected=expected(state.data))
    assert result['status'] == 'partial'
    assert len(client.calls) == 1 and not operations.blocked


@pytest.mark.asyncio
async def test_server_rejection_does_not_block_future_explicit_attempt():
    operations, client, state = setup(['rejected'])
    change = {'building_uid': 123, 'building_id': 10101, 'recipe_id': 1, 'recipe_times': 5}
    baseline = expected(state.data)
    assert (await operations.apply(change, expected=baseline))['status'] == 'rejected'
    assert not operations.blocked
    assert (await operations.apply(change, expected=baseline))['status'] == 'applied'


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    lambda s: s.update(valid=False),
    lambda s: s['architectures'][0].update(id=10102),
    lambda s: s['architectures'][0].update(level=2),
    lambda s: s['architectures'][0].update(level_up_complete_time='9'),
    lambda s: s['architectures'][0].update(is_need_cut_ribbon=True),
    lambda s: s['architectures'][0]['work_old'].update(recipe_id=2),
])
async def test_changed_configuration_is_rechecked_at_the_actual_send_boundary(mutation):
    operations, client, state = setup()
    client.before = mutation
    result = await operations.apply({'building_uid': 123, 'building_id': 10101,
                                     'recipe_id': 1, 'recipe_times': 5}, expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls and not operations.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    lambda s: s['workers'][0].update(raw_state='128'),
    lambda s: s.update(dispatch_lumi_uids=[OLD]),
    lambda s: s.update(workers=s['workers'][1:]),
])
async def test_recipe_only_change_rechecks_the_existing_worker_before_sending(mutation):
    operations, client, state = setup()
    client.before = mutation
    result = await operations.apply({'building_uid': 123, 'building_id': 10101,
                                     'recipe_id': 1, 'recipe_times': 5}, expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls and not operations.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    lambda s: s.update(roster_ready=False),
    lambda s: s.update(dispatch_ready=False),
    lambda s: s.update(dispatch_lumi_uids=[NEW]),
    lambda s: s.update(workers=s['workers'][:1]),
    lambda s: s['workers'][1].update(raw_state=None),
    lambda s: s['workers'][1].update(raw_state='128'),
    lambda s: s['workers'][1].update(raw_state='4'),
    lambda s: s['workers'][1].update(id=100101),
    lambda s: s['workers'][1].update(occupancy=[{'kind': 'dorm', 'architecture_uid': 124}]),
    lambda s: s['workers'][1].update(occupancy=[{'kind': 'work_old', 'architecture_uid': 124}]),
])
async def test_worker_must_remain_owned_type_matched_and_free_of_confirmed_conflicts(mutation):
    operations, client, state = setup()
    client.before = mutation
    result = await operations.apply({'building_uid': 123, 'building_id': 10101, 'lumi_uid': NEW},
                                    expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls


@pytest.mark.asyncio
async def test_work_and_collect_bits_alone_are_not_treated_as_the_mutex_mask():
    operations, client, state = setup()
    state.data['workers'][1]['raw_state'] = str((1 << 1) | (1 << 4))
    result = await operations.apply({'building_uid': 123, 'building_id': 10101, 'lumi_uid': NEW},
                                    expected=expected(state.data))
    assert result['status'] == 'applied'


@pytest.mark.asyncio
async def test_unchanged_settings_are_a_noop_without_native_requests():
    operations, client, state = setup()
    result = await operations.apply({'building_uid': 123, 'building_id': 10101, 'lumi_uid': OLD},
                                    expected=expected(state.data))
    assert result['status'] == 'applied' and not client.calls


@pytest.mark.asyncio
async def test_overlapping_operations_do_not_send_the_same_old_configuration_twice():
    operations, client, state = setup()
    original = client.request
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def paused(*args, **kwargs):
        requests.append(args[0])
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    client.request = paused
    change = {'building_uid': 123, 'building_id': 10101, 'recipe_id': 1, 'recipe_times': 5}
    baseline = expected(state.data)
    first = asyncio.create_task(operations.apply(change, expected=baseline))
    await entered.wait()
    second = asyncio.create_task(operations.apply(change, expected=baseline))
    await asyncio.sleep(0)
    assert requests == [3403] and not client.calls
    release.set()
    assert [result['status'] for result in await asyncio.gather(first, second)] == ['applied', 'rejected']
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('recipe_id', True), ('recipe_times', -1), ('recipe_times', None),
                                         ('lumi_uid', 123), ('lumi_uid', '0'), ('building_uid', 1 << 31),
                                         ('private_extra', 'synthetic-private-value')])
async def test_invalid_wire_values_are_rejected_before_sending(field, value):
    operations, client, state = setup()
    change = {'building_uid': 123, 'building_id': 10101, 'recipe_id': 1, 'recipe_times': 5, field: value}
    result = await operations.apply(change, expected=expected(state.data))
    assert result['status'] == 'rejected' and not client.calls
    assert 'synthetic-private' not in str(result)


@pytest.mark.asyncio
async def test_known_autonomous_branch_sends_zero_times_instead_of_inventing_a_batch():
    operations, client, state = setup()
    state.data['architectures'][0]['id'] = 10102
    state.data['architectures'][0]['work_old']['lumi_id'] = 101301
    state.data['workers'][0]['id'] = 101301
    result = await operations.apply({'building_uid': 123, 'building_id': 10102,
                                     'recipe_id': 7, 'recipe_times': 0}, expected=expected(state.data))
    assert result['status'] == 'applied'
    assert client.calls[0][2]['recipe_times'] == 0


@pytest.mark.asyncio
async def test_cancel_after_send_blocks_writes_and_does_not_expose_cancellation_text():
    operations, client, state = setup(['cancel'])
    with pytest.raises(asyncio.CancelledError) as caught:
        await operations.apply({'building_uid': 123, 'building_id': 10101, 'recipe_id': 1,
                                'recipe_times': 5}, expected=expected(state.data))
    assert operations.blocked and len(client.calls) == 1
    assert not caught.value.args


@pytest.mark.asyncio
async def test_adapter_keeps_read_access_after_unknown_write_until_session_expires():
    from test_game_provider import Runtime, provider
    state = State()
    runtime = Runtime()
    runtime.home_state, runtime.home_client = state, Client(state, ['timeout'])
    adapter = provider(runtime)
    await adapter.login('synthetic', 'synthetic')
    baseline = expected((await adapter.home_snapshot()))
    change = {'building_uid': 123, 'building_id': 10101, 'recipe_id': 1, 'recipe_times': 5}
    assert (await adapter.apply_home_change(change, expected=baseline))['status'] == 'uncertain'
    assert (await adapter.home_snapshot())['valid']
    assert (await adapter.apply_home_change(change, expected=baseline))['status'] == 'uncertain'
    assert len(runtime.home_client.calls) == 1
    runtime.can_query = False
    from rumi_market.errors import ApiError
    with pytest.raises(ApiError) as caught:
        await adapter.home_snapshot()
    assert caught.value.status_code == 401
    await adapter.close()
