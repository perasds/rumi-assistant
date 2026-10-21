"""Single-use normal login orchestration and an explicitly gated read client.

SDK transports use line3 config and the default line1 password origin; enabling
a human CAPTCHA handler selects the verified config's first login origin. The
caller provides an already loaded SDK context, version client, independently
sourced game identity/environment and game transport. This module does not
load an account, device session or preferences, and never performs a purchase.
"""

import asyncio

import httpx
from pydantic import SecretStr

from ..errors import ApiError, provider_error
from ..sdk_app_config import SdkAppConfig
from ..sdk_device import SdkDeviceIdentity
from ..sdk_captcha import SdkCaptchaConfig
from ..sdk_http import SDK_REQUEST_STAGES, CaptchaHandler, SdkHttpClient, SdkResponseError
from ..sdk_parameters import SdkRequestContext
from ..sdk_saved_login import copy_login_state
from .account_state import GoldItemState
from .client import GameClientError
from .handshake import GameHandshake, GameHandshakeResult
from .home_state import HomeState
from .inventory_state import InventoryState
from .order_state import OrderState
from .order_codec import ORDER_CLIENT_VERSION, ORDER_HOT_VERSIONS
from .home_capacity_state import HomeCapacityState
from .season_state import SeasonState
from .market_query import GameMarketQuery
from .profile import ProjectClientContext, build_game_login_profile
from .profile_sync import LoginProfileCollector
from .version import VersionHttpClient
from .redbag_codec import REDBAG_CLIENT_VERSION, REDBAG_HOT_VERSIONS
from .redbag_operations import RedbagOperations
from .redbag_state import RedbagState


SDK_CONFIG_ORIGIN = "https://line3-sdk-center-login-sh.biligame.net"
SDK_LOGIN_ORIGIN = "https://line1-sdk-center-login-sh.biligame.net"
_GAME_ERRORS = frozenset({
    "GAME_CLIENT_CLOSED", "GAME_CLIENT_INVALID_COMMAND", "GAME_CLIENT_INVALID_TIMEOUT",
    "GAME_CLIENT_NOT_CONNECTED", "GAME_CONNECTION_FAILED", "GAME_NOTIFICATION_BUSY",
    "GAME_NOTIFICATION_OVERFLOW", "GAME_NOTIFICATION_TIMEOUT", "GAME_NOTIFICATION_UNREGISTERED",
    "GAME_PROTOCOL_INVALID", "GAME_REQUEST_REJECTED", "GAME_REQUEST_SEQUENCE_IN_USE",
    "GAME_REQUEST_TIMEOUT", "GAME_FRAME_INVALID", "GAME_TRANSPORT_CLOSED",
    "GAME_TRANSPORT_INVALID_TIMEOUT", "GAME_TRANSPORT_NOT_CONNECTED", "GAME_TRANSPORT_TIMEOUT",
    "GAME_TRANSPORT_UNAVAILABLE",
})
_RUNTIME_ERRORS = frozenset({
    "GAME_RUNTIME_INVALID", "GAME_RUNTIME_IDENTITY_MISMATCH", "GAME_RUNTIME_CLOSED",
    "GAME_RUNTIME_PROTOCOL_INVALID", "GAME_RUNTIME_FAILED",
})


class GameRuntimeError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _safe_failure(error: Exception) -> Exception:
    """Copy public error facts without retaining credential-bearing stack frames."""
    try:
        if isinstance(error, SdkResponseError):
            stage = error.request_stage
            if type(stage) is not str or stage not in SDK_REQUEST_STAGES:
                return provider_error(ApiError("AUTH_REJECTED", ""))
            number = error.response_code
            if type(number) is not int or not -(1 << 31) <= number < 1 << 31:
                number = None
            return SdkResponseError(response_code=number, request_stage=stage)
        if isinstance(error, ApiError):
            code = error.code if type(error.code) is str else ""
            return provider_error(ApiError(code, ""))
        if isinstance(error, GameClientError) and error.code in _GAME_ERRORS:
            number = error.response_code
            if number is not None and (type(number) is not int or not -(1 << 31) <= number < 1 << 31):
                number = None
            return GameClientError(error.code, response_code=number)
        if isinstance(error, GameRuntimeError) and error.code in _RUNTIME_ERRORS:
            return GameRuntimeError(error.code)
        if isinstance(error, (ValueError, TypeError)):
            return GameRuntimeError("GAME_RUNTIME_PROTOCOL_INVALID")
    except Exception:
        pass
    return GameRuntimeError("GAME_RUNTIME_FAILED")


async def _complete_cleanup(operation) -> tuple[bool, bool]:
    """Finish cleanup even if startup cancellation is repeated while closing."""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if task.cancelled():
        return True, True
    return task.result(), cancelled


class GameRuntime:
    def __init__(self, sdk_context: SdkRequestContext, version_client: VersionHttpClient,
                 client_context: ProjectClientContext, game_transport, *,
                 sdk_config_transport: httpx.AsyncBaseTransport | None = None,
                 sdk_login_transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 15.0, page_window: int = 40, desc: bool = False,
                 captcha_handler: CaptchaHandler | None = None,
                 profile_archive=None):
        self._sdk_context = sdk_context
        self._version_client = version_client
        self._client_context = client_context
        self._transport = game_transport
        self._config_transport = sdk_config_transport
        self._login_transport = sdk_login_transport
        self._captcha_handler = captcha_handler
        self._profile_archive = profile_archive
        self._timeout = timeout
        self._page_window = page_window
        self._desc = desc
        self._config_client = self._login_client = None
        self._handshake = self._market = None
        self._account_state = None
        self._home_state = None
        self._season_state = None
        self._inventory_state = self._order_state = self._home_capacity_state = None
        self._started = self._closed = False
        self._saved_login_state = None
        self._login_preparation = self._sms_ticket = None
        self._auth_lock = asyncio.Lock()
        self._version_warning = None
        self._redbag_state = self._redbag_operations = None
        self._redbag_url = self._redbag_nation = None
        self._redbag_language = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def result(self) -> GameHandshakeResult | None:
        return self._handshake.result if self._handshake is not None else None

    @property
    def profile_archive_status(self) -> str | None:
        return self._handshake.profile_archive_status if self._handshake is not None else None

    @property
    def version_warning(self) -> str | None:
        return self._version_warning

    @property
    def can_query(self) -> bool:
        return not self._closed and self._handshake is not None and self._handshake.can_query

    @property
    def account_state(self) -> GoldItemState | None:
        """Internal session state; closed transports cannot retain a balance."""
        state = self._account_state
        if state is not None and (self._closed or (self._handshake is not None and self._handshake.client.closed)):
            state.invalidate()
        return state

    @property
    def gold_balance(self) -> int | None:
        state = self.account_state
        return state.gold_balance if state is not None and self.can_query else None

    @property
    def home_state(self) -> HomeState | None:
        """Internal live Home state, invalidated when its session disconnects."""
        state = self._home_state
        if state is not None and (self._closed or (self._handshake is not None and self._handshake.client.closed)):
            state.invalidate()
        return state

    @property
    def home_client(self):
        """Public session client for Home operations after the normal login gate."""
        if not self.can_query:
            raise GameRuntimeError("GAME_RUNTIME_QUERY_UNAVAILABLE")
        return self._handshake.client

    @property
    def redbag_state(self):
        return self._redbag_state

    @property
    def redbag_available(self) -> bool:
        result = self.result
        return bool(self.can_query and self._redbag_state is not None
            and self._redbag_state.verified_version and not self._redbag_state.closed
            and self._redbag_url and result is not None
            and type(result.zone_id) is int and 0 < result.zone_id < 1 << 32
            and type(result.role_id) is int and 0 < result.role_id < 1 << 64)

    @property
    def redbag_operations(self):
        if self._redbag_state is not None and self._redbag_state.expired:
            raise ApiError('GAME_SESSION_EXPIRED', '游戏会话已过期，请重新登录', 401)
        if not self.redbag_available:
            raise ApiError('REDBAG_UNAVAILABLE', '当前版本或聊天连接信息尚未就绪', 409)
        if self._redbag_operations is None:
            result = self.result
            self._redbag_operations = RedbagOperations(self._handshake.client, self._redbag_state,
                chat_url=self._redbag_url, role_id=str(result.role_id),
                server_time=lambda: self.server_time_seconds, zone_id=result.zone_id,
                nation_code=self._redbag_nation, language=self._redbag_language, timeout=self._timeout)
        return self._redbag_operations

    async def redbag_stop(self):
        operations, self._redbag_operations = self._redbag_operations, None
        if operations is not None:
            await operations.stop()

    @property
    def inventory_state(self):
        state = self._inventory_state
        if state is not None and not self.can_query:
            state.invalidate()
        return state

    @property
    def order_state(self):
        state = self._order_state
        if state is not None and not self.can_query:
            state.invalidate()
        return state

    @property
    def home_capacity_state(self):
        state = self._home_capacity_state
        if state is not None and not self.can_query:
            state.invalidate()
        return state

    @property
    def season_snapshot(self) -> dict:
        state = self._season_state
        if state is None:
            return {'valid': False, 'current_season_id': None, 'unavailable_reason': 'schema_unavailable'}
        if not self.can_query:
            state.clear()
            return state.snapshot()
        try:
            now = self.server_time_seconds
        except GameRuntimeError:
            now = None
        return state.snapshot(now)

    @property
    def server_time_seconds(self) -> int:
        if not self.can_query:
            raise GameRuntimeError("GAME_RUNTIME_QUERY_UNAVAILABLE")
        seconds = self._handshake.server_time_seconds
        if type(seconds) is not int or seconds <= 0:
            raise GameRuntimeError("GAME_RUNTIME_CLOCK_UNAVAILABLE")
        return seconds

    @property
    def market(self) -> GameMarketQuery:
        """Access is a local gate, not proof of server inventory or buy eligibility."""
        if not self.can_query:
            raise GameRuntimeError("GAME_RUNTIME_QUERY_UNAVAILABLE")
        if self._market is None:
            self._market = GameMarketQuery(self._handshake.client,
                                           page_window=self._page_window, desc=self._desc)
        return self._market

    def _require_open(self) -> None:
        if self._closed:
            raise GameRuntimeError("GAME_RUNTIME_CLOSED")

    def _validate(self) -> None:
        context = self._sdk_context
        if (type(context) is not SdkRequestContext or type(context.app_config) is not SdkAppConfig
                or type(context.identity) is not SdkDeviceIdentity
                or type(context.app_config.app_key) is not SecretStr
                or type(context.app_config.app_key.get_secret_value()) is not str
                or not context.app_config.app_key.get_secret_value()
                or type(self._version_client) is not VersionHttpClient
                or type(self._client_context) is not ProjectClientContext
                or type(self._timeout) not in (int, float) or not 0 < self._timeout <= 60
                or type(self._page_window) is not int or not 0 < self._page_window < (1 << 31)-1
                or type(self._desc) is not bool
                or (self._captcha_handler is not None and not callable(self._captcha_handler))):
            raise GameRuntimeError("GAME_RUNTIME_INVALID")
        if (self._version_client.identity is not context.identity
                or self._version_client.device_id != self._client_context.game_device_id):
            raise GameRuntimeError("GAME_RUNTIME_IDENTITY_MISMATCH")

    async def start(self, account: str, password: str) -> GameHandshakeResult:
        try:
            async with self._auth_lock:
                return await self._start(account, password)
        finally:
            account = password = ""

    async def send_sms(self, mobile: str, country_code: str) -> None:
        try:
            async with self._auth_lock:
                await self._send_sms(mobile, country_code)
        finally:
            mobile = country_code = ""

    async def _send_sms(self, mobile: str, country_code: str) -> None:
        self._require_open()
        if self._started:
            raise GameRuntimeError("GAME_RUNTIME_ALREADY_STARTED")
        self._sms_ticket = None
        failure, cancelled, captcha_key = None, False, None
        try:
            if not all(type(value) is str and value for value in (mobile, country_code)):
                raise ApiError("AUTH_REJECTED", "", 401)
            _, _, captcha_config = await self._prepare_login()
            captcha_key = await self._login_client.send_phone_sms(mobile, country_code,
                captcha_config=captcha_config, captcha_handler=self._captcha_handler)
            self._require_open()
            if type(captcha_key) is not SecretStr or not captcha_key.get_secret_value():
                raise ValueError("Invalid SMS ticket")
            self._sms_ticket = (mobile, country_code, captcha_key)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            failure = _safe_failure(error)
        finally:
            mobile = country_code = ""
            captcha_key = captcha_config = None
        if failure is not None or cancelled:
            self._closed = True
            _, interrupted = await _complete_cleanup(self._close_http())
            _, game_interrupted = await _complete_cleanup(self._close_game())
            if cancelled or interrupted or game_interrupted:
                raise asyncio.CancelledError()
            raise failure

    async def start_sms(self, mobile: str, country_code: str, otp: str) -> GameHandshakeResult:
        try:
            async with self._auth_lock:
                return await self._start("", "", sms_login=(mobile, country_code, otp))
        finally:
            mobile = country_code = otp = ""

    async def _prepare_login(self):
        if self._login_preparation is not None:
            return self._login_preparation
        self._validate()
        context = self._sdk_context
        version = await self._version_client.fetch()
        self._require_open()
        adapted_hot_version = max(ORDER_HOT_VERSIONS)
        if (version.version, version.hot_version) != (ORDER_CLIENT_VERSION, adapted_hot_version):
            self._version_warning = (
                f'工具适配版本：{ORDER_CLIENT_VERSION} / 热更 {adapted_hot_version}；'
                f'游戏实际版本：{version.version} / 热更 {version.hot_version}')
        self._config_client = SdkHttpClient(SDK_CONFIG_ORIGIN,
            context.app_config.app_key.get_secret_value(), purpose="config",
            common_parameters=context.common_parameters, extra_headers=context.extra_headers,
            transport=self._config_transport)
        config = await self._config_client.fetch_config()
        self._require_open()
        configured = context.with_config(config)
        captcha_config = (SdkCaptchaConfig.from_response(config)
                          if self._captcha_handler is not None else None)
        self._login_client = SdkHttpClient(captcha_config.login_origin if captcha_config is not None else SDK_LOGIN_ORIGIN,
            configured.app_config.app_key.get_secret_value(),
            common_parameters=configured.common_parameters, extra_headers=configured.extra_headers,
            sms_feign_sign=configured.sms_feign_sign, transport=self._login_transport)
        self._login_preparation = (configured, version, captcha_config)
        return self._login_preparation

    def export_login_state(self) -> dict | None:
        if not self.can_query or self._saved_login_state is None:
            return None
        return copy_login_state(self._saved_login_state)

    async def restore_login(self, state: dict) -> GameHandshakeResult:
        try:
            async with self._auth_lock:
                return await self._start("", "", login_state=copy_login_state(state))
        finally:
            state = None

    async def _start(self, account: str, password: str, *, login_state=None, sms_login=None) -> GameHandshakeResult:
        if self._started:
            raise GameRuntimeError("GAME_RUNTIME_ALREADY_STARTED")
        self._require_open()
        failed, cancelled, facts = None, False, None
        retry_sms = False
        context = configured = version = credentials = profile = config = None
        captcha_config = sms_ticket = None
        try:
            self._validate()
            if login_state is not None:
                identity = login_state["identity"]
                if (identity["udid"] != self._sdk_context.identity.udid
                        or identity["bd_id"] != self._sdk_context.identity.bd_id
                        or identity["game_device_id"] != self._client_context.game_device_id):
                    raise ApiError("AUTH_REJECTED", "保存的登录信息已失效，请重新登录", 401)
            elif sms_login is not None:
                sms_ticket = self._sms_ticket
                if (sms_ticket is None or sms_ticket[:2] != sms_login[:2]
                        or not all(type(value) is str and value for value in sms_login)):
                    raise ApiError("AUTH_REJECTED", "", 401)
            elif type(account) is not str or not account or type(password) is not str or not password:
                raise ApiError("AUTH_REJECTED", "", 401)
            if sms_login is None:
                self._sms_ticket = None
            configured, version, captcha_config = await self._prepare_login()
            if sms_login is not None:
                credentials = await self._login_client.login_phone_sms(*sms_login, sms_ticket[2],
                    captcha_config=captcha_config, captcha_handler=self._captcha_handler)
            elif login_state is None:
                credentials = await self._login_client.login(account, password,
                    captcha_config=captcha_config, captcha_handler=self._captcha_handler)
            else:
                credentials = await self._login_client.login_token(login_state)
            password = account = ""
            self._require_open()
            self._sms_ticket = None
            self._started = True
            profile = build_game_login_profile(configured, credentials.game_user,
                                                version, self._client_context)
            if version.version == REDBAG_CLIENT_VERSION and version.hot_version in REDBAG_HOT_VERSIONS:
                try:
                    self._redbag_state = RedbagState(verified_version=True)
                except ValueError:
                    # Optional chat resources cannot interrupt the established login path.
                    self._redbag_state = None
            self._redbag_nation = profile.country_code
            self._redbag_language = self._client_context.language_type
            # Hot-update differences are advisory; each consumer still validates its native baseline.
            self._account_state = GoldItemState(verified_version=True)
            self._home_state = HomeState(verified_version=True)
            self._inventory_state = InventoryState(verified_version=True)
            self._order_state = OrderState(verified_version=True)
            self._home_capacity_state = HomeCapacityState(verified_version=True)
            self._season_state = SeasonState()
            collector = LoginProfileCollector(self._profile_archive) if self._profile_archive is not None else None
            self._handshake = GameHandshake(self._transport, timeout=self._timeout,
                                            profile_collector=collector, account_state=self._account_state,
                                            home_state=self._home_state, inventory_state=self._inventory_state,
                                            order_state=self._order_state, home_capacity_state=self._home_capacity_state,
                                            season_state=self._season_state, redbag_state=self._redbag_state)
            self._profile_archive = None
            self._transport = None  # The handshake now owns this transport.
            facts = await self._handshake.start(profile)
            self._redbag_url = version.chat_gate_url_for(facts.zone_id)
            self._require_open()
            if self.can_query and login_state is not None:
                self._saved_login_state = copy_login_state(login_state)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            retry_sms = (sms_login is not None and isinstance(error, SdkResponseError)
                         and error.request_stage == "sdk_sms_login" and not self._closed)
            failed = (ApiError("AUTH_SMS_REJECTED", "验证码未通过验证，请检查后重试", 401)
                      if retry_sms else _safe_failure(error))
        finally:
            # Do not retain caller credentials or SDK/profile objects in errors.
            account = password = ""
            context = configured = version = credentials = profile = config = None
            captcha_config = None
            login_state = None
            sms_login = sms_ticket = None

        if retry_sms:
            raise failed
        close_failed, interrupted = await _complete_cleanup(self._close_http())
        cancelled = cancelled or interrupted
        if failed is not None or cancelled or close_failed or self._closed:
            self._closed = True
            game_failed, interrupted = await _complete_cleanup(self._close_game())
            cancelled = cancelled or interrupted
            if failed is None and not cancelled:
                failed = GameRuntimeError("GAME_RUNTIME_CLOSE_FAILED" if close_failed or game_failed
                                          else "GAME_RUNTIME_CLOSED")
        if cancelled:
            raise asyncio.CancelledError()
        if failed is not None:
            raise failed
        return facts

    async def _close_http(self) -> bool:
        self._login_preparation = self._sms_ticket = None
        failed = False
        for name in ("_config_client", "_login_client", "_version_client"):
            client = getattr(self, name)
            setattr(self, name, None)
            if client is not None:
                try:
                    await client.close()
                except (Exception, asyncio.CancelledError):
                    failed = True
        self._sdk_context = self._client_context = None
        self._config_transport = self._login_transport = None
        self._captcha_handler = None
        return failed

    async def _close_game(self) -> bool:
        self._saved_login_state = None
        redbag_failed = False
        try:
            await self.redbag_stop()
        except (Exception, asyncio.CancelledError):
            redbag_failed = True
        if self._redbag_state is not None:
            self._redbag_state.close()
        self._redbag_state = None
        self._redbag_url = self._redbag_nation = self._redbag_language = None
        if self._season_state is not None:
            self._season_state.clear()
        for name in ('_inventory_state', '_order_state', '_home_capacity_state'):
            consumer = getattr(self, name)
            setattr(self, name, None)
            if consumer is not None:
                consumer.close()
        handshake, transport = self._handshake, self._transport
        state, self._account_state = self._account_state, None
        if state is not None:
            state.close()
        home_state, self._home_state = self._home_state, None
        if home_state is not None:
            home_state.close()
        self._handshake = self._market = self._transport = None
        self._profile_archive = None
        failed = redbag_failed
        try:
            if handshake is not None:
                await handshake.close()
            elif transport is not None:
                await transport.close()
        except (Exception, asyncio.CancelledError):
            failed = True
        return failed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._season_state is not None:
            self._season_state.clear()
        if self._account_state is not None:
            self._account_state.close()
        if self._home_state is not None:
            self._home_state.close()
        for consumer in (self._inventory_state, self._order_state, self._home_capacity_state):
            if consumer is not None:
                consumer.close()
        http_failed, cancelled = await _complete_cleanup(self._close_http())
        game_failed, interrupted = await _complete_cleanup(self._close_game())
        if cancelled or interrupted:
            raise asyncio.CancelledError()
        if http_failed or game_failed:
            raise GameRuntimeError("GAME_RUNTIME_CLOSE_FAILED")
