"""Pure fixed-APK CAPTCHA projections; no requests, solver or persistence.

Evidence: artifacts/rumi/login-research/sdk-captcha-200007-contract.sanitized.json
and sdk-http-interceptor-order.sanitized.json. The normal 200007 entry uses
legacy start_captcha; retry must pass through the normal signer again.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from urllib.parse import quote_plus, urlsplit


CAPTCHA_JS_INTERFACE = "BiliJsObject"
CAPTCHA_JS_CALLBACKS = ("finishWithResult", "goBackWithResult")
_RESPONSE_FIELDS = ("challenge", "ctoken", "gs", "gt", "gt_user_id", "image_token", "url")
_H5_FIELDS = ("captcha", "captcha_type", "challenge", "code", "ctoken",
              "seccode", "status", "token", "userid", "validate")
_MAX_RESULT_CHARS = 65536


class SdkCaptchaError(ValueError):
    """Fixed application-owned error code; never contains response data."""


def _success(payload: object) -> bool:
    return (isinstance(payload, Mapping) and type(payload.get("code")) in (int, str)
            and payload.get("code") in (0, "0"))


def _https_url(value: object, *, origin: bool) -> bool:
    # These are project URL guards, not native SDK fallback/normalization rules.
    if (type(value) is not str or not value or value.strip() != value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        return False
    parsed = None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = None
    if parsed is None:
        return False
    return (parsed.scheme == "https" and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and port in (None, 443)
            and (not origin or (parsed.path in ("", "/")
                                and not parsed.query and not parsed.fragment)))


def _strings(payload: Mapping, names: tuple[str, ...], error: str,
             *, integer_fields: tuple[str, ...] = (), boolean_scalars: bool = False) -> dict:
    result = {}
    for name in names:
        value = payload.get(name)
        if boolean_scalars and type(value) is bool:
            value = "true" if value else "false"
        if name in integer_fields and type(value) is int:
            # Narrow project compatibility for observed SDK String scalars.
            if not -(2**63) <= value < 2**63:
                raise SdkCaptchaError(error)
            value = str(value)
        if value is not None and (type(value) is not str
                or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
            raise SdkCaptchaError(error)
        result[name] = value
    return result


@dataclass(frozen=True, repr=False)
class SdkCaptchaConfig:
    login_origin: str
    h5_base_url: str
    login_origins: tuple[str, ...]

    @classmethod
    def from_response(cls, payload: Mapping[str, object]) -> "SdkCaptchaConfig":
        """Use only this verified response's explicit HTTPS login candidates.

        GSCBaseConfig.c/1 source SHA097a730a0153f420f6e9f4de556fd070cfc0e5cb7dfa561505924a27207ecc2d;
        a(String[])/2 SHA19f15e4060cb4c0323d91649552289501390a5bac8038df4dd62c1d65d9e8906;
        a(List,List)/11 SHAdfcab73d5aa4f6c0d57e67faf4deaaf5251c5a54eaa5a158a4045b5d0cd9bbee.
        Native comma split does not Trim; source config precedes old fallback.
        Missing config/fallback and non-HTTPS selection are deliberately rejected.
        The caller must verify config authenticity; this method performs no I/O.
        """
        error = "SDK_CAPTCHA_CONFIG_INVALID"
        if (not _success(payload) or payload.get("config_login_android_https") != "1"
                or type(payload.get("config_login_android_https")) is not str):
            raise SdkCaptchaError(error)
        raw = payload.get("config_login_https")
        h5 = payload.get("config_risk_verify_url")
        if type(raw) is not str or not _https_url(h5, origin=False):
            raise SdkCaptchaError(error)
        # Equivalent first-occurrence order to native reverse remove/add(0).
        routes = tuple(dict.fromkeys(value for value in raw.split(",") if value))
        if not routes or any(not _https_url(value, origin=True) for value in routes):
            raise SdkCaptchaError(error)
        return cls(routes[0], h5, routes)


@dataclass(frozen=True, repr=False)
class SdkCaptchaChallenge:
    captcha_type: str
    challenge: str | None
    ctoken: str | None
    gs: str | None
    gt: str | None
    gt_user_id: str | None
    image_token: str | None
    url: str | None

    @classmethod
    def from_response(cls, payload: Mapping[str, object]) -> "SdkCaptchaChallenge":
        """Project the top-level start response's known H5 String fields.

        Legacy callback source SHA55b85d64bec6117ab7817fd31bab9db172516f5f4ccba114978e5dabd56729e7.
        JSON integer captcha_type 1/2 and integer gs match observed String-model
        scalars. This project covers those finite cases, not every native Gson
        coercion, and rejects complex fields and incomplete challenges.
        """
        error = "SDK_CAPTCHA_RESPONSE_INVALID"
        if not _success(payload):
            raise SdkCaptchaError(error)
        captcha_type = payload.get("captcha_type")
        if type(captcha_type) is int and captcha_type in (1, 2):
            captcha_type = str(captcha_type)
        if type(captcha_type) is not str or captcha_type not in ("geetest", "1", "2"):
            raise SdkCaptchaError(error)
        fields = _strings(payload, _RESPONSE_FIELDS, error, integer_fields=("gs",))
        required = {"geetest": ("challenge", "gt", "ctoken"),
                    "1": ("challenge", "gt", "gt_user_id"),
                    "2": ("url", "image_token")}[captcha_type]
        if any(not fields[name] for name in required):
            raise SdkCaptchaError(error)
        return cls(captcha_type, **fields)

    def build_h5_url(self, config: SdkCaptchaConfig, *, response_code: int | str = 200007) -> str:
        """Return the private in-memory H5 URL; never log or persist it.

        CaptchaActivity.d(model)/1 source
        SHA6b66c7fceb97937dde67cc90a3de6597cce84edb892f547f85bf5bdbebd363ec.
        All branches append directly, except type 2 image URL uses Java UTF-8
        URLEncoder. Supported response codes share the normal risk base getter.
        """
        if not isinstance(config, SdkCaptchaConfig) or not _https_url(config.h5_base_url, origin=False):
            raise SdkCaptchaError("SDK_CAPTCHA_CONFIG_INVALID")
        if (type(response_code) not in (int, str)
                or response_code not in (200007, "200007", 200005, "200005",
                                         200001, "200001", 200000, "200000")):
            raise SdkCaptchaError("SDK_CAPTCHA_RESPONSE_INVALID")
        if self.captcha_type == "1":
            # StringBuilder.append((String)null) is the literal "null".
            gs = "null" if self.gs is None else self.gs
            query = (f"?captcha_type=1&challenge={self.challenge}&gt={self.gt}"
                     f"&userid={self.gt_user_id}&gs={gs}")
        elif self.captcha_type == "2":
            # urllib keeps ~ unescaped; Java URLEncoder escapes it and keeps *.
            encoded = quote_plus(self.url, safe="*-._", encoding="utf-8").replace("~", "%7E")
            query = f"?captcha_type=2&url={encoded}&token={self.image_token}"
        elif self.captcha_type == "geetest":
            query = f"?captcha_type=geetest&challenge={self.challenge}&gt={self.gt}"
        else:
            raise SdkCaptchaError("SDK_CAPTCHA_RESPONSE_INVALID")
        if response_code in (200005, "200005", 200001, "200001"):
            query += "&failed=1"
        return config.h5_base_url + query

    def project_completion(self, raw_result: str) -> dict[str, str]:
        """Forward only a normal human H5 completion through the SDK bindings.

        finishWithResult source
        SHA562e0ad1328a10f92f7d699c313b2c57a052a52d702df3f4b35aae7d3f2ffea4.
        CaptchaInterceptor rebuild source
        SHA90f87234c091e30f135076a71dd341a972c4a37e828933b92fae90bd63d1c86e.
        Type and ctoken always come from the start response, including geetest.
        copy.google.json's String adapter uses nextString for JSON numbers and
        Boolean.toString for booleans; known object/array fields remain invalid.
        JsonReader.nextString source
        SHA96eb8e55423448128d75b99853c6c62615b164a6fee82a43e59614fe8a814782.
        Result values remain opaque; this does not solve or submit a CAPTCHA.
        """
        if type(raw_result) is not str or len(raw_result) > _MAX_RESULT_CHARS:
            raise SdkCaptchaError("SDK_CAPTCHA_COMPLETION_INVALID")
        if raw_result in ("", "null"):
            raise SdkCaptchaError("SDK_CAPTCHA_CANCELLED")
        payload = None
        try:
            # Preserve the number's wire spelling (e.g. 1.00, 1e3), as native
            # JsonReader.nextString does, instead of converting through float.
            payload = json.loads(raw_result, parse_int=str, parse_float=str)
        except (ValueError, RecursionError):
            pass
        # Raise outside except: JSONDecodeError must not retain its private doc.
        if not isinstance(payload, Mapping):
            raise SdkCaptchaError("SDK_CAPTCHA_COMPLETION_INVALID")
        fields = _strings(payload, _H5_FIELDS, "SDK_CAPTCHA_COMPLETION_INVALID",
                          boolean_scalars=True)
        ctoken = {"geetest": self.ctoken, "1": self.gt_user_id, "2": self.image_token}.get(self.captcha_type)
        if self.captcha_type not in ("geetest", "1", "2"):
            raise SdkCaptchaError("SDK_CAPTCHA_RESPONSE_INVALID")
        def value(name):
            return fields[name] or ""
        return {"captcha_type": self.captcha_type, "ctoken": ctoken or "",
                "challenge": value("challenge"), "validate": value("validate"),
                "seccode": value("seccode"),
                "img_code": value("captcha" if self.captcha_type == "2" else "code"),
                "gt_user_id": value("userid"), "captcha_code": value("captcha"),
                "image_token": value("token")}
