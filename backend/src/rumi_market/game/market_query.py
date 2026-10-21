"""Read-only market RPCs and RAM-only raw quotes from the fixed public schema.

Source Game.dll SHA256:
3ba755a6360e3cd8ce36d6e17b86545158a01641a0bdf9b99985c5b09f215431.
Native builders: LobbyServerInvoker.GetAuctionLumiDetailData 0x60040f8,
Cmd_SystemShop_QueryItemPrice_Req 0x6004142. Paging: controller 0x600774f,
0x6007750, 0x6007755. Source reports: market-protocol-public.sanitized.json
and market-buy-eligibility.sanitized.json in artifacts/rumi/pet-catalog.

This layer assumes an already connected GameClient. It performs no login,
purchase, persistence, stock inference or account-specific eligibility check.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal

from .client import GameClient, GameClientError
from .proto import GameProtoCodec, GameProtoError


COLOR_REQUEST = 5325
COLOR_ACK = 5326
SHOP_REQUEST = 3522
SHOP_ACK = 3523
WATCH_START_REQUEST = 5311
WATCH_START_ACK = 5312
WATCH_STOP_REQUEST = 5313
WATCH_STOP_ACK = 5314
WATCH_CLOSE_TIMEOUT = 1.0  # Local cleanup budget, not a server protocol value.
DEFAULT_PAGE_WINDOW = 40  # Project policy; native UI derives its window from layout.
SystemShopKind = Literal["normal_pet", "item"]


class GameMarketQueryError(ValueError):
    pass


@dataclass(frozen=True, kw_only=True, repr=False)
class AuctionCursor:
    """Opaque server course and the next native request-side start index."""

    begin_index: int
    course: str


@dataclass(frozen=True, repr=False)
class AuctionQuote:
    """Only raw Brief quote/ID fields; None preserves optional-field absence.

    expire_time has its wire Int64 value, without assigning a time unit.
    A fixed_price, including zero, does not prove direct-buy eligibility.
    """

    uid: str | None
    lumi_id: int | None
    fixed_price: int | None
    bid_price: int | None
    expire_time: int | None
    quality: int | None


@dataclass(frozen=True, repr=False)
class AuctionDetail:
    """Raw source One fields; optional absence never means false or zero."""

    uid: str | None
    lumi_id: int | None
    fixed_price: int | None
    sell_role_id: int | None
    is_sold_out: bool | None
    up_time: int | None
    expire_seconds: int | None
    extern_seconds: int | None


@dataclass(frozen=True, repr=False)
class AuctionPage:
    lumi_id: int | None
    begin_index: int | None
    end_index: int | None
    quotes: tuple[AuctionQuote, ...]
    course: str | None
    next_cursor: AuctionCursor | None


@dataclass(frozen=True, kw_only=True, repr=False)
class SystemShopCandidate:
    """Explicit source-catalog ID/quality and current object's Price input.

    The caller selects known online MarketPrice candidates; normal-pet IDs
    must resolve in TbLumi. This layer does not treat every Lumi ID as a shop
    candidate and does not invent a current Price from catalog metadata.
    """

    config_id: int
    quality: int
    price: int


@dataclass(frozen=True, repr=False)
class SystemShopQuote:
    """Proto3 values; zero/default has no scalar presence in this schema.

    Price display/cache filtering belongs to a later layer. No field here
    denotes server inventory, configured PurchaseNum or permission to buy.
    """

    id: int
    type: int
    quality: int
    price: int
    today_init_price: int
    today_init_time: int


@dataclass(frozen=True, repr=False)
class SystemShopBatch:
    kind: SystemShopKind
    type: int
    quotes: tuple[SystemShopQuote, ...]


def _int32(value: object) -> bool:
    return type(value) is int and -(1 << 31) <= value < 1 << 31


def _optional(message, name):
    return getattr(message, name) if message.HasField(name) else None


class GameMarketQuery:
    def __init__(self, client: GameClient, *, page_window: int = DEFAULT_PAGE_WINDOW,
                 desc: bool = False):
        # False is this project's ascending query direction, not a runtime UI claim.
        if (type(page_window) is not int or not 0 < page_window < (1 << 31) - 1
                or type(desc) is not bool):
            raise GameMarketQueryError("GAME_MARKET_QUERY_INVALID")
        self._client = client
        self._page_window = page_window
        self._desc = desc
        self._codec = GameProtoCodec()
        self._detail_lock = asyncio.Lock()

    async def query_auction_detail(self, uid: str) -> AuctionDetail | None:
        """Read one watched listing, then confirm Stop before releasing it.

        Start/Stop are source-bound 5311/5312 and 5313/5314. Waiting for
        Stop confirmation is project policy; failure closes the connection.
        """
        if type(uid) is not str or not uid:
            raise GameMarketQueryError("GAME_MARKET_QUERY_INVALID")
        cancelled = False
        try:
            await self._detail_lock.acquire()
        except asyncio.CancelledError:
            cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        try:
            payload = self._codec.encode("SNKProto.Cmd_Market_Auction_Watch_Start_Req", {"uid": uid})
            result, failure = None, None
            try:
                response = await self._client.request(WATCH_START_REQUEST, WATCH_START_ACK, payload)
                ack = self._codec.decode("SNKProto.Cmd_Market_Auction_Watch_Start_Ack", response.data).message
                if not ack.HasField("uid") or ack.uid != uid:
                    raise GameMarketQueryError("GAME_MARKET_ACK_MISMATCH")
                if ack.HasField("lumi_info"):
                    one = ack.lumi_info
                    if not one.HasField("uid") or one.uid != uid:
                        raise GameMarketQueryError("GAME_MARKET_ACK_MISMATCH")
                    result = AuctionDetail(*(_optional(one, field) for field in (
                        "uid", "lumi_id", "fixed_price", "sell_role_id", "is_sold_out",
                        "up_time", "expire_seconds", "extern_seconds")))
            except asyncio.CancelledError:
                cancelled = True
            except GameMarketQueryError:
                failure = GameMarketQueryError("GAME_MARKET_ACK_MISMATCH")
            except GameProtoError:
                failure = GameProtoError("GAME_PROTO_INVALID")
            except GameClientError as error:
                if error.code in ("GAME_REQUEST_REJECTED", "GAME_REQUEST_TIMEOUT"):
                    number = error.response_code
                    failure = GameClientError(error.code, response_code=number if _int32(number) else None)
                else:
                    failure = GameMarketQueryError("GAME_MARKET_DETAIL_FAILED")
            except Exception:
                failure = GameMarketQueryError("GAME_MARKET_DETAIL_FAILED")
            finally:
                # Shield the owned cleanup from cancellation, including a second
                # cancellation while the first is already being cleaned up.
                cleanup = asyncio.create_task(self._stop_auction_watch(uid))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleaned = False
                if cleanup.cancelled():
                    cancelled = True
                else:
                    try:
                        cleaned = cleanup.result() is True
                    except BaseException:
                        pass
                if not cleaned:
                    # A cancelled-before-start Stop never ran its own body.
                    # Close here, with one deadline even under repeated cancel.
                    closer = asyncio.create_task(self._client.close())
                    deadline = asyncio.get_running_loop().time() + WATCH_CLOSE_TIMEOUT
                    while not closer.done():
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            await asyncio.wait({closer}, timeout=remaining)
                        except asyncio.CancelledError:
                            cancelled = True
                    if not closer.done():
                        closer.cancel()
                    # Never wait indefinitely for a close that ignores cancel.
                    closer.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            if cancelled:
                raise asyncio.CancelledError
            if not cleaned:
                raise GameMarketQueryError("GAME_MARKET_WATCH_CLEANUP_FAILED")
            if failure is not None:
                raise failure
            return result
        finally:
            self._detail_lock.release()

    async def _stop_auction_watch(self, uid: str) -> bool:
        try:
            payload = self._codec.encode("SNKProto.Cmd_Market_Auction_Watch_Stop_Req", {"uid": uid})
            response = await self._client.request(WATCH_STOP_REQUEST, WATCH_STOP_ACK, payload)
            self._codec.decode("SNKProto.Cmd_Market_Auction_Watch_Stop_Ack", response.data)
            return True
        except BaseException:
            pass
        return False

    async def query_color_page(self, cursor: AuctionCursor | None = None, *,
                               page_window: int | None = None) -> AuctionPage:
        window = self._page_window if page_window is None else page_window
        if type(window) is not int or not 0 < window < (1 << 31) - 1:
            raise GameMarketQueryError("GAME_MARKET_QUERY_INVALID")
        if cursor is None:
            cursor = AuctionCursor(begin_index=1, course="")
        if (type(cursor) is not AuctionCursor or type(cursor.begin_index) is not int
                or not 0 < cursor.begin_index < 1 << 31 or type(cursor.course) is not str
                or cursor.begin_index + window >= 1 << 31):
            raise GameMarketQueryError("GAME_MARKET_QUERY_INVALID")
        end = cursor.begin_index + window
        payload = self._codec.encode("SNKProto.Cmd_Market_Auction_QueryLumiDetail_Req", {
            # The native proto2 builder explicitly sets all six, even 0/False/empty.
            "lumi_id": 0, "begin_index": cursor.begin_index, "end_index": end,
            "order_type": 3, "course": cursor.course, "desc": self._desc,
        })
        response = await self._client.request(COLOR_REQUEST, COLOR_ACK, payload)
        ack = self._codec.decode("SNKProto.Cmd_Market_Auction_QueryLumiDetail_Ack",
                                 response.data).message
        if ack.HasField("lumi_id") and ack.lumi_id != 0:
            raise GameMarketQueryError("GAME_MARKET_ACK_MISMATCH")
        quotes = tuple(AuctionQuote(*(_optional(row, field) for field in (
            "uid", "lumi_id", "fixed_price", "bid_price", "expire_time", "quality")))
                       for row in ack.lumi_list)
        # Global source consumer uses lumi_list only, without lumi1/lumi2/lumi3.
        course = _optional(ack, "course")
        next_cursor = AuctionCursor(begin_index=end + 1, course=course) if course else None
        return AuctionPage(_optional(ack, "lumi_id"), _optional(ack, "begin_index"),
                           _optional(ack, "end_index"), quotes, course, next_cursor)

    async def iter_color_pages(self) -> AsyncIterator[AuctionPage]:
        """Stream pages without retaining a listing or interpreting opaque course.

        Only absent/empty course ends native pagination; an empty or short list
        with nonempty course still needs the next request. The index range is
        the native begin/end formula, without claiming server inclusivity.
        """
        cursor = None
        while True:
            page = await self.query_color_page(cursor)
            yield page
            cursor = page.next_cursor
            if cursor is None:
                return

    async def query_system_shop(self, kind: SystemShopKind,
                                candidates: Sequence[SystemShopCandidate]) -> SystemShopBatch:
        if (type(kind) is not str or kind not in ("normal_pet", "item")
                or not isinstance(candidates, (tuple, list))
                or any(type(row) is not SystemShopCandidate
                       or not all(_int32(value) for value in (row.config_id, row.quality, row.price))
                       for row in candidates)):
            raise GameMarketQueryError("GAME_MARKET_QUERY_INVALID")
        outer_type, resource_type = (2, 4) if kind == "normal_pet" else (1, 2)
        payload = self._codec.encode("SNKProto.Cmd_SystemShop_QueryItemPrice_Req", {
            "type": outer_type,
            "item_list": [{"id": row.config_id, "type": resource_type,
                           "quality": row.quality if outer_type == 2 else 0,
                           "price": row.price} for row in candidates],
        })
        response = await self._client.request(SHOP_REQUEST, SHOP_ACK, payload)
        ack = self._codec.decode("SNKProto.Cmd_SystemShop_QueryItemPrice_Ack", response.data).message
        if ack.type != outer_type:
            raise GameMarketQueryError("GAME_MARKET_ACK_MISMATCH")
        return SystemShopBatch(kind, ack.type, tuple(SystemShopQuote(
            row.id, row.type, row.quality, row.price, row.today_init_price, row.today_init_time)
            for row in ack.item_list))
