"""Provider purchase orchestration with synthetic in-memory game responses."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from rumi_market.errors import ApiError
from rumi_market.game.client import GameClientError
from rumi_market.game.market_query import GameMarketQuery, AuctionCursor
from rumi_market.game.proto import GameProtoCodec
from rumi_market.providers import ProviderListing


PRIVATE = "synthetic-private-order"
CODEC = GameProtoCodec()


class AccountState:
    def __init__(self):
        self.gold_balance, self.gold_revision = 1000, 1
        self.wait_calls, self.invalidated = [], False
        self.wait_result = True

    def invalidate(self):
        self.gold_balance, self.invalidated = None, True

    async def wait_for_gold_balance(self, **options):
        self.wait_calls.append(options)
        if isinstance(self.wait_result, BaseException):
            raise self.wait_result
        if self.wait_result:
            self.gold_balance = options["expected"]
            self.gold_revision += 1
        return self.wait_result


class Client:
    def __init__(self):
        self.sent, self.closed = [], False
        self.detail = dict(uid=PRIVATE, lumi_id=701, fixed_price=100,
                           sell_role_id=77, is_sold_out=False,
                           up_time=900, expire_seconds=200, extern_seconds=0)
        self.start_uid = PRIVATE
        self.buy_error = self.stop_error = None
        self.before_send_change = None
        self.before_send_wait = None
        self.before_send_ready = asyncio.Event()
        self.buy_started = asyncio.Event()
        self.buy_release = None

    async def request(self, command, ack, payload, **options):
        if command == 5319:
            self.before_send_ready.set()
            if self.before_send_wait is not None:
                await self.before_send_wait.wait()
            if self.before_send_change is not None:
                self.before_send_change()
            assert callable(options.get("before_send"))
            if options["before_send"]() is not True:
                raise GameClientError("GAME_REQUEST_NOT_SENT")
        self.sent.append((command, ack, payload))
        if command == 5311:
            body = CODEC.encode("SNKProto.Cmd_Market_Auction_Watch_Start_Ack", {
                "uid": self.start_uid, "lumi_info": self.detail})
        elif command == 5319:
            self.buy_started.set()
            if self.buy_release is not None:
                await self.buy_release.wait()
            if self.buy_error is not None:
                raise self.buy_error
            body = CODEC.encode("SNKProto.Cmd_Market_Auction_Buy_Ack", {
                "uid": PRIVATE, "lumi_id": 701, "lumi_uid": 42})
        elif command == 5313:
            if self.stop_error is not None:
                raise self.stop_error
            body = b""
        elif command == 5325:
            body = CODEC.encode("SNKProto.Cmd_Market_Auction_QueryLumiDetail_Ack", {
                "lumi_list": [{"uid": PRIVATE, "lumi_id": 701, "fixed_price": 100}],
                "course": "synthetic-course"})
        else:
            pytest.fail("Unexpected game command")
        return SimpleNamespace(command=ack, code=0, data=body)

    async def close(self):
        self.closed = True


class Runtime:
    def __init__(self):
        self.client = Client()
        self.market = GameMarketQuery(self.client)
        self.account_state = AccountState()
        self.server_time_seconds = 1000
        self.can_query, self.closed = True, False

    @property
    def gold_balance(self):
        return self.account_state.gold_balance

    async def start(self, *unused):
        return SimpleNamespace(role_id=123, zone_name="synthetic-zone", needs_activation=False,
                               needs_initial_pet_selection=False)

    async def close(self):
        self.closed, self.can_query = True, False
        await self.client.close()


async def connected():
    from rumi_market.game_provider import IndependentGameProvider
    runtime = Runtime()
    provider = IndependentGameProvider(runtime, SimpleNamespace(), login_verified=True)
    await provider.login("synthetic", "synthetic")
    return provider, runtime


def row():
    return ProviderListing(PRIVATE, "color", "701", 90)


async def buy(provider, **options):
    return await provider.purchase_color(row(), max_price_gold=200, min_balance_gold=50,
                                         stop_requested=lambda: False, **options)


def assert_error(error, code):
    assert error.code == str(error) == code
    assert error.status_code == 409
    assert PRIVATE not in str(error.body())
    assert error.__context__ is error.__cause__ is None


async def test_native_watch_buy_stop_order_uses_current_price_and_awaits_balance():
    from rumi_market.providers import ProviderPurchaseReceipt
    provider, runtime = await connected()
    assert provider.gold_balance == 1000
    receipt = await buy(provider)
    assert isinstance(receipt, ProviderPurchaseReceipt)
    assert (receipt.raw_id, receipt.pet_id, receipt.price, receipt.balance_ready) == (PRIVATE, "701", 100, True)
    assert PRIVATE not in repr(receipt)
    with pytest.raises(FrozenInstanceError):
        receipt.price = 1
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5319, 5313]
    request = CODEC.decode("SNKProto.Cmd_Market_Auction_Buy_Req", runtime.client.sent[1][2]).message
    assert (request.uid, request.sell_role_id, request.price) == (PRIVATE, 77, 100)
    assert runtime.account_state.wait_calls == [{"expected": 900, "after_revision": 1, "timeout": 5}]
    assert provider.gold_balance == 900
    assert not runtime.market._detail_lock.locked()


async def test_purchase_pagination_uses_native_independent_cursor_and_window_40():
    provider, runtime = await connected()
    provider._cursors["GUI"] = ("ranked_color", 0, object())
    page = await provider.purchase_page(None)
    assert len(page.items) == 1 and page.items[0] == ProviderListing(PRIVATE, "color", "701", 100)
    assert isinstance(page.next_cursor, AuctionCursor) and page.next_cursor.begin_index == 42
    assert not page.complete and "GUI" in provider._cursors
    await provider.purchase_page(page.next_cursor)
    requests = [CODEC.decode("SNKProto.Cmd_Market_Auction_QueryLumiDetail_Req", payload).message
                for command, _, payload in runtime.client.sent if command == 5325]
    assert [(request.begin_index, request.end_index, request.order_type, request.desc) for request in requests] == [
        (1, 41, 3, False), (42, 82, 3, False)]
    assert "GUI" in provider._cursors and not provider._query_lock.locked()


@pytest.mark.parametrize("field,value", [
    ("uid", "other"), ("lumi_id", 702), ("sell_role_id", 123), ("sell_role_id", 0),
    ("is_sold_out", True), ("fixed_price", 0), ("fixed_price", -1),
    ("fixed_price", 200), ("fixed_price", 201), ("up_time", 800),
    ("extern_seconds", None), ("is_sold_out", None), ("sell_role_id", None),
    ("up_time", None), ("expire_seconds", None),
])
async def test_unavailable_or_unqualified_details_are_skipped_and_watch_stopped(field, value):
    provider, runtime = await connected()
    if value is None:
        runtime.client.detail.pop(field)
    else:
        runtime.client.detail[field] = value
    assert await buy(provider) is None
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5313]
    assert runtime.account_state.wait_calls == []


@pytest.mark.parametrize("balance,code", [(None, "PURCHASE_BALANCE_UNAVAILABLE"), (149, "PURCHASE_BALANCE_LIMIT")])
async def test_balance_must_cover_price_plus_retained_minimum(balance, code):
    provider, runtime = await connected()
    runtime.account_state.gold_balance = balance
    with pytest.raises(ApiError) as caught:
        await buy(provider)
    assert_error(caught.value, code)
    assert not any(command == 5319 for command, _, _ in runtime.client.sent)


async def test_retained_minimum_equality_is_allowed():
    provider, runtime = await connected()
    runtime.account_state.gold_balance = 150
    assert (await buy(provider)).balance_ready
    assert runtime.account_state.wait_calls[0]["expected"] == 50


async def test_send_lock_gate_records_balance_and_revision_at_actual_send():
    provider, runtime = await connected()

    def change():
        runtime.account_state.gold_balance = 950
        runtime.account_state.gold_revision = 2

    runtime.client.before_send_change = change
    assert (await buy(provider)).balance_ready
    assert runtime.account_state.wait_calls == [{"expected": 850, "after_revision": 2, "timeout": 5}]


@pytest.mark.parametrize("balance,code", [(None, "PURCHASE_BALANCE_UNAVAILABLE"), (100, "PURCHASE_BALANCE_LIMIT")])
async def test_send_lock_gate_rechecks_balance_without_submitting(balance, code):
    provider, runtime = await connected()
    runtime.client.before_send_change = lambda: setattr(runtime.account_state, "gold_balance", balance)
    with pytest.raises(ApiError) as caught:
        await buy(provider)
    assert_error(caught.value, code)
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5313]


async def test_stop_while_waiting_for_send_lock_does_not_submit():
    provider, runtime = await connected()
    release = runtime.client.before_send_wait = asyncio.Event()
    stopped = False
    task = asyncio.create_task(provider.purchase_color(row(), max_price_gold=200, min_balance_gold=50,
                                                      stop_requested=lambda: stopped))
    await asyncio.wait_for(runtime.client.before_send_ready.wait(), 1)
    stopped = True
    release.set()
    assert await task is None
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5313]


async def test_stop_before_watch_sends_nothing():
    provider, runtime = await connected()
    assert await provider.purchase_color(row(), max_price_gold=200, min_balance_gold=50,
                                         stop_requested=lambda: True) is None
    assert runtime.client.sent == []


@pytest.mark.parametrize("wait_result", [False, TimeoutError(PRIVATE), RuntimeError(PRIVATE)])
async def test_success_is_preserved_when_balance_does_not_confirm(wait_result):
    provider, runtime = await connected()
    runtime.account_state.wait_result = wait_result
    receipt = await buy(provider)
    assert receipt.price == 100 and not receipt.balance_ready
    assert runtime.account_state.invalidated and provider.gold_balance is None


async def test_success_is_preserved_and_connection_closed_when_stop_fails():
    provider, runtime = await connected()
    runtime.client.stop_error = RuntimeError(PRIVATE)
    receipt = await buy(provider)
    assert receipt.price == 100 and not receipt.balance_ready
    assert runtime.client.closed and runtime.account_state.invalidated
    assert not runtime.market._detail_lock.locked()


@pytest.mark.parametrize("error", [GameClientError("GAME_REQUEST_TIMEOUT"), RuntimeError(PRIVATE),
                                         GameClientError("GAME_CONNECTION_FAILED")])
async def test_unknown_outcome_blocks_any_further_purchase_in_session(error):
    provider, runtime = await connected()
    runtime.client.buy_error = error
    for _ in range(2):
        with pytest.raises(ApiError) as caught:
            await buy(provider)
        assert_error(caught.value, "PURCHASE_OUTCOME_UNKNOWN")
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5319, 5313]
    assert runtime.account_state.invalidated and provider.gold_balance is None


async def test_explicit_rejection_is_safe_and_not_automatically_retried():
    provider, runtime = await connected()
    runtime.client.buy_error = GameClientError("GAME_REQUEST_REJECTED", response_code=2003)
    with pytest.raises(ApiError) as caught:
        await buy(provider)
    assert_error(caught.value, "PURCHASE_REJECTED")
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5319, 5313]


async def test_cancellation_after_submission_waits_for_success_and_keeps_receipt():
    provider, runtime = await connected()
    runtime.client.buy_release = asyncio.Event()
    task = asyncio.create_task(buy(provider))
    await asyncio.wait_for(runtime.client.buy_started.wait(), 1)
    task.cancel(PRIVATE)
    await asyncio.sleep(0)
    assert not task.done()
    runtime.client.buy_release.set()
    receipt = await task
    assert receipt.price == 100 and receipt.balance_ready
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5319, 5313]


async def test_purchases_are_serialized_separately_from_gui_cursor_lock():
    provider, runtime = await connected()
    runtime.client.buy_release = asyncio.Event()
    first = asyncio.create_task(buy(provider))
    await asyncio.wait_for(runtime.client.buy_started.wait(), 1)
    second = asyncio.create_task(buy(provider))
    await asyncio.sleep(0)
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5319]
    assert not provider._query_lock.locked()
    runtime.client.buy_release.set()
    assert (await first).balance_ready and (await second).balance_ready
    assert [command for command, _, _ in runtime.client.sent] == [5311, 5319, 5313] * 2


async def test_lazy_provider_proxies_purchase_operations_without_extra_builds():
    from rumi_market.live_runtime import LiveProviderFactory
    runtime = Runtime()
    builds = []

    async def builder():
        builds.append(True)
        return runtime, SimpleNamespace(items=())

    provider = LiveProviderFactory(runtime_builder=builder)()
    assert provider.gold_balance is None and builds == []
    await provider.login("synthetic", "synthetic")
    assert provider.gold_balance == 1000
    assert len((await provider.purchase_page(None)).items) == 1
    assert (await buy(provider)).price == 100
    assert builds == [True]


async def test_unverified_provider_has_no_balance_and_refuses_purchases():
    from rumi_market.providers import UnverifiedGameProvider
    provider = UnverifiedGameProvider()
    assert provider.gold_balance is None
    for operation in (lambda: provider.purchase_page(None), lambda: buy(provider)):
        with pytest.raises(ApiError):
            await operation()


async def test_excessively_long_pet_identity_is_rejected_without_integer_parse_leak():
    provider, runtime = await connected()
    candidate = ProviderListing(PRIVATE, "color", "7" * 5000, 90)
    with pytest.raises(ApiError) as caught:
        await provider.purchase_color(candidate, max_price_gold=200, min_balance_gold=50,
                                      stop_requested=lambda: False)
    assert_error(caught.value, "PURCHASE_INVALID")
    assert runtime.client.sent == []


async def test_unreadable_account_state_becomes_fixed_balance_unavailable_error():
    provider, runtime = await connected()

    class Unreadable:
        gold_revision = 1

        @property
        def gold_balance(self):
            raise RuntimeError(PRIVATE)

    runtime.account_state = Unreadable()
    with pytest.raises(ApiError) as caught:
        await buy(provider)
    assert_error(caught.value, "PURCHASE_BALANCE_UNAVAILABLE")
    assert runtime.client.sent == []


async def test_cleanup_ignoring_cancellation_cannot_hold_confirmed_receipt_forever(monkeypatch):
    import rumi_market.game.auction_execution as module
    provider, runtime = await connected()
    release = asyncio.Event()

    async def stubborn_stop(uid):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return False

    monkeypatch.setattr(module, "WATCH_CLEANUP_TIMEOUT", .01, raising=False)
    monkeypatch.setattr(runtime.market, "_stop_auction_watch", stubborn_stop)
    task = asyncio.create_task(buy(provider))
    try:
        done, _ = await asyncio.wait({task}, timeout=.1)
        assert done
        receipt = task.result()
        assert receipt.price == 100 and not receipt.balance_ready
        assert runtime.client.closed and runtime.account_state.invalidated
    finally:
        release.set()
        await asyncio.wait_for(task, 1)
        await asyncio.sleep(0)
