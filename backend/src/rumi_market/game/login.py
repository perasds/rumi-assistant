"""Pure normal SDK login requests from fixed public LoginServerInvoker IL.

Game.dll SHA256: 3ba755a6360e3cd8ce36d6e17b86545158a01641a0bdf9b99985c5b09f215431.
Sources: GetGameInfo 0x6004173, LoginByChannel 0x6004174, EnterGame 0x6004177.
This module reads no device state, preferences or SDK session and does no I/O.
"""

import re
from dataclasses import dataclass, fields

from pydantic import SecretStr

from .proto import GameProtoCodec


@dataclass(frozen=True, kw_only=True, repr=False)
class GameLoginProfile:
    """Explicit game callback and runtime values, without guessed defaults.

    game_uid is the game callback UID, not automatically the SDK HTTP uid.
    client_version is Application.version; time_zone_hours is BaseUtcOffset.Hours.
    meta_version preserves the source string, including its trailing newline.
    """

    game_uid: str
    game_token: SecretStr
    client_version: str
    meta_version: str
    hot_version: int
    channel: str
    openid: str
    device_id: str
    global_channel_id: str
    global_game_id: str
    brand_id: str
    area_id: str
    ad_channel_id: str
    remote_ip: str
    time_zone_hours: int
    login_type: str
    model: str
    os_version: str
    network: str
    language_type: int
    country_code: str
    platform: str


@dataclass(frozen=True, repr=False)
class GameLoginRequest:
    """Business payload; the client assigns sequence and builds the outer envelope."""

    command_id: int
    message_name: str
    payload: bytes


class GameLoginError(ValueError):
    pass


def _validate(profile: GameLoginProfile) -> None:
    valid = type(profile) is GameLoginProfile
    if valid:
        for field in fields(profile):
            value = getattr(profile, field.name)
            if field.type is str:
                valid = type(value) is str
            elif field.type is int:
                valid = type(value) is int and -(2**31) <= value < 2**31
            else:
                valid = type(value) is SecretStr
            if not valid:
                break
    if not valid:
        raise GameLoginError("GAME_LOGIN_PROFILE_INVALID")


def _sdk_int(value: str) -> int:
    """SDK decimal identifiers use Int32.TryParse, then an explicit -1 fallback."""
    value = value.strip(" \t\r\n\v\f")
    if re.fullmatch(r"[+-]?[0-9]+", value) is None:
        return -1
    negative = value.startswith("-")
    digits = value.lstrip("+-").lstrip("0") or "0"
    if len(digits) > 10:
        return -1
    number = int(digits) * (-1 if negative else 1)
    return number if -(2**31) <= number < 2**31 else -1


def build_sdk_login_request(profile: GameLoginProfile) -> GameLoginRequest:
    _validate(profile)
    channel, brand, area = (_sdk_int(value) for value in
                            (profile.global_channel_id, profile.brand_id, profile.area_id))
    message_name = "SNKProto.Cmd_Net_LumiLogin_Req"
    payload = GameProtoCodec().encode(message_name, {
        "account_type": 4,
        "client_version": profile.client_version,
        "meta_version": profile.meta_version,
        "hot_version": profile.hot_version,
        "channel": profile.channel,
        "openid": profile.openid,
        "param_bili": {
            "uid": profile.game_uid, "access_token": profile.game_token.get_secret_value(),
            "global_channel_id": channel, "brand_id": brand, "area_id": area,
        },
        "game_info": {
            "udid": profile.device_id, "sdk_uid": profile.game_uid,
            "global_channel_id": str(channel), "brand_id": str(brand), "area_id": str(area),
            "ad_channel_id": profile.ad_channel_id, "sdk_version": profile.client_version,
            "remote_ip": profile.remote_ip, "time_zone": profile.time_zone_hours,
            "login_channel": profile.login_type, "model": profile.model,
            "os_version": profile.os_version, "network": profile.network,
            "language_type": profile.language_type, "country_code": profile.country_code,
            "b_global_game_id": _sdk_int(profile.global_game_id), "platform": profile.platform,
        },
    })
    return GameLoginRequest(2901, message_name, payload)


def build_enter_game_request(profile: GameLoginProfile) -> GameLoginRequest:
    """Only EnterGame(false, '', 0); session, role_id and remote_ip remain absent."""
    _validate(profile)
    message_name = "SNKProto.Cmd_Role_EnterGame_Req"
    payload = GameProtoCodec().encode(message_name, {
        "is_reconnect": False,
        "b_udid": profile.device_id, "b_sdk_uid": profile.game_uid,
        "b_global_channel_id": profile.global_channel_id,
        "b_brand_id": profile.brand_id, "b_area_id": profile.area_id,
        "b_ad_channel_id": profile.ad_channel_id, "b_platform": profile.platform,
        "b_version": profile.client_version, "time_zone": profile.time_zone_hours,
        "login_channel": profile.login_type,
        # IL282 loads this literal; the computed ProtobufVersion local is unused.
        "s_protocol_version": "not_check_git_proto_version",
    })
    return GameLoginRequest(2911, message_name, payload)
