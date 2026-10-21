import asyncio

import pytest

from rumi_market.game.client import GameClient, GameClientError
from test_game_client import MemoryTransport


async def test_observer_gets_only_complete_deduplicated_responses_without_filling_backlog():
    transport = MemoryTransport()
    responses = []
    ready = asyncio.Event()

    def observe(response):
        responses.append(response)
        if response.command == 7002:
            ready.set()

    client = GameClient(transport, notification_commands=frozenset({2913}), response_observer=observe)
    await client.connect()
    try:
        transport.respond(7000, 0, b"part-one", remaining=1, notify=1)
        transport.respond(7000, 0, b"part-two", notify=1)
        transport.respond(7000, 0, b"duplicate", notify=1)
        for sequence in range(2, 102):
            transport.respond(7001, 0, b"item", notify=sequence)
        transport.respond(7002, 0, b"done", notify=102)
        await asyncio.wait_for(ready.wait(), 1)
        assert responses[0].data == b"part-onepart-two"
        assert len(responses) == 102
        assert not client.closed
        assert not client._backlog[2913]
    finally:
        await client.close()


async def test_observer_failure_does_not_break_ack_or_notification_delivery():
    transport = MemoryTransport()

    def failing_observer(response):
        raise RuntimeError("synthetic-private-observer-error")

    client = GameClient(transport, notification_commands=frozenset({2913}), response_observer=failing_observer)
    await client.connect()
    try:
        call = asyncio.create_task(client.request(2901, 2902, b"request"))
        request = await transport.next_request()
        transport.respond(2902, request.ReqCmdSeq, b"ack")
        assert (await call).data == b"ack"
        transport.respond(2913, 0, b"end", notify=1)
        assert (await client.wait_notification(2913)).data == b"end"
        assert client.observer_failed is True
        assert not client.closed
    finally:
        await client.close()


def test_observer_must_be_callable():
    with pytest.raises(GameClientError, match="^GAME_CLIENT_INVALID_OBSERVER$"):
        GameClient(MemoryTransport(), response_observer=1)


async def test_unknown_observations_cannot_evict_selected_notification_deduplication():
    transport = MemoryTransport()
    client = GameClient(transport, notification_commands=frozenset({2913}),
                        response_observer=lambda response: None)
    await client.connect()
    try:
        marker = asyncio.create_task(client.request(2901, 2902, b"marker"))
        request = await transport.next_request()
        transport.respond(2913, 0, b"end", notify=1)
        for sequence in range(2, 130):
            transport.respond(7001, 0, b"unknown", notify=sequence)
        transport.respond(2913, 0, b"end", notify=1)
        transport.respond(2902, request.ReqCmdSeq, b"marker-ack")
        await marker
        assert len(client._backlog[2913]) == 1
        assert not client.closed
    finally:
        await client.close()
