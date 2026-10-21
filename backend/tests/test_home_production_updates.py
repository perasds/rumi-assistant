"""Native production notifications replace one architecture without losing Home."""

import asyncio
from types import SimpleNamespace

import pytest

from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.home_codec import HomeProtoCodec
from rumi_market.game.home_state import HomeState
from rumi_market.game.order_operations import GameOrderOperations
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.order_models import OrderSettings
from test_order_collection_integration import CollectionRuntime
from test_order_service import setup


def notice(command, **fields):
    return ResponseEnvelope(command, 0, 1, 0, 0,
                            HomeProtoCodec().encode(command, **fields))


def architecture(uid=12, **fields):
    return {'architecture_uid': uid, 'architecture_id': 10101, **fields}


@pytest.mark.parametrize('change_type', [4, 5, 9, 10, 11, 12])
def test_production_notification_replaces_only_target_uid_and_preserves_home_baselines(change_type):
    home = HomeState(verified_version=True)
    home.observe(notice(2971, lumi_list=[
        {'lumi_uid': 21, 'lumi_id': 701, 'lumi_state': 0,
         'init_aptitude': {'aptitude_work': 32}},
        {'lumi_uid': 31, 'lumi_id': 702, 'lumi_state': 0}]))
    home.observe(notice(2913))
    home.observe(notice(3001, architecture_list=[
        architecture(architecture_level=1, is_hide=False,
            work_old={'lumi_uid': 21, 'recipe_id': 1, 'recipe_complete_times': 0,
                      'recipe_update_time': 777},
            cur_recipe={'recipe_id': 7, 'recipe_lumi_uid': 21},
            dorm={'lumi_list': [{'lumi_uid': 21}]},
            store_info={'base_rewards': [{'item_id': 20001, 'count': 1}],
                        'extra_rewards': [{'item_id': 20002, 'count': 2}]}),
        architecture(13, architecture_level=2, actFarm={'id': 1,
            'dispatchStores': {900: {'id': 900, 'lumiUid': 31}}})],
        home_lumi_list=[{'lumi_uid': 21, 'lumi_id': 701}],
        lumi_list=[{'lumi_uid': 21, 'work_energy': 90},
                   {'lumi_uid': 31, 'work_energy': 80}]))
    before = home.snapshot()
    assert before['valid'] and before['roster_ready'] and before['dispatch_ready']

    home.observe(notice(3003, change_list=[{'change_type': change_type,
        'architecture_info': architecture(
            work_old={'lumi_uid': 21, 'recipe_id': 2, 'recipe_complete_times': 3},
            store_info={'base_rewards': [{'item_id': 20001, 'count': 3}]})}]))

    after = home.snapshot()
    assert after['valid'] is True
    assert after['sequence'] == before['sequence'] + 1
    assert after['roster_ready'] and after['dispatch_ready']
    assert after['home_lumis'] == before['home_lumis']
    assert after['dispatch_lumi_uids'] == before['dispatch_lumi_uids'] == ['31']
    updated, untouched = after['architectures']
    assert untouched == before['architectures'][1]
    assert updated['uid'] == 12 and updated['id'] == 10101
    assert updated['work_old']['recipe_id'] == 2
    assert updated['work_old']['recipe_complete_times'] == 3
    assert updated['store_items'] == [{'item_id': '20001', 'count': 3}]
    # The client replaces architecture_info; omitted old data must not survive.
    assert updated['level'] is None and updated['is_hide'] is None
    assert updated['work_old']['recipe_update_time'] is None
    assert updated['cur_recipe'] is None and updated['dorm'] is None
    assert updated['store_extra_items'] == []
    assert after['workers'][1] == before['workers'][1]
    for field in ('uid', 'id', 'raw_state', 'work_aptitude', 'work_energy'):
        assert after['workers'][0][field] == before['workers'][0][field]
    assert after['workers'][0]['occupancy'] == [
        {'kind': 'work_old', 'architecture_uid': 12}]


@pytest.mark.parametrize('command', [3001, 3003])
def test_omitted_native_store_count_is_zero_without_invalidating_home(command):
    home = HomeState(verified_version=True)
    home.observe(notice(3001, architecture_list=[architecture()]))
    row = architecture(store_info={
        'base_rewards': [{'item_id': 20001}],
        'extra_rewards': [{'item_id': 20002}]})
    fields = ({'architecture_list': [row]} if command == 3001 else
              {'change_list': [{'change_type': 5, 'architecture_info': row}]})
    message = notice(command, **fields)
    decoded = HomeProtoCodec().decode(command, message.data)
    native_row = (decoded.architecture_list[0] if command == 3001 else
                  decoded.change_list[0].architecture_info)
    assert not native_row.store_info.base_rewards[0].HasField('count')
    assert native_row.store_info.base_rewards[0].count == 0

    home.observe(message)

    snapshot = home.snapshot()
    assert snapshot['valid'] is True
    assert snapshot['architectures'][0]['store_items'] == [{'item_id': '20001', 'count': 0}]
    assert snapshot['architectures'][0]['store_extra_items'] == [{'item_id': '20002', 'count': 0}]


@pytest.mark.parametrize('change_type', [3, 6, 7, 8, 13, 99])
def test_invalid_home_notification_reason_is_cleared_by_full_baseline(change_type):
    home = HomeState(verified_version=True)
    home.observe(notice(3001, architecture_list=[architecture()]))
    home.observe(notice(3003, change_list=[{
        'change_type': change_type, 'architecture_info': architecture()}]))
    invalid = home.snapshot()
    assert invalid['valid'] is False and invalid['architectures'] == []
    assert invalid['unavailable_reason'] == 'HOME_STATE_INVALID'

    home.observe(notice(3001, architecture_list=[architecture()]))

    restored = home.snapshot()
    assert restored['valid'] is True
    assert restored['unavailable_reason'] is None


def test_rejected_combined_notification_logs_only_inner_command_and_change_type(caplog):
    home = HomeState(verified_version=True)
    home.observe(notice(3001, architecture_list=[architecture()]))
    private_marker = 'synthetic-private-marker'
    roster = notice(2971, lumi_list=[{'lumi_uid': 21, 'psName': private_marker}])
    change = notice(3003, change_list=[{
        'change_type': 99, 'architecture_info': architecture()}])
    combined = notice(2016, unit_lst=[
        {'id': roster.command, 'msg': roster.data},
        {'id': change.command, 'msg': change.data}])

    with caplog.at_level('WARNING'):
        home.observe(combined)

    assert home.snapshot()['valid'] is False
    assert any('command=3003' in record.getMessage() and
               'architecture_change_type=99' in record.getMessage()
               for record in caplog.records)
    assert private_marker not in caplog.text
    assert repr(combined.data) not in caplog.text
    assert repr(change.data) not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,message', [
    ('HOME_STATE_INVALID', '家园数据更新异常，请重新登录'),
    (None, '家园尚未同步完成'),
])
async def test_order_state_distinguishes_invalid_home_from_pending_baseline(reason, message):
    service, session, provider = setup()
    provider.raw['home'].update(valid=False, unavailable_reason=reason)

    state = await service.state(session)

    assert not state['ready'] and not state['execution_ready']
    assert state['message'] == message
    assert state['execution_reason'] == message


class ProductionUpdateRuntime(CollectionRuntime):
    """Change only the production notice emitted alongside native bulk collection."""

    def __init__(self, change_type):
        self.production_change_type = change_type
        self.production_updated = asyncio.Event()
        self.production_snapshot = None
        self.home_valid_history = []
        super().__init__('store', production_notice=True)

    def observe(self, command, payload):
        is_production_update = command == 3003 and self.production_notice
        if is_production_update:
            message = self.home_codec.decode(command, payload)
            assert len(message.change_list) == 1
            assert message.change_list[0].change_type == 5
            message.change_list[0].change_type = self.production_change_type
            payload = message.SerializeToString(deterministic=True)
        super().observe(command, payload)
        snapshot = self.home_state.snapshot()
        self.home_valid_history.append(snapshot['valid'])
        if is_production_update:
            self.production_snapshot = snapshot
            self.production_updated.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('change_type', [10, 11, 12])
async def test_native_production_update_keeps_order_ready_through_claim_and_complete(change_type):
    runtime = ProductionUpdateRuntime(change_type)
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=[]), login_verified=True)
    view = await provider.login('synthetic-account', 'synthetic-password')
    service, session, _ = setup(provider)
    service.sync_timeout = .3
    session.view = view
    session.order_settings = OrderSettings(max_orders=1, allow_production=False)
    state = await service.state(session)
    assert state['ready'] and state['execution_ready']
    assert runtime.home_state.snapshot()['architectures'][0]['store_items'] == []

    await service.start(session, f'synthetic-production-notify-{change_type}', state['revision'])
    try:
        await asyncio.wait_for(runtime.production_updated.wait(), 1)
        updated_state = await service.state(session)
        assert updated_state['ready'] and updated_state['execution_ready']
        assert runtime.production_snapshot['valid'] is True
        assert runtime.production_snapshot['architectures'][0]['store_items'] == [
            {'item_id': '20001', 'count': 3}]
        await asyncio.wait_for(runtime.claim_acked.wait(), 1)
        assert isinstance(provider._order_operations, GameOrderOperations)
        assert runtime.calls == [(3027, 3028, {})]
        assert session.order_task.completed == 0
        runtime.deliver('inventory')
        await asyncio.wait_for(session.order_task.task, 1)

        final_state = await service.state(session)
        assert final_state['ready'] and final_state['execution_ready']
        assert all(runtime.home_valid_history)
        assert runtime.calls == [(3027, 3028, {}), (3302, 3303, {'slot_id': 1})]
        assert runtime.production_notifications == 1
        assert session.order_task.status == 'completed'
        assert session.order_task.completed == 1 and session.order_task.cans == 5
        assert runtime.home_state.snapshot()['architectures'][0]['store_items'] == []
        assert runtime.inventory_state.snapshot()['items'] == {'20001': 0, '54001': 5}
        assert runtime.order_state.snapshot()['orders'] == []
    finally:
        if not session.order_task.task.done():
            session.order_task.task.cancel()
            await session.order_task.task
