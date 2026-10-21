"""Hot71 runtime readiness and fail-closed controls; synthetic transports only."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from rumi_market.errors import ApiError
from rumi_market.game.auction_execution import GameAuctionExecution
from rumi_market.game.home_codec import HomeProtoCodec
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.game.profile_sync import _load_schema
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.home_work_config import HomeWorkConfig
from test_game_runtime import PRIVATE, key
from test_hot69_runtime_integration import CODEC, GOLD_BALANCE, setup_version
from test_order_service import setup as order_setup
from test_season_state import activity, notification


async def server(game, hot, missing):
    login = await game.next_request()
    assert login.ReqCmdId == 2901
    profile = CODEC.decode('SNKProto.Cmd_Net_LumiLogin_Req', login.ReqData).message
    assert profile.client_version == '1.5.3' and profile.hot_version == hot
    game.respond(2902, login.ReqCmdSeq,
        CODEC.encode('SNKProto.Cmd_Net_LumiLogin_Ack', {'role_id': 123}))
    enter = await game.next_request()
    assert enter.ReqCmdId == 2911

    orders = {'slot_list': [{
        'slot_id': 1, 'is_unlock': True, 'order_info': {'order_type': 1,
            'item_need': [{'item_id': 20001, 'count': 3}],
            'item_reward': [{'item_id': 54001, 'count': 5}]},
    }]}
    if missing not in ('home', 'orders'):
        fields = {'architecture_list': [{
            'architecture_uid': 12, 'architecture_id': 10101, 'architecture_level': 1,
            'work_old': {'lumi_uid': 0, 'recipe_id': 0}, 'store_info': {},
        }]}
        if missing != 'invalid_orders':
            fields['order_archi'] = orders
        game.respond(3001, 0, HomeProtoCodec().encode(3001, **fields), notify=1)
    elif missing == 'home':
        game.respond(3301, 0, OrderProtoCodec().encode(3301, order_archi=orders), notify=1)
    # Home and order baselines share 3001; withhold it to test missing orders.
    messages, _, _ = _load_schema()
    if missing != 'inventory':
        game.respond(2941, 0, messages[2941](item_list=[
            {'item_id': 20001, 'item_uid': 0, 'count': 3},
            {'item_id': 10001, 'item_uid': 0, 'count': GOLD_BALANCE},
        ]).SerializeToString(), notify=2)
    if missing != 'capacity':
        game.respond(3036, 0, messages[3036](buff_list=[]).SerializeToString(), notify=3)
    if missing != 'season':
        game.respond(8001, 0, notification(rows=[activity(system=700020)]).data, notify=4)
    game.respond(2912, enter.ReqCmdSeq,
        CODEC.encode('SNKProto.Cmd_Role_EnterGame_Ack', {
            'zone_id': 7, 'zone_name': 'synthetic-zone', 'server_time_ms': 150000}))
    game.respond(2913, 0, CODEC.encode('SNKProto.Cmd_Notify_LoginEnd', {}), notify=5)


@asynccontextmanager
async def logged_in(key, *, hot=71, missing=None):
    runtime, game, events = setup_version(key, hot)
    peer = asyncio.create_task(server(game, hot, missing))
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=[]), login_verified=True)
    states = {}
    try:
        view = await provider.login('synthetic-account', PRIVATE)
        await peer
        assert view.authenticated and runtime.can_query and game.connections == 1
        assert events == ['version', 'config', 'cipher', 'login']
        states = {'orders': runtime.order_state, 'inventory': runtime.inventory_state,
                  'home': runtime.home_state, 'capacity': runtime.home_capacity_state}
        service, session, _ = order_setup(provider)
        session.view = view
        yield runtime, game, provider, service, session, states
        assert session.order_task is None and not service.roles
        assert game.outgoing.empty()  # No order, purchase, or inspection request.
    finally:
        peer.cancel()
        await provider.close()
        assert runtime.closed and not runtime.can_query and game.closed
        assert all(not state.snapshot()['valid'] for state in states.values())
        assert runtime.gold_balance is None and not runtime.season_snapshot['valid']


@pytest.mark.parametrize('component', ['home', 'inventory', 'capacity', 'orders', 'season'])
@pytest.mark.parametrize('hot', [69, 71])
async def test_hot71_accepts_each_verified_native_baseline(key, component, hot):
    async with logged_in(key, hot=hot) as (runtime, _, _, _, _, states):
        snapshot = runtime.season_snapshot if component == 'season' else states[component].snapshot()
        assert snapshot['valid'], snapshot['unavailable_reason']


@pytest.mark.parametrize('hot', [69, 71])
async def test_hot71_complete_sync_exposes_order_readiness_and_configured_capacity(key, hot):
    async with logged_in(key, hot=hot) as (runtime, _, provider, service, session, _):
        state = await service.state(session)
        assert state['ready'] and state['execution_ready'], state['execution_reason']
        assert state['orders'][0]['can_complete'] is True
        raw = await provider.order_snapshot()
        assert raw['inventory']['items']['20001'] == 3
        assert raw['home']['capacity_state']['bonus'] == 0
        assert raw['home']['architectures'][0]['capacity_remaining'] == HomeWorkConfig().level(10101, 1)['capacity']
        assert runtime.gold_balance == GOLD_BALANCE
        assert runtime.season_snapshot['current_season_id'] == 2


@pytest.mark.parametrize('hot', [68, 70, 73])
async def test_hot_version_difference_does_not_block_complete_native_baselines(key, hot):
    async with logged_in(key, hot=hot) as (runtime, _, _, service, session, states):
        for state in states.values():
            snapshot = state.snapshot()
            assert snapshot['valid'], snapshot['unavailable_reason']
        assert runtime.season_snapshot['valid'] and runtime.gold_balance == GOLD_BALANCE
        state = await service.state(session)
        assert state['ready'] and state['execution_ready'], state['execution_reason']
        assert session.order_task is None and not service.roles


@pytest.mark.parametrize('missing,reason', [
    ('orders', '订单尚未同步完成'),
    ('invalid_orders', '订单数据更新异常，请重新登录'),
    ('inventory', '背包物资尚未同步完成'),
    ('home', '家园尚未同步完成'),
])
@pytest.mark.parametrize('hot', [69, 71])
async def test_hot71_version_support_does_not_bypass_invalid_or_incomplete_order_sync(key, missing, reason, hot):
    async with logged_in(key, hot=hot, missing=missing) as (_, _, _, service, session, _):
        state = await service.state(session)
        assert not state['ready'] and not state['execution_ready']
        assert state['execution_reason'] == reason
        with pytest.raises(ApiError, match='ORDER_STATE_UNAVAILABLE'):
            await service.start(session, 'synthetic-incomplete-sync', state['revision'])


@pytest.mark.parametrize('hot', [69, 71])
async def test_hot71_missing_capacity_remains_unknown_instead_of_assuming_zero_bonus(key, hot):
    async with logged_in(key, hot=hot, missing='capacity') as (_, _, provider, _, _, states):
        capacity = states['capacity'].snapshot()
        assert not capacity['valid'] and capacity['bonus'] is None
        assert capacity['unavailable_reason'] == 'HOME_CAPACITY_BASELINE_MISSING'
        raw = await provider.order_snapshot()
        assert raw['home']['valid']
        assert raw['home']['architectures'][0].get('capacity_remaining') is None


@pytest.mark.parametrize('hot', [69, 71])
async def test_hot71_missing_season_does_not_invent_current_season(key, hot):
    async with logged_in(key, hot=hot, missing='season') as (runtime, _, _, _, _, _):
        snapshot = runtime.season_snapshot
        assert not snapshot['valid'] and snapshot['current_season_id'] is None
        assert snapshot['unavailable_reason'] == 'baseline_pending'


@pytest.mark.parametrize('hot', [69, 71])
async def test_hot71_missing_gold_baseline_keeps_purchase_balance_guard_closed(key, hot):
    async with logged_in(key, hot=hot, missing='inventory') as (runtime, _, provider, _, _, _):
        assert runtime.gold_balance is None and provider.gold_balance is None
        # Inspect the existing guard without starting a purchase task or request.
        assert GameAuctionExecution(runtime, 123)._balance_error(0, 0) == 'PURCHASE_BALANCE_UNAVAILABLE'
