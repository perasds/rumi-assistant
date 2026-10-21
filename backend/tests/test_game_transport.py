import asyncio
import importlib
import ssl

import pytest


HOST = "prod-cn-ob-gate-lumigo.bilibiligame.net"
SECRET = "synthetic-private-payload"


def implementation():
    return importlib.import_module("rumi_market.game.transport")


class FakeWriter:
    def __init__(self):
        self.writes = []
        self.drains = self.closes = self.waits = self.aborts = 0
        self.write_error = self.drain_error = self.close_error = None
        self.wait_hangs = False
        self.transport = self

    def write(self, frame):
        if self.write_error:
            raise self.write_error
        self.writes.append(frame)

    async def drain(self):
        self.drains += 1
        if self.drain_error:
            raise self.drain_error

    def close(self):
        self.closes += 1
        if self.close_error:
            raise self.close_error

    async def wait_closed(self):
        self.waits += 1
        if self.wait_hangs:
            await asyncio.Event().wait()

    def abort(self):
        self.aborts += 1


async def connected(monkeypatch, *, reader=None, writer=None, timeout=0.1, keep_idle=False):
    module = implementation()
    reader = reader or asyncio.StreamReader()
    writer = writer or FakeWriter()
    calls = []

    async def open_connection(*args, **kwargs):
        calls.append((args, kwargs))
        return reader, writer

    monkeypatch.setattr(module.asyncio, "open_connection", open_connection)
    client = module.GameTransport(timeout=timeout, keep_idle=keep_idle)
    await client.connect()
    return client, reader, writer, calls


def assert_safe(error, code):
    assert error.code == code
    assert str(error) == code
    assert SECRET not in repr(error)
    assert error.__context__ is None
    assert error.__cause__ is None


async def test_connection_uses_fixed_gateway_and_default_certificate_validation(monkeypatch):
    client, _, writer, calls = await connected(monkeypatch)
    args, kwargs = calls[0]
    assert args == (HOST, 16880)
    assert kwargs["server_hostname"] == HOST
    context = kwargs["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    await client.connect()
    assert len(calls) == 1
    await client.close()
    await client.close()
    assert writer.closes == writer.waits == 1


async def test_send_writes_one_complete_big_endian_frame_and_drains(monkeypatch):
    client, _, writer, _ = await connected(monkeypatch)
    try:
        payload = b"synthetic-payload" * 20
        await client.send(payload)
        assert writer.writes == [len(payload).to_bytes(2, "big") + payload]
        assert writer.drains == 1
    finally:
        await client.close()


async def test_segmented_headers_payloads_and_coalesced_frames_preserve_boundaries(monkeypatch):
    client, reader, _, _ = await connected(monkeypatch)

    async def feed():
        for chunk in [b"\x00", b"\x03a", b"bc\x00\x02de"]:
            reader.feed_data(chunk)
            await asyncio.sleep(0)

    feeder = asyncio.create_task(feed())
    try:
        assert await client.receive() == b"abc"
        assert await client.receive() == b"de"
        await feeder
    finally:
        await client.close()


async def test_maximum_uint16_payload_is_read_without_truncation(monkeypatch):
    client, reader, _, _ = await connected(monkeypatch)
    reader.feed_data(b"\xff\xff" + b"x" * 65535)
    try:
        assert await client.receive() == b"x" * 65535
    finally:
        await client.close()


async def test_zero_length_incoming_frame_closes_without_reading_trailing_data(monkeypatch):
    client, reader, writer, _ = await connected(monkeypatch)
    reader.feed_data(b"\x00\x00" + SECRET.encode())
    with pytest.raises(implementation().GameTransportError) as failure:
        await client.receive()
    assert_safe(failure.value, "GAME_FRAME_INVALID")
    assert client.closed and writer.closes == 1
    assert client._reader is client._writer is None


@pytest.mark.parametrize("partial", [b"", b"\x00", b"\x00\x05ab"],
                         ids=["no-header", "partial-header", "partial-payload"])
async def test_eof_discards_partial_frame_and_has_no_raw_exception_context(monkeypatch, partial):
    client, reader, writer, _ = await connected(monkeypatch)
    reader.feed_data(partial)
    reader.feed_eof()
    with pytest.raises(implementation().GameTransportError) as failure:
        await client.receive()
    assert_safe(failure.value, "GAME_TRANSPORT_CLOSED")
    assert client.closed and writer.closes == 1
    assert client._reader is client._writer is None


async def test_partial_frame_timeout_closes_and_returns_fixed_classification(monkeypatch):
    client, reader, writer, _ = await connected(monkeypatch, timeout=0.01)
    reader.feed_data(b"\x00\x05ab")
    with pytest.raises(implementation().GameTransportError) as failure:
        await client.receive()
    assert_safe(failure.value, "GAME_TRANSPORT_TIMEOUT")
    assert client.closed and writer.closes == 1


@pytest.mark.parametrize("payload", [b"", b"x" * 65536, SECRET],
                         ids=["empty", "oversized", "wrong-type"])
async def test_invalid_outbound_payload_closes_before_any_write(monkeypatch, payload):
    client, _, writer, _ = await connected(monkeypatch)
    with pytest.raises(implementation().GameTransportError) as failure:
        await client.send(payload)
    assert_safe(failure.value, "GAME_FRAME_INVALID")
    assert client.closed and writer.closes == 1
    assert writer.writes == []


@pytest.mark.parametrize("operation", ["write", "drain", "read"])
async def test_stream_failures_close_and_do_not_keep_private_raw_context(monkeypatch, operation):
    class FailingReader:
        async def readexactly(self, count):
            raise OSError(SECRET)

    writer = FakeWriter()
    reader = FailingReader() if operation == "read" else None
    if operation == "write":
        writer.write_error = OSError(SECRET)
    if operation == "drain":
        writer.drain_error = OSError(SECRET)
    client, _, _, _ = await connected(monkeypatch, reader=reader, writer=writer)
    with pytest.raises(implementation().GameTransportError) as failure:
        await (client.receive() if operation == "read" else client.send(SECRET.encode()))
    assert_safe(failure.value, "GAME_TRANSPORT_UNAVAILABLE")
    assert client.closed and writer.closes == 1


@pytest.mark.parametrize("exception", [ssl.SSLCertVerificationError(SECRET), OSError(SECRET)],
                         ids=["invalid-certificate", "connect-error"])
async def test_connection_failures_are_safe_and_leave_transport_closed(monkeypatch, exception):
    module = implementation()

    async def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(module.asyncio, "open_connection", fail)
    client = module.GameTransport(timeout=0.01)
    with pytest.raises(module.GameTransportError) as failure:
        await client.connect()
    assert_safe(failure.value, "GAME_TRANSPORT_UNAVAILABLE")
    assert client.closed and client._reader is client._writer is None


async def test_connection_timeout_is_bounded_and_safe(monkeypatch):
    module = implementation()

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(module.asyncio, "open_connection", hang)
    client = module.GameTransport(timeout=0.01)
    with pytest.raises(module.GameTransportError) as failure:
        await client.connect()
    assert_safe(failure.value, "GAME_TRANSPORT_TIMEOUT")
    assert client.closed


@pytest.mark.parametrize("mode", ["wait-timeout", "close-error"])
async def test_close_is_bounded_aborts_on_failure_and_is_idempotent(monkeypatch, mode):
    writer = FakeWriter()
    writer.wait_hangs = mode == "wait-timeout"
    writer.close_error = OSError(SECRET) if mode == "close-error" else None
    client, _, _, _ = await connected(monkeypatch, writer=writer, timeout=0.01)
    await client.close()
    await client.close()
    assert client.closed and client._reader is client._writer is None
    assert writer.closes == writer.waits == writer.aborts == 1


async def test_cancelling_receive_closes_before_propagating_cancellation(monkeypatch):
    client, _, writer, _ = await connected(monkeypatch)
    task = asyncio.create_task(client.receive())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed and writer.closes == 1


async def test_close_during_connection_closes_late_streams(monkeypatch):
    module = implementation()
    started, release = asyncio.Event(), asyncio.Event()
    writer = FakeWriter()

    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return asyncio.StreamReader(), writer

    monkeypatch.setattr(module.asyncio, "open_connection", delayed)
    client = module.GameTransport(timeout=0.1)
    task = asyncio.create_task(client.connect())
    await started.wait()
    await client.close()
    release.set()
    with pytest.raises(module.GameTransportError) as failure:
        await task
    assert_safe(failure.value, "GAME_TRANSPORT_CLOSED")
    assert client.closed and writer.closes == 1


async def test_closed_transport_rejects_reuse_without_connecting(monkeypatch):
    client, _, _, calls = await connected(monkeypatch)
    await client.close()
    for operation in [client.connect, client.receive, lambda: client.send(b"x")]:
        with pytest.raises(implementation().GameTransportError) as failure:
            await operation()
        assert_safe(failure.value, "GAME_TRANSPORT_CLOSED")
    assert len(calls) == 1


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 61, 10**400, SECRET],
                         ids=["zero", "negative", "bool", "nan", "infinite", "over-limit", "huge-integer", "wrong-type"])
def test_timeout_must_be_a_finite_positive_bounded_number(timeout):
    with pytest.raises(implementation().GameTransportError) as failure:
        implementation().GameTransport(timeout=timeout)
    assert_safe(failure.value, "GAME_TRANSPORT_INVALID_TIMEOUT")


async def test_keep_idle_waits_past_timeout_then_reads_normal_frames_without_heartbeat(monkeypatch):
    client, reader, writer, _ = await connected(monkeypatch, timeout=0.01, keep_idle=True)
    task = asyncio.create_task(client.receive())
    try:
        await asyncio.sleep(0.04)
        assert not task.done() and not client.closed and writer.closes == 0
        reader.feed_data(b"\x00\x03abc\x00\x02de")
        assert await asyncio.wait_for(task, 0.1) == b"abc"
        assert await client.receive() == b"de"
        assert writer.writes == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("partial", [b"\x00", b"\x00\x30" + SECRET.encode()],
                         ids=["partial-header", "partial-payload"])
async def test_keep_idle_partial_frame_still_has_bounded_timeout_and_safe_close(monkeypatch, partial):
    client, reader, writer, _ = await connected(monkeypatch, timeout=0.01, keep_idle=True)
    reader.feed_data(partial)
    with pytest.raises(implementation().GameTransportError) as failure:
        await asyncio.wait_for(client.receive(), 0.1)
    assert_safe(failure.value, "GAME_TRANSPORT_TIMEOUT")
    assert not hasattr(failure.value, "partial")
    assert client.closed and writer.closes == writer.waits == 1
    assert client._reader is client._writer is None


@pytest.mark.parametrize("partial", [b"", b"\x00", b"\x00\x30" + SECRET.encode()],
                         ids=["idle", "partial-header", "partial-payload"])
async def test_keep_idle_eof_returns_closed_without_private_incomplete_read(monkeypatch, partial):
    client, reader, writer, _ = await connected(monkeypatch, timeout=0.01, keep_idle=True)
    reader.feed_data(partial)
    reader.feed_eof()
    with pytest.raises(implementation().GameTransportError) as failure:
        await client.receive()
    assert_safe(failure.value, "GAME_TRANSPORT_CLOSED")
    assert not hasattr(failure.value, "partial")
    assert client.closed and writer.closes == writer.waits == 1


async def test_cancelling_keep_idle_receive_closes_and_releases_stream_reader_waiter(monkeypatch):
    client, reader, writer, _ = await connected(monkeypatch, timeout=0.01, keep_idle=True)
    task = asyncio.create_task(client.receive())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed and writer.closes == writer.waits == 1
    assert client._reader is client._writer is None and reader._waiter is None


async def test_close_keep_idle_receive_wakes_waiter_and_closes_once(monkeypatch):
    client, reader, writer, _ = await connected(monkeypatch, timeout=0.01, keep_idle=True)
    task = asyncio.create_task(client.receive())
    try:
        await asyncio.sleep(0)
        assert not task.done()
        await client.close()
        with pytest.raises(implementation().GameTransportError) as failure:
            await asyncio.wait_for(task, 0.1)
        assert_safe(failure.value, "GAME_TRANSPORT_CLOSED")
        assert client.closed and writer.closes == writer.waits == 1
        assert client._reader is client._writer is None and reader._waiter is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("keep_idle", [1, None, SECRET], ids=["integer", "none", "string"])
def test_keep_idle_requires_an_explicit_boolean(keep_idle):
    with pytest.raises(implementation().GameTransportError) as failure:
        implementation().GameTransport(keep_idle=keep_idle)
    assert_safe(failure.value, "GAME_TRANSPORT_INVALID_IDLE_MODE")
