"""Hot72 readiness and unknown-version controls using synthetic login only."""

import pytest

from rumi_market.game.auction_execution import GameAuctionExecution
from rumi_market.home_work_config import HomeWorkConfig
from test_game_runtime import key
from test_hot69_runtime_integration import GOLD_BALANCE
from test_hot71_runtime_integration import logged_in
from test_redbag_runtime import logged_in as redbag_logged_in


@pytest.mark.parametrize('component', ['home', 'inventory', 'capacity', 'orders', 'season', 'gold'])
async def test_hot72_accepts_each_verified_native_baseline(key, component):
    async with logged_in(key, hot=72) as (runtime, _, _, _, _, states):
        if component == 'gold':
            assert runtime.gold_balance == GOLD_BALANCE
            return
        snapshot = runtime.season_snapshot if component == 'season' else states[component].snapshot()
        assert snapshot['valid'], snapshot['unavailable_reason']


async def test_hot72_complete_sync_exposes_order_gold_capacity_and_season_readiness(key):
    async with logged_in(key, hot=72) as (runtime, game, provider, service, session, _):
        state = await service.state(session)
        assert state['ready'] and state['execution_ready'], state['execution_reason']
        assert state['orders'][0]['can_complete'] is True
        raw = await provider.order_snapshot()
        assert raw['inventory']['items']['20001'] == 3
        assert raw['home']['capacity_state']['bonus'] == 0
        assert raw['home']['architectures'][0]['capacity_remaining'] == HomeWorkConfig().level(10101, 1)['capacity']
        assert runtime.gold_balance == provider.gold_balance == GOLD_BALANCE
        assert runtime.season_snapshot['current_season_id'] == 2
        assert game.outgoing.empty()


async def test_hot73_complete_sync_allows_order_and_trade_consumers_with_version_notice(key):
    async with logged_in(key, hot=73) as (runtime, game, _, service, session, states):
        for tracker in states.values():
            snapshot = tracker.snapshot()
            assert snapshot['valid'], snapshot['unavailable_reason']
        assert runtime.gold_balance == GOLD_BALANCE
        assert runtime.season_snapshot['current_season_id'] == 2
        assert not runtime.redbag_available
        state = await service.state(session)
        assert state['ready'] and state['execution_ready'], state['execution_reason']
        assert state['orders'][0]['can_complete'] is True
        assert game.outgoing.empty()


@pytest.mark.parametrize('hot,warning', [
    (72, None),
    (73, '工具适配版本：1.5.3 / 热更 72；游戏实际版本：1.5.3 / 热更 73'),
])
async def test_login_notice_compares_adapted_and_actual_game_versions(key, hot, warning):
    async with logged_in(key, hot=hot) as (runtime, _, _, _, session, _):
        assert getattr(runtime, 'version_warning', None) == warning
        assert session.view.message == warning


@pytest.mark.parametrize('missing,reason', [
    ('orders', '订单尚未同步完成'),
    ('invalid_orders', '订单数据更新异常，请重新登录'),
    ('inventory', '背包物资尚未同步完成'),
    ('home', '家园尚未同步完成'),
])
@pytest.mark.parametrize('hot', [72, 73])
async def test_hot_version_notice_does_not_bypass_missing_or_invalid_order_inputs(key, missing, reason, hot):
    async with logged_in(key, hot=hot, missing=missing) as (_, game, _, service, session, _):
        state = await service.state(session)
        assert not state['ready'] and not state['execution_ready']
        assert state['execution_reason'] == reason
        assert game.outgoing.empty()


@pytest.mark.parametrize('hot', [72, 73])
async def test_hot_version_notice_keeps_missing_production_capacity_unknown(key, hot):
    async with logged_in(key, hot=hot, missing='capacity') as (_, game, provider, _, _, states):
        capacity = states['capacity'].snapshot()
        assert not capacity['valid'] and capacity['bonus'] is None
        assert capacity['unavailable_reason'] == 'HOME_CAPACITY_BASELINE_MISSING'
        raw = await provider.order_snapshot()
        assert raw['home']['valid']
        assert raw['home']['architectures'][0].get('capacity_remaining') is None
        assert game.outgoing.empty()


@pytest.mark.parametrize('hot', [72, 73])
async def test_hot_version_notice_does_not_invent_a_missing_current_season(key, hot):
    async with logged_in(key, hot=hot, missing='season') as (runtime, game, _, _, _, _):
        snapshot = runtime.season_snapshot
        assert not snapshot['valid'] and snapshot['current_season_id'] is None
        assert snapshot['unavailable_reason'] == 'baseline_pending'
        assert game.outgoing.empty()


@pytest.mark.parametrize('hot', [72, 73])
async def test_hot_version_notice_keeps_missing_gold_purchase_send_guard_closed(key, hot):
    async with logged_in(key, hot=hot, missing='inventory') as (runtime, game, provider, _, _, _):
        assert runtime.gold_balance is None and provider.gold_balance is None
        assert GameAuctionExecution(runtime, 123)._balance_error(0, 0) == 'PURCHASE_BALANCE_UNAVAILABLE'
        assert game.outgoing.empty()


@pytest.mark.parametrize('hot,server_id,available', [(72, 7, True), (72, 8, False), (73, 7, False)])
async def test_hot72_redbag_readiness_requires_verified_version_and_selected_server(key, hot, server_id, available):
    async with redbag_logged_in(key, hot=hot, server_id=server_id) as (runtime, game, provider):
        assert runtime.redbag_available is available
        assert provider.redbag_available is available
        assert game.outgoing.empty()  # Readiness never opens chat or claims a packet.
