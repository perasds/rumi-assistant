import asyncio

import pytest

from rumi_market.game.client import GameClient, GameClientError
from test_game_client import MemoryTransport


async def test_stop_is_checked_inside_send_lock_before_any_purchase_bytes():
    transport = MemoryTransport()
    client = GameClient(transport)
    await client.connect()
    await client._send_lock.acquire()
    stopped = False
    task = asyncio.create_task(client.request(5319, 5320, b"synthetic-purchase",
                                              before_send=lambda: not stopped))
    try:
        await asyncio.sleep(0)
        stopped = True
        client._send_lock.release()
        with pytest.raises(GameClientError, match="^GAME_REQUEST_NOT_SENT$"):
            await task
        assert transport.outgoing.empty() and client.pending_count == 0
        assert not client.closed
    finally:
        if client._send_lock.locked():
            client._send_lock.release()
        await client.close()


async def test_guard_allows_exactly_one_normal_request_and_ack():
    transport = MemoryTransport()
    client = GameClient(transport)
    await client.connect()
    checks = []
    def guard():
        checks.append(True)
        return True
    task = asyncio.create_task(client.request(5319, 5320, b"synthetic-purchase", before_send=guard))
    try:
        request = await transport.next_request()
        transport.respond(5320, request.ReqCmdSeq, b"synthetic-ack")
        assert (await task).data == b"synthetic-ack"
        assert checks == [True]
    finally:
        await client.close()


async def test_guard_failure_never_exposes_private_error_or_sends():
    transport = MemoryTransport()
    client = GameClient(transport)
    await client.connect()
    def guard():
        raise RuntimeError("synthetic-private-guard-error")
    try:
        with pytest.raises(GameClientError) as caught:
            await client.request(5319, 5320, b"synthetic-purchase", before_send=guard)
        assert caught.value.code == "GAME_REQUEST_NOT_SENT"
        assert caught.value.__context__ is None
        assert transport.outgoing.empty()
    finally:
        await client.close()
