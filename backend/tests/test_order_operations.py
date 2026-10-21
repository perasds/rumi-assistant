"""Single-attempt order operations with fresh-state send guards."""

import asyncio
from copy import deepcopy
import importlib

import pytest

from rumi_market.game.client import GameClientError
from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.order_service import order_revision


def snapshot():
    return {"role_key": (1, 2), "server_time_seconds": 1000,
            "orders": {"valid": True, "sequence": 1, "orders": [{
                "slot_id": "1", "is_unlock": True, "order_type": 1,
                "materials": [{"item_id": "101", "count": 3}], "rewards": []}]},
            "inventory": {"valid": True, "sequence": 1, "items": {"101": 5}},
            "home": {"valid": True, "sequence": 1, "architectures": [{
                "uid": 123, "id": 10101, "work_old": {"recipe_id": 1}, "cur_recipe": None,
                "is_hide": False, "is_need_cut_ribbon": False, "level_up_complete_time": "0",
                "has_farm_data": False, "store_items": [{"item_id": "101", "count": 2}]}]}}


class Client:
    closed = False

    def __init__(self, raw, behavior="ack"):
        self.raw = raw
        self.behavior = behavior
        self.calls = []
        self.before = None
        self.after = None

    async def request(self, command, response_command, payload, *, before_send):
        if self.before:
            self.before()
        if not before_send():
            raise GameClientError("GAME_REQUEST_NOT_SENT")
        codec = OrderProtoCodec()
        request = codec.decode(command, payload)
        fields = {field.name: value for field, value in request.ListFields()}
        self.calls.append((command, response_command, fields))
        if command == 3029:
            return ResponseEnvelope(response_command, 1, 0, 0, 0, codec.encode(3030))
        if self.after:
            await self.after()
        if self.behavior == "timeout":
            raise GameClientError("GAME_REQUEST_TIMEOUT")
        if self.behavior == "cancel":
            raise asyncio.CancelledError("synthetic-private-cancellation")
        if self.behavior == "rejected":
            raise GameClientError("GAME_REQUEST_REJECTED", response_code=7)
        ack = ({"reward": [{"item_id": 20001, "count": 2**63 - 1}]} if command == 3302 else {
            "archi_uid": fields["archi_uid"], "archi_id": fields["archi_id"],
            "item_show_list": [{"item_id": 101, "count": 2}]})
        if self.behavior == "wrong_identity":
            ack["archi_uid"] += 1
        if self.behavior == "empty":
            ack = {}
        if self.behavior == "invalid_reward":
            ack = {"reward": [{"item_id": 20001}]}
        data = b"\xff" if self.behavior == "malformed" else codec.encode(response_command, **ack)
        return ResponseEnvelope(response_command, 1, 0, 0, 0, data)


def setup(behavior="ack"):
    try:
        module = importlib.import_module("rumi_market.game.order_operations")
    except ModuleNotFoundError:
        pytest.fail("Order operations are not implemented")
    raw = snapshot()
    client = Client(raw, behavior)
    can_write = [True]
    stop = asyncio.Event()
    operations = module.GameOrderOperations(client, lambda: deepcopy(raw), lambda: can_write[0])
    return operations, client, raw, stop, can_write


@pytest.mark.asyncio
async def test_complete_uses_exact_slot_request_and_returns_confirmed_rewards():
    operations, client, raw, stop, _ = setup()
    result = await operations.apply({"action": "complete", "slot_id": "1"},
                                    expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result == {"status": "applied", "rewards": [{"item_id": "20001", "count": 2**63 - 1}]}
    assert client.calls == [(3302, 3303, {"slot_id": 1})]


@pytest.mark.asyncio
async def test_claim_uses_native_architecture_identity_and_checks_ack_identity():
    operations, client, raw, stop, _ = setup()
    result = await operations.apply({"action": "claim", "building_uid": "123", "building_id": "10101"},
                                    expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result == {"status": "applied", "rewards": [{"item_id": "101", "count": 2}]}
    assert client.calls == [(3025, 3026, {"archi_uid": 123, "archi_id": 10101})]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["revision", "stop", "write", "closed"])
async def test_before_send_rechecks_revision_stop_permission_and_connection(mutation):
    operations, client, raw, stop, can_write = setup()
    revision = order_revision(raw)
    def before():
        if mutation == "revision": raw["inventory"]["sequence"] += 1
        elif mutation == "stop": stop.set()
        elif mutation == "write": can_write[0] = False
        else: client.closed = True
    client.before = before
    result = await operations.apply({"action": "complete", "slot_id": "1"},
                                    expected_revision=revision, stop_requested=stop.is_set)
    assert result["status"] == "rejected" and not client.calls and not operations.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"action": "refresh", "slot_id": "1"}, {"action": "complete", "slot_id": 1},
    {"action": "complete", "slot_id": "01"}, {"action": "complete", "slot_id": "-1"},
    {"action": "complete", "slot_id": "1", "extra": "synthetic-private-value"},
    {"action": "claim", "building_uid": "2147483648", "building_id": "10101"},
    {"action": "claim", "building_uid": "123", "building_id": 10101}, {}, None,
])
async def test_invalid_operations_and_unverified_refresh_never_send(bad):
    operations, client, raw, stop, _ = setup()
    result = await operations.apply(bad, expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result == {"status": "rejected", "rewards": []} and not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["locked", "unknown_type", "empty_materials", "insufficient", "missing_inventory", "duplicate_slot"])
async def test_complete_requires_a_known_unlocked_funded_order(mutation):
    operations, client, raw, stop, _ = setup()
    order = raw["orders"]["orders"][0]
    if mutation == "locked": order["is_unlock"] = False
    elif mutation == "unknown_type": order["order_type"] = None
    elif mutation == "empty_materials": order["materials"] = []
    elif mutation == "insufficient": order["materials"] *= 2
    elif mutation == "missing_inventory": raw["inventory"]["valid"] = False
    else: raw["orders"]["orders"].append(deepcopy(order))
    result = await operations.apply({"action": "complete", "slot_id": "1"},
                                    expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result["status"] == "rejected" and not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["no_store", "zero_store", "no_work", "other_branch", "wrong_id", "duplicate_architecture"])
async def test_claim_requires_positive_ordinary_store_and_exact_building(mutation):
    operations, client, raw, stop, _ = setup()
    row = raw["home"]["architectures"][0]
    if mutation == "no_store": row["store_items"] = None
    elif mutation == "zero_store": row["store_items"][0]["count"] = 0
    elif mutation == "no_work": row["work_old"] = None
    elif mutation == "other_branch": row["has_farm_data"] = True
    elif mutation == "wrong_id": row["id"] = 10201
    else: raw["home"]["architectures"].append(deepcopy(row))
    result = await operations.apply({"action": "claim", "building_uid": "123", "building_id": "10101"},
                                    expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result["status"] == "rejected" and not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["timeout", "malformed", "invalid_reward"])
async def test_unknown_sent_complete_result_blocks_retries(behavior):
    operations, client, raw, stop, _ = setup(behavior)
    operation = {"action": "complete", "slot_id": "1"}
    kwargs = {"expected_revision": order_revision(raw), "stop_requested": stop.is_set}
    assert (await operations.apply(operation, **kwargs))["status"] == "uncertain"
    assert operations.blocked
    assert (await operations.apply(operation, **kwargs))["status"] == "uncertain"
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["wrong_identity", "empty", "malformed"])
async def test_claim_ack_must_confirm_identity_or_block_retries(behavior):
    operations, client, raw, stop, _ = setup(behavior)
    result = await operations.apply({"action": "claim", "building_uid": "123", "building_id": "10101"},
                                    expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result["status"] == "uncertain" and operations.blocked


@pytest.mark.asyncio
async def test_explicit_server_rejection_is_safe_and_does_not_block():
    operations, client, raw, stop, _ = setup("rejected")
    result = await operations.apply({"action": "complete", "slot_id": "1"},
                                    expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert result["status"] == "rejected" and not operations.blocked


@pytest.mark.asyncio
async def test_cancellation_after_send_marks_unknown_and_hides_exception_text():
    operations, client, raw, stop, _ = setup("cancel")
    with pytest.raises(asyncio.CancelledError) as captured:
        await operations.apply({"action": "complete", "slot_id": "1"},
                               expected_revision=order_revision(raw), stop_requested=stop.is_set)
    assert operations.blocked and "synthetic-private" not in str(captured.value)


@pytest.mark.asyncio
async def test_same_instance_serializes_writes_and_cannot_replay_a_confirmed_revision():
    operations, client, raw, stop, _ = setup()
    entered, release = asyncio.Event(), asyncio.Event()
    async def after():
        entered.set()
        await release.wait()
    client.after = after
    operation = {"action": "complete", "slot_id": "1"}
    kwargs = {"expected_revision": order_revision(raw), "stop_requested": stop.is_set}
    first = asyncio.create_task(operations.apply(operation, **kwargs))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(operations.apply(operation, **kwargs))
    await asyncio.sleep(0)
    assert len(client.calls) == 1
    release.set()
    assert (await first)["status"] == "applied"
    assert (await second)["status"] == "rejected"
    assert len(client.calls) == 1
