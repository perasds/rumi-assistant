"""Independent SDK password and SMS transport; not a game-session provider."""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from .errors import ApiError, provider_error
from .sdk_captcha import SdkCaptchaChallenge, SdkCaptchaConfig, SdkCaptchaError
from .sdk_primitives import sdk_encrypt_password, sdk_sign
from .sdk_user import (SdkGameUser, SdkUserError, project_password_login, project_phone_sms_login,
                       project_token_login)
from .sdk_saved_login import copy_login_state, expiry_value, renewal_threshold, valid_login_text


SDK_HOSTS = frozenset({
    "line1-sdk-center-login-sh.biligame.net",
    "line3-sdk-center-login-sh.biligame.net",
    "line3-login.biligame.net", "line1-login.biligame.net",
    "line3-sdkcenter-login.bilibiligame.net",
})
SDK_CONFIG_HOSTS = frozenset({
    "p.biligame.com", "line3-sdk-center-login-sh.biligame.net",
    "line3-sdkcenter-login.bilibiligame.net",
})
CAPTCHA_PATH = "/api/client/start_captcha"
TOKEN_LOGIN_PATH = "/api/external/user.token.oauth.login/v3"
TOKEN_RENEWAL_PATH = "/api/external/token.renewal/v3"
SMS_SEND_PATH = "/api/external/otp/send/v3"
SMS_LOGIN_PATH = "/api/external/login/otp/v3"
LOGIN_PATHS = frozenset({"/api/external/issue/cipher/v3", "/api/external/login/v3", CAPTCHA_PATH,
                         TOKEN_LOGIN_PATH, TOKEN_RENEWAL_PATH, SMS_SEND_PATH, SMS_LOGIN_PATH})
CONFIG_PATH = "/api/external/config/v3"
_REQUEST_STAGES = {
    CONFIG_PATH: "sdk_config",
    "/api/external/issue/cipher/v3": "sdk_cipher",
    "/api/external/login/v3": "sdk_password",
    CAPTCHA_PATH: "sdk_captcha",
    TOKEN_LOGIN_PATH: "sdk_token",
    TOKEN_RENEWAL_PATH: "sdk_renewal",
    SMS_SEND_PATH: "sdk_sms_send",
    SMS_LOGIN_PATH: "sdk_sms_login",
}
SDK_REQUEST_STAGES = frozenset(_REQUEST_STAGES.values())
Parameters = Mapping[str, str]
HeaderFactory = Callable[[str, str, Parameters], Parameters]
CaptchaHandler = Callable[[SdkCaptchaChallenge, SdkCaptchaConfig], Awaitable[str]]
_CAPTCHA_HANDLER_ERRORS = frozenset({"AUTH_CAPTCHA_CANCELLED", "AUTH_CAPTCHA_UNAVAILABLE",
    "AUTH_CAPTCHA_PAGE_UNAVAILABLE", "AUTH_CAPTCHA_TIMEOUT", "AUTH_CAPTCHA_INVALID"})


@dataclass(frozen=True, repr=False)
class SdkCredentials:
    uid: str
    access_key: SecretStr
    game_user: SdkGameUser
    expires: str | None = None
    renewal_expires: str | None = None


def _response_code(value: object) -> int | None:
    if type(value) is int:
        return value if -(2**31) <= value < 2**31 else None
    if type(value) is not str or re.fullmatch(r"[+-]?[0-9]+", value) is None:
        return None
    negative = value.startswith("-")
    digits = value.lstrip("+-").lstrip("0") or "0"
    if len(digits) > 10:
        return None
    number = int(digits) * (-1 if negative else 1)
    return number if -(2**31) <= number < 2**31 else None


class SdkResponseError(ApiError):
    """Fixed rejection body plus only bounded code and a known request stage."""

    def __init__(self, *, response_code: object, request_stage: str):
        if type(request_stage) is not str or request_stage not in SDK_REQUEST_STAGES:
            raise ValueError("Invalid SDK response stage")
        super().__init__("AUTH_REJECTED", "游戏认证信息未通过验证", 401)
        self._response_code = _response_code(response_code)
        self._request_stage = request_stage
        self._captcha_required = (request_stage in {"sdk_password", "sdk_sms_send", "sdk_sms_login"}
            and type(response_code) in (int, str) and response_code in (200007, "200007"))

    @property
    def response_code(self) -> int | None:
        return self._response_code

    @property
    def request_stage(self) -> str:
        return self._request_stage

    @property
    def captcha_required(self) -> bool:
        return self._captcha_required


def _upstream_failed() -> ApiError:
    return ApiError("AUTH_UPSTREAM_FAILED", "游戏认证服务暂不可用", 503, True)


class SdkHttpClient:
    def __init__(self, origin: str, app_key: str, *,
                 common_parameters: Callable[[str], Parameters], extra_headers: HeaderFactory,
                 sms_feign_sign: Callable[[str], str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock_ms: Callable[[], int] | None = None, purpose: str = "login"):
        valid = False
        try:
            url = urlsplit(origin)
            hosts = SDK_HOSTS if purpose == "login" else SDK_CONFIG_HOSTS if purpose == "config" else ()
            valid = (url.scheme == "https" and url.hostname in hosts
                     and url.port in (None, 443) and url.path in ("", "/")
                     and not url.query and not url.fragment and url.username is None)
        except (ValueError, TypeError, AttributeError):
            pass
        if not valid:
            raise ValueError("Invalid SDK config origin" if purpose == "config" else "Invalid SDK login origin")
        if not isinstance(app_key, str) or not app_key or not callable(common_parameters) or not callable(extra_headers):
            raise ValueError("SDK configuration is incomplete")
        self._app_key = SecretStr(app_key)
        self._host = url.hostname
        self._paths = LOGIN_PATHS if purpose == "login" else frozenset({CONFIG_PATH})
        self._parameters = common_parameters
        self._headers = extra_headers
        self._sms_feign_sign = sms_feign_sign
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._closed = False
        self._http = httpx.AsyncClient(base_url=origin.rstrip("/"), transport=transport,
                                      timeout=15, follow_redirects=False)

    async def _post(self, path: str, business: Parameters) -> dict:
        if self._closed or path not in self._paths:
            raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")
        form, headers = None, None
        try:
            parameters = self._parameters(path)
            if not isinstance(parameters, Mapping):
                raise ValueError("Invalid parameter map")
            prepared = {**parameters, "timestamp": str(self._clock_ms()), "domain": self._host, **business}
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in prepared.items()):
                raise ValueError("Invalid parameter type")
            prepared = {k: v for k, v in prepared.items() if k and v}
            prepared["sign"] = sdk_sign(prepared, self._app_key.get_secret_value())
            supplied = self._headers(prepared["timestamp"], prepared["sign"], dict(prepared))
            if not isinstance(supplied, Mapping) or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in supplied.items()):
                raise ValueError("Invalid header map")
            if path == SMS_LOGIN_PATH:
                if not callable(self._sms_feign_sign):
                    raise ValueError("SMS signing is unavailable")
                feign = self._sms_feign_sign(prepared["sign"])
                if type(feign) is not str or not feign:
                    raise ValueError("Invalid SMS signature")
                prepared["feign_sign"] = feign
            form, headers = prepared, dict(supplied)
        except Exception:
            pass
        if form is None:
            raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")

        payload = None
        try:
            response = await self._http.post(path, data=form, headers=headers)
            if response.status_code == 200 and len(response.content) <= 1048576:
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
        except (httpx.RequestError, ValueError, RuntimeError):
            pass
        if payload is None:
            raise _upstream_failed()
        code = payload.get("code")
        if type(code) not in (int, str):
            raise _upstream_failed()
        # CacheLogin permits cached continuation for these two SDK codes. This
        # client requires a real successful token response, but keeps them
        # retryable rather than treating service failure as a revoked account.
        if path == TOKEN_LOGIN_PATH and code in (60005, "60005", 60006, "60006"):
            raise _upstream_failed()
        if code not in (0, "0"):
            raise SdkResponseError(response_code=code, request_stage=_REQUEST_STAGES[path])
        return payload

    async def fetch_config(self) -> dict:
        return await self._post(CONFIG_PATH, {"version": "3"})

    async def start_captcha(self) -> dict:
        """Fetch legacy captcha bootstrap fields without completing the challenge."""
        return await self._post(CAPTCHA_PATH, {})

    async def _post_sms(self, path: str, business: Parameters, *,
                        captcha_config: SdkCaptchaConfig | None,
                        captcha_handler: CaptchaHandler | None) -> dict:
        result = failure = None
        try:
            result = await self._post(path, business)
        except SdkResponseError as error:
            failure = error
        if failure is not None:
            if not failure.captcha_required or captcha_handler is None:
                raise failure
            fields = await self._captcha_completion(captcha_config, captcha_handler)
            result = await self._post(path, {**business, **fields})
        return result

    async def send_phone_sms(self, mobile: str, country_code: str, *,
                             captcha_config: SdkCaptchaConfig | None = None,
                             captcha_handler: CaptchaHandler | None = None) -> SecretStr:
        if not valid_login_text(mobile, 128) or not valid_login_text(country_code, 16):
            raise ApiError("AUTH_REJECTED", "游戏认证信息未通过验证", 401)
        result = await self._post_sms(SMS_SEND_PATH, {"otp_channel_no": mobile,
            "country_code": country_code, "otp_channel_category": "tel", "otp_type": "login", "version": "3"},
            captcha_config=captcha_config, captcha_handler=captcha_handler)
        ticket = result.get("captcha_key")
        if not valid_login_text(ticket, 4096):
            raise _upstream_failed()
        return SecretStr(ticket)

    async def login_phone_sms(self, mobile: str, country_code: str, otp: str,
                              captcha_key: SecretStr | str, *,
                              captcha_config: SdkCaptchaConfig | None = None,
                              captcha_handler: CaptchaHandler | None = None) -> SdkCredentials:
        ticket = captcha_key.get_secret_value() if type(captcha_key) is SecretStr else captcha_key
        if (not valid_login_text(mobile, 128) or not valid_login_text(country_code, 16)
                or not valid_login_text(otp, 128) or not valid_login_text(ticket, 4096)):
            raise ApiError("AUTH_REJECTED", "游戏认证信息未通过验证", 401)
        result = await self._post_sms(SMS_LOGIN_PATH, {"mobile": mobile, "country_code": country_code,
            "otp": otp, "captcha_key": ticket, "version": "3"},
            captcha_config=captcha_config, captcha_handler=captcha_handler)
        game_user = None
        try:
            game_user = project_phone_sms_login(result)
        except SdkUserError:
            pass
        if game_user is None:
            raise _upstream_failed()
        return self._credentials(result, game_user)

    async def _captcha_completion(self, config: SdkCaptchaConfig, handler: CaptchaHandler) -> dict[str, str]:
        if type(config) is not SdkCaptchaConfig or not callable(handler):
            raise ApiError("AUTH_CAPTCHA_FAILED", "人工验证码未完成", 401)
        bootstrap = await self.start_captcha()
        if self._closed:
            bootstrap = None
            raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")
        challenge = raw = fields = None
        cancelled = False
        code = "AUTH_CAPTCHA_FAILED"
        try:
            challenge = SdkCaptchaChallenge.from_response(bootstrap)
            raw = await handler(challenge, config)
            if not self._closed:
                fields = challenge.project_completion(raw)
        except asyncio.CancelledError:
            cancelled = True
        except SdkCaptchaError as error:
            if (type(error) is SdkCaptchaError and len(error.args) == 1
                    and type(error.args[0]) is str and error.args[0] == "SDK_CAPTCHA_CANCELLED"):
                code = "AUTH_CAPTCHA_CANCELLED"
        except ApiError as error:
            if type(error) is ApiError and type(error.code) is str and error.code in _CAPTCHA_HANDLER_ERRORS:
                code = error.code
        except Exception:
            pass
        finally:
            bootstrap = challenge = raw = None
        if cancelled:
            raise asyncio.CancelledError()
        if self._closed:
            raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")
        if fields is None:
            raise provider_error(ApiError(code, ""))
        return fields

    async def login(self, account: str, password: str, *, captcha_config: SdkCaptchaConfig | None = None,
                    captcha_handler: CaptchaHandler | None = None) -> SdkCredentials:
        if "/api/external/login/v3" not in self._paths:
            raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")
        if not isinstance(account, str) or not account or not isinstance(password, str) or not password:
            raise ApiError("AUTH_REJECTED", "游戏认证信息未通过验证", 401)
        cipher = await self._post("/api/external/issue/cipher/v3",
                                  {"cipher_type": "bili_login_rsa", "version": "3"})
        encrypted = None
        try:
            encrypted = sdk_encrypt_password(password, cipher["cipher_key"], cipher["hash"])
        except (KeyError, ValueError, TypeError):
            pass
        password = ""
        if encrypted is None:
            raise _upstream_failed()
        business = {"user_id": account, "pwd": encrypted, "version": "3"}
        result = failure = None
        try:
            result = await self._post("/api/external/login/v3", business)
        except SdkResponseError as error:
            failure = error
        if failure is not None:
            if not failure.captcha_required or captcha_handler is None:
                raise failure
            failure = None
            fields = await self._captcha_completion(captcha_config, captcha_handler)
            result = await self._post("/api/external/login/v3", {**business, **fields})
        uid, key = result.get("uid"), result.get("access_key")
        if (type(uid) not in (int, str) or not str(uid)
                or not isinstance(key, str) or not key):
            raise _upstream_failed()
        game_user = None
        try:
            game_user = project_password_login(result)
        except SdkUserError:
            pass
        if game_user is None:
            raise _upstream_failed()
        return self._credentials(result, game_user)

    def _credentials(self, result: dict, game_user: SdkGameUser, *,
                     expires: str | None = None) -> SdkCredentials:
        uid = result.get("uid")
        if (type(uid) not in (int, str) or not valid_login_text(str(uid), 128)
                or not valid_login_text(game_user.game_uid, 128)
                or not valid_login_text(game_user.token.get_secret_value(), 4096)):
            raise _upstream_failed()
        values = None
        try:
            deadline = expiry_value(result.get("expires", expires))
            values = (deadline, renewal_threshold(deadline, self._clock_ms()))
        except ValueError:
            pass
        if values is None:
            raise _upstream_failed()
        return SdkCredentials(str(result["uid"]), SecretStr(result["access_key"]), game_user, *values)

    async def login_token(self, state: dict) -> SdkCredentials:
        saved = copy_login_state(state)
        business = {"uid": saved["uid"], "access_key": saved["access_key"]}
        expires = saved["expires"]
        now = self._clock_ms()
        threshold = saved["renewal_expires"]
        if threshold is None:
            threshold = renewal_threshold(expires, now)
        # CacheLogin 500035 continues token login even when renewal fails.
        # Unknown expiry is kept unknown; no RN missing-expiry default is invented.
        if expires is not None and (now > int(expires) or now > int(threshold)):
            renewed_expiry = None
            try:
                renewed = await self._post(TOKEN_RENEWAL_PATH, business)
                renewed_expiry = expiry_value(renewed.get("expires"))
            except (ApiError, ValueError):
                pass
            if renewed_expiry is not None:
                expires = renewed_expiry
        # RN sends only uid/access_key: retain common version 1, not password's 3.
        result = await self._post(TOKEN_LOGIN_PATH, business)
        user = None
        try:
            user = project_token_login(result, cached_login_type=saved["game_user"]["login_type"])
        except SdkUserError:
            pass
        if user is None:
            raise _upstream_failed()
        credentials = self._credentials(result, user, expires=expires)
        if (credentials.uid != saved["uid"] or user.game_uid != saved["game_user"]["game_uid"]):
            raise ApiError("AUTH_REJECTED", "游戏认证信息未通过验证", 401)
        return credentials

    async def close(self) -> None:
        self._closed = True
        failed = False
        try:
            await self._http.aclose()
        except Exception:
            failed = True
        finally:
            self._http.cookies.clear()
            self._app_key = SecretStr("")
            self._parameters = None
            self._headers = None
            self._sms_feign_sign = None
        if failed:
            raise _upstream_failed()
