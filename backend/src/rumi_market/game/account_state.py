"""Session-owned live gold state from bounded server item notifications.

The caller must verify the fixed public table version before enabling this
tracker. It does no network/disk work while observing and never reads archives.
"""

import asyncio
from collections import deque
from functools import lru_cache
import math

from .envelope import ResponseEnvelope
from .profile_sync import _load_schema
from .proto import BUSINESS_MESSAGE_LIMIT


GOLD_CLIENT_VERSION = "1.5.3"
GOLD_HOT_VERSION = 62
# Versions 64/65/69/71/72 were separately checked against official Game/VIB resources.
# hot72 evidence is in docs/hot72-runtime-compatibility.public.json.
GOLD_HOT_VERSIONS = frozenset({GOLD_HOT_VERSION, 64, 65, 69, 71, 72})
GOLD_ITEM_ID = 10001
MAX_COMBINE_DEPTH = 4
MAX_COMBINE_UNITS = 512
MAX_ITEM_ROWS = 16384
MAX_ITEM_CHANGES = 4096
MAX_GOLD_HISTORY = 128
_COMMANDS = frozenset({2016, 2941, 2942})


@lru_cache(maxsize=1)
def _account_schema():
    classes, item, _ = _load_schema()
    return {command: classes[command] for command in _COMMANDS}, item


class GoldItemState:
    """Follow absolute item counts, not the ItemChange ADD/SUB enum.

    The actual CmdRepeatChange contract is NEW=1, DEL=2, MOD=3. Initial full
    data establishes a baseline; deltas cannot establish or repair one.
    """

    def __init__(self, verified_version: bool):
        if type(verified_version) is not bool:
            raise ValueError("GAME_ACCOUNT_STATE_INVALID")
        self._verified = verified_version
        self._closed = False
        self._valid = False
        self._gold_items: dict[int, int] = {}
        self._gold_balance: int | None = None
        self._gold_revision = 0
        self._gold_history: deque[tuple[int, int]] = deque(maxlen=MAX_GOLD_HISTORY)
        self._changed = asyncio.Event()
        self._classes, self._item_class = {}, None
        if verified_version:
            try:
                classes, self._item_class = _account_schema()
                self._classes = dict(classes)
            except Exception:
                self._verified = False

    @property
    def gold_balance(self) -> int | None:
        return self._gold_balance if self._valid and not self._closed else None

    @property
    def gold_revision(self) -> int:
        return self._gold_revision

    def _signal(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def invalidate(self) -> None:
        self._valid = False
        self._gold_balance = None
        self._gold_items.clear()
        self._gold_history.clear()
        self._signal()

    def close(self) -> None:
        self._closed = True
        self.invalidate()
        self._classes.clear()
        self._item_class = None

    def observe(self, response: ResponseEnvelope) -> None:
        if self._closed or not self._verified:
            return
        if not isinstance(response, ResponseEnvelope):
            self.invalidate()
            return
        if response.command not in _COMMANDS:
            return
        if response.remaining != 0 or response.code != 0:
            self.invalidate()
            return
        try:
            self._observe(response.command, response.data, depth=0, units=[0])
        except Exception:
            # Never retain raw response objects or exception text in state.
            self.invalidate()

    @staticmethod
    def _item(item, *, needs_count: bool) -> tuple[int, int, int]:
        if (not item.HasField("item_id") or not 0 < item.item_id < 1 << 31
                or (needs_count and (not item.HasField("count") or item.count < 0))):
            raise ValueError("GAME_ACCOUNT_STATE_INVALID")
        # Native protobuf getters use the legitimate default uid=0.
        return item.item_id, item.item_uid, item.count

    def _publish(self) -> None:
        total = sum(self._gold_items.values())
        if not 0 <= total < 1 << 63:
            raise ValueError("GAME_ACCOUNT_STATE_INVALID")
        self._gold_balance = total
        self._valid = True
        self._gold_revision += 1
        self._gold_history.append((self._gold_revision, total))
        self._signal()

    def _observe(self, command: int, data: bytes, *, depth: int, units: list[int]) -> None:
        if command not in _COMMANDS:
            return
        if depth > MAX_COMBINE_DEPTH or len(data) > BUSINESS_MESSAGE_LIMIT:
            raise ValueError("GAME_ACCOUNT_STATE_INVALID")
        message = self._classes[command].FromString(data)
        if command == 2016:
            units[0] += len(message.unit_lst)
            if units[0] > MAX_COMBINE_UNITS:
                raise ValueError("GAME_ACCOUNT_STATE_INVALID")
            for unit in message.unit_lst:
                if not unit.HasField("id") or unit.id <= 0:
                    raise ValueError("GAME_ACCOUNT_STATE_INVALID")
                self._observe(unit.id, unit.msg, depth=depth + 1, units=units)
            return
        if command == 2941:
            if len(message.item_list) > MAX_ITEM_ROWS:
                raise ValueError("GAME_ACCOUNT_STATE_INVALID")
            items, seen = {}, set()
            for row in message.item_list:
                item_id, uid, count = self._item(row, needs_count=True)
                if (item_id, uid) in seen:
                    raise ValueError("GAME_ACCOUNT_STATE_INVALID")
                seen.add((item_id, uid))
                if item_id == GOLD_ITEM_ID:
                    items[uid] = count
            self._gold_items = items
            self._publish()
            return
        if len(message.change_list) > MAX_ITEM_CHANGES:
            raise ValueError("GAME_ACCOUNT_STATE_INVALID")
        changed = False
        for change in message.change_list:
            kind = change.change_type
            if kind not in (1, 2, 3):
                raise ValueError("GAME_ACCOUNT_STATE_INVALID")
            item = self._item_class.FromString(change.data)
            item_id, uid, count = self._item(item, needs_count=kind != 2)
            if item_id != GOLD_ITEM_ID or not self._valid:
                continue
            if kind == 1 and uid not in self._gold_items:
                if len(self._gold_items) >= MAX_ITEM_ROWS:
                    raise ValueError("GAME_ACCOUNT_STATE_INVALID")
                self._gold_items[uid] = count
                changed = True
            elif kind == 2 and uid in self._gold_items:
                del self._gold_items[uid]
                changed = True
            elif kind == 3 and uid in self._gold_items:
                self._gold_items[uid] = count
                changed = True
        if changed:
            self._publish()

    async def wait_for_gold_balance(self, expected: int, after_revision: int, timeout: float = 5) -> bool:
        """Wait until a valid newer notification has shown the exact balance.

        Bounded history tolerates a subsequent income notification overtaking
        this waiter. It records observation, not a request's causal attribution.
        """
        if (type(expected) is not int or not 0 <= expected < 1 << 63
                or type(after_revision) is not int or after_revision < 0
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ValueError("GAME_ACCOUNT_STATE_INVALID")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._valid and not self._closed:
            if any(revision > after_revision and balance == expected
                   for revision, balance in self._gold_history):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                return False
        return False
