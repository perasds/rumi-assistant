"""Fixed Android password callback projection; no SDK or transport execution."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import SecretStr


class SdkUserError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class SdkGameUser:
    game_uid: str
    token: SecretStr
    login_type: str


def project_password_login(payload: Mapping[str, object]) -> SdkGameUser:
    """Project a successful password payload using the verified APK branches.

    Android RN overwrites login_type with AccountPwd=1; ProxyUserListener maps
    that value to 0. This deliberately covers only boolean JSON open-ID flags,
    not other login methods, coercions, challenges or runtime hotfix behavior.
    """
    return _project_login(payload, "0")


def project_phone_sms_login(payload: Mapping[str, object]) -> SdkGameUser:
    """q.f.a overwrites native login_type=2; Proxy maps it to game type 6."""
    return _project_login(payload, "6")


def _project_login(payload: Mapping[str, object], login_type: str) -> SdkGameUser:
    if not isinstance(payload, Mapping):
        raise SdkUserError("SDK_USER_INVALID")
    code = payload.get("code")
    if type(code) not in (int, str) or code not in (0, "0"):
        raise SdkUserError("SDK_USER_INVALID")
    flag = payload.get("game_open_id_enable")
    if flag is not None and type(flag) is not bool:
        raise SdkUserError("SDK_USER_INVALID")
    # The native Bundle writes only the chosen field, so the other response
    # identifier cannot supply the adapter's fallback when this field is empty.
    uid = payload.get("game_open_id" if flag is True else "uid")
    token = payload.get("access_key")
    if (type(uid) not in (int, str) or not str(uid)
            or not isinstance(token, str) or not token):
        raise SdkUserError("SDK_USER_INVALID")
    return SdkGameUser(str(uid), SecretStr(token), login_type)


def project_token_login(payload: Mapping[str, object], *, cached_login_type: str) -> SdkGameUser:
    """CacheLogin 500034 retains the cached type; 500038.toJson overrides it.

    Only this project's password origin is restorable: Android native type 1
    maps to game type 0. No other cached SDK login type is inferred here.
    """
    if type(cached_login_type) is not str or cached_login_type != "0":
        raise SdkUserError("SDK_USER_INVALID")
    return project_password_login(payload)
