"""Normal account-free VSC request from the fixed APK's public source.

Main.BuildVersionParams 0x6000163 supplies the field order. Plugins HttpManager
0x60001d0/0x60001d4/0x60001d5 supplies headers, UTF-8 SHA256 and the old counter.
Root.GmUrl in level0 supplies the three allowed origins. Source evidence is in
artifacts/rumi/pet-catalog/vsc-public-version-query.sanitized.json.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from ..errors import ApiError
from ..sdk_device import SdkDeviceIdentity


VERSION_ORIGINS = frozenset({
    "https://l1-prod-cn-ob-vs-lumigo.bilibiligame.net",
    "https://l3-prod-cn-ob-vs-lumigo.bilibiligame.net",
    "https://lumigo-cn-prod-vs.bilibiligame.net",
})
VERSION_PATH = "/api/v1/vs/client/VscQueryVersion"
_PUBLIC_VERSION = re.compile(r"[0-9]+(?:[._][0-9x]+)*\Z")


@dataclass(frozen=True, kw_only=True, repr=False)
class VersionRequestContext:
    """Caller-owned signing material and an explicitly sourced fresh profile.

    profile_source records the profile's source declaration. This client only
    supports a fresh independent cache: aid is empty, SDKDataCenter's initial
    nation_code is CN, and the DTO's unassigned queueToken remains null.
    device_id is supplied separately from SDK HTTP identity.udid and bd_id:
    Game SDKData.DeviceId reads curBuvid/getBuvidV2(CACHE). String.Empty is a
    source-supported initialization state, not an after-consent native fallback.
    identity is retained for subsequent SDK requests; no device or preference
    state is read and no replacement identity is generated here.
    """

    gm_secret: SecretStr
    identity: SdkDeviceIdentity
    device_id: str
    channel: str
    version: str
    build_version: str
    profile_source: str


@dataclass(frozen=True, repr=False)
class ChatServerInfo:
    server_id: int
    chat_gate_url: str


@dataclass(frozen=True)
class VersionResult:
    version: str
    hot_version: int
    chat_servers: tuple[ChatServerInfo, ...] = ()

    def chat_gate_url_for(self, server_id: int) -> str | None:
        if type(server_id) is not int:
            return None
        return next((row.chat_gate_url for row in self.chat_servers if row.server_id == server_id), None)


def _upstream_failed() -> ApiError:
    return ApiError("AUTH_UPSTREAM_FAILED", "游戏版本服务暂不可用", 503, True)


def _request_unverified() -> ApiError:
    return ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏版本请求参数尚未确认")


def _validate_context(context: VersionRequestContext) -> None:
    valid = (type(context) is VersionRequestContext
             and type(context.gm_secret) is SecretStr
             and type(context.gm_secret.get_secret_value()) is str
             and bool(context.gm_secret.get_secret_value())
             and type(context.identity) is SdkDeviceIdentity
             and type(context.device_id) is str
             and all(type(value) is str and bool(value) for value in
                     (context.identity.udid, context.identity.bd_id, context.channel,
                      context.version, context.build_version, context.profile_source))
             and _PUBLIC_VERSION.fullmatch(context.version) is not None)
    if not valid:
        raise ValueError("Version request context is incomplete")


def _nonnegative_int32(value: object) -> int | None:
    if type(value) is int:
        return value if 0 <= value <= 2147483647 else None
    if type(value) is not str or re.fullmatch(r"[0-9]+", value) is None:
        return None
    digits = value.lstrip("0") or "0"
    if len(digits) > 10:
        return None
    number = int(digits)
    return number if number <= 2147483647 else None


def _public_result(payload: object) -> VersionResult | None:
    if type(payload) is not dict or type(payload.get("code")) not in (int, str):
        return None
    if payload["code"] not in (0, "0") or type(payload.get("data")) is not dict:
        return None
    data = payload["data"]
    version, hot = data.get("version"), data.get("hotVersion")
    if (type(version) is not str or len(version) > 64
            or _PUBLIC_VERSION.fullmatch(version) is None or type(hot) is not dict):
        return None
    number = _nonnegative_int32(hot.get("version"))
    return None if number is None else VersionResult(version, number, _chat_servers(data.get('serverList')))


def _chat_servers(value: object) -> tuple[ChatServerInfo, ...]:
    """Optional VSC server metadata; invalid chat data must not break login."""
    if type(value) is not list or len(value) > 1024:
        return ()
    rows, seen, duplicates = [], set(), set()
    for item in value:
        if type(item) is not dict:
            continue
        server_id = _nonnegative_int32(item.get('serverId'))
        if server_id is None:
            continue
        if server_id in seen:
            duplicates.add(server_id)
        seen.add(server_id)
        url = item.get('chat_gate_url')
        if type(url) is not str or not 0 < len(url) <= 2048 or any(c.isspace() for c in url):
            continue
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in {'ws', 'wss'} or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.fragment
                    or (parsed.port is not None and not 0 < parsed.port <= 65535)):
                continue
        except ValueError:
            continue
        rows.append(ChatServerInfo(server_id, url))
    return tuple(row for row in rows if row.server_id not in duplicates)


class VersionHttpClient:
    def __init__(self, origin: str, context: VersionRequestContext, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock_seconds: Callable[[], int] | None = None):
        valid = False
        try:
            url = urlsplit(origin)
            valid = (url.scheme == "https" and "https://" + (url.hostname or "") in VERSION_ORIGINS
                     and url.port in (None, 443) and url.path in ("", "/")
                     and not url.query and not url.fragment and url.username is None)
        except (ValueError, TypeError, AttributeError):
            pass
        if not valid:
            raise ValueError("Invalid game version origin")
        _validate_context(context)
        if clock_seconds is not None and not callable(clock_seconds):
            raise ValueError("Invalid game version clock")
        self._identity = context.identity
        self._device_id = context.device_id
        self._context = context
        self._clock_seconds = clock_seconds or (lambda: time.time_ns() // 1000000000)
        self._session_id = 0
        self._closed = False
        self._http = httpx.AsyncClient(base_url=origin.rstrip("/"), transport=transport,
                                      timeout=15, follow_redirects=False)

    @property
    def identity(self) -> SdkDeviceIdentity:
        return self._identity

    @property
    def device_id(self) -> str:
        """Original caller declaration, retained after private context cleanup."""
        return self._device_id

    async def fetch(self) -> VersionResult:
        if self._closed:
            raise _request_unverified()
        body, headers = None, None
        try:
            timestamp = self._clock_seconds()
            if type(timestamp) is not int or not -(2**63) <= timestamp < 2**63:
                raise ValueError("Invalid Unix seconds")
            context = self._context
            payload = {"channel": context.channel, "version": context.version,
                       "buildVersion": context.build_version, "timestamp": timestamp,
                       "aid": "", "deviceId": context.device_id,
                       "nation_code": "CN", "queueToken": None}
            prepared = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            sign = hashlib.sha256(prepared + context.gm_secret.get_secret_value().encode("utf-8")).hexdigest()
            headers = {"X-App-Id": "1000069", "X-App-Client-Sign": sign,
                       "Content-Type": "application/json;charset=utf-8", "sessionId": str(self._session_id)}
            self._session_id = (self._session_id + 1) % (2**64)
            body = prepared
        except Exception:
            pass
        if body is None:
            raise _request_unverified()

        result = None
        try:
            response = await self._http.post(VERSION_PATH, content=body, headers=headers)
            if response.status_code == 200 and len(response.content) <= 1048576:
                result = _public_result(response.json())
        except (httpx.RequestError, ValueError, RuntimeError):
            pass
        if result is None:
            raise _upstream_failed()
        return result

    async def close(self) -> None:
        self._closed = True
        failed = False
        try:
            await self._http.aclose()
        except Exception:
            failed = True
        finally:
            self._http.cookies.clear()
            self._context = None
        if failed:
            raise _upstream_failed()
