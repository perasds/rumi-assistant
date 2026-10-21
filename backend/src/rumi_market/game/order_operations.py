"""Single-attempt order completion and ordinary production collection.

An applied result confirms the matching server ACK. The caller separately
confirms the corresponding inventory/order/store notifications before advancing.
"""

import asyncio
from copy import deepcopy
import re

from .client import GameClientError
from .envelope import ResponseEnvelope
from .order_codec import OrderProtoCodec


_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ITEMS = 256


class _Rejected(Exception):
    pass


def _id(value, *, minimum=1, bits=31):
    if (type(value) is not str or not value.isascii() or not value.isdecimal()
            or not 0 < len(value) <= 20):
        raise _Rejected()
    number = int(value)
    if str(number) != value or not minimum <= number < 1 << bits:
        raise _Rejected()
    return number


def _count(value):
    if type(value) is not int or value < 0:
        raise _Rejected()
    return value


def _item_totals(rows):
    if type(rows) is not list or len(rows) > _MAX_ITEMS:
        raise _Rejected()
    totals = {}
    for row in rows:
        if type(row) is not dict:
            raise _Rejected()
        item = str(_id(row.get("item_id")))
        totals[item] = totals.get(item, 0) + _count(row.get("count"))
    return totals


def _rewards(rows):
    if len(rows) > _MAX_ITEMS:
        raise _Rejected()
    result = []
    for row in rows:
        if (not row.HasField("item_id") or row.item_id <= 0
                or not row.HasField("count") or row.count < 0):
            raise _Rejected()
        result.append({"item_id": str(row.item_id), "count": row.count})
    return result


class GameOrderOperations:
    def __init__(self, client, snapshot, can_write):
        if not callable(snapshot) or not callable(can_write):
            raise ValueError("GAME_ORDER_OPERATION_INVALID")
        self._client, self._snapshot, self._can_write = client, snapshot, can_write
        self._codec = OrderProtoCodec()
        self._lock = asyncio.Lock()
        self._applied_revision = None
        self.blocked = False

    def _context(self, operation, expected_revision, stop_requested):
        # Import at call time: the service owns the public revision contract and
        # may itself be imported by provider/application construction.
        from ..order_service import order_revision
        if (self.blocked or self._client.closed or self._can_write() is not True
                or not callable(stop_requested) or stop_requested() is not False
                or type(expected_revision) is not str or not _REVISION.fullmatch(expected_revision)
                or type(operation) is not dict
                or (expected_revision == self._applied_revision
                    and operation.get('action') not in {'update_work', 'claim_all'})):
            raise _Rejected()
        raw = self._snapshot()
        if type(raw) is not dict or order_revision(raw) != expected_revision:
            raise _Rejected()
        required = ("inventory", "home") if operation.get("action") in {"claim", "claim_all", "update_work"} else ("orders", "inventory", "home")
        for name in required:
            if type(raw.get(name)) is not dict or raw[name].get("valid") is not True:
                raise _Rejected()
        if operation.get('action') in {'update_work', 'claim_all'}:
            if set(operation) != {'action'}:
                raise _Rejected()
            return (3027, 3028, {}) if operation['action'] == 'claim_all' else (3029, 3030, {})
        if operation.get("action") == "complete":
            if set(operation) != {"action", "slot_id"}:
                raise _Rejected()
            slot = _id(operation["slot_id"], minimum=0)
            orders = [row for row in raw["orders"]["orders"] if row.get("slot_id") == str(slot)]
            if (len(orders) != 1 or orders[0].get("is_unlock") is not True
                    or type(orders[0].get("order_type")) is not int or orders[0]["order_type"] < 0):
                raise _Rejected()
            needs = _item_totals(orders[0].get("materials"))
            if not needs or not any(needs.values()):
                raise _Rejected()
            inventory = raw["inventory"].get("items")
            if type(inventory) is not dict or any(
                    _count(inventory.get(item, 0)) < count for item, count in needs.items()):
                raise _Rejected()
            return 3302, 3303, {"slot_id": slot}
        if operation.get("action") == "claim":
            if set(operation) != {"action", "building_uid", "building_id"}:
                raise _Rejected()
            uid, building = _id(operation["building_uid"]), _id(operation["building_id"])
            rows = [row for row in raw["home"]["architectures"] if row.get("uid") == uid]
            if len(rows) != 1:
                raise _Rejected()
            row = rows[0]
            if (row.get("id") != building or type(row.get("work_old")) is not dict
                    or row.get("cur_recipe") is not None or row.get("has_farm_data")
                    or row.get("is_hide") or row.get("is_need_cut_ribbon")
                    or row.get("level_up_complete_time") not in (None, 0, "0")
                    or not any(_item_totals(row.get("store_items")).values())):
                raise _Rejected()
            return 3025, 3026, {"archi_uid": uid, "archi_id": building}
        # Refresh price/cooldown and its resource effects are not yet verified.
        raise _Rejected()

    async def apply(self, operation, *, expected_revision, stop_requested):
        async with self._lock:
            if self.blocked:
                return {"status": "uncertain", "rewards": []}
            sent = False
            try:
                operation = deepcopy(operation)
                command, ack_command, fields = self._context(operation, expected_revision, stop_requested)
                payload = self._codec.encode(command, **fields)
                def before_send():
                    nonlocal sent
                    try:
                        self._context(operation, expected_revision, stop_requested)
                    except Exception:
                        return False
                    sent = True
                    return True
                response = await self._client.request(command, ack_command, payload, before_send=before_send)
                if (not sent or not isinstance(response, ResponseEnvelope)
                        or response.command != ack_command or response.remaining != 0):
                    raise _Rejected()
                if response.code != 0:
                    return {"status": "rejected", "rewards": []}
                ack = self._codec.decode(ack_command, response.data)
                if command in (3027, 3029):
                    # These native ACKs are empty. They confirm the request,
                    # not any produced quantity or collected reward.
                    return {"status": "applied", "rewards": []}
                if command == 3025:
                    if any(not ack.HasField(name) or getattr(ack, name) != value for name, value in fields.items()):
                        raise _Rejected()
                    rewards = _rewards(ack.item_show_list)
                else:
                    rewards = _rewards(ack.reward)
                self._applied_revision = expected_revision
                return {"status": "applied", "rewards": rewards}
            except asyncio.CancelledError:
                self.blocked = self.blocked or sent
                raise asyncio.CancelledError() from None
            except GameClientError as error:
                if error.code in {"GAME_REQUEST_REJECTED", "GAME_REQUEST_NOT_SENT"}:
                    return {"status": "rejected", "rewards": []}
            except Exception:
                pass
            if sent:
                self.blocked = True
                return {"status": "uncertain", "rewards": []}
            return {"status": "rejected", "rewards": []}
