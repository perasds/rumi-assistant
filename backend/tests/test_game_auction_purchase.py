"""Synthetic purchase wire tests; no game connection or account data."""

import asyncio
from dataclasses import FrozenInstanceError
import importlib
from types import SimpleNamespace

import pytest

from rumi_market.game.client import GameClient, GameClientError
from rumi_market.game.proto import GameProtoCodec


PRIVATE = "synthetic-private-listing"
REQ = "SNKProto.Cmd_Market_Auction_Buy_Req"
ACK = "SNKProto.Cmd_Market_Auction_Buy_Ack"


def implementation():
    return importlib.import_module("rumi_market.game.auction_purchase")


class FakeClient:
    def __init__(self, *, response=None, error=None):
        self.response, self.error = response, error
        self.calls = []
        self.keyword_calls = []

    async def request(self, command, ack, data, **kwargs):
        self.calls.append((command, ack, data))
        self.keyword_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def ack_response(fields=None, *, code=0, data=None, command=5320):
    if data is None:
        data = GameProtoCodec().encode(ACK, fields if fields is not None else {
            "uid": PRIVATE, "lumi_id": 701, "lumi_uid": 42})
    return SimpleNamespace(code=code, data=data, command=command)


def assert_safe(error, code):
    assert error.code == str(error) == code
    assert error.__context__ is None and error.__cause__ is None
    assert PRIVATE not in repr(error)


async def test_success_sends_one_native_request_and_returns_frozen_private_receipt():
    module = implementation()
    client = FakeClient(response=ack_response())
    receipt = await module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701)
    assert len(client.calls) == 1
    assert client.keyword_calls == [{}]
    command, ack, payload = client.calls[0]
    assert (command, ack) == (5319, 5320)
    request = GameProtoCodec().decode(REQ, payload).message
    assert {field.name for field, _ in request.ListFields()} == {"uid", "sell_role_id", "price"}
    assert (request.uid, request.sell_role_id, request.price) == (PRIVATE, 77, 12345)
    assert isinstance(receipt, module.AuctionPurchaseReceipt)
    assert (receipt.uid, receipt.lumi_id, receipt.lumi_uid, receipt.price) == (PRIVATE, 701, 42, 12345)
    assert PRIVATE not in repr(receipt)
    with pytest.raises(FrozenInstanceError):
        receipt.price = 10


@pytest.mark.parametrize("changes", [
    {"uid": value} for value in ("", " ", None, b"uid", True, 1)
] + [
    {"seller_role_id": value} for value in (True, 0, -1, 1 << 64, "77", 7.0)
] + [
    {"price": value} for value in (True, -1, 1 << 64, "12345", 7.0)
] + [
    {"lumi_id": value} for value in (True, 0, -1, 1 << 31, "701", 7.0)
])
async def test_invalid_arguments_fail_before_sending(changes):
    module = implementation()
    client = FakeClient()
    arguments = {"uid": PRIVATE, "seller_role_id": 77, "price": 12345, "lumi_id": 701}
    arguments.update(changes)
    with pytest.raises(module.AuctionPurchaseError) as caught:
        await module.GameAuctionPurchase(client).buy(**arguments)
    assert_safe(caught.value, "PURCHASE_INVALID")
    assert client.calls == []


@pytest.mark.parametrize("price", [0, (1 << 64) - 1])
async def test_unsigned_wire_boundaries_are_preserved(price):
    module = implementation()
    client = FakeClient(response=ack_response({"uid": PRIVATE, "lumi_id": (1 << 31) - 1,
                                              "lumi_uid": (1 << 64) - 1}))
    receipt = await module.GameAuctionPurchase(client).buy(
        PRIVATE, (1 << 64) - 1, price, lumi_id=(1 << 31) - 1)
    assert (receipt.price, receipt.lumi_uid) == (price, (1 << 64) - 1)


@pytest.mark.parametrize("fields", [
    {}, {"lumi_id": 701, "lumi_uid": 42},
    {"uid": "other", "lumi_id": 701, "lumi_uid": 42},
    {"uid": ""},
    {"uid": PRIVATE, "lumi_id": 702, "lumi_uid": 42},
    {"uid": PRIVATE, "lumi_id": -1},
    {"uid": PRIVATE, "lumi_id": -(1 << 31)},
])
async def test_missing_or_mismatched_ack_is_unknown_and_never_retried(fields):
    module = implementation()
    client = FakeClient(response=ack_response(fields))
    with pytest.raises(module.AuctionPurchaseError) as caught:
        await module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701)
    assert_safe(caught.value, "PURCHASE_OUTCOME_UNKNOWN")
    assert len(client.calls) == 1


@pytest.mark.parametrize(("fields", "obtained_uid"), [
    ({"uid": PRIVATE}, None),
    ({"uid": PRIVATE, "lumi_id": 0}, None),
    ({"uid": PRIVATE, "lumi_uid": 0}, None),
    ({"uid": PRIVATE, "lumi_id": 0, "lumi_uid": 0}, None),
    ({"uid": PRIVATE, "lumi_id": 701}, None),
    ({"uid": PRIVATE, "lumi_id": 701, "lumi_uid": 0}, None),
    ({"uid": PRIVATE, "lumi_uid": 42}, 42),
    ({"uid": PRIVATE, "lumi_id": 0, "lumi_uid": 42}, 42),
])
async def test_matching_order_uid_confirms_purchase_without_optional_pet_fields(fields, obtained_uid):
    module = implementation()
    client = FakeClient(response=ack_response(fields))
    receipt = await module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701)
    assert (receipt.uid, receipt.lumi_id, receipt.lumi_uid, receipt.price) == (PRIVATE, 701, obtained_uid, 12345)
    assert len(client.calls) == 1 and PRIVATE not in repr(receipt)


@pytest.mark.parametrize(("client", "reason"), [
    (FakeClient(error=GameClientError("GAME_REQUEST_TIMEOUT")), "REQUEST_TIMEOUT"),
    (FakeClient(error=TimeoutError(PRIVATE)), "REQUEST_TIMEOUT"),
    (FakeClient(error=GameClientError("GAME_CONNECTION_FAILED")), "CONNECTION_FAILED"),
    (FakeClient(error=GameClientError("GAME_TRANSPORT_CLOSED")), "CONNECTION_FAILED"),
    (FakeClient(error=GameClientError(PRIVATE)), "REQUEST_FAILED"),
    (FakeClient(error=GameClientError("GAME_REQUEST_REJECTED", response_code=PRIVATE)), "REQUEST_FAILED"),
    (FakeClient(error=RuntimeError(PRIVATE)), "REQUEST_FAILED"),
    (FakeClient(error=asyncio.CancelledError(PRIVATE)), "REQUEST_CANCELLED"),
    (FakeClient(response=ack_response({})), "ACK_MISSING_UID"),
    (FakeClient(response=ack_response({"uid": "synthetic-private-other-order"})), "ACK_UID_MISMATCH"),
    (FakeClient(response=ack_response({"uid": PRIVATE, "lumi_id": -1})), "ACK_PET_MISMATCH"),
    (FakeClient(response=ack_response(data=b"\xff")), "ACK_DECODE_FAILED"),
    (FakeClient(response=ack_response(code=True)), "ACK_INVALID_ENVELOPE"),
])
async def test_unknown_outcome_logs_only_one_fixed_reason_without_private_values(client, reason, caplog):
    module = implementation()
    with caplog.at_level("WARNING", logger=module.__name__):
        with pytest.raises(module.AuctionPurchaseError) as caught:
            await module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701)
    assert_safe(caught.value, "PURCHASE_OUTCOME_UNKNOWN")
    records = [record for record in caplog.records if record.name == module.__name__]
    assert len(records) == 1
    assert records[0].getMessage() == f"Auction purchase outcome unknown ({reason})"
    assert records[0].args == (reason,)
    assert records[0].exc_info is None and records[0].stack_info is None
    assert "synthetic-private" not in caplog.text and len(client.calls) == 1


@pytest.mark.parametrize("response", [
    SimpleNamespace(code=0, data=b"\xff", command=5320),
    SimpleNamespace(code=0, data=PRIVATE, command=5320),
    SimpleNamespace(code=True, data=b"", command=5320),
    SimpleNamespace(code="0", data=b"", command=5320),
    SimpleNamespace(code=1 << 31, data=b"", command=5320),
    SimpleNamespace(code=0, data=b"", command=5322),
    None,
])
async def test_invalid_ack_envelope_or_wire_is_unknown(response):
    module = implementation()
    client = FakeClient(response=response)
    with pytest.raises(module.AuctionPurchaseError) as caught:
        await module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701)
    assert_safe(caught.value, "PURCHASE_OUTCOME_UNKNOWN")
    assert len(client.calls) == 1


@pytest.mark.parametrize("code", [-1, 1204, 2003, (1 << 31) - 1])
@pytest.mark.parametrize("from_client", [False, True])
async def test_explicit_nonzero_business_code_is_rejected(code, from_client):
    module = implementation()
    client = (FakeClient(error=GameClientError("GAME_REQUEST_REJECTED", response_code=code))
              if from_client else FakeClient(response=ack_response(code=code, data=b"\xff")))
    with pytest.raises(module.AuctionPurchaseError) as caught:
        await module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701)
    assert_safe(caught.value, "PURCHASE_REJECTED")
    assert type(caught.value.response_code) is int and caught.value.response_code == code
    assert len(client.calls) == 1


@pytest.mark.parametrize("error", [
    GameClientError("GAME_REQUEST_TIMEOUT"), GameClientError("GAME_CONNECTION_FAILED"),
    GameClientError("GAME_REQUEST_REJECTED"),
    GameClientError("GAME_REQUEST_REJECTED", response_code=True),
    GameClientError("GAME_REQUEST_REJECTED", response_code=0),
    GameClientError("GAME_REQUEST_REJECTED", response_code="2003"),
    GameClientError("GAME_REQUEST_REJECTED", response_code=1 << 31),
    RuntimeError(PRIVATE), asyncio.CancelledError(PRIVATE), TimeoutError(PRIVATE),
])
async def test_request_failure_or_cancel_is_unknown_without_original_context(error):
    module = implementation()
    client = FakeClient(error=error)
    with pytest.raises(module.AuctionPurchaseError) as caught:
        await module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701)
    assert_safe(caught.value, "PURCHASE_OUTCOME_UNKNOWN")
    assert caught.value.response_code is None and len(client.calls) == 1


async def test_real_client_correlates_command_and_sequence_without_repeating_purchase():
    module = implementation()
    codec = GameProtoCodec()

    class MemoryTransport:
        def __init__(self):
            self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()

        async def connect(self):
            pass

        async def send(self, payload):
            self.outgoing.put_nowait(payload)

        async def receive(self):
            return await self.incoming.get()

        async def close(self):
            pass

    transport = MemoryTransport()
    client = GameClient(transport)
    await client.connect()
    try:
        task = asyncio.create_task(module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701))
        request = codec.decode("SNKProto.ReqProto", await asyncio.wait_for(transport.outgoing.get(), 1)).message
        assert request.ReqCmdId == 5319
        for command, sequence in ((5322, request.ReqCmdSeq), (5320, request.ReqCmdSeq + 1),
                                  (5320, request.ReqCmdSeq)):
            transport.incoming.put_nowait(codec.encode("SNKProto.RspProto", {
                "RspCmdId": command, "RspCmdSeq": sequence, "RspCode": 0,
                "RspData": codec.encode(ACK, {"uid": PRIVATE, "lumi_id": 701, "lumi_uid": 42})}))
        assert (await asyncio.wait_for(task, 1)).lumi_uid == 42
        assert transport.outgoing.empty() and client.pending_count == 0
    finally:
        await client.close()


async def test_task_cancellation_after_request_is_unknown_and_sends_only_once():
    module = implementation()
    started = asyncio.Event()

    class WaitingClient(FakeClient):
        async def request(self, *args):
            self.calls.append(args)
            started.set()
            await asyncio.Future()

    client = WaitingClient()
    task = asyncio.create_task(module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701))
    await started.wait()
    task.cancel(PRIVATE)
    with pytest.raises(module.AuctionPurchaseError) as caught:
        await task
    assert_safe(caught.value, "PURCHASE_OUTCOME_UNKNOWN")
    assert len(client.calls) == 1


async def test_before_send_predicate_is_forwarded_without_early_evaluation():
    module = implementation()
    client = FakeClient(response=ack_response())

    def predicate():
        pytest.fail("Only GameClient may evaluate the predicate inside its send lock")

    receipt = await module.GameAuctionPurchase(client).buy(
        PRIVATE, 77, 12345, lumi_id=701, before_send=predicate)
    assert receipt.lumi_uid == 42
    assert client.keyword_calls == [{"before_send": predicate}]


async def test_explicit_not_sent_is_distinct_from_rejected_or_unknown():
    module = implementation()
    client = FakeClient(error=GameClientError("GAME_REQUEST_NOT_SENT", response_code=PRIVATE))
    with pytest.raises(module.AuctionPurchaseError) as caught:
        await module.GameAuctionPurchase(client).buy(PRIVATE, 77, 12345, lumi_id=701)
    assert_safe(caught.value, "PURCHASE_NOT_SENT")
    assert caught.value.response_code is None and len(client.calls) == 1
