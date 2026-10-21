"""Session-owned complete item counts, projected from verified absolute updates."""

from datetime import datetime, timezone
from functools import lru_cache

from .envelope import ResponseEnvelope
from .profile_sync import _load_schema
from .proto import BUSINESS_MESSAGE_LIMIT


MAX_COMBINE_DEPTH = 4
MAX_COMBINE_UNITS = 512
MAX_ITEM_ROWS = 16384
MAX_ITEM_CHANGES = 4096
_COMMANDS = frozenset({2016, 2941, 2942})


@lru_cache(maxsize=1)
def _inventory_schema():
    classes, item, _ = _load_schema()
    return {command: classes[command] for command in _COMMANDS}, item


def _item(message, *, needs_count):
    if (not message.HasField("item_id") or message.item_id <= 0
            or (needs_count and (not message.HasField("count") or message.count < 0))):
        raise ValueError("GAME_INVENTORY_STATE_INVALID")
    # The verified consumer uses uid=0 for non-instanced stackable items.
    return (message.item_id, message.item_uid), message.count


class InventoryState:
    def __init__(self, verified_version: bool):
        if type(verified_version) is not bool:
            raise ValueError("GAME_INVENTORY_STATE_INVALID")
        self._verified = verified_version
        self._closed = self._valid = False
        self._sequence = 0
        self._observed_at = None
        self._items = {}
        self._received_totals, self._spent_totals = {}, {}
        self._movement_epoch = 0
        self._unavailable_reason = "INVENTORY_BASELINE_MISSING" if verified_version else "INVENTORY_VERSION_UNVERIFIED"
        self._classes, self._item_class = {}, None
        if verified_version:
            try:
                self._classes, self._item_class = _inventory_schema()
            except Exception:
                self._verified = False
                self._unavailable_reason = "INVENTORY_SCHEMA_UNAVAILABLE"

    @property
    def sequence(self):
        return self._sequence

    def invalidate(self):
        self._valid = False
        self._items.clear()
        self._received_totals.clear()
        self._spent_totals.clear()
        self._movement_epoch += 1
        self._observed_at = None
        self._sequence += 1
        if self._verified:
            self._unavailable_reason = "INVENTORY_STATE_INVALID"

    def close(self):
        self._closed = True
        self.invalidate()
        self._classes = {}
        self._item_class = None
        self._unavailable_reason = "INVENTORY_CLOSED"

    def snapshot(self):
        totals = {}
        if self._valid and not self._closed:
            for (item_id, _), count in self._items.items():
                key = str(item_id)
                totals[key] = totals.get(key, 0) + count
        return {"valid": self._valid and not self._closed, "sequence": self._sequence,
                "observed_at": self._observed_at, "items": totals,
                "received_totals": dict(self._received_totals), "spent_totals": dict(self._spent_totals),
                "movement_epoch": self._movement_epoch,
                "unavailable_reason": self._unavailable_reason}

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
                raise ValueError("GAME_INVENTORY_STATE_INVALID")
            self._observe(response.command, response.data, depth=0, units=[0])
        except Exception:
            self.invalidate()

    def _publish(self, items, stackable_movements=None):
        stackable_movements = stackable_movements or {}
        if self._valid and items == self._items and not stackable_movements:
            return
        if self._valid:
            # Compare item totals, not individual instance movements. One
            # absolute notification can replace instances without changing stock.
            changes = {}
            for (item_id, _), count in items.items():
                key = str(item_id)
                changes[key] = changes.get(key, 0) + count
            for (item_id, _), count in self._items.items():
                key = str(item_id)
                changes[key] = changes.get(key, 0) - count
            for item_id in changes.keys() | stackable_movements.keys():
                change = changes.get(item_id, 0)
                received, spent = stackable_movements.get(item_id, (max(change, 0), max(-change, 0)))
                if received:
                    self._received_totals[item_id] = self._received_totals.get(item_id, 0) + received
                if spent:
                    self._spent_totals[item_id] = self._spent_totals.get(item_id, 0) + spent
        self._items = items
        self._valid = True
        self._unavailable_reason = None
        self._sequence += 1
        self._observed_at = datetime.now(timezone.utc).isoformat()

    def _observe(self, command, data, *, depth, units):
        if command not in _COMMANDS:
            return
        if depth > MAX_COMBINE_DEPTH or len(data) > BUSINESS_MESSAGE_LIMIT:
            raise ValueError("GAME_INVENTORY_STATE_INVALID")
        message = self._classes[command].FromString(data)
        if command == 2016:
            units[0] += len(message.unit_lst)
            if units[0] > MAX_COMBINE_UNITS:
                raise ValueError("GAME_INVENTORY_STATE_INVALID")
            for unit in message.unit_lst:
                if not unit.HasField("id") or unit.id <= 0:
                    raise ValueError("GAME_INVENTORY_STATE_INVALID")
                if unit.id in _COMMANDS and not unit.HasField("msg"):
                    raise ValueError("GAME_INVENTORY_STATE_INVALID")
                self._observe(unit.id, unit.msg, depth=depth + 1, units=units)
            return
        if command == 2941:
            if len(message.item_list) > MAX_ITEM_ROWS:
                raise ValueError("GAME_INVENTORY_STATE_INVALID")
            items = {}
            for row in message.item_list:
                key, count = _item(row, needs_count=True)
                if key in items:
                    raise ValueError("GAME_INVENTORY_STATE_INVALID")
                items[key] = count
            self._publish(items)
            return
        if len(message.change_list) > MAX_ITEM_CHANGES:
            raise ValueError("GAME_INVENTORY_STATE_INVALID")
        items = dict(self._items)
        instanced = {str(item_id) for item_id, uid in items if uid != 0}
        stackable_movements = {}
        for change in message.change_list:
            if not change.HasField("change_type") or change.change_type not in (1, 2, 3):
                raise ValueError("GAME_INVENTORY_STATE_INVALID")
            key, count = _item(self._item_class.FromString(change.data), needs_count=change.change_type != 2)
            if key[1] != 0:
                instanced.add(str(key[0]))
            if not self._valid:
                continue
            previous = items.get(key, 0)
            if change.change_type == 1 and key not in items:
                if len(items) >= MAX_ITEM_ROWS:
                    raise ValueError("GAME_INVENTORY_STATE_INVALID")
                items[key] = count
            elif change.change_type == 2:
                items.pop(key, None)
            elif change.change_type == 3 and key in items:
                items[key] = count
            difference = items.get(key, 0) - previous
            if key[1] == 0 and difference:
                movement = stackable_movements.setdefault(str(key[0]), [0, 0])
                movement[0 if difference > 0 else 1] += abs(difference)
        if self._valid:
            # Instance migrations only prove the aggregate net change. Isolated
            # uid=0 rows also prove each ordered debit/credit inside this notice.
            for item_id in instanced:
                stackable_movements.pop(item_id, None)
            self._publish(items, stackable_movements)
