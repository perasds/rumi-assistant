"""Version HTTP -> runtime login -> native notices must share one verified gate."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from rumi_market.game.home_codec import HomeProtoCodec
from rumi_market.game.profile_sync import _load_schema
from rumi_market.game.proto import GameProtoCodec
from rumi_market.game_provider import IndependentGameProvider
from test_game_runtime import PRIVATE, key, setup as runtime_setup
from test_order_service import setup as order_setup
from test_season_state import activity, notification


CODEC = GameProtoCodec()
GOLD_BALANCE = 12345678


def setup_version(key, hot):
    runtime, game, events, transports, *_ = runtime_setup(key)
    handle_original_version = transports[0].handler

    async def version_response(request):
        # Keep the real request signer/parser; only the synthetic peer's public
        # hotVersion response differs from the established runtime fixture.
        response = await handle_original_version(request)
        body = response.json()
        body['data']['hotVersion']['version'] = str(hot)
        return httpx.Response(200, json=body)

    transports[0].handler = version_response
    return runtime, game, events


async def server(game, hot):
    login = await game.next_request()
    assert login.ReqCmdId == 2901
    profile = CODEC.decode('SNKProto.Cmd_Net_LumiLogin_Req', login.ReqData).message
    assert profile.hot_version == hot
    game.respond(2902, login.ReqCmdSeq,
        CODEC.encode('SNKProto.Cmd_Net_LumiLogin_Ack', {'role_id': 123}))
    enter = await game.next_request()
    assert enter.ReqCmdId == 2911

    home = HomeProtoCodec().encode(3001, architecture_list=[{
        'architecture_uid': 12, 'architecture_id': 10101, 'architecture_level': 1,
        'work_old': {'lumi_uid': 0, 'recipe_id': 0}, 'store_info': {},
    }], order_archi={'slot_list': [{
        'slot_id': 1, 'is_unlock': True, 'order_info': {'order_type': 1,
            'item_need': [{'item_id': 20001, 'count': 3}],
            'item_reward': [{'item_id': 54001, 'count': 5}]},
    }]})
    profile_messages, _, _ = _load_schema()
    inventory = profile_messages[2941](item_list=[
        {'item_id': 20001, 'item_uid': 0, 'count': 3},
        {'item_id': 10001, 'item_uid': 0, 'count': GOLD_BALANCE},
    ]).SerializeToString()
    capacity = profile_messages[3036](buff_list=[]).SerializeToString()
    game.respond(3001, 0, home, notify=1)
    game.respond(2941, 0, inventory, notify=2)
    game.respond(3036, 0, capacity, notify=3)
    game.respond(8001, 0, notification(rows=[activity(system=700020)]).data, notify=4)
    game.respond(2912, enter.ReqCmdSeq,
        CODEC.encode('SNKProto.Cmd_Role_EnterGame_Ack', {
            'zone_id': 7, 'zone_name': 'synthetic-zone', 'server_time_ms': 150000}))
    game.respond(2913, 0, CODEC.encode('SNKProto.Cmd_Notify_LoginEnd', {}), notify=5)


@pytest.mark.parametrize('hot', [65, 68, 69, 70, 71, 72, 73])
async def test_runtime_consumes_complete_native_baselines_across_hot_versions(key, hot):
    runtime, game, events = setup_version(key, hot)
    peer = asyncio.create_task(server(game, hot))
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=[]), login_verified=True)
    consumers = {}
    try:
        view = await provider.login('synthetic-account', PRIVATE)
        await peer
        assert view.authenticated and runtime.can_query and game.connections == 1
        assert events == ['version', 'config', 'cipher', 'login']
        consumers = {'orders': runtime.order_state, 'inventory': runtime.inventory_state,
                     'home': runtime.home_state, 'capacity': runtime.home_capacity_state}
        projections = {name: state.snapshot() for name, state in consumers.items()}
        assert {name: value['valid'] for name, value in projections.items()} == {
            name: True for name in projections
        }, {name: value['unavailable_reason'] for name, value in projections.items()}
        assert runtime.gold_balance == GOLD_BALANCE
        assert runtime.season_snapshot['valid']
        assert runtime.season_snapshot['current_season_id'] == 2

        service, session, _ = order_setup(provider)
        session.view = view
        state = await service.state(session)
        assert state['ready'] and state['execution_ready']
        assert state['orders'][0]['can_complete'] is True
        assert projections['inventory']['items']['20001'] == 3
        assert projections['capacity']['bonus'] == 0
        assert game.outgoing.empty()  # Login/readiness never starts an order or purchase.
    finally:
        peer.cancel()
        await provider.close()
        assert runtime.closed and not runtime.can_query and game.closed
        assert all(not state.snapshot()['valid'] for state in consumers.values())
        assert runtime.gold_balance is None and not runtime.season_snapshot['valid']
