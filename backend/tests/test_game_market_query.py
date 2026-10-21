import asyncio
import importlib
import subprocess
import sys
import textwrap

import pytest

from rumi_market.game.client import GameClient, GameClientError
from rumi_market.game.proto import GameProtoCodec, GameProtoError


PRIVATE = "synthetic-private-market-uid-or-course"
CODEC = GameProtoCodec()
COLOR_REQ = "SNKProto.Cmd_Market_Auction_QueryLumiDetail_Req"
COLOR_ACK = "SNKProto.Cmd_Market_Auction_QueryLumiDetail_Ack"
SHOP_REQ = "SNKProto.Cmd_SystemShop_QueryItemPrice_Req"
SHOP_ACK = "SNKProto.Cmd_SystemShop_QueryItemPrice_Ack"
WATCH_START_REQ = "SNKProto.Cmd_Market_Auction_Watch_Start_Req"
WATCH_START_ACK = "SNKProto.Cmd_Market_Auction_Watch_Start_Ack"
WATCH_STOP_REQ = "SNKProto.Cmd_Market_Auction_Watch_Stop_Req"
WATCH_STOP_ACK = "SNKProto.Cmd_Market_Auction_Watch_Stop_Ack"


def implementation():
    return importlib.import_module("rumi_market.game.market_query")


class MemoryTransport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = False

    async def connect(self):
        pass

    async def send(self, payload):
        await self.outgoing.put(payload)

    async def receive(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True

    async def next_request(self):
        raw = await asyncio.wait_for(self.outgoing.get(), 1)
        return CODEC.decode("SNKProto.ReqProto", raw).message

    def respond(self, request, command, message_name, fields, *, code=0, sequence=None):
        self.incoming.put_nowait(CODEC.encode("SNKProto.RspProto", {
            "RspCmdId": command,
            "RspCmdSeq": request.ReqCmdSeq if sequence is None else sequence,
            "RspCode": code,
            "RspData": CODEC.encode(message_name, fields),
        }))


async def connected(*, client_timeout=2, **options):
    module = implementation()
    transport = MemoryTransport()
    client = GameClient(transport, timeout=client_timeout)
    query = module.GameMarketQuery(client, **options)
    await client.connect()
    return client, transport, query


async def test_first_color_query_sets_all_native_builder_fields_and_exact_ack():
    client, transport, query = await connected(page_window=40, desc=False)
    try:
        call = asyncio.create_task(query.query_color_page())
        envelope = await transport.next_request()
        assert envelope.ReqCmdId == 5325
        assert not envelope.HasField("ReqNotifySeq")
        request = CODEC.decode(COLOR_REQ, envelope.ReqData).message
        assert {field.name for field, _ in request.ListFields()} == {
            "lumi_id", "begin_index", "end_index", "order_type", "course", "desc"}
        assert (request.lumi_id, request.begin_index, request.end_index,
                request.order_type, request.course, request.desc) == (0, 1, 41, 3, "", False)
        # Neither a different ACK command nor a different sequence completes this read.
        transport.respond(envelope, 5327, COLOR_ACK, {"course": PRIVATE})
        transport.respond(envelope, 5326, COLOR_ACK, {"course": PRIVATE}, sequence=999)
        transport.respond(envelope, 5326, COLOR_ACK, {"lumi_id": 0, "course": ""})
        page = await call
        assert page.quotes == () and page.next_cursor is None
        assert page.lumi_id == 0 and page.begin_index is None and page.end_index is None
        assert client.pending_count == 0
    finally:
        await client.close()


async def test_color_quotes_keep_zero_negative_prices_raw_expiry_and_optional_presence():
    client, transport, query = await connected(desc=True)
    try:
        call = asyncio.create_task(query.query_color_page())
        sent = await transport.next_request()
        assert CODEC.decode(COLOR_REQ, sent.ReqData).message.desc is True
        transport.respond(sent, 5326, COLOR_ACK, {
            "lumi_list": [{"uid": PRIVATE, "lumi_id": 701, "fixed_price": 0,
                           "bid_price": -5, "expire_time": 9876543210123, "quality": 4}, {}],
            "lumi1": {"uid": "featured-not-in-global-list"},
            "lumi2": {"uid": "featured-not-in-global-list"},
            "lumi3": {"uid": "featured-not-in-global-list"},
            "course": PRIVATE,
        })
        page = await call
        assert len(page.quotes) == 2
        quote, missing = page.quotes
        assert (quote.uid, quote.lumi_id, quote.fixed_price, quote.bid_price,
                quote.expire_time, quote.quality) == (PRIVATE, 701, 0, -5, 9876543210123, 4)
        assert (missing.uid, missing.lumi_id, missing.fixed_price, missing.bid_price,
                missing.expire_time, missing.quality) == (None,) * 6
        assert page.course == page.next_cursor.course == PRIVATE
        assert PRIVATE not in repr(page) + repr(quote) + repr(page.next_cursor)
        assert not hasattr(quote, "direct_buy_eligible")
    finally:
        await client.close()


async def test_pagination_preserves_opaque_course_and_uses_previous_request_end():
    client, transport, query = await connected(page_window=2)
    pages = []

    async def consume():
        async for page in query.iter_color_pages():
            pages.append(page)

    task = asyncio.create_task(consume())
    try:
        for begin, end, course, next_course, rows in (
            (1, 3, "", PRIVATE, [{"fixed_price": 0}]),
            (4, 6, PRIVATE, PRIVATE, []),
            (7, 9, PRIVATE, "", [{"fixed_price": 20}]),
        ):
            sent = await transport.next_request()
            request = CODEC.decode(COLOR_REQ, sent.ReqData).message
            assert (request.begin_index, request.end_index, request.course) == (begin, end, course)
            # ACK indices remain raw metadata; they do not replace native request-side indices.
            transport.respond(sent, 5326, COLOR_ACK, {
                "begin_index": 99, "end_index": 100, "course": next_course, "lumi_list": rows})
        await task
        assert len(pages) == 3 and pages[1].quotes == ()
        assert pages[0].begin_index == 99 and pages[0].end_index == 100
        assert pages[-1].next_cursor is None and transport.outgoing.empty()
    finally:
        task.cancel()
        await client.close()


async def test_color_page_window_override_uses_its_actual_end_without_changing_default():
    client, transport, query = await connected()
    cursor = None
    try:
        for window, begin, end, course in (
            (10, 1, 11, ""), (2, 12, 14, PRIVATE), (None, 15, 55, PRIVATE),
        ):
            call = asyncio.create_task(query.query_color_page(cursor, page_window=window))
            sent = await transport.next_request()
            request = CODEC.decode(COLOR_REQ, sent.ReqData).message
            assert {field.name for field, _ in request.ListFields()} == {
                "lumi_id", "begin_index", "end_index", "order_type", "course", "desc"}
            assert (request.lumi_id, request.begin_index, request.end_index,
                    request.order_type, request.course, request.desc) == (0, begin, end, 3, course, False)
            transport.respond(sent, 5326, COLOR_ACK, {
                "begin_index": 100, "end_index": 101, "course": PRIVATE})
            cursor = (await call).next_cursor
            assert cursor.begin_index == end + 1 and cursor.course == PRIVATE
    finally:
        await client.close()


@pytest.mark.parametrize("window", [0, -1, True, 1.5, 2**31 - 1, 2**31])
async def test_invalid_color_page_window_override_fails_before_io(window):
    module = implementation()
    client, transport, query = await connected()
    try:
        with pytest.raises(module.GameMarketQueryError, match="^GAME_MARKET_QUERY_INVALID$"):
            await query.query_color_page(page_window=window)
        assert transport.outgoing.empty()
    finally:
        await client.close()


async def test_color_page_window_override_validates_cursor_sum_before_io():
    module = implementation()
    client, transport, query = await connected(page_window=2)
    try:
        cursor = module.AuctionCursor(begin_index=2**31 - 10, course=PRIVATE)
        with pytest.raises(module.GameMarketQueryError, match="^GAME_MARKET_QUERY_INVALID$"):
            await query.query_color_page(cursor, page_window=10)
        assert transport.outgoing.empty()
    finally:
        await client.close()


@pytest.mark.parametrize("course_fields", [{}, {"course": ""}])
async def test_absent_or_empty_course_terminates_even_with_quotes(course_fields):
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_color_page())
        sent = await transport.next_request()
        transport.respond(sent, 5326, COLOR_ACK, {**course_fields, "lumi_list": [{"uid": PRIVATE}]})
        page = await call
        assert len(page.quotes) == 1 and page.next_cursor is None
        assert page.course == course_fields.get("course")
    finally:
        await client.close()


@pytest.mark.parametrize("kind,outer_type,resource_type,quality", [
    ("normal_pet", 2, 4, 4), ("item", 1, 2, 0),
])
async def test_system_shop_uses_native_fields_and_preserves_every_raw_quote(kind, outer_type, resource_type, quality):
    module = implementation()
    client, transport, query = await connected()
    try:
        candidate = module.SystemShopCandidate(config_id=701, quality=4, price=0)
        call = asyncio.create_task(query.query_system_shop(kind, (candidate,)))
        sent = await transport.next_request()
        assert sent.ReqCmdId == 3522
        request = CODEC.decode(SHOP_REQ, sent.ReqData).message
        assert request.type == outer_type and len(request.item_list) == 1
        item = request.item_list[0]
        assert (item.id, item.type, item.quality, item.price) == (701, resource_type, quality, 0)
        assert {field.name for field in item.DESCRIPTOR.fields} == {
            "id", "type", "quality", "price", "today_init_price", "today_init_time"}
        # The fixed SystemShop schema is proto3, so zero values have no scalar presence.
        assert not item.DESCRIPTOR.fields_by_name["price"].has_presence
        assert item.today_init_price == item.today_init_time == 0
        transport.respond(sent, 3523, SHOP_ACK, {"type": outer_type, "item_list": [
            {"id": 701, "type": resource_type, "quality": quality, "price": 0,
             "today_init_price": 0, "today_init_time": 123},
            {"id": 702, "type": 999, "quality": -1, "price": -7, "today_init_price": 20},
        ]})
        batch = await call
        assert batch.kind == kind and batch.type == outer_type
        assert [(q.id, q.type, q.quality, q.price, q.today_init_price, q.today_init_time)
                for q in batch.quotes] == [(701, resource_type, quality, 0, 0, 123),
                                          (702, 999, -1, -7, 20, 0)]
        assert not hasattr(batch.quotes[0], "stock")
    finally:
        await client.close()


async def test_system_shop_sends_explicit_current_object_price_without_guessing():
    module = implementation()
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_system_shop("normal_pet", (
            module.SystemShopCandidate(config_id=702, quality=1, price=12345),)))
        sent = await transport.next_request()
        request = CODEC.decode(SHOP_REQ, sent.ReqData).message
        assert request.item_list[0].price == 12345
        transport.respond(sent, 3523, SHOP_ACK, {"type": 2})
        assert (await call).quotes == ()
    finally:
        await client.close()


@pytest.mark.parametrize("method", ["color", "shop"])
async def test_business_errors_propagate_with_original_code_and_no_quote_or_cursor(method):
    module = implementation()
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_color_page() if method == "color" else
                                   query.query_system_shop("item", (module.SystemShopCandidate(
                                       config_id=1, quality=0, price=0),)))
        sent = await transport.next_request()
        transport.respond(sent, 5326 if method == "color" else 3523,
                          COLOR_ACK if method == "color" else SHOP_ACK,
                          {"course": PRIVATE} if method == "color" else {"type": 1}, code=-41)
        with pytest.raises(GameClientError) as failure:
            await call
        assert failure.value.code == "GAME_REQUEST_REJECTED"
        assert failure.value.response_code == -41
        assert PRIVATE not in repr(failure.value) and failure.value.__context__ is None
        assert client.pending_count == 0
    finally:
        await client.close()


@pytest.mark.parametrize("method", ["color", "shop"])
async def test_wrong_business_scope_rejected_without_decoded_values_in_error(method):
    module = implementation()
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_color_page() if method == "color" else
                                   query.query_system_shop("normal_pet", (module.SystemShopCandidate(
                                       config_id=1, quality=4, price=0),)))
        sent = await transport.next_request()
        transport.respond(sent, 5326 if method == "color" else 3523,
                          COLOR_ACK if method == "color" else SHOP_ACK,
                          {"lumi_id": 123, "course": PRIVATE} if method == "color" else {"type": 1})
        with pytest.raises(module.GameMarketQueryError) as failure:
            await call
        assert str(failure.value) == "GAME_MARKET_ACK_MISMATCH"
        assert PRIVATE not in repr(failure.value)
    finally:
        await client.close()


@pytest.mark.parametrize("window", [0, -1, True, 1.5, 2**31])
def test_invalid_window_fails_before_io(window):
    module = implementation()
    with pytest.raises(module.GameMarketQueryError, match="^GAME_MARKET_QUERY_INVALID$"):
        module.GameMarketQuery(None, page_window=window)


@pytest.mark.parametrize("cursor", [True, 0, PRIVATE])
async def test_explicit_bad_cursor_fails_before_io(cursor):
    module = implementation()
    client, transport, query = await connected()
    try:
        with pytest.raises(module.GameMarketQueryError, match="^GAME_MARKET_QUERY_INVALID$"):
            await query.query_color_page(cursor)
        assert transport.outgoing.empty()
    finally:
        await client.close()


async def test_int32_cursor_overflow_and_bad_candidates_fail_before_io():
    module = implementation()
    client, transport, query = await connected(page_window=40)
    try:
        for cursor in (module.AuctionCursor(begin_index=2**31-1, course=PRIVATE),
                       module.AuctionCursor(begin_index=True, course=PRIVATE),
                       module.AuctionCursor(begin_index=1, course=object())):
            with pytest.raises(module.GameMarketQueryError, match="^GAME_MARKET_QUERY_INVALID$"):
                await query.query_color_page(cursor)
        for kind, candidates in (("unknown", ()), ("item", (object(),)),
                                 ("item", (module.SystemShopCandidate(config_id=True, quality=0, price=0),)),
                                 ("normal_pet", (module.SystemShopCandidate(config_id=1, quality=4, price=2**31),))):
            with pytest.raises(module.GameMarketQueryError, match="^GAME_MARKET_QUERY_INVALID$"):
                await query.query_system_shop(kind, candidates)
        assert transport.outgoing.empty()
        assert PRIVATE not in repr(module.AuctionCursor(begin_index=1, course=PRIVATE))
    finally:
        await client.close()


async def test_invalid_business_wire_is_rejected_without_raw_payload_in_exception():
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_color_page())
        sent = await transport.next_request()
        transport.incoming.put_nowait(CODEC.encode("SNKProto.RspProto", {
            "RspCmdId": 5326, "RspCmdSeq": sent.ReqCmdSeq,
            "RspData": b"\xff" + PRIVATE.encode(),
        }))
        with pytest.raises(GameProtoError) as failure:
            await call
        assert str(failure.value) == "GAME_PROTO_INVALID"
        assert PRIVATE not in repr(failure.value) and failure.value.__context__ is None
    finally:
        await client.close()


async def acknowledge_stop(transport, uid=PRIVATE, *, code=0):
    stop = await transport.next_request()
    assert stop.ReqCmdId == 5313
    message = CODEC.decode(WATCH_STOP_REQ, stop.ReqData).message
    assert message.HasField("uid") and message.uid == uid
    transport.respond(stop, 5314, WATCH_STOP_ACK, {}, code=code)


async def test_auction_detail_preserves_one_presence_and_waits_for_stop_ack():
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        assert start.ReqCmdId == 5311
        request = CODEC.decode(WATCH_START_REQ, start.ReqData).message
        assert request.HasField("uid") and request.uid == PRIVATE
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": PRIVATE, "lumi_info": {
            "uid": PRIVATE, "lumi_id": 701, "fixed_price": 0, "sell_role_id": 0,
            "is_sold_out": False, "up_time": 123456789, "expire_seconds": 20,
            "extern_seconds": 0}})
        stop = await transport.next_request()
        assert stop.ReqCmdId == 5313 and not call.done()
        transport.respond(stop, 5314, WATCH_STOP_ACK, {})
        detail = await call
        assert (detail.uid, detail.lumi_id, detail.fixed_price, detail.sell_role_id,
                detail.is_sold_out, detail.up_time, detail.expire_seconds,
                detail.extern_seconds) == (PRIVATE, 701, 0, 0, False, 123456789, 20, 0)
        assert PRIVATE not in repr(detail)
        assert not hasattr(detail, "direct_buy_eligible")
        assert not client.closed and client.pending_count == 0
    finally:
        await client.close()


@pytest.mark.parametrize("detail_fields", [None, {"uid": PRIVATE}])
async def test_auction_detail_distinguishes_absent_one_from_absent_optional_values(detail_fields):
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        fields = {"uid": PRIVATE}
        if detail_fields is not None:
            fields["lumi_info"] = detail_fields
        transport.respond(start, 5312, WATCH_START_ACK, fields)
        await acknowledge_stop(transport)
        detail = await call
        if detail_fields is None:
            assert detail is None
        else:
            assert detail.uid == PRIVATE
            assert (detail.lumi_id, detail.fixed_price, detail.sell_role_id,
                    detail.is_sold_out, detail.up_time, detail.expire_seconds,
                    detail.extern_seconds) == (None,) * 7
    finally:
        await client.close()


@pytest.mark.parametrize("fields", [
    {}, {"uid": "other"}, {"uid": PRIVATE, "lumi_info": {}},
    {"uid": PRIVATE, "lumi_info": {"uid": "other"}},
])
async def test_auction_detail_rejects_wrong_or_missing_uid_and_still_stops(fields):
    module = implementation()
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        transport.respond(start, 5312, WATCH_START_ACK, fields)
        await acknowledge_stop(transport)
        with pytest.raises(module.GameMarketQueryError, match="^GAME_MARKET_ACK_MISMATCH$") as caught:
            await call
        assert caught.value.__context__ is None
        assert PRIVATE not in repr(caught.value)
        assert not client.closed
    finally:
        await client.close()


@pytest.mark.parametrize("uid", [None, True, 1, b"uid", ""])
async def test_invalid_detail_uid_fails_before_start_or_stop(uid):
    module = implementation()
    client, transport, query = await connected()
    try:
        with pytest.raises(module.GameMarketQueryError, match="^GAME_MARKET_QUERY_INVALID$"):
            await query.query_auction_detail(uid)
        assert transport.outgoing.empty()
    finally:
        await client.close()


async def test_detail_rejection_stops_watch_and_preserves_safe_business_code():
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        transport.respond(start, 5312, WATCH_START_ACK, {}, code=-41)
        await acknowledge_stop(transport)
        with pytest.raises(GameClientError) as caught:
            await call
        assert caught.value.code == "GAME_REQUEST_REJECTED"
        assert caught.value.response_code == -41
        assert caught.value.__context__ is None and PRIVATE not in repr(caught.value)
    finally:
        await client.close()


async def test_invalid_detail_wire_stops_watch_without_exposing_payload():
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        transport.incoming.put_nowait(CODEC.encode("SNKProto.RspProto", {
            "RspCmdId": 5312, "RspCmdSeq": start.ReqCmdSeq,
            "RspData": b"\xff" + PRIVATE.encode()}))
        await acknowledge_stop(transport)
        with pytest.raises(GameProtoError, match="^GAME_PROTO_INVALID$") as caught:
            await call
        assert caught.value.__context__ is None and PRIVATE not in repr(caught.value)
    finally:
        await client.close()


async def test_cancelled_detail_finishes_stop_even_on_repeated_cancellation():
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        await transport.next_request()
        call.cancel(PRIVATE)
        stop = await transport.next_request()
        assert stop.ReqCmdId == 5313 and not call.done()
        call.cancel(PRIVATE)
        transport.respond(stop, 5314, WATCH_STOP_ACK, {})
        with pytest.raises(asyncio.CancelledError) as caught:
            await call
        assert caught.value.args == ()
        assert client.pending_count == 0 and not client.closed
    finally:
        await client.close()


@pytest.mark.parametrize("stop_result", ["rejected", "timeout", "malformed"])
async def test_stop_failure_closes_connection_and_returns_fixed_cleanup_error(stop_result):
    module = implementation()
    client, transport, query = await connected(client_timeout=0.05)
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": PRIVATE})
        stop = await transport.next_request()
        if stop_result == "rejected":
            transport.respond(stop, 5314, WATCH_STOP_ACK, {}, code=-2)
        elif stop_result == "malformed":
            transport.incoming.put_nowait(CODEC.encode("SNKProto.RspProto", {
                "RspCmdId": 5314, "RspCmdSeq": stop.ReqCmdSeq, "RspData": b"\xff"}))
        with pytest.raises(module.GameMarketQueryError,
                           match="^GAME_MARKET_WATCH_CLEANUP_FAILED$") as caught:
            await call
        assert caught.value.__context__ is None and PRIVATE not in repr(caught.value)
        assert client.closed and transport.closed and client.pending_count == 0
    finally:
        await client.close()


async def test_details_are_serialized_through_stop_confirmation():
    client, transport, query = await connected()
    try:
        first = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        second = asyncio.create_task(query.query_auction_detail("second"))
        await asyncio.sleep(0)
        assert transport.outgoing.empty()
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": PRIVATE})
        await acknowledge_stop(transport)
        assert await first is None
        start = await transport.next_request()
        assert CODEC.decode(WATCH_START_REQ, start.ReqData).message.uid == "second"
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": "second"})
        await acknowledge_stop(transport, "second")
        assert await second is None
    finally:
        await client.close()


async def test_detail_start_timeout_still_stops_before_propagating_timeout():
    client, transport, query = await connected(client_timeout=0.05)
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        await transport.next_request()
        await acknowledge_stop(transport)
        with pytest.raises(GameClientError, match="^GAME_REQUEST_TIMEOUT$") as caught:
            await call
        assert caught.value.__context__ is None
        assert not client.closed and client.pending_count == 0
    finally:
        await client.close()


async def test_cancellation_during_stop_wait_closes_connection_if_stop_times_out():
    client, transport, query = await connected(client_timeout=0.05)
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": PRIVATE})
        stop = await transport.next_request()
        assert stop.ReqCmdId == 5313
        call.cancel(PRIVATE)
        with pytest.raises(asyncio.CancelledError) as caught:
            await call
        assert caught.value.args == ()
        assert client.closed and transport.closed and client.pending_count == 0
    finally:
        await client.close()


async def test_cancelled_lock_waiter_sends_no_watch_and_preserves_active_query():
    client, transport, query = await connected()
    try:
        first = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        waiting = asyncio.create_task(query.query_auction_detail("never-sent"))
        await asyncio.sleep(0)
        waiting.cancel(PRIVATE)
        with pytest.raises(asyncio.CancelledError) as caught:
            await waiting
        assert caught.value.args == () and transport.outgoing.empty()
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": PRIVATE})
        await acknowledge_stop(transport)
        assert await first is None
        assert transport.outgoing.empty() and not client.closed
    finally:
        await client.close()


def test_cleanup_cancelled_before_first_step_terminates_closes_and_releases_lock():
    # An OS-level timeout also detects an event-loop busy loop, which an
    # asyncio timeout in the same process cannot interrupt.
    script = textwrap.dedent('''
        import asyncio
        from types import SimpleNamespace
        from rumi_market.game.market_query import GameMarketQuery
        from rumi_market.game.proto import GameProtoCodec

        private = "synthetic-private-cancel-message"
        codec = GameProtoCodec()
        original_create_task = asyncio.create_task

        def cancel_cleanup(coro, *args, **kwargs):
            task = original_create_task(coro, *args, **kwargs)
            if coro.cr_code.co_name == "_stop_auction_watch":
                task.cancel(private)
            return task

        class Client:
            closed = False
            async def request(self, command, ack, data):
                assert command == 5311
                return SimpleNamespace(data=codec.encode(
                    "SNKProto.Cmd_Market_Auction_Watch_Start_Ack", {"uid": "uid"}))
            async def close(self):
                self.closed = True

        async def main():
            client = Client()
            query = GameMarketQuery(client)
            asyncio.create_task = cancel_cleanup
            try:
                await query.query_auction_detail("uid")
            except asyncio.CancelledError as error:
                assert error.args == () and error.__context__ is None
            else:
                raise AssertionError("Cancelled cleanup must fail the query")
            assert client.closed and not query._detail_lock.locked()
            print("cleanup-cancellation-safe")

        asyncio.run(main())
    ''')
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=3, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cleanup-cancellation-safe"


async def test_unconfirmed_stop_bounds_client_close_and_releases_detail_lock(monkeypatch):
    module = implementation()
    client, transport, query = await connected(client_timeout=0.02)
    original_close = client.close
    close_started, close_cancelled = asyncio.Event(), asyncio.Event()

    async def stalled_close():
        await original_close()
        close_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            close_cancelled.set()
            raise

    monkeypatch.setattr(client, "close", stalled_close)
    monkeypatch.setattr(module, "WATCH_CLOSE_TIMEOUT", 0.02, raising=False)
    call = asyncio.create_task(query.query_auction_detail(PRIVATE))
    try:
        start = await transport.next_request()
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": PRIVATE})
        await transport.next_request()  # Leave Stop unacknowledged.
        await asyncio.wait_for(close_started.wait(), 0.2)
        # Repeated parent cancellation must not cancel the owned close early.
        call.cancel(PRIVATE)
        await asyncio.sleep(0)
        call.cancel(PRIVATE)
        finished, _ = await asyncio.wait({call}, timeout=0.2)
        assert finished == {call}, "Client close exceeded the cleanup budget"
        with pytest.raises(asyncio.CancelledError) as caught:
            await call
        assert caught.value.args == () and caught.value.__context__ is None
        assert close_cancelled.is_set() and client.closed
        assert not query._detail_lock.locked()
    finally:
        monkeypatch.setattr(client, "close", original_close)
        if not call.done():
            call.cancel()
            await asyncio.wait({call}, timeout=0.1)
        await original_close()


async def test_cleanup_task_cancelled_while_waiting_for_stop_closes_client():
    module = implementation()
    client, transport, query = await connected()
    try:
        call = asyncio.create_task(query.query_auction_detail(PRIVATE))
        start = await transport.next_request()
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": PRIVATE})
        await transport.next_request()
        cleanup = next(task for task in asyncio.all_tasks()
                       if task.get_coro().__qualname__.endswith("._stop_auction_watch"))
        cleanup.cancel(PRIVATE)
        finished, _ = await asyncio.wait({call}, timeout=0.2)
        assert finished == {call}
        with pytest.raises(module.GameMarketQueryError,
                           match="^GAME_MARKET_WATCH_CLEANUP_FAILED$") as caught:
            await call
        assert caught.value.__context__ is None and PRIVATE not in repr(caught.value)
        assert client.closed and client.pending_count == 0
        assert not query._detail_lock.locked()
    finally:
        await client.close()


async def test_close_ignoring_cancellation_does_not_hold_query_or_detail_lock(monkeypatch):
    module = implementation()
    client, transport, query = await connected(client_timeout=0.02)
    original_close = client.close
    release_close, close_cancelled, close_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn_close():
        await original_close()
        try:
            await release_close.wait()
        except asyncio.CancelledError:
            close_cancelled.set()
            await release_close.wait()
        finally:
            close_finished.set()

    monkeypatch.setattr(client, "close", stubborn_close)
    monkeypatch.setattr(module, "WATCH_CLOSE_TIMEOUT", 0.02)
    call = asyncio.create_task(query.query_auction_detail(PRIVATE))
    try:
        start = await transport.next_request()
        transport.respond(start, 5312, WATCH_START_ACK, {"uid": PRIVATE})
        await transport.next_request()
        finished, _ = await asyncio.wait({call}, timeout=0.2)
        assert finished == {call}
        with pytest.raises(module.GameMarketQueryError,
                           match="^GAME_MARKET_WATCH_CLEANUP_FAILED$") as caught:
            await call
        assert caught.value.__context__ is None
        await asyncio.wait_for(close_cancelled.wait(), 0.1)
        assert not close_finished.is_set() and not query._detail_lock.locked()
        assert client.closed
    finally:
        release_close.set()
        await asyncio.wait_for(close_finished.wait(), 0.2)
        await original_close()
