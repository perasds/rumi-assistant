"""Listen to new chat cards and make one guarded claim on the main connection."""
import asyncio
from collections import OrderedDict

from ..errors import ApiError
from ..redbag_models import RedbagCandidate, RedbagReceipt
from .client import GameClientError
from .redbag_chat import RedbagChat, validate_chat_url
from .redbag_codec import RedbagProtoCodec
from .redbag_state import RedbagState


_OUTCOMES = {2103: "missing", 2104: "expired", 2106: "self",
             2107: "already_claimed", 2109: "full"}
_FAILURES = {2108: "REDBAG_DAILY_LIMIT", 2113: "REDBAG_CONDITION_NOT_MET",
             1017: "REDBAG_RATE_LIMITED", -5: "GAME_SESSION_EXPIRED",
             -8: "GAME_SESSION_EXPIRED", -17: "REDBAG_UNAVAILABLE",
             -20: "REDBAG_UNAVAILABLE", -23: "REDBAG_UNAVAILABLE"}
_MESSAGES = {"REDBAG_DAILY_LIMIT": "今日红包领取已达上限",
    "REDBAG_CONDITION_NOT_MET": "尚未满足红包领取条件", "REDBAG_RATE_LIMITED": "红包请求过于频繁",
    "GAME_SESSION_EXPIRED": "游戏会话已失效，请重新登录", "REDBAG_UNAVAILABLE": "红包功能尚未就绪",
    "REDBAG_UNCERTAIN": "红包领取结果尚未确认", "REDBAG_QUEUE_FULL": "红包待处理队列已满",
    "REDBAG_UPSTREAM_FAILED": "红包服务暂不可用"}


def _error(code):
    return ApiError(code, _MESSAGES[code], 401 if code == "GAME_SESSION_EXPIRED" else 503)


def _identity(value):
    return (type(value) is str and value.isascii() and value.isdecimal()
            and len(value) <= 20 and 0 < int(value) < 1 << 64 and str(int(value)) == value)


def _share_id(value):
    return type(value) is str and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


def _stamina(rewards):
    if not 0 < len(rewards) <= 64:
        raise ValueError
    total = 0
    for item in rewards:
        if (not item.HasField("item_id") or not item.HasField("count")
                or item.item_id <= 0 or item.count <= 0):
            raise ValueError
        if item.item_id == 10004:
            total += item.count
    if total >= 1 << 63:
        raise ValueError
    return total


class RedbagOperations:
    def __init__(self, client, state, *, chat_url, role_id, server_time, zone_id,
                 nation_code, language, timeout=15, connect_factory=None):
        validate_chat_url(chat_url)
        if (not isinstance(state, RedbagState) or not _identity(role_id) or not callable(server_time)
                or type(zone_id) is not int or not 0 < zone_id < 1 << 32
                or type(language) is not int or not 0 <= language < 1 << 32
                or type(nation_code) is not str or not 0 < len(nation_code) <= 16
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 60):
            raise _error("REDBAG_UNAVAILABLE")
        self._client, self._state = client, state
        self._url, self._role_id, self._now = chat_url, role_id, server_time
        self._zone, self._nation, self._language = zone_id, nation_code, language
        self._timeout = timeout
        self._codec = RedbagProtoCodec()
        self._queue = asyncio.Queue(maxsize=128)
        self._seen, self._updates = OrderedDict(), OrderedDict()
        self._confirmed = OrderedDict()
        self._failure = None
        self._ready = False
        self._stopped = False
        self._chat = RedbagChat(self._codec, self._receive, self._fail,
                               timeout=timeout, connect_factory=connect_factory)

    def _main_available(self):
        return (not getattr(self._client, "closed", True) and not self._state.closed
                and self._state.verified_version)

    def _require_main(self):
        if getattr(self._client, "closed", True) or self._state.closed or self._state.expired:
            self._state.invalidate(expired=True)
            raise _error("GAME_SESSION_EXPIRED")
        if not self._state.verified_version:
            raise _error("REDBAG_UNAVAILABLE")

    async def start(self):
        self._require_main()
        if self._stopped:
            raise _error("REDBAG_UNAVAILABLE")
        if self._ready:
            return
        try:
            if not self._state.has_auth:
                response = await self._client.request(23938, 23939, self._codec.encode(23938),
                    before_send=lambda: not self._stopped and self._main_available())
                self._state.observe(response)
            params = await self._state.auth(timeout=self._timeout)
            auth = self._codec.encode(23920, params=params)
            params.clear()
            await self._chat.start(self._url)
            response = self._codec.decode(23921, await self._chat.request(23920, 23921, auth))
            auth = b""
            if response.code != 0:
                raise _error("REDBAG_UPSTREAM_FAILED")
            # Ordinary RequestChatInfo calls JoinWorldChannel(0). Its other calls pull history.
            join = self._codec.encode(23903, channel_id=0, nation_code=self._nation,
                                      language=self._language, zone_id=self._zone)
            response = self._codec.decode(23904, await self._chat.request(23903, 23904, join))
            if response.error_code:
                raise _error("REDBAG_UPSTREAM_FAILED")
            # Join ACK messages, history notifications and session lists never enter the live queue.
            self._require_main()
            if self._stopped:
                raise _error("REDBAG_UNAVAILABLE")
            self._ready = True
            self._chat.heartbeat()
        except GameClientError as exc:
            code = _FAILURES.get(exc.response_code, "REDBAG_UPSTREAM_FAILED")
            await self._chat.stop()
            raise _error(code) from None
        except ApiError:
            await self._chat.stop()
            raise
        except asyncio.CancelledError:
            await self._chat.stop()
            raise
        except Exception:
            await self._chat.stop()
            raise _error("REDBAG_UPSTREAM_FAILED") from None

    def _fail(self, code):
        if self._failure is not None or self._stopped:
            return
        self._failure = code
        self._ready = False
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    def _remember(self, mapping, key, value, limit=1024):
        mapping[key] = value
        mapping.move_to_end(key)
        if len(mapping) > limit:
            mapping.popitem(last=False)

    def _receive(self, command, payload):
        if self._stopped or self._failure or command not in {23909, 23940}:
            return
        if not self._ready:
            return
        try:
            message = self._codec.decode(command, payload)
            if command == 23940:
                if _share_id(message.share_str_id):
                    self._remember(self._updates, message.share_str_id,
                        (message.taken_count, message.max_count, message.expire_time))
                return
            if len(message.messages) > 256:
                self._fail("REDBAG_QUEUE_FULL")
                return
            for row in message.messages:
                if (row.msg_type != 13 or not row.HasField("common_info")
                        or row.common_info.share_type != 3 or not row.common_info.param):
                    continue
                try:
                    card = self._codec.decode("HomeVisitShareCardParam", row.common_info.param)
                except ValueError:
                    continue
                if (not _share_id(card.share_str_id) or not card.sender_uid
                        or str(card.sender_uid) == self._role_id or card.share_str_id in self._seen
                        or card.expire_time <= self._now() or card.max_count <= 0
                        or card.taken_count < 0 or card.taken_count >= card.max_count):
                    continue
                candidate = RedbagCandidate(card.share_str_id, str(card.sender_uid), card.expire_time)
                if self._queue.full():
                    self._fail("REDBAG_QUEUE_FULL")
                    return
                self._remember(self._seen, card.share_str_id, True)
                self._queue.put_nowait(candidate)
        except (ValueError, TypeError):
            self._fail("REDBAG_UPSTREAM_FAILED")

    async def next(self):
        while True:
            self._require_main()
            if self._failure:
                raise _error(self._failure)
            if self._stopped or not self._ready:
                raise _error("REDBAG_UNAVAILABLE")
            try:
                # Local liveness check; no extra network request is made.
                candidate = await asyncio.wait_for(self._queue.get(), 1)
            except TimeoutError:
                continue
            if candidate is None:
                continue
            update = self._updates.get(candidate.id)
            if update is not None and (update[1] > 0 and update[0] >= update[1]
                                       or update[2] > 0 and update[2] <= self._now()):
                continue
            if candidate.expires_at and candidate.expires_at <= self._now():
                continue
            return candidate

    def _rejection(self, code):
        if code in _OUTCOMES:
            return RedbagReceipt(_OUTCOMES[code])
        failure = _FAILURES.get(code, "REDBAG_UPSTREAM_FAILED")
        if failure == "GAME_SESSION_EXPIRED":
            self._state.invalidate(expired=True)
        elif failure == "REDBAG_UNAVAILABLE":
            self._state.invalidate()
        raise _error(failure)

    async def claim(self, candidate, *, stop_requested):
        if not isinstance(candidate, RedbagCandidate) or not _share_id(candidate.id) or not _identity(candidate.sender_role_id):
            raise _error("REDBAG_UNAVAILABLE")
        if self._stopped or stop_requested():
            return None
        self._require_main()
        if not self._ready or self._failure:
            raise _error(self._failure or "REDBAG_UNAVAILABLE")
        if candidate.sender_role_id == self._role_id:
            return RedbagReceipt("self")
        if candidate.id in self._confirmed:
            return RedbagReceipt("already_claimed")
        if candidate.expires_at and candidate.expires_at <= self._now():
            return RedbagReceipt("expired")
        sent = False
        def before_send():
            nonlocal sent
            allowed = (not self._stopped and not stop_requested() and self._main_available()
                and self._ready and not self._failure
                and (not candidate.expires_at or candidate.expires_at > self._now()))
            if allowed:
                sent = True
            return allowed
        response = None
        failure = None
        try:
            response = await self._client.request(4011, 4012, self._codec.encode(4011, id=candidate.id),
                                                  before_send=before_send)
        except GameClientError as exc:
            if exc.code == "GAME_REQUEST_NOT_SENT":
                if self._stopped or stop_requested():
                    return None
                self._require_main()
                if candidate.expires_at and candidate.expires_at <= self._now():
                    return RedbagReceipt("expired")
                failure = self._failure or "REDBAG_UNAVAILABLE"
            elif exc.code == "GAME_REQUEST_REJECTED" and type(exc.response_code) is int:
                return self._rejection(exc.response_code)
            else:
                failure = "REDBAG_UNCERTAIN" if sent else "REDBAG_UPSTREAM_FAILED"
        except (Exception, asyncio.CancelledError):
            failure = "REDBAG_UNCERTAIN" if sent else "REDBAG_UPSTREAM_FAILED"
        if failure:
            raise _error(failure)
        if response.code:
            return self._rejection(response.code)
        valid = None
        try:
            ack = self._codec.decode(4012, response.data)
            if response.command == 4012 and ack.id == candidate.id:
                valid = _stamina(ack.reward_list)
        except (ValueError, AttributeError):
            pass
        if valid is None:
            raise _error("REDBAG_UNCERTAIN")
        self._remember(self._confirmed, candidate.id, True)
        return RedbagReceipt("claimed", valid)

    async def reconcile(self, candidate):
        self._require_main()
        if not isinstance(candidate, RedbagCandidate) or not _share_id(candidate.id):
            raise _error("REDBAG_UNCERTAIN")
        response = None
        failed = False
        try:
            response = await self._client.request(4009, 4010, self._codec.encode(4009, id=candidate.id),
                                                  before_send=self._main_available)
        except GameClientError as exc:
            if exc.code == "GAME_REQUEST_REJECTED" and type(exc.response_code) is int:
                return self._rejection(exc.response_code)
            failed = True
        except Exception:
            failed = True
        if failed:
            raise _error("REDBAG_UNCERTAIN")
        if response.code:
            return self._rejection(response.code)
        result = None
        try:
            ack = self._codec.decode(4010, response.data)
            role = int(self._role_id)
            if response.command == 4010 and ack.HasField("share") and role in ack.share.roles:
                result = _stamina(ack.share.roles[role].awards)
        except (ValueError, AttributeError):
            pass
        if result is None:
            return None
        self._remember(self._confirmed, candidate.id, True)
        return RedbagReceipt("claimed", result)

    async def stop(self):
        self._stopped, self._ready = True, False
        await self._chat.stop()
        self._state.clear_auth()
        self._seen.clear()
        self._updates.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)
