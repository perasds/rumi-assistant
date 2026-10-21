import asyncio
from types import SimpleNamespace

import pytest

from rumi_market.game.handshake import GameHandshake
from rumi_market.game.proto import GameProtoCodec
from rumi_market.game.version import VersionResult
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.live_runtime import _LazyLiveProvider
from test_game_client import MemoryTransport
from test_game_login import profile
from test_game_runtime import key, setup, PRIVATE
from test_season_state import activity, notification


CODEC = GameProtoCodec()


async def server(game):
    login = await game.next_request()
    game.respond(2902, login.ReqCmdSeq, CODEC.encode('SNKProto.Cmd_Net_LumiLogin_Ack', {'role_id': 123}))
    enter = await game.next_request()
    game.respond(8001, 0, notification(rows=[activity(system=700020)]).data, notify=1)
    game.respond(2912, enter.ReqCmdSeq, CODEC.encode('SNKProto.Cmd_Role_EnterGame_Ack', {
        'zone_id': 7, 'zone_name': 'synthetic-zone', 'server_time_ms': 150000}))
    game.respond(2913, 0, CODEC.encode('SNKProto.Cmd_Notify_LoginEnd', {}), notify=2)


@pytest.mark.parametrize('hot', [64, 65, 66])
async def test_factory_relays_full_live_provider_season_across_hot_versions(key, monkeypatch, hot):
    clock = [10.0]
    monkeypatch.setattr('rumi_market.game.handshake.monotonic', lambda: clock[0])
    runtime, game, *_ = setup(key)
    async def version():
        return VersionResult('1.5.3', hot)
    runtime._version_client.fetch = version
    provider = IndependentGameProvider(runtime, SimpleNamespace(), login_verified=True)
    lazy = _LazyLiveProvider(lambda: None)
    lazy._delegate = provider
    task = asyncio.create_task(server(game))
    try:
        await runtime.start('synthetic-account', PRIVATE)
        await task
        snapshot = lazy.season_snapshot
        assert snapshot['valid']
        assert snapshot['current_season_id'] == 2
        # The next read resolves against the current game clock, not login time.
        clock[0] += 51
        assert not lazy.season_snapshot['valid']
        assert lazy.season_snapshot['unavailable_reason'] == 'no_active_season'
        clock[0] = 0
        assert lazy.season_snapshot['unavailable_reason'] == 'clock_unavailable'
        await runtime.close()
        assert not lazy.season_snapshot['valid']
    finally:
        task.cancel()
        await runtime.close()


async def test_handshake_marks_only_current_login_and_clears_on_close():
    from rumi_market.game.season_state import SeasonState
    state = SeasonState()
    transport = MemoryTransport()
    handshake = GameHandshake(transport, season_state=state)
    task = asyncio.create_task(server(transport))
    try:
        await handshake.start(profile())
        await task
        assert state.snapshot(150)['current_season_id'] == 2
    finally:
        task.cancel()
        await handshake.close()
    assert not state.snapshot(150)['valid']
    # Receiving a baseline after close must not resurrect the previous login.
    transport.respond(8001, 0, notification(rows=[activity()]).data, notify=3)
    await asyncio.sleep(0)
    assert not state.snapshot(150)['valid']
