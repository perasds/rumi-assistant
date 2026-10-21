"""Watch-scoped native purchase execution; only a confirmed ACK is a receipt."""

import asyncio

from ..errors import ApiError
from ..providers import ProviderListing, ProviderPurchaseReceipt
from .auction_purchase import AuctionPurchaseError, GameAuctionPurchase
from .market_query import WATCH_START_REQUEST, WATCH_START_ACK
from .proto import GameProtoCodec


WATCH_CLEANUP_TIMEOUT = 5.0  # Local budgets, not server protocol guarantees.
CONNECTION_CLOSE_TIMEOUT = 1.0


def purchase_error(code):
    messages = {
        "PURCHASE_INVALID": "购买参数不合法",
        "PURCHASE_DETAIL_UNAVAILABLE": "商品详情暂不可用，本次未提交购买",
        "PURCHASE_BALANCE_UNAVAILABLE": "金币余额尚未确认，请重新登录后再试",
        "PURCHASE_BALANCE_LIMIT": "金币不足或购买后低于最低保留余额",
        "PURCHASE_REJECTED": "服务器拒绝了本次购买",
        "PURCHASE_OUTCOME_UNKNOWN": "购买结果尚未确认，本会话已停止购买，请在游戏中核对",
    }
    return ApiError(code, messages[code], 409)


async def _finish_owned(task, on_cancel=None):
    """A caller cancellation cannot discard a submitted transaction's ACK."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if on_cancel is not None:
                on_cancel()
            if task.done():
                return task.result()


async def _bounded_cleanup(operation, timeout):
    task = asyncio.create_task(operation)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
        return False
    try:
        return task.result()
    except BaseException:
        return False


class GameAuctionExecution:
    def __init__(self, runtime, role_id):
        self._runtime, self._role_id = runtime, role_id

    @staticmethod
    def _stopped(callback):
        try:
            return callback() is not False
        except Exception:
            return True

    def _balance_error(self, price, minimum):
        try:
            balance = self._runtime.account_state.gold_balance
            revision = self._runtime.account_state.gold_revision
        except Exception:
            return "PURCHASE_BALANCE_UNAVAILABLE"
        if (type(balance) is not int or not 0 <= balance < 1 << 63
                or type(revision) is not int or revision < 0):
            return "PURCHASE_BALANCE_UNAVAILABLE"
        if balance < price + minimum:
            return "PURCHASE_BALANCE_LIMIT"
        return None

    def _invalidate(self):
        try:
            self._runtime.account_state.invalidate()
        except Exception:
            pass

    async def _cleanup(self, market, uid):
        return await _bounded_cleanup(market._stop_auction_watch(uid), WATCH_CLEANUP_TIMEOUT) is True

    async def _close(self, client):
        await _bounded_cleanup(client.close(), CONNECTION_CLOSE_TIMEOUT)

    def _detail_price(self, response, row, maximum):
        if (type(response.command) is not int or response.command != WATCH_START_ACK
                or type(response.code) is not int or response.code != 0):
            raise ValueError
        ack = GameProtoCodec().decode("SNKProto.Cmd_Market_Auction_Watch_Start_Ack", response.data).message
        if not ack.HasField("uid") or ack.uid != row.raw_id or not ack.HasField("lumi_info"):
            return None
        one = ack.lumi_info
        if not all(one.HasField(field) for field in (
                "uid", "lumi_id", "sell_role_id", "is_sold_out", "fixed_price",
                "up_time", "expire_seconds", "extern_seconds")):
            return None
        if (one.uid != row.raw_id or str(one.lumi_id) != row.pet_id
                or not 0 < one.sell_role_id < 1 << 64
                or one.sell_role_id == self._role_id or one.is_sold_out
                or not 0 < one.fixed_price < maximum
                or any(value < 0 for value in (one.up_time, one.expire_seconds, one.extern_seconds))):
            return None
        # Source-confirmed One fields use seconds; Brief.expire_time is not used.
        deadline = one.up_time + one.expire_seconds + one.extern_seconds
        now = self._runtime.server_time_seconds
        if (deadline >= 1 << 63 or type(now) is not int or not 0 < now < 1 << 63):
            raise ValueError
        if now >= deadline:
            return None
        return one.sell_role_id, one.fixed_price

    async def execute(self, row, *, max_price_gold, min_balance_gold, stop_requested):
        if (type(row) is not ProviderListing or row.category != "color"
                or type(row.raw_id) is not str or not row.raw_id.strip()
                or type(row.pet_id) is not str or len(row.pet_id) > 10 or not row.pet_id.isascii()
                or not row.pet_id.isdecimal() or not 0 < int(row.pet_id) < 1 << 31
                or row.pet_id != str(int(row.pet_id))
                or type(max_price_gold) is not int or not 0 < max_price_gold < 1 << 63
                or type(min_balance_gold) is not int or not 0 <= min_balance_gold < 1 << 63
                or not callable(stop_requested)):
            raise purchase_error("PURCHASE_INVALID")
        if self._stopped(stop_requested):
            return None
        initial_error = self._balance_error(0, min_balance_gold)
        if initial_error is not None:
            raise purchase_error(initial_error)
        market = self._runtime.market
        try:
            await market._detail_lock.acquire()
        except asyncio.CancelledError:
            return None
        failure, receipt, gate_error = None, None, None
        pre_balance = pre_revision = None
        buy_started = cancelled_before_send = False

        def cancel_pending_send():
            nonlocal cancelled_before_send
            if pre_balance is None:
                cancelled_before_send = True

        def before_send():
            nonlocal gate_error, pre_balance, pre_revision
            if cancelled_before_send or self._stopped(stop_requested):
                return False
            gate_error = self._balance_error(price, min_balance_gold)
            if gate_error is not None:
                return False
            pre_balance = self._runtime.account_state.gold_balance
            pre_revision = self._runtime.account_state.gold_revision
            return True

        try:
            payload = GameProtoCodec().encode("SNKProto.Cmd_Market_Auction_Watch_Start_Req", {"uid": row.raw_id})
            response = await market._client.request(WATCH_START_REQUEST, WATCH_START_ACK, payload)
            detail = self._detail_price(response, row, max_price_gold)
            if detail is not None and not self._stopped(stop_requested):
                seller, price = detail
                gate_error = self._balance_error(price, min_balance_gold)
                if gate_error is not None:
                    failure = gate_error
                else:
                    buy_started = True
                    task = asyncio.create_task(GameAuctionPurchase(market._client).buy(
                        row.raw_id, seller, price, lumi_id=int(row.pet_id), before_send=before_send))
                    confirmed = await _finish_owned(task, cancel_pending_send)
                    receipt = ProviderPurchaseReceipt(row.raw_id, row.pet_id, confirmed.price)
        except AuctionPurchaseError as error:
            if error.code == "PURCHASE_NOT_SENT":
                failure = gate_error
            elif error.code == "PURCHASE_REJECTED":
                failure = "PURCHASE_REJECTED"
            else:
                failure = "PURCHASE_OUTCOME_UNKNOWN"
        except asyncio.CancelledError:
            failure = "PURCHASE_OUTCOME_UNKNOWN" if pre_balance is not None else None
        except Exception:
            failure = "PURCHASE_OUTCOME_UNKNOWN" if buy_started else "PURCHASE_DETAIL_UNAVAILABLE"
        finally:
            cleaned = await _finish_owned(asyncio.create_task(self._cleanup(market, row.raw_id)))
            if not cleaned:
                self._invalidate()
                await _finish_owned(asyncio.create_task(self._close(market._client)))
            market._detail_lock.release()
        if receipt is not None:
            ready = False
            if cleaned:
                try:
                    ready = await _finish_owned(asyncio.create_task(
                        self._runtime.account_state.wait_for_gold_balance(
                            expected=pre_balance - receipt.price, after_revision=pre_revision, timeout=5))) is True
                except (Exception, asyncio.CancelledError):
                    pass
            if not ready:
                self._invalidate()
            return ProviderPurchaseReceipt(receipt.raw_id, receipt.pet_id, receipt.price, ready)
        if failure is None and not cleaned:
            failure = "PURCHASE_DETAIL_UNAVAILABLE"
        if failure is not None:
            if failure == "PURCHASE_OUTCOME_UNKNOWN":
                self._invalidate()
            raise purchase_error(failure)
        return None
