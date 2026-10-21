"""One native 5319/5320 purchase attempt with an explicitly confirmed receipt.

Public source validation for hot versions 62/64: docs/purchase-ack-verification.json.
Native success consumers confirm the order UID; pet fields are optional.
Watch, account eligibility, balance and cleanup belong to the caller.
An unknown outcome must never be automatically retried by this layer.
"""

import asyncio
from dataclasses import dataclass
import logging

from .client import GameClient, GameClientError
from .proto import GameProtoCodec, GameProtoError


BUY_REQUEST = 5319
BUY_ACK = 5320
_LOGGER = logging.getLogger(__name__)
_REQUEST_FAILURE_REASONS = {
    "GAME_REQUEST_TIMEOUT": "REQUEST_TIMEOUT",
    "GAME_TRANSPORT_TIMEOUT": "REQUEST_TIMEOUT",
    "GAME_CONNECTION_FAILED": "CONNECTION_FAILED",
    "GAME_CLIENT_CLOSED": "CONNECTION_FAILED",
    "GAME_CLIENT_NOT_CONNECTED": "CONNECTION_FAILED",
    "GAME_TRANSPORT_CLOSED": "CONNECTION_FAILED",
    "GAME_TRANSPORT_NOT_CONNECTED": "CONNECTION_FAILED",
    "GAME_TRANSPORT_UNAVAILABLE": "CONNECTION_FAILED",
}


class AuctionPurchaseError(Exception):
    def __init__(self, code: str, *, response_code: int | None = None):
        super().__init__(code)
        self.code = code
        self.response_code = response_code


@dataclass(frozen=True, repr=False)
class AuctionPurchaseReceipt:
    uid: str
    lumi_id: int
    lumi_uid: int | None
    price: int


def _int32(value):
    return type(value) is int and -(1 << 31) <= value < 1 << 31


class GameAuctionPurchase:
    def __init__(self, client: GameClient):
        self._client = client
        self._codec = GameProtoCodec()

    async def buy(self, uid: str, seller_role_id: int, price: int, *,
                  lumi_id: int, before_send=None) -> AuctionPurchaseReceipt:
        if (type(uid) is not str or not uid.strip()
                or type(seller_role_id) is not int or not 0 < seller_role_id < 1 << 64
                or type(price) is not int or not 0 <= price < 1 << 64
                or type(lumi_id) is not int or not 0 < lumi_id < 1 << 31):
            raise AuctionPurchaseError("PURCHASE_INVALID")
        payload = None
        try:
            payload = self._codec.encode("SNKProto.Cmd_Market_Auction_Buy_Req", {
                "uid": uid, "sell_role_id": seller_role_id, "price": price})
        except GameProtoError:
            pass
        if payload is None:
            raise AuctionPurchaseError("PURCHASE_INVALID")

        receipt, response, ack = None, None, None
        failure, response_code = "PURCHASE_OUTCOME_UNKNOWN", None
        reason = "REQUEST_FAILED"
        try:
            options = {} if before_send is None else {"before_send": before_send}
            response = await self._client.request(BUY_REQUEST, BUY_ACK, payload, **options)
            reason = "ACK_INVALID_ENVELOPE"
            if (type(response.command) is int and response.command == BUY_ACK
                    and _int32(response.code)):
                if response.code != 0:
                    failure, response_code = "PURCHASE_REJECTED", response.code
                else:
                    reason = "ACK_DECODE_FAILED"
                    ack = self._codec.decode("SNKProto.Cmd_Market_Auction_Buy_Ack", response.data).message
                    if not ack.HasField("uid"):
                        reason = "ACK_MISSING_UID"
                    elif ack.uid != uid:
                        reason = "ACK_UID_MISMATCH"
                    elif ack.HasField("lumi_id") and ack.lumi_id not in (0, lumi_id):
                        reason = "ACK_PET_MISMATCH"
                    else:
                        obtained_uid = ack.lumi_uid if ack.HasField("lumi_uid") and ack.lumi_uid != 0 else None
                        receipt = AuctionPurchaseReceipt(uid, lumi_id, obtained_uid, price)
        except GameClientError as error:
            reason = (_REQUEST_FAILURE_REASONS.get(error.code, "REQUEST_FAILED")
                      if type(error.code) is str else "REQUEST_FAILED")
            if error.code == "GAME_REQUEST_NOT_SENT":
                failure = "PURCHASE_NOT_SENT"
            elif (error.code == "GAME_REQUEST_REJECTED" and _int32(error.response_code)
                    and error.response_code != 0):
                failure, response_code = "PURCHASE_REJECTED", error.response_code
        except asyncio.CancelledError:
            reason = "REQUEST_CANCELLED"
        except TimeoutError:
            reason = "REQUEST_TIMEOUT"
        except Exception:
            pass
        finally:
            payload, response, ack = None, None, None
        if receipt is None:
            if failure == "PURCHASE_OUTCOME_UNKNOWN":
                _LOGGER.warning("Auction purchase outcome unknown (%s)", reason)
            # Raise after leaving every handler so private upstream exceptions
            # are absent from both __context__ and __cause__.
            raise AuctionPurchaseError(failure, response_code=response_code)
        return receipt
