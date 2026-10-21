import asyncio
import gc
import importlib

import pytest

from rumi_market.game.proto import GameProtoCodec
from rumi_market.game.transport import GameTransportError


SECRET = b"synthetic-private-game-data"


def implementation():
    return importlib.import_module("rumi_market.game.client")


class MemoryTransport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.connections = self.closes = 0
        self.closed = False
        self.send_error = None

    async def connect(self):
        self.connections += 1

    async def send(self, payload):
        if self.send_error:
            raise self.send_error
        await self.outgoing.put(payload)

    async def receive(self):
        result = await self.incoming.get()
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        self.closes += 1
        self.closed = True

    async def next_request(self):
        payload = await asyncio.wait_for(self.outgoing.get(), 0.5)
        return GameProtoCodec().decode("SNKProto.ReqProto", payload).message

    def respond(self, command, sequence, data=b"", *, code=0, remaining=0, notify=0):
        self.incoming.put_nowait(GameProtoCodec().encode("SNKProto.RspProto", {
            "RspCmdId": command, "RspCmdSeq": sequence, "RspNotifySeq": notify,
            "RspCode": code, "Coalescing": remaining, "RspData": data}))


def assert_safe(error, code):
    assert str(error) == error.code == code
    assert SECRET.decode() not in repr(error)
    assert error.__context__ is None
    assert error.__cause__ is None


async def connected(*, notifications=frozenset(), timeout=0.2):
    transport = MemoryTransport()
    client = implementation().GameClient(transport, notification_commands=notifications,
                                         timeout=timeout)
    await client.connect()
    return client, transport


async def test_correlates_out_of_order_responses_by_explicit_ack_and_sequence():
    client, transport = await connected()
    try:
        first = asyncio.create_task(client.request(2901, 2902, SECRET))
        second = asyncio.create_task(client.request(5325, 5326, b"market"))
        one, two = await transport.next_request(), await transport.next_request()
        assert (one.ReqCmdId, one.ReqCmdSeq, one.ReqData) == (2901, 1, SECRET)
        assert (two.ReqCmdId, two.ReqCmdSeq) == (5325, 2)
        assert not one.HasField("ReqNotifySeq")
        transport.respond(9999, 1, b"wrong-command")
        transport.respond(2902, 9999, b"wrong-sequence")
        transport.respond(5326, 2, b"second")
        assert (await second).data == b"second"
        assert not first.done()
        transport.respond(2902, 1, b"first")
        assert (await first).data == b"first"
        assert client.pending_count == 0
    finally:
        await client.close()


async def test_notification_before_ack_is_retained_and_does_not_complete_request():
    # The synthetic notification number is explicit; no odd/even classification.
    client, transport = await connected(notifications=frozenset({7000}))
    try:
        request = asyncio.create_task(client.request(2911, 2912, b"enter"))
        sent = await transport.next_request()
        transport.respond(7000, 0, b"login-end", notify=5)
        notice = await client.wait_notification(7000)
        assert notice.data == b"login-end"
        assert not request.done()
        transport.respond(2912, sent.ReqCmdSeq, b"role")
        assert (await request).data == b"role"
    finally:
        await client.close()


async def test_reassembles_ack_fragments_before_completing_pending_call():
    client, transport = await connected()
    try:
        call = asyncio.create_task(client.request(5325, 5326, b"query"))
        sent = await transport.next_request()
        transport.respond(5326, sent.ReqCmdSeq, b"a" * 50000, remaining=1)
        transport.respond(5326, sent.ReqCmdSeq, b"b" * 30000)
        response = await call
        assert response.data == b"a" * 50000 + b"b" * 30000
        assert response.remaining == 0
        assert client.pending_count == 0
    finally:
        await client.close()


async def test_nonzero_ack_rejects_and_releases_call_without_retaining_business_data():
    client, transport = await connected()
    try:
        call = asyncio.create_task(client.request(2901, 2902, SECRET))
        sent = await transport.next_request()
        transport.respond(2902, sent.ReqCmdSeq, SECRET, code=-41)
        with pytest.raises(implementation().GameClientError) as failure:
            await call
        assert_safe(failure.value, "GAME_REQUEST_REJECTED")
        assert failure.value.response_code == -41
        assert client.pending_count == 0
    finally:
        await client.close()


async def test_request_timeout_removes_waiter_and_ignores_late_ack():
    # Allow Windows event-loop timer granularity while still exercising a timeout.
    client, transport = await connected(timeout=0.5)
    try:
        call = asyncio.create_task(client.request(5325, 5326, b"query"))
        sent = await transport.next_request()
        with pytest.raises(implementation().GameClientError) as failure:
            await call
        assert_safe(failure.value, "GAME_REQUEST_TIMEOUT")
        assert client.pending_count == 0
        transport.respond(5326, sent.ReqCmdSeq, b"late")
        next_call = asyncio.create_task(client.request(5325, 5326, b"new"))
        following = await transport.next_request()
        transport.respond(5326, following.ReqCmdSeq, b"fresh")
        assert (await next_call).data == b"fresh"
    finally:
        await client.close()


async def test_close_releases_request_and_notification_waiters_and_is_idempotent():
    client, transport = await connected(notifications=frozenset({7000}))
    call = asyncio.create_task(client.request(2901, 2902, SECRET))
    await transport.next_request()
    notice = asyncio.create_task(client.wait_notification(7000))
    await asyncio.sleep(0)
    await client.close()
    for task in (call, notice):
        with pytest.raises(implementation().GameClientError) as failure:
            await task
        assert_safe(failure.value, "GAME_CLIENT_CLOSED")
    assert client.pending_count == 0
    assert transport.closed and transport.closes == 1
    await client.close()
    assert transport.closes == 1


@pytest.mark.parametrize("incoming,expected", [
    (b"\xff", "GAME_PROTOCOL_INVALID"),
    (GameTransportError("GAME_TRANSPORT_CLOSED"), "GAME_TRANSPORT_CLOSED"),
    (RuntimeError(SECRET.decode()), "GAME_CONNECTION_FAILED"),
])
async def test_receive_failure_releases_all_waiters_and_discards_raw_exception(incoming, expected):
    client, transport = await connected()
    calls = [asyncio.create_task(client.request(2901, 2902, SECRET)),
             asyncio.create_task(client.request(5325, 5326, b"query"))]
    await transport.next_request()
    await transport.next_request()
    transport.incoming.put_nowait(incoming)
    for call in calls:
        with pytest.raises(implementation().GameClientError) as failure:
            await call
        assert_safe(failure.value, expected)
    assert client.pending_count == 0 and transport.closed
    await client.close()


async def test_cancelling_one_request_preserves_connection_and_other_requests():
    client, transport = await connected()
    try:
        call = asyncio.create_task(client.request(5325, 5326, SECRET))
        await transport.next_request()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert client.pending_count == 0 and not transport.closed
        other = asyncio.create_task(client.request(2901, 2902, b"login"))
        sent = await transport.next_request()
        transport.respond(2902, sent.ReqCmdSeq, b"ok")
        assert (await other).data == b"ok"
    finally:
        await client.close()


async def test_unregistered_notification_is_rejected_before_waiting():
    client, _ = await connected()
    try:
        with pytest.raises(implementation().GameClientError) as failure:
            await client.wait_notification(7000)
        assert_safe(failure.value, "GAME_NOTIFICATION_UNREGISTERED")
    finally:
        await client.close()


async def test_sequence_collision_does_not_remove_an_existing_request():
    from rumi_market.game.envelope import RequestSequencer

    client, transport = await connected()
    try:
        first = asyncio.create_task(client.request(2901, 2902, SECRET))
        sent = await transport.next_request()
        # Force the uint32 wrap/collision branch without sending billions of requests.
        client._sequencer = RequestSequencer(last_sequence=0)
        with pytest.raises(implementation().GameClientError) as failure:
            await client.request(2901, 2902, b"collision")
        assert_safe(failure.value, "GAME_REQUEST_SEQUENCE_IN_USE")
        assert client.pending_count == 1
        transport.respond(2902, sent.ReqCmdSeq, b"original")
        assert (await first).data == b"original"
    finally:
        await client.close()


async def test_send_failure_releases_request_and_closes_connection():
    client, transport = await connected()
    transport.send_error = RuntimeError(SECRET.decode())
    with pytest.raises(implementation().GameClientError) as failure:
        await client.request(2901, 2902, SECRET)
    assert_safe(failure.value, "GAME_CONNECTION_FAILED")
    assert client.pending_count == 0 and transport.closed
    await client.close()


async def test_notifications_are_deduplicated_after_complete_response_delivery():
    client, transport = await connected(notifications=frozenset({7000}))
    try:
        transport.respond(7000, 0, b"first", notify=5)
        transport.respond(7000, 0, b"duplicate", notify=5)
        transport.respond(7000, 0, b"next", notify=6)
        assert (await client.wait_notification(7000)).data == b"first"
        assert (await client.wait_notification(7000)).data == b"next"
    finally:
        await client.close()


async def test_calls_require_connection_and_cannot_reopen_after_close():
    client = implementation().GameClient(MemoryTransport())
    with pytest.raises(implementation().GameClientError) as failure:
        await client.request(2901, 2902, b"")
    assert_safe(failure.value, "GAME_CLIENT_NOT_CONNECTED")
    await client.close()
    with pytest.raises(implementation().GameClientError) as failure:
        await client.connect()
    assert_safe(failure.value, "GAME_CLIENT_CLOSED")


async def test_cancel_during_send_does_not_leave_an_unretrieved_future_error():
    class BlockedSend(MemoryTransport):
        async def send(self, payload):
            await self.outgoing.put(payload)
            await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    reports = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: reports.append(context))
    transport = BlockedSend()
    client = implementation().GameClient(transport)
    try:
        await client.connect()
        call = asyncio.create_task(client.request(2901, 2902, SECRET))
        await transport.next_request()
        call.cancel()
        try:
            await call
        except asyncio.CancelledError:
            pass
        del call
        gc.collect()
        await asyncio.sleep(0)
        assert client.pending_count == 0 and transport.closed
        assert reports == []
    finally:
        await client.close()
        loop.set_exception_handler(previous)


async def test_registered_notification_receipt_tracks_successful_complete_envelopes():
    client, transport = await connected(notifications=frozenset({2934}))
    try:
        assert not client.received_notification(2934)
        transport.respond(2934, 0, b"init", notify=6, remaining=1)
        transport.respond(2934, 0, b"pet", notify=6)
        assert (await client.wait_notification(2934)).data == b"initpet"
        assert client.received_notification(2934)
    finally:
        await client.close()
    assert not client.received_notification(2934)
