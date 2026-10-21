"""Season changes must stop the real native buy chain before transport.send."""
import asyncio
from types import SimpleNamespace

import pytest

from rumi_market.errors import ApiError
from rumi_market.game.auction_execution import GameAuctionExecution
from rumi_market.game.client import GameClient
from rumi_market.game.market_query import GameMarketQuery
from test_game_client import MemoryTransport
from test_provider_purchase import AccountState, CODEC, PRIVATE, row
from test_season_state import activity, notification, ready_state


@pytest.fixture
async def native_execution():
    transport = MemoryTransport()
    client = GameClient(transport, timeout=0.5)
    await client.connect()
    runtime = SimpleNamespace(client=client, market=GameMarketQuery(client),
                              account_state=AccountState(), server_time_seconds=150)
    context = SimpleNamespace(transport=transport, client=client, runtime=runtime,
                              season=ready_state(), target=1, checks=[], tasks=[])
    try:
        yield context
    finally:
        if client._send_lock.locked():
            client._send_lock.release()
        await client.close()
        for task in context.tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*context.tasks, return_exceptions=True), 1)


def start_purchase(context):
    def stopped():
        snapshot = context.season.snapshot(context.runtime.server_time_seconds)
        stop = not (snapshot["valid"] is True
                    and snapshot["current_season_id"] == context.target)
        context.checks.append((snapshot, stop))
        return stop

    task = asyncio.create_task(GameAuctionExecution(context.runtime, 123).execute(
        row(), max_price_gold=200, min_balance_gold=50, stop_requested=stopped))
    context.tasks.append(task)
    return task


def respond_watch(context, request):
    # This item remains for sale after the season ends, so item expiry cannot
    # explain a missing buy request in the season-expiration cases.
    detail = {"uid": PRIVATE, "lumi_id": 701, "fixed_price": 100,
              "sell_role_id": 77, "is_sold_out": False, "up_time": 100,
              "expire_seconds": 2000, "extern_seconds": 0}
    payload = CODEC.encode("SNKProto.Cmd_Market_Auction_Watch_Start_Ack",
                           {"uid": PRIVATE, "lumi_info": detail})
    context.transport.respond(5312, request.ReqCmdSeq, payload)


def invalidate_season(context, mutation):
    if mutation == "expired":
        context.runtime.server_time_seconds = 201
    elif mutation == "clock_unavailable":
        context.runtime.server_time_seconds = None
    elif mutation == "different_target":
        context.season.observe(notification(8002, [activity(system=700020)]))
    else:
        assert mutation == "invalid_notification"
        context.season.observe(notification(8002, payload=b"\xff"))


async def assert_no_buy_and_finish_watch(context, task, *, detail_clock_invalid=False):
    # Read the actual native transport queue. A bypassed before_send would put
    # 5319 here and fail immediately; fake provider purchase counters are unused.
    request = await context.transport.next_request()
    assert request.ReqCmdId == 5313
    stop = CODEC.decode("SNKProto.Cmd_Market_Auction_Watch_Stop_Req", request.ReqData).message
    assert stop.uid == PRIVATE
    context.transport.respond(5314, request.ReqCmdSeq)
    if detail_clock_invalid:
        with pytest.raises(ApiError) as caught:
            await asyncio.wait_for(task, 1)
        assert caught.value.code == "PURCHASE_DETAIL_UNAVAILABLE"
    else:
        assert await asyncio.wait_for(task, 1) is None
        assert context.checks[-1][1] is True
    assert context.transport.outgoing.empty() and context.client.pending_count == 0
    assert not context.client.closed
    assert not context.runtime.market._detail_lock.locked()
    assert not context.runtime.account_state.invalidated
    assert context.runtime.account_state.wait_calls == []


@pytest.mark.parametrize("mutation", [
    "expired", "clock_unavailable", "different_target", "invalid_notification",
])
async def test_season_change_while_waiting_for_watch_never_sends_native_buy(native_execution, mutation):
    context = native_execution
    task = start_purchase(context)
    watch = await context.transport.next_request()
    assert watch.ReqCmdId == 5311 and context.client.pending_count == 1
    assert context.checks[-1][1] is False
    invalidate_season(context, mutation)
    respond_watch(context, watch)
    await assert_no_buy_and_finish_watch(context, task,
                                        detail_clock_invalid=mutation == "clock_unavailable")


@pytest.mark.parametrize("mutation", [
    "expired", "clock_unavailable", "different_target", "invalid_notification",
])
async def test_season_is_rechecked_inside_native_send_lock_before_buy_bytes(native_execution, mutation):
    context = native_execution
    task = start_purchase(context)
    watch = await context.transport.next_request()
    assert watch.ReqCmdId == 5311
    await context.client._send_lock.acquire()
    respond_watch(context, watch)

    async def buy_waits_for_native_send_lock():
        # At this point Watch was sent already. There is exactly one other
        # operation: the real GameAuctionPurchase buy awaiting GameClient's lock.
        while not context.client._send_lock._waiters:
            assert not task.done()
            await asyncio.sleep(0)

    await asyncio.wait_for(buy_waits_for_native_send_lock(), 1)
    assert len(context.client._send_lock._waiters) == 1
    assert len(context.checks) >= 2 and all(not stopped for _, stopped in context.checks)
    assert context.transport.outgoing.empty() and context.client.pending_count == 0
    invalidate_season(context, mutation)
    context.client._send_lock.release()
    await assert_no_buy_and_finish_watch(context, task)
    assert len(context.checks) >= 3


async def test_valid_season_at_inclusive_end_allows_exactly_one_native_buy(native_execution):
    context = native_execution
    task = start_purchase(context)
    watch = await context.transport.next_request()
    assert watch.ReqCmdId == 5311
    context.runtime.server_time_seconds = 200
    respond_watch(context, watch)
    buy = await context.transport.next_request()
    assert buy.ReqCmdId == 5319
    request = CODEC.decode("SNKProto.Cmd_Market_Auction_Buy_Req", buy.ReqData).message
    assert (request.uid, request.sell_role_id, request.price) == (PRIVATE, 77, 100)
    context.transport.respond(5320, buy.ReqCmdSeq, CODEC.encode(
        "SNKProto.Cmd_Market_Auction_Buy_Ack", {"uid": PRIVATE, "lumi_id": 701, "lumi_uid": 42}))
    stop = await context.transport.next_request()
    assert stop.ReqCmdId == 5313
    context.transport.respond(5314, stop.ReqCmdSeq)
    receipt = await asyncio.wait_for(task, 1)
    assert receipt.price == 100 and receipt.balance_ready
    assert len(context.checks) >= 3 and all(not stopped for _, stopped in context.checks)
    assert context.transport.outgoing.empty() and context.client.pending_count == 0
