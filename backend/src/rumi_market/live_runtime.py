"""Fresh session-owned live clients, using the successfully verified path.

Capabilities create no context or network client. First login loads the pinned
APK and VSC assets, then GameRuntime verifies the current SDK config rule.
Project UUID/environment declarations match the successful local probe; they
do not claim to reproduce an official device's BUVID or cached session.
"""

import asyncio
from dataclasses import replace
import importlib.util
from pathlib import Path
import platform
import time
import uuid

from .errors import ApiError, auth_required, provider_error
from .game.profile import ProjectClientContext
from .game.runtime import GameRuntime
from .game.shop_catalog import load_shop_catalog
from .game.transport import GameTransport
from .game.version import VersionHttpClient, VersionRequestContext
from .game.version_assets import load_version_assets
from .game_provider import IndependentGameProvider
from .models import Capabilities
from .material_paths import DEFAULT_APK, resolve_apk_path
from .profile_archive import AccountProfileArchive
from .sdk_parameters import SdkRequestContext
from .sdk_device import SdkDeviceIdentity
from .sdk_saved_login import copy_login_state


_MANUAL_SCRIPT = Path(__file__).resolve().parents[3] / "scripts/manual_captcha.py"


def _safe_error(error):
    try:
        if isinstance(error, ApiError) and type(error.code) is str:
            return provider_error(ApiError(error.code, ""))
    except Exception:
        pass
    return ApiError("AUTH_UPSTREAM_FAILED", "游戏认证服务暂不可用", retryable=True)


async def _quiet_close(resource):
    if resource is not None:
        try:
            await resource.close()
        except (Exception, asyncio.CancelledError):
            pass


def _load_manual_captcha():
    spec = importlib.util.spec_from_file_location("_rumi_live_manual_captcha", _MANUAL_SCRIPT)
    if spec is None or spec.loader is None:
        raise ValueError("Manual CAPTCHA unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.manual_captcha


async def manual_captcha(challenge, config):
    """Load the normal local human bridge only on the SDK's 200007 branch."""
    result, failed, cancelled = None, None, False
    handler = None
    try:
        handler = _load_manual_captcha()
        result = await handler(challenge, config)
    except asyncio.CancelledError:
        cancelled = True
    except ApiError as error:
        failed = _safe_error(error)
    except Exception:
        failed = ApiError("AUTH_CAPTCHA_UNAVAILABLE", "本机验证窗口暂不可用")
    finally:
        handler = challenge = config = None
    if cancelled:
        result = None
        raise asyncio.CancelledError()
    if failed is not None:
        result = None
        raise failed
    return result


async def build_live_runtime(apk_path=None, *, login_state=None):
    """Validate/load the fixed source once per login and transfer ownership."""
    version = transport = None
    saved = copy_login_state(login_state) if login_state is not None else None
    result, failed = None, False
    try:
        apk_path = resolve_apk_path(apk_path)
        sdk = SdkRequestContext.from_apk(Path(apk_path))
        if saved is not None:
            sdk = replace(sdk, identity=SdkDeviceIdentity(saved["identity"]["udid"],
                                                        saved["identity"]["bd_id"]))
        assets = load_version_assets(Path(apk_path))
        shop = load_shop_catalog()
        client = ProjectClientContext(
            game_device_id=(saved["identity"]["game_device_id"] if saved is not None else str(uuid.uuid4())),
            device_id_source="project_generated_uuid_for_independent_client",
            model="RumiMarket Python client", os_version=platform.system() + " " + platform.release(),
            network="unknown", time_zone_hours=int(-time.timezone / 3600),
            language_type=1, platform="Android")
        version = VersionHttpClient(assets.origins[0], VersionRequestContext(
            gm_secret=assets.gm_secret, identity=sdk.identity, device_id=client.game_device_id,
            channel=assets.channel, version=assets.version, build_version=assets.build_version,
            profile_source=assets.profile_source + ";" + client.device_id_source))
        transport = GameTransport(timeout=15, keep_idle=True)
        result = (GameRuntime(sdk, version, client, transport, captcha_handler=manual_captcha,
                              profile_archive=AccountProfileArchive()), shop)
    except Exception:
        failed = True
    if failed:
        await _quiet_close(version)
        await _quiet_close(transport)
        sdk = assets = client = shop = version = transport = None
        login_state = saved = None
        raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录资源未通过校验")
    return result


class LiveProviderFactory:
    def __init__(self, *, apk_path=None, runtime_builder=None):
        if runtime_builder is not None and not callable(runtime_builder):
            raise ValueError("Invalid live runtime builder")
        self._builder = runtime_builder or (lambda **kwargs: build_live_runtime(apk_path, **kwargs))

    def __call__(self):
        return _LazyLiveProvider(self._builder)


class _LazyLiveProvider:
    def __init__(self, builder):
        self._builder = builder
        self._delegate = None
        self._closed = self._started = False
        self._auth_lock = asyncio.Lock()

    def capabilities(self):
        return Capabilities(integration_ready=True, methods=["account", "sms"], reason_code=None, message=None)

    @property
    def connection_alive(self) -> bool:
        return (not self._closed and self._delegate is not None
                and self._delegate.connection_alive)

    @property
    def gold_balance(self):
        return None if self._closed or self._delegate is None else self._delegate.gold_balance

    @property
    def server_time_seconds(self):
        return None if self._closed or self._delegate is None else self._delegate.server_time_seconds

    @property
    def season_snapshot(self):
        if self._closed or self._delegate is None:
            return {'valid': False, 'current_season_id': None}
        return self._delegate.season_snapshot

    @property
    def redbag_identity(self):
        return None if self._closed or self._delegate is None else self._delegate.redbag_identity

    @property
    def redbag_available(self):
        return bool(not self._closed and self._delegate is not None and self._delegate.redbag_available)

    async def redbag_start(self):
        return await self._require_delegate().redbag_start()

    async def redbag_next(self):
        return await self._require_delegate().redbag_next()

    async def redbag_claim(self, candidate, *, stop_requested):
        return await self._require_delegate().redbag_claim(candidate, stop_requested=stop_requested)

    async def redbag_reconcile(self, candidate):
        return await self._require_delegate().redbag_reconcile(candidate)

    async def redbag_stop(self):
        if self._delegate is not None:
            return await self._delegate.redbag_stop()

    async def login(self, account, password):
        try:
            async with self._auth_lock:
                return await self._login(account, password)
        finally:
            account = password = ""

    async def send_sms(self, mobile, country_code):
        try:
            async with self._auth_lock:
                await self._sms_operation(mobile, country_code)
        finally:
            mobile = country_code = ""

    async def login_sms(self, mobile, country_code, otp):
        try:
            async with self._auth_lock:
                return await self._sms_operation(mobile, country_code, otp, login=True)
        finally:
            mobile = country_code = otp = ""

    async def _sms_operation(self, mobile, country_code, otp=None, *, login=False):
        failure, cancelled, retry_sms, view = None, False, False, None
        try:
            if self._closed or self._started:
                raise ApiError("AUTH_UPSTREAM_FAILED", "请重新创建游戏会话", retryable=True)
            if login:
                if self._delegate is None:
                    raise ApiError("AUTH_REJECTED", "请先获取短信验证码", 401)
                view = await self._delegate.login_sms(mobile, country_code, otp)
                self._started = True
            else:
                if self._delegate is None:
                    await self._prepare_delegate()
                await self._delegate.send_sms(mobile, country_code)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            retry_sms = (login and isinstance(error, ApiError)
                         and error.code == "AUTH_SMS_REJECTED" and not self._closed)
            failure = _safe_error(error)
        finally:
            mobile = country_code = otp = ""
        if cancelled or failure is not None:
            if not retry_sms:
                await _quiet_close(self)
            if cancelled:
                raise asyncio.CancelledError()
            raise failure
        return view

    async def _prepare_delegate(self, login_state=None):
        runtime = None
        try:
            runtime, shop = (await self._builder() if login_state is None
                             else await self._builder(login_state=login_state))
            if self._closed:
                raise ApiError("AUTH_UPSTREAM_FAILED", "游戏会话已关闭")
            names = {entry.config_id: entry.name for entry in shop.items if entry.name}
            self._delegate = IndependentGameProvider(runtime, shop, login_verified=True, item_names=names)
            runtime = None  # The delegate owns it now.
        finally:
            login_state = None
            await _quiet_close(runtime)

    def export_login_state(self):
        if self._closed or self._delegate is None:
            return None
        return self._delegate.export_login_state()

    async def restore_login(self, state: dict):
        try:
            async with self._auth_lock:
                return await self._login("", "", login_state=copy_login_state(state))
        finally:
            state = None

    async def _login(self, account, password, *, login_state=None):
        if self._closed or self._started:
            raise ApiError("AUTH_UPSTREAM_FAILED", "请重新创建游戏会话", retryable=True)
        self._started = True
        view = failed = None
        cancelled = False
        try:
            if self._delegate is None:
                await self._prepare_delegate(login_state)
            view = (await self._delegate.login(account, password) if login_state is None
                    else await self._delegate.restore_login(login_state))
        except asyncio.CancelledError:
            cancelled = True
        except Exception as error:
            failed = _safe_error(error)
        finally:
            account = password = ""
            login_state = None
        if cancelled or failed is not None:
            await _quiet_close(self)
            if cancelled:
                raise asyncio.CancelledError()
            raise failed
        return view

    def _require_delegate(self):
        if self._closed or self._delegate is None:
            raise auth_required()
        return self._delegate

    async def start_qr(self):
        raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "扫码登录协议尚未接通")

    async def poll_qr(self, handle):
        raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "扫码登录协议尚未接通")

    async def select_role(self, role_id):
        return await self._require_delegate().select_role(role_id)

    async def personal_info(self):
        return await self._require_delegate().personal_info()

    async def fetch_page(self, category, cursor):
        return await self._require_delegate().fetch_page(category, cursor)

    async def fetch_ranked_color_page(self, cursor):
        return await self._require_delegate().fetch_ranked_color_page(cursor)

    async def home_snapshot(self):
        return await self._require_delegate().home_snapshot()

    async def order_snapshot(self):
        return await self._require_delegate().order_snapshot()

    @property
    def order_identity(self):
        return self._require_delegate().order_identity

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        return await self._require_delegate().apply_order_operation(operation,
            expected_revision=expected_revision, stop_requested=stop_requested)

    async def reconcile_order_operations(self, *, expected_revision, require_orders=True):
        return await self._require_delegate().reconcile_order_operations(
            expected_revision=expected_revision, require_orders=require_orders)

    async def apply_home_change(self, change, *, expected):
        return await self._require_delegate().apply_home_change(change, expected=expected)

    async def purchase_page(self, cursor):
        return await self._require_delegate().purchase_page(cursor)

    async def purchase_color(self, row, *, max_price_gold, min_balance_gold, stop_requested):
        return await self._require_delegate().purchase_color(
            row, max_price_gold=max_price_gold, min_balance_gold=min_balance_gold,
            stop_requested=stop_requested)

    async def close(self):
        self._closed = True
        delegate, self._delegate = self._delegate, None
        self._builder = None
        failed = False
        if delegate is not None:
            try:
                await delegate.close()
            except Exception:
                failed = True
        if failed:
            raise ApiError("UPSTREAM_UNAVAILABLE", "游戏连接关闭失败", retryable=True)
