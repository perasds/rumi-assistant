"""Redbag/order coexistence through real codecs and synthetic transports only."""

import asyncio
from itertools import count
from types import SimpleNamespace

import pytest

from rumi_market.game.account_state import GoldItemState
from rumi_market.game.client import GameClient
from rumi_market.game.inventory_state import InventoryState
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.game.order_operations import GameOrderOperations
from rumi_market.game.order_state import OrderState
from rumi_market.game.redbag_codec import RedbagProtoCodec
from rumi_market.game.redbag_operations import RedbagOperations
from rumi_market.game.redbag_state import RedbagState
from rumi_market.order_models import OrderSettings
from rumi_market.order_service import OrderRun, order_revision
from rumi_market.redbag_models import RedbagCandidate
from test_game_account_state import changes, full, item
from test_game_client import MemoryTransport
from test_order_service import setup as order_service_setup
from test_order_state import notice as order_notice
from test_redbag_operations import Socket


@pytest.fixture
async def peer():
    transport = MemoryTransport()
    gold, inventory = GoldItemState(True), InventoryState(True)
    orders, redbag_state = OrderState(True), RedbagState(True)
    order_codec, redbag_codec = OrderProtoCodec(), RedbagProtoCodec()

    def observe(response):
        for state in (gold, inventory, orders, redbag_state):
            state.observe(response)

    client = GameClient(transport, timeout=1,
        notification_commands=frozenset({2941, 2942, 3301}), response_observer=observe)
    socket = Socket(redbag_codec)

    async def connect(*args, **kwargs):
        return socket

    redbags = RedbagOperations(client, redbag_state,
        chat_url="wss://chat.bilibiligame.net/ws", role_id="1", server_time=lambda: 1000,
        zone_id=7, nation_code="CN", language=1, timeout=1, connect_factory=connect)

    def snapshot():
        return {"role_key": (7, 1), "server_time_seconds": 1000,
            "orders": orders.snapshot(), "inventory": inventory.snapshot(),
            "home": {"valid": True, "sequence": 1, "architectures": [], "workers": []}}

    async def order_snapshot():
        return snapshot()

    tasks = []

    def run(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    sequences = count(1)

    async def notify(response):
        transport.respond(response.command, 0, response.data, notify=next(sequences))
        # Receipt is a barrier: the sole receive loop has also projected this update.
        await client.wait_notification(response.command)

    try:
        await client.connect()
        await notify(full(item(1000), item(30, item_id=10004),
                          item(5, item_id=101), item(0, item_id=102)))
        await notify(order_notice(3301))
        started = run(redbags.start())
        auth = await transport.next_request()
        assert auth.ReqCmdId == 23938
        transport.respond(23939, auth.ReqCmdSeq,
            redbag_codec.encode(23939, params={"auth": "synthetic-auth"}))
        await started
        yield SimpleNamespace(transport=transport, client=client, socket=socket,
            gold=gold, inventory=inventory, snapshot=snapshot, order_snapshot=order_snapshot,
            order_codec=order_codec, redbag_codec=redbag_codec, redbags=redbags,
            order_operations=GameOrderOperations(client, snapshot, lambda: True),
            candidate=RedbagCandidate("redbag-A", "2", 1100), run=run, notify=notify)
    finally:
        await redbags.stop()
        await client.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_redbag_stamina_notification_cannot_confirm_order_material_settlement(peer):
    before = peer.snapshot()
    gold_revision = peer.gold.gold_revision
    order = peer.run(peer.order_operations.apply({"action": "complete", "slot_id": "1"},
        expected_revision=order_revision(before), stop_requested=lambda: False))
    order_request = await peer.transport.next_request()
    assert order_request.ReqCmdId == 3302
    claim = peer.run(peer.redbags.claim(peer.candidate, stop_requested=lambda: False))
    claim_request = await peer.transport.next_request()
    assert claim_request.ReqCmdId == 4011
    peer.transport.respond(3303, order_request.ReqCmdSeq,
        peer.order_codec.encode(3303, reward=[{"item_id": 102, "count": 4}]))
    peer.transport.respond(4012, claim_request.ReqCmdSeq,
        peer.redbag_codec.encode(4012, id="redbag-A",
            reward_list=[{"item_id": 10004, "count": 18}]))
    assert (await order)["status"] == "applied"
    assert (await claim).stamina == 18
    await peer.notify(changes((3, item(48, item_id=10004))))
    await peer.notify(order_notice(3301, slots=[]))
    assert peer.inventory.snapshot()["items"] == {
        "10001": 1000, "10004": 48, "101": 5, "102": 0}
    assert (peer.gold.gold_balance, peer.gold.gold_revision) == (1000, gold_revision)

    service, session, _ = order_service_setup(SimpleNamespace(order_snapshot=peer.order_snapshot))
    run = OrderRun((7, 1), "synthetic-redbag-settlement", "", OrderSettings(), 2000)
    original = before["orders"]["orders"][0]

    async def settled():
        return await service._settle(session, before, original["materials"],
            original["rewards"], run, require_rewards=True)

    # The slot changed and bag sequence advanced, but only stamina has arrived.
    assert await settled() is False
    await peer.notify(changes((3, item(2, item_id=101))))
    assert await settled() is False
    await peer.notify(changes((3, item(4, item_id=102))))
    assert await settled() is True
    assert (peer.gold.gold_balance, peer.gold.gold_revision) == (1000, gold_revision)


async def test_shared_main_client_requires_command_and_sequence_for_each_receipt(peer):
    claim = peer.run(peer.redbags.claim(peer.candidate, stop_requested=lambda: False))
    claim_request = await peer.transport.next_request()
    order = peer.run(peer.order_operations.apply({"action": "complete", "slot_id": "1"},
        expected_revision=order_revision(peer.snapshot()), stop_requested=lambda: False))
    order_request = await peer.transport.next_request()
    assert (claim_request.ReqCmdId, order_request.ReqCmdId) == (4011, 3302)
    assert claim_request.ReqCmdSeq != order_request.ReqCmdSeq
    claim_ack = peer.redbag_codec.encode(4012, id="redbag-A",
        reward_list=[{"item_id": 10004, "count": 18}])
    order_ack = peer.order_codec.encode(3303, reward=[{"item_id": 102, "count": 4}])

    peer.transport.respond(4012, order_request.ReqCmdSeq, claim_ack)
    peer.transport.respond(3303, claim_request.ReqCmdSeq, order_ack)
    await peer.notify(changes((3, item(48, item_id=10004))))
    assert not claim.done() and not order.done() and peer.client.pending_count == 2
    assert peer.gold.gold_balance == 1000

    peer.transport.respond(4012, claim_request.ReqCmdSeq, claim_ack)
    receipt = await claim
    assert (receipt.outcome, receipt.stamina) == ("claimed", 18)
    assert not order.done() and peer.client.pending_count == 1
    peer.transport.respond(3303, order_request.ReqCmdSeq, order_ack)
    assert await order == {"status": "applied", "rewards": [{"item_id": "102", "count": 4}]}
    assert peer.client.pending_count == 0 and peer.transport.connections == 1


async def test_normal_redbag_stop_preserves_in_flight_claim_and_order_on_main_client(peer):
    claim = peer.run(peer.redbags.claim(peer.candidate, stop_requested=lambda: False))
    claim_request = await peer.transport.next_request()
    order = peer.run(peer.order_operations.apply({"action": "complete", "slot_id": "1"},
        expected_revision=order_revision(peer.snapshot()), stop_requested=lambda: False))
    order_request = await peer.transport.next_request()
    assert (claim_request.ReqCmdId, order_request.ReqCmdId) == (4011, 3302)

    await peer.redbags.stop()
    assert peer.socket.closed and not peer.client.closed and not peer.transport.closed
    assert not claim.done() and not order.done() and peer.client.pending_count == 2
    peer.transport.respond(3303, order_request.ReqCmdSeq,
        peer.order_codec.encode(3303, reward=[{"item_id": 102, "count": 4}]))
    assert (await order)["status"] == "applied"
    peer.transport.respond(4012, claim_request.ReqCmdSeq,
        peer.redbag_codec.encode(4012, id="redbag-A",
            reward_list=[{"item_id": 10004, "count": 18}]))
    receipt = await claim
    assert (receipt.outcome, receipt.stamina) == ("claimed", 18)
    assert peer.client.pending_count == 0
    assert peer.transport.connections == 1 and peer.transport.closes == 0
