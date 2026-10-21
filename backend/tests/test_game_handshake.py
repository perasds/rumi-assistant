import asyncio
import importlib

import pytest

from rumi_market.game.proto import GameProtoCodec
from test_game_client import MemoryTransport, SECRET
from test_game_login import profile


def implementation():
    return importlib.import_module("rumi_market.game.handshake")


def payload(name, fields):
    return GameProtoCodec().encode("SNKProto." + name, fields)


async def complete_server(transport, *, role_id=123, activation=False, init_pet=False,
                          server_time_ms=1791970000000):
    login = await transport.next_request()
    assert login.ReqCmdId == 2901
    decoded = GameProtoCodec().decode("SNKProto.Cmd_Net_LumiLogin_Req", login.ReqData).message
    assert decoded.param_bili.uid == "synthetic-game-uid"
    assert decoded.param_bili.access_token == "synthetic-game-token"
    login_fields = {} if role_id is None else {"role_id": role_id}
    transport.respond(2902, login.ReqCmdSeq, payload("Cmd_Net_LumiLogin_Ack", login_fields))
    enter = await transport.next_request()
    assert enter.ReqCmdId == 2911 and enter.ReqCmdSeq == 2
    decoded = GameProtoCodec().decode("SNKProto.Cmd_Role_EnterGame_Req", enter.ReqData).message
    assert not decoded.is_reconnect
    assert not decoded.HasField("session") and not decoded.HasField("role_id")
    if init_pet:
        # 2934 presence itself triggers the source's non-null InitLumiData branch.
        transport.respond(2934, 0, b"", notify=1)
    # Deliberately deliver LoginEnd before EnterAck to exercise notification retention.
    transport.respond(2913, 0, payload("Cmd_Notify_LoginEnd", {
        "use_cd_key": activation, "cd_key": ""}), notify=2)
    fields = {"zone_id": 7, "zone_name": "synthetic-zone"}
    if server_time_ms is not None:
        fields["server_time_ms"] = server_time_ms
    transport.respond(2912, enter.ReqCmdSeq, payload("Cmd_Role_EnterGame_Ack", fields))


@pytest.mark.parametrize("role_id", [123, 0, None])
async def test_normal_handshake_preserves_role_presence_and_waits_for_ack_and_end(role_id):
    transport = MemoryTransport()
    handshake = implementation().GameHandshake(transport)
    assert not handshake.can_query
    server = asyncio.create_task(complete_server(transport, role_id=role_id))
    try:
        result = await handshake.start(profile())
        await server
        assert result.role_id == role_id
        assert (result.zone_id, result.zone_name) == (7, "synthetic-zone")
        assert result.server_time_ms == 1791970000000
        assert not result.needs_activation and not result.needs_initial_pet_selection
        assert handshake.can_query
        assert transport.connections == 1
        assert "synthetic-game-token" not in repr(result)
    finally:
        await handshake.close()
    assert not handshake.can_query and handshake.result is None


@pytest.mark.parametrize("activation,init_pet", [(True, False), (False, True), (True, True)])
async def test_activation_or_initial_pet_gate_prevents_query_ready(activation, init_pet):
    transport = MemoryTransport()
    handshake = implementation().GameHandshake(transport)
    server = asyncio.create_task(complete_server(transport, activation=activation, init_pet=init_pet))
    try:
        result = await handshake.start(profile())
        await server
        assert result.needs_activation is activation
        assert result.needs_initial_pet_selection is init_pet
        assert not handshake.can_query
    finally:
        await handshake.close()


async def test_successful_enter_ack_alone_does_not_complete_handshake():
    from rumi_market.game.client import GameClientError

    transport = MemoryTransport()
    handshake = implementation().GameHandshake(transport, timeout=0.5)
    task = asyncio.create_task(handshake.start(profile()))
    login = await transport.next_request()
    transport.respond(2902, login.ReqCmdSeq, payload("Cmd_Net_LumiLogin_Ack", {}))
    enter = await transport.next_request()
    transport.respond(2912, enter.ReqCmdSeq, payload("Cmd_Role_EnterGame_Ack", {}))
    with pytest.raises(GameClientError, match="^GAME_NOTIFICATION_TIMEOUT$"):
        await task
    assert not handshake.can_query and handshake.result is None and transport.closed


async def test_game_login_rejection_stops_before_enter_request_and_closes():
    from rumi_market.game.client import GameClientError

    transport = MemoryTransport()
    handshake = implementation().GameHandshake(transport)
    task = asyncio.create_task(handshake.start(profile()))
    login = await transport.next_request()
    transport.respond(2902, login.ReqCmdSeq, SECRET, code=-9)
    with pytest.raises(GameClientError) as failure:
        await task
    assert failure.value.code == "GAME_REQUEST_REJECTED"
    assert failure.value.response_code == -9
    assert transport.outgoing.empty() and transport.closed
    assert handshake.result is None and not handshake.can_query


async def test_cancellation_during_handshake_releases_connection():
    transport = MemoryTransport()
    handshake = implementation().GameHandshake(transport)
    task = asyncio.create_task(handshake.start(profile()))
    await transport.next_request()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.closed and not handshake.can_query and handshake.result is None


async def test_handshake_instance_cannot_start_a_second_authentication():
    transport = MemoryTransport()
    handshake = implementation().GameHandshake(transport)
    server = asyncio.create_task(complete_server(transport))
    try:
        await handshake.start(profile())
        await server
        with pytest.raises(implementation().GameHandshakeError, match="^GAME_HANDSHAKE_ALREADY_STARTED$"):
            await handshake.start(profile())
        assert transport.connections == 1
    finally:
        await handshake.close()


async def test_initial_pet_notification_after_end_updates_gate_and_current_result():
    transport = MemoryTransport()
    handshake = implementation().GameHandshake(transport)
    server = asyncio.create_task(complete_server(transport))
    try:
        await handshake.start(profile())
        await server
        assert handshake.can_query
        transport.respond(2934, 0, b"", notify=3)
        await handshake.client.wait_notification(2934)
        assert not handshake.can_query
        assert handshake.result.needs_initial_pet_selection
    finally:
        await handshake.close()


async def test_server_clock_uses_ack_milliseconds_and_monotonic_second_boundary(monkeypatch):
    module = implementation()
    now = [10.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0], raising=False)
    transport = MemoryTransport()
    handshake = module.GameHandshake(transport)
    assert handshake.server_time_seconds is None
    server = asyncio.create_task(complete_server(transport, server_time_ms=1700000000125))
    try:
        await handshake.start(profile())
        await server
        assert handshake.server_time_seconds == 1700000000
        now[0] = 10.5
        assert handshake.server_time_seconds == 1700000000
        now[0] = 10.875
        assert handshake.server_time_seconds == 1700000001
        now[0] = 12.0
        assert handshake.server_time_seconds == 1700000002
    finally:
        await handshake.close()
    assert handshake.server_time_seconds is None


@pytest.mark.parametrize("server_time_ms", [None, 0, -1])
async def test_missing_or_invalid_server_clock_stays_unknown(monkeypatch, server_time_ms):
    module = implementation()
    monkeypatch.setattr(module, "monotonic", lambda: 10.0, raising=False)
    transport = MemoryTransport()
    handshake = module.GameHandshake(transport)
    server = asyncio.create_task(complete_server(transport, server_time_ms=server_time_ms))
    try:
        await handshake.start(profile())
        await server
        assert handshake.can_query and handshake.server_time_seconds is None
    finally:
        await handshake.close()


@pytest.mark.parametrize("invalid_now", [9.0, 10.5, float("nan"), float("inf")])
async def test_backwards_or_invalid_monotonic_invalidates_clock(monkeypatch, invalid_now):
    module = implementation()
    now = [10.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0], raising=False)
    transport = MemoryTransport()
    handshake = module.GameHandshake(transport)
    server = asyncio.create_task(complete_server(transport))
    try:
        await handshake.start(profile())
        await server
        now[0] = 11.0
        assert handshake.server_time_seconds == 1791970001
        now[0] = invalid_now
        assert handshake.server_time_seconds is None
        now[0] = 12.0
        assert handshake.server_time_seconds is None
    finally:
        await handshake.close()


async def test_enter_ack_anchors_clock_before_delayed_login_end(monkeypatch):
    module = implementation()
    now = [10.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0], raising=False)
    transport = MemoryTransport()
    handshake = module.GameHandshake(transport)
    waiting = asyncio.Event()
    original_wait = handshake.client.wait_notification

    async def observed_wait(command):
        waiting.set()
        return await original_wait(command)

    monkeypatch.setattr(handshake.client, "wait_notification", observed_wait)
    task = asyncio.create_task(handshake.start(profile()))
    try:
        login = await transport.next_request()
        transport.respond(2902, login.ReqCmdSeq, payload("Cmd_Net_LumiLogin_Ack", {}))
        enter = await transport.next_request()
        transport.respond(2912, enter.ReqCmdSeq, payload("Cmd_Role_EnterGame_Ack", {
            "server_time_ms": 1700000000000}))
        await asyncio.wait_for(waiting.wait(), 1)
        assert not task.done() and not handshake.can_query
        now[0] = 14.0
        transport.respond(2913, 0, payload("Cmd_Notify_LoginEnd", {}), notify=1)
        await task
        assert handshake.server_time_seconds == 1700000004
    finally:
        task.cancel()
        await handshake.close()
