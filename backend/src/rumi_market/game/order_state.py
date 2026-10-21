"""Session-owned order projection following verified full-replacement consumers."""

from copy import deepcopy
from datetime import datetime, timezone

from .envelope import ResponseEnvelope
from .order_codec import OrderProtoCodec
from .proto import BUSINESS_MESSAGE_LIMIT


MAX_COMBINE_DEPTH = 4
MAX_COMBINE_UNITS = 512
MAX_ORDER_SLOTS = 256
MAX_ORDER_ITEMS = 256
MAX_PROJECTION_ROWS = 16384
_COMMANDS = frozenset({2016, 3001, 3301})


def _optional(message, name):
    if not message.HasField(name):
        return None
    value = getattr(message, name)
    if value < 0:
        raise ValueError("GAME_ORDER_STATE_INVALID")
    return value


def _items(messages):
    if len(messages) > MAX_ORDER_ITEMS:
        raise ValueError("GAME_ORDER_STATE_INVALID")
    result = []
    for item in messages:
        if (not item.HasField("item_id") or item.item_id <= 0
                or not item.HasField("count") or item.count < 0):
            raise ValueError("GAME_ORDER_STATE_INVALID")
        result.append({"item_id": str(item.item_id), "count": item.count})
    return result


def _slot(message):
    if not message.HasField("slot_id") or message.slot_id < 0 or not message.HasField("is_unlock"):
        raise ValueError("GAME_ORDER_STATE_INVALID")
    order = message.order_info if message.HasField("order_info") else None
    return {"slot_id": str(message.slot_id), "is_unlock": message.is_unlock,
            "order_type": _optional(order, "order_type") if order is not None else None,
            "materials": _items(order.item_need) if order is not None else [],
            "rewards": _items(order.item_reward) if order is not None else [],
            "refresh_time": _optional(order, "refresh_time") if order is not None else None,
            "refresh_seconds": _optional(order, "refresh_seconds") if order is not None else None}


class OrderState:
    def __init__(self, verified_version: bool):
        if type(verified_version) is not bool:
            raise ValueError("GAME_ORDER_STATE_INVALID")
        self._verified = verified_version
        self._closed = self._valid = False
        self._sequence = 0
        self._observed_at = None
        self._orders = []
        self._today_complete_times = self._current_energy = self._limit_energy = None
        self._unavailable_reason = "ORDER_BASELINE_MISSING" if verified_version else "ORDER_VERSION_UNVERIFIED"
        self._codec = None
        if verified_version:
            try:
                self._codec = OrderProtoCodec()
            except Exception:
                self._verified = False
                self._unavailable_reason = "ORDER_SCHEMA_UNAVAILABLE"

    @property
    def sequence(self):
        return self._sequence

    def invalidate(self):
        self._valid = False
        self._orders.clear()
        self._today_complete_times = self._current_energy = self._limit_energy = None
        self._observed_at = None
        self._sequence += 1
        if self._verified:
            self._unavailable_reason = "ORDER_STATE_INVALID"

    def close(self):
        self._closed = True
        self.invalidate()
        self._codec = None
        self._unavailable_reason = "ORDER_CLOSED"

    def snapshot(self):
        return deepcopy({"valid": self._valid and not self._closed, "sequence": self._sequence,
                         "observed_at": self._observed_at, "orders": self._orders,
                         "today_complete_times": self._today_complete_times,
                         "current_energy": self._current_energy, "limit_energy": self._limit_energy,
                         "unavailable_reason": self._unavailable_reason})

    def observe(self, response: ResponseEnvelope):
        if self._closed or not self._verified:
            return
        if not isinstance(response, ResponseEnvelope):
            self.invalidate()
            return
        if response.command not in _COMMANDS:
            return
        try:
            if response.code != 0 or response.remaining != 0:
                raise ValueError("GAME_ORDER_STATE_INVALID")
            self._observe(response.command, response.data, depth=0, units=[0])
        except Exception:
            self.invalidate()

    def _observe(self, command, data, *, depth, units):
        if command not in _COMMANDS:
            return
        if depth > MAX_COMBINE_DEPTH or len(data) > BUSINESS_MESSAGE_LIMIT:
            raise ValueError("GAME_ORDER_STATE_INVALID")
        message = self._codec.decode(command, data)
        if command == 2016:
            units[0] += len(message.unit_lst)
            if units[0] > MAX_COMBINE_UNITS:
                raise ValueError("GAME_ORDER_STATE_INVALID")
            for unit in message.unit_lst:
                if not unit.HasField("id") or unit.id <= 0:
                    raise ValueError("GAME_ORDER_STATE_INVALID")
                if unit.id in _COMMANDS and not unit.HasField("msg"):
                    raise ValueError("GAME_ORDER_STATE_INVALID")
                self._observe(unit.id, unit.msg, depth=depth + 1, units=units)
            return
        if not message.HasField("order_archi") or len(message.order_archi.slot_list) > MAX_ORDER_SLOTS:
            raise ValueError("GAME_ORDER_STATE_INVALID")
        info = message.order_archi
        orders, seen, count = [], set(), 0
        for message_slot in info.slot_list:
            slot = _slot(message_slot)
            if slot["slot_id"] in seen:
                raise ValueError("GAME_ORDER_STATE_INVALID")
            seen.add(slot["slot_id"])
            count += 1 + len(slot["materials"]) + len(slot["rewards"])
            if count > MAX_PROJECTION_ROWS:
                raise ValueError("GAME_ORDER_STATE_INVALID")
            orders.append(slot)
        counters = (_optional(info, "today_complete_times"), _optional(info, "currentEnergy"), _optional(info, "limitEnergy"))
        if self._valid and orders == self._orders and counters == (
                self._today_complete_times, self._current_energy, self._limit_energy):
            return
        self._orders = orders
        self._today_complete_times, self._current_energy, self._limit_energy = counters
        self._valid = True
        self._unavailable_reason = None
        self._sequence += 1
        self._observed_at = datetime.now(timezone.utc).isoformat()
