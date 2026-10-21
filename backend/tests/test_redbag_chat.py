"""Synthetic chat socket checks: live-only cards, bounded memory, secret cleanup."""
import asyncio

import pytest

from rumi_market.errors import ApiError
from test_redbag_operations import setup, implementation


@pytest.mark.asyncio
async def test_only_new_broadcast_redbag_cards_enter_queue_and_duplicates_are_ignored():
    module, operations, _, socket, _ = await setup()
    codec = module.RedbagProtoCodec()
    card = codec.encode("HomeVisitShareCardParam", share_str_id="new-A", sender_uid=2,
        taken_count=1, max_count=5, expire_time=1100)
    fields = {"chat_type": 1, "messages": [{"sender_id": 2, "msg_type": 13,
        "common_info": {"share_type": 3, "param": card}}]}
    try:
        await socket.queue.put(codec.encode_ws(23910, codec.encode(23910, **fields)))
        await socket.queue.put(codec.encode_ws(23909, codec.encode(23909, **fields)))
        await socket.queue.put(codec.encode_ws(23909, codec.encode(23909, **fields)))
        candidate = await asyncio.wait_for(operations.next(), .2)
        assert candidate.id == "new-A" and candidate.sender_role_id == "2"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(operations.next(), .02)
    finally: await operations.stop()


@pytest.mark.asyncio
async def test_auth_state_is_ram_only_and_cleared_on_close():
    module = implementation()
    codec = module.RedbagProtoCodec()
    state = module.RedbagState(verified_version=True)
    from types import SimpleNamespace
    state.observe(SimpleNamespace(command=2919, code=0,
        data=codec.encode(2919, params={"auth": "synthetic-secret"})))
    assert (await state.auth(timeout=.1))["auth"] == "synthetic-secret"
    assert "synthetic-secret" not in repr(state)
    state.close()
    assert not state.has_auth
    with pytest.raises(ApiError): await state.auth(timeout=.01)


@pytest.mark.asyncio
async def test_unverified_version_and_untrusted_endpoint_fail_before_any_send():
    module = implementation()
    for url in ("wss://evil.example/ws", "tcp://chat.bilibiligame.net:443", "wss://a@chat.bilibiligame.net"):
        with pytest.raises(ApiError):
            module.RedbagOperations(None, module.RedbagState(verified_version=True),
                chat_url=url, role_id="1", server_time=lambda: 1000, zone_id=1,
                nation_code="CN", language=1)


@pytest.mark.asyncio
async def test_queue_overflow_stops_only_redbag_and_main_close_wakes_waiter():
    module, operations, client, socket, _ = await setup()
    codec = module.RedbagProtoCodec()
    rows = []
    for index in range(129):
        card = codec.encode("HomeVisitShareCardParam", share_str_id=f"new-{index}",
            sender_uid=2, taken_count=1, max_count=5, expire_time=1100)
        rows.append({"msg_type": 13, "common_info": {"share_type": 3, "param": card}})
    try:
        await socket.queue.put(codec.encode_ws(23909, codec.encode(23909, messages=rows)))
        await asyncio.sleep(0)
        with pytest.raises(ApiError) as failure: await operations.next()
        assert failure.value.code == "REDBAG_QUEUE_FULL" and not client.closed
    finally: await operations.stop()
    _, operations, client, _, _ = await setup()
    try:
        task = asyncio.create_task(operations.next())
        await asyncio.sleep(0)
        client.closed = True
        with pytest.raises(ApiError) as failure: await asyncio.wait_for(task, 1.2)
        assert failure.value.code == "GAME_SESSION_EXPIRED"
    finally: await operations.stop()


@pytest.mark.asyncio
async def test_auth_expiration_invalidates_redbag_without_exposing_auth_values():
    module, operations, _, _, _ = await setup()
    from types import SimpleNamespace
    try:
        operations._state.observe(SimpleNamespace(command=4012, code=-8, data=b""))
        with pytest.raises(ApiError) as failure: await operations.next()
        assert failure.value.code == "GAME_SESSION_EXPIRED"
    finally: await operations.stop()


@pytest.mark.asyncio
async def test_verified_official_chat_port_8917_is_allowed():
    _, operations, _, socket, _ = await setup(chat_url="wss://chat.bilibiligame.net:8917/ws")
    try:
        assert socket.sent[:2] == [23920, 23903]
    finally:
        await operations.stop()
