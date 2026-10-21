"""One claim attempt, exact server errors, and server-only reconciliation."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest

from rumi_market.errors import ApiError
from rumi_market.game.client import GameClientError


def implementation():
    try:
        return importlib.import_module("rumi_market.game.redbag_operations")
    except ModuleNotFoundError:
        pytest.fail("The redbag operations are not implemented")


class Socket:
    def __init__(self, codec):
        self.codec, self.queue, self.sent = codec, asyncio.Queue(), []
        self.closed = False

    async def send(self, payload):
        command, data = self.codec.decode_ws(payload)
        self.sent.append(command)
        response = {23920: 23921, 23903: 23904}.get(command)
        if response:
            fields = {"code": 0} if response == 23921 else {"error_code": 0}
            await self.queue.put(self.codec.encode_ws(response, self.codec.encode(response, **fields)))

    async def recv(self):
        result = await self.queue.get()
        if result is None:
            raise EOFError("synthetic-private-upstream")
        return result

    async def close(self):
        self.closed = True
        self.queue.put_nowait(None)


class Client:
    closed = False

    def __init__(self, codec):
        self.codec, self.calls = codec, []
        self.behavior = "ok"
        self.query_roles = {}
        self.before = None
        self.block = None

    async def request(self, command, ack, payload, *, before_send=None):
        if self.before:
            self.before()
        if before_send is not None and not before_send():
            raise GameClientError("GAME_REQUEST_NOT_SENT")
        self.calls.append(command)
        if command == 23938:
            data = self.codec.encode(23939, params={"auth": "synthetic-secret"})
        elif command == 4009:
            data = self.codec.encode(4010, share={"id": 9, "roles": self.query_roles})
        else:
            if self.block:
                self.block[0].set()
                await self.block[1].wait()
            if self.behavior == "timeout":
                raise GameClientError("GAME_REQUEST_TIMEOUT")
            if isinstance(self.behavior, int):
                raise GameClientError("GAME_REQUEST_REJECTED", response_code=self.behavior)
            fields = {"id": "redbag-A", "reward_list": [{"item_id": 10004, "count": 18}]}
            if self.behavior == "wrong_id": fields["id"] = "redbag-B"
            if self.behavior == "empty": fields["reward_list"] = []
            data = self.codec.encode(4012, **fields)
        return SimpleNamespace(command=ack, code=0, data=data)


async def setup(*, chat_url="wss://chat.bilibiligame.net/ws"):
    module = implementation()
    codec = module.RedbagProtoCodec()
    state = module.RedbagState(verified_version=True)
    client = Client(codec)
    socket = Socket(codec)
    async def connect(*args, **kwargs): return socket
    operations = module.RedbagOperations(client, state, chat_url=chat_url,
        role_id="1", server_time=lambda: 1000, zone_id=1, nation_code="CN", language=1,
        timeout=.1, connect_factory=connect)
    await operations.start()
    candidate = module.RedbagCandidate("redbag-A", "2", 1100)
    return module, operations, client, socket, candidate


@pytest.mark.asyncio
async def test_claim_once_uses_primary_connection_and_exact_ack_rewards():
    _, operations, client, socket, candidate = await setup()
    try:
        result = await operations.claim(candidate, stop_requested=lambda: False)
        assert result.outcome == "claimed" and result.stamina == 18
        assert client.calls == [23938, 4011]
        assert socket.sent[:2] == [23920, 23903]
    finally:
        await operations.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("error,outcome", [(2103, "missing"), (2104, "expired"),
    (2106, "self"), (2107, "already_claimed"), (2109, "full")])
async def test_known_server_rejections_are_definite_outcomes(error, outcome):
    _, operations, client, _, candidate = await setup()
    client.behavior = error
    try:
        assert (await operations.claim(candidate, stop_requested=lambda: False)).outcome == outcome
        assert client.calls.count(4011) == 1
    finally: await operations.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["timeout", "wrong_id", "empty"])
async def test_sent_claim_without_valid_receipt_is_uncertain_and_never_retried(behavior):
    _, operations, client, _, candidate = await setup()
    client.behavior = behavior
    try:
        with pytest.raises(ApiError) as failure:
            await operations.claim(candidate, stop_requested=lambda: False)
        assert failure.value.code == "REDBAG_UNCERTAIN"
        assert client.calls.count(4011) == 1
    finally: await operations.stop()


@pytest.mark.asyncio
async def test_reconcile_requires_own_server_awards_and_stop_does_not_close_primary():
    _, operations, client, socket, candidate = await setup()
    try:
        assert await operations.reconcile(candidate) is None
        client.query_roles = {1: {"awards": [{"item_id": 10004, "count": 28}]}}
        receipt = await operations.reconcile(candidate)
        assert receipt.outcome == "claimed" and receipt.stamina == 28
        assert client.calls.count(4011) == 0
    finally: await operations.stop()
    assert socket.closed and not client.closed


@pytest.mark.asyncio
async def test_stop_guard_at_actual_primary_send_prevents_claim():
    _, operations, client, _, candidate = await setup()
    stopped = [False]
    client.before = lambda: stopped.__setitem__(0, True)
    try:
        assert await operations.claim(candidate, stop_requested=lambda: stopped[0]) is None
        assert 4011 not in client.calls
    finally: await operations.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code", [(2108, "REDBAG_DAILY_LIMIT"),
    (2113, "REDBAG_CONDITION_NOT_MET"), (1017, "REDBAG_RATE_LIMITED"), (-5, "GAME_SESSION_EXPIRED")])
async def test_terminal_rejections_are_sanitized_and_never_retried(error, code):
    _, operations, client, _, candidate = await setup()
    client.behavior = error
    try:
        with pytest.raises(ApiError) as failure:
            await operations.claim(candidate, stop_requested=lambda: False)
        assert failure.value.code == code and client.calls.count(4011) == 1
    finally: await operations.stop()


@pytest.mark.asyncio
async def test_normal_stop_waits_for_existing_claim_receipt_without_cancelling_main_request():
    _, operations, client, socket, candidate = await setup()
    entered, release = asyncio.Event(), asyncio.Event()
    client.block = (entered, release)
    task = asyncio.create_task(operations.claim(candidate, stop_requested=lambda: False))
    await entered.wait()
    await operations.stop()
    assert socket.closed and not task.done() and not client.closed
    release.set()
    assert (await task).outcome == "claimed"
