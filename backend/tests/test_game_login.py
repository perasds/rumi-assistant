"""Pure game-login requests against the pinned schema, using synthetic inputs."""

from dataclasses import MISSING, fields, replace

from pydantic import SecretStr
import pytest

from rumi_market.game.proto import GameProtoCodec


def profile(**changes):
    from rumi_market.game.login import GameLoginProfile
    values = dict(
        game_uid="synthetic-game-uid", game_token=SecretStr("synthetic-game-token"),
        client_version="synthetic-application-version", meta_version="1.0.8027.51055\n",
        hot_version=702, channel="synthetic-http-channel", openid="",
        device_id="synthetic-device", global_channel_id="001001", global_game_id="1000069",
        brand_id="0101", area_id="0", ad_channel_id="", remote_ip="198.51.100.42",
        time_zone_hours=0, login_type="synthetic-login-type", model="synthetic-model",
        os_version="synthetic-os", network="WIFI", language_type=1,
        country_code="CN", platform="synthetic-runtime-platform",
    )
    return GameLoginProfile(**(values | changes))


def decode(request):
    return GameProtoCodec().decode(request.message_name, request.payload).message


def test_sdk_login_uses_real_nested_fields_and_all_seventeen_game_info_setters():
    from rumi_market.game.login import build_sdk_login_request
    request = build_sdk_login_request(profile())
    message = decode(request)
    assert request.command_id == 2901
    assert request.message_name == "SNKProto.Cmd_Net_LumiLogin_Req"
    assert {field.name for field, value in message.ListFields()} == {
        "account_type", "param_bili", "client_version", "meta_version", "game_info",
        "channel", "hot_version", "openid",
    }
    assert message.account_type == 4
    assert message.client_version == "synthetic-application-version"
    assert message.meta_version == "1.0.8027.51055\n"
    assert message.hot_version == 702
    assert message.channel == "synthetic-http-channel"
    assert message.openid == "" and message.HasField("openid")
    auth = message.param_bili
    assert {field.name for field, value in auth.ListFields()} == {
        "uid", "access_token", "global_channel_id", "brand_id", "area_id",
    }
    assert auth.uid == "synthetic-game-uid"
    assert auth.access_token == "synthetic-game-token"
    assert (auth.global_channel_id, auth.brand_id, auth.area_id) == (1001, 101, 0)
    expected = dict(
        udid="synthetic-device", sdk_uid="synthetic-game-uid", global_channel_id="1001",
        brand_id="101", area_id="0", ad_channel_id="", sdk_version="synthetic-application-version",
        remote_ip="198.51.100.42", time_zone=0, login_channel="synthetic-login-type",
        model="synthetic-model", os_version="synthetic-os", network="WIFI", language_type=1,
        country_code="CN", b_global_game_id=1000069, platform="synthetic-runtime-platform",
    )
    assert {field.name for field, value in message.game_info.ListFields()} == set(expected)
    for name, value in expected.items():
        assert message.game_info.HasField(name)
        assert getattr(message.game_info, name) == value


def test_normal_enter_game_preserves_raw_channel_strings_and_source_absence():
    from rumi_market.game.login import build_enter_game_request
    request = build_enter_game_request(profile())
    message = decode(request)
    assert request.command_id == 2911
    assert request.message_name == "SNKProto.Cmd_Role_EnterGame_Req"
    assert {field.name for field, value in message.ListFields()} == {
        "is_reconnect", "b_udid", "b_sdk_uid", "b_global_channel_id", "b_brand_id",
        "b_area_id", "b_platform", "b_ad_channel_id", "b_version", "s_protocol_version",
        "time_zone", "login_channel",
    }
    assert message.is_reconnect is False and message.HasField("is_reconnect")
    assert message.b_udid == "synthetic-device"
    assert message.b_sdk_uid == "synthetic-game-uid"
    assert (message.b_global_channel_id, message.b_brand_id, message.b_area_id) == ("001001", "0101", "0")
    assert message.b_version == "synthetic-application-version"
    assert message.b_platform == "synthetic-runtime-platform"
    assert message.s_protocol_version == "not_check_git_proto_version"
    assert message.time_zone == 0 and message.HasField("time_zone")
    assert message.login_channel == "synthetic-login-type"
    assert message.b_ad_channel_id == "" and message.HasField("b_ad_channel_id")
    assert not any(message.HasField(name) for name in ("session", "role_id", "remote_ip"))


@pytest.mark.parametrize("raw, expected", [
    (" +001001 ", 1001), ("-2147483648", -2147483648), ("2147483647", 2147483647),
    ("2147483648", -1), ("-2147483649", -1), ("1.0", -1), ("", -1),
    ("0x10", -1), ("١٢", -1),
])
def test_sdk_identifier_int32_parse_and_failure_are_distinct_from_raw_enter_game(raw, expected):
    from rumi_market.game.login import build_sdk_login_request, build_enter_game_request
    inputs = profile(global_channel_id=raw, brand_id=raw, area_id=raw, global_game_id=raw)
    login, enter = decode(build_sdk_login_request(inputs)), decode(build_enter_game_request(inputs))
    auth, info = login.param_bili, login.game_info
    assert (auth.global_channel_id, auth.brand_id, auth.area_id) == (expected,) * 3
    assert (info.global_channel_id, info.brand_id, info.area_id) == (str(expected),) * 3
    assert info.b_global_game_id == expected
    assert (enter.b_global_channel_id, enter.b_brand_id, enter.b_area_id) == (raw,) * 3


def test_explicit_empty_and_zero_runtime_values_keep_presence():
    from rumi_market.game.login import build_sdk_login_request, build_enter_game_request
    inputs = profile()
    values = {field.name: "" for field in fields(inputs) if type(getattr(inputs, field.name)) is str}
    inputs = replace(inputs, **values, game_token=SecretStr(""), hot_version=0,
                     time_zone_hours=0, language_type=0)
    login, enter = decode(build_sdk_login_request(inputs)), decode(build_enter_game_request(inputs))
    # Unlike the optional GameInfo fields, hot_version has no presence in the source schema.
    assert not login.DESCRIPTOR.fields_by_name["hot_version"].has_presence
    assert login.hot_version == 0
    assert "hot_version" not in {field.name for field, value in login.ListFields()}
    assert login.HasField("openid") and login.openid == ""
    assert len(login.game_info.ListFields()) == 17
    assert login.game_info.HasField("language_type") and login.game_info.language_type == 0
    assert login.game_info.b_global_game_id == -1
    assert login.param_bili.HasField("access_token") and login.param_bili.access_token == ""
    assert len(enter.ListFields()) == 12


def test_every_profile_value_is_explicit_and_sensitive_values_are_not_in_repr():
    from rumi_market.game.login import GameLoginProfile, build_sdk_login_request
    inputs = profile()
    assert all(field.default is MISSING and field.default_factory is MISSING
               for field in fields(GameLoginProfile))
    assert "synthetic" not in repr(inputs)
    assert "synthetic" not in repr(build_sdk_login_request(inputs))
    values = {field.name: getattr(inputs, field.name) for field in fields(inputs)}
    del values["hot_version"]
    with pytest.raises(TypeError):
        GameLoginProfile(**values)


@pytest.mark.parametrize("changes", [
    {"hot_version": 2**31}, {"language_type": -(2**31) - 1}, {"time_zone_hours": True},
    {"device_id": None}, {"game_uid": SecretStr("private-synthetic-value")},
    {"game_token": "private-synthetic-value"},
])
def test_invalid_profile_has_fixed_error_without_private_value_or_exception_context(changes):
    from rumi_market.game.login import GameLoginError, build_sdk_login_request, build_enter_game_request
    inputs = profile(**changes)
    for build in (build_sdk_login_request, build_enter_game_request):
        with pytest.raises(GameLoginError) as caught:
            build(inputs)
        assert str(caught.value) == "GAME_LOGIN_PROFILE_INVALID"
        assert caught.value.__context__ is None
        assert "private-synthetic-value" not in repr(caught.value)
