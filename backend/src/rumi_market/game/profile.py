"""Assemble a fresh password/SMS login profile from four explicit contexts.

The 1.5.3 template is backed by profile-fresh-source-contract.sanitized.json and
runtime-version-sources.sanitized.json under artifacts/rumi/pet-catalog. SDK
app_ver and VSC version checks are template consistency guards, not executions
of Application.version. No account/device preferences, identity generation or
network operations occur here.
"""

from dataclasses import dataclass

from pydantic import SecretStr

from ..sdk_app_config import SdkAppConfig
from ..sdk_device import SdkDeviceIdentity
from ..sdk_parameters import SdkRequestContext
from ..sdk_user import SdkGameUser
from .login import GameLoginProfile
from .version import VersionResult


_APPLICATION_VERSION = "1.5.3"
_META_VERSION = "1.0.8027.51055\n"


class GameProfileError(ValueError):
    pass


@dataclass(frozen=True, kw_only=True, repr=False)
class ProjectClientContext:
    """Caller-owned client environment, with separately declared game identity.

    game_device_id is the explicit value for the game's BUVID field. Its source
    declaration is caller metadata, not verification of a native fallback or
    server acceptance. An explicit empty value is preserved; no HTTP UDID/BDID
    fallback is used. time_zone_hours is the standard BaseUtcOffset.Hours
    component; language_type and the other environment values belong to this
    project and do not claim to describe the official client's current state.
    """

    game_device_id: str
    device_id_source: str
    model: str
    os_version: str
    network: str
    time_zone_hours: int
    language_type: int
    platform: str


def _valid(sdk_context, user, version, client_context) -> bool:
    return (
        type(sdk_context) is SdkRequestContext
        and type(sdk_context.app_config) is SdkAppConfig
        and type(sdk_context.app_config.app_ver) is str
        and type(sdk_context.identity) is SdkDeviceIdentity
        and type(user) is SdkGameUser
        and type(user.game_uid) is str and bool(user.game_uid)
        and type(user.token) is SecretStr
        and type(user.token.get_secret_value()) is str
        and bool(user.token.get_secret_value())
        and type(user.login_type) is str and user.login_type in {"0", "6"}
        and type(version) is VersionResult
        and type(version.version) is str
        and type(version.hot_version) is int and 0 <= version.hot_version < 2**31
        and type(client_context) is ProjectClientContext
        and all(type(value) is str for value in (
            client_context.game_device_id, client_context.device_id_source,
            client_context.model, client_context.os_version,
            client_context.network, client_context.platform,
        ))
        and bool(client_context.device_id_source.strip())
        and type(client_context.time_zone_hours) is int
        and -23 <= client_context.time_zone_hours <= 23
        and type(client_context.language_type) is int
        and -(2**31) <= client_context.language_type < 2**31
    )


def build_game_login_profile(
    sdk_context: SdkRequestContext,
    user: SdkGameUser,
    version: VersionResult,
    client_context: ProjectClientContext,
) -> GameLoginProfile:
    """Use the fixed build template and verified password/SMS projection.

    The context's distributor, SDK app ID/version and HTTP identity are not the
    game's global channel IDs, Application.version or BUVID. The known fixed
    APK's public IDs are filled below; hot_version is the validated VSC result.
    """
    valid = False
    try:
        valid = _valid(sdk_context, user, version, client_context)
    except Exception:
        pass
    if not valid:
        raise GameProfileError("GAME_PROFILE_INVALID")
    if (sdk_context.app_config.app_ver != _APPLICATION_VERSION
            or version.version != _APPLICATION_VERSION):
        raise GameProfileError("GAME_PROFILE_VERSION_UNSUPPORTED")

    return GameLoginProfile(
        game_uid=user.game_uid, game_token=user.token, login_type=user.login_type,
        client_version=_APPLICATION_VERSION, meta_version=_META_VERSION,
        hot_version=version.hot_version, channel="Android_CN_CDN", openid="",
        device_id=client_context.game_device_id,
        global_channel_id="1001", global_game_id="1000069", brand_id="101", area_id="0",
        ad_channel_id="", remote_ip="", country_code="CN",
        model=client_context.model, os_version=client_context.os_version,
        network=client_context.network, time_zone_hours=client_context.time_zone_hours,
        language_type=client_context.language_type, platform=client_context.platform,
    )
