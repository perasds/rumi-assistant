"""Pure fixed-source profile assembly using synthetic caller-owned contexts."""

from dataclasses import FrozenInstanceError, replace

from pydantic import SecretStr
import pytest

from rumi_market.sdk_app_config import SdkAppConfig
from rumi_market.sdk_device import SdkDeviceIdentity
from rumi_market.sdk_parameters import SdkRequestContext
from rumi_market.sdk_user import SdkGameUser
from rumi_market.game.version import VersionResult


def sdk_context(**app_changes):
    config = SdkAppConfig(
        "synthetic-merchant", "synthetic-sdk-app", "synthetic-server",
        "synthetic-sdk-version", SecretStr("synthetic-sdk-app-key"),
        app_ver="1.5.3",
    )
    return SdkRequestContext(
        replace(config, **app_changes),
        SdkDeviceIdentity("synthetic-http-udid", "synthetic-http-bdid"),
        b"synthetic-rule", "synthetic-signature", "1", b"synthetic-public-pem",
    )


def client_context(**changes):
    from rumi_market.game.profile import ProjectClientContext

    values = dict(
        game_device_id="synthetic-game-buvid-雪",
        device_id_source="synthetic caller-owned identity policy; native value unverified",
        model="synthetic-project-model", os_version="synthetic-project-os",
        network="synthetic-project-network", time_zone_hours=-5,
        language_type=1, platform="synthetic-project-platform",
    )
    return ProjectClientContext(**(values | changes))


def inputs():
    return dict(
        sdk_context=sdk_context(),
        user=SdkGameUser("synthetic-game-uid", SecretStr("synthetic-game-token"), "0"),
        version=VersionResult("1.5.3", 62), client_context=client_context(),
    )


def build(**changes):
    from rumi_market.game.profile import build_game_login_profile

    return build_game_login_profile(**(inputs() | changes))


def assert_failure(changes, code="GAME_PROFILE_INVALID"):
    from rumi_market.game.profile import GameProfileError

    with pytest.raises(GameProfileError) as failure:
        build(**changes)
    assert str(failure.value) == code
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert "synthetic" not in str(failure.value)


def test_four_contexts_fill_fixed_source_fields_without_generating_identity(monkeypatch):
    from rumi_market.game.profile import build_game_login_profile

    values = inputs()
    monkeypatch.setattr(SdkDeviceIdentity, "create", lambda: pytest.fail("new identity"))
    monkeypatch.setattr(SdkRequestContext, "from_apk", lambda path: pytest.fail("APK I/O"))
    profile = build_game_login_profile(**values)
    assert profile.game_uid == values["user"].game_uid
    assert profile.game_token is values["user"].token
    assert profile.login_type == "0"
    assert (profile.client_version, profile.meta_version, profile.hot_version) == (
        "1.5.3", "1.0.8027.51055\n", 62,
    )
    assert profile.channel == "Android_CN_CDN"
    assert (profile.global_channel_id, profile.global_game_id,
            profile.brand_id, profile.area_id) == ("1001", "1000069", "101", "0")
    assert (profile.openid, profile.ad_channel_id, profile.remote_ip,
            profile.country_code) == ("", "", "", "CN")
    assert profile.device_id == "synthetic-game-buvid-雪"
    assert profile.device_id not in (values["sdk_context"].identity.udid,
                                     values["sdk_context"].identity.bd_id)
    assert (profile.model, profile.os_version, profile.network, profile.time_zone_hours,
            profile.language_type, profile.platform) == (
        "synthetic-project-model", "synthetic-project-os", "synthetic-project-network",
        -5, 1, "synthetic-project-platform",
    )
    assert values["sdk_context"].channel_id == "1"
    assert values["sdk_context"].app_config.app_id == "synthetic-sdk-app"


def test_assembled_profile_keeps_meta_lf_and_game_request_presence():
    from rumi_market.game.login import build_enter_game_request, build_sdk_login_request
    from rumi_market.game.proto import GameProtoCodec

    profile = build(version=VersionResult("1.5.3", 0))
    codec = GameProtoCodec()
    login = build_sdk_login_request(profile)
    message = codec.decode(login.message_name, login.payload).message
    assert message.meta_version.endswith("\n")
    assert message.openid == "" and message.HasField("openid")
    assert message.game_info.udid == "synthetic-game-buvid-雪"
    assert message.game_info.sdk_version == "1.5.3"
    assert message.game_info.remote_ip == "" and message.game_info.HasField("remote_ip")
    assert message.game_info.ad_channel_id == "" and message.game_info.HasField("ad_channel_id")
    assert message.hot_version == 0
    enter = build_enter_game_request(profile)
    entered = codec.decode(enter.message_name, enter.payload).message
    assert entered.b_udid == "synthetic-game-buvid-雪"
    assert (entered.b_global_channel_id, entered.b_brand_id, entered.b_area_id) == (
        "1001", "101", "0",
    )
    assert not any(entered.HasField(name) for name in ("session", "role_id", "remote_ip"))


def test_verified_sms_game_login_projection_preserves_type_six():
    values = inputs()
    profile = build(user=replace(values["user"], login_type="6"))
    assert profile.login_type == "6"


def test_explicit_client_values_are_not_normalized_or_promoted_to_native_provenance():
    context = client_context(game_device_id=" caller-owned-雪 ",
                             device_id_source="caller claims native; not verified by factory")
    profile = build(client_context=context)
    assert profile.device_id == " caller-owned-雪 "
    assert not hasattr(profile, "device_id_source")
    assert not hasattr(profile, "native_identity_verified")
    assert "caller claims native" not in repr(context)
    assert "caller-owned" not in repr(context) and "caller-owned" not in repr(profile)
    with pytest.raises(FrozenInstanceError):
        context.game_device_id = "replacement"


def test_explicit_empty_device_value_is_preserved_with_its_source_declaration():
    profile = build(client_context=client_context(
        game_device_id="", device_id_source="caller explicitly declares unavailable BUVID",
    ))
    assert profile.device_id == ""


@pytest.mark.parametrize("app_ver, result_ver", [
    ("1.5.4", "1.5.3"), ("1.5.3", "1.5.4"), ("2.0.0", "2.0.0"),
    ("synthetic-private-version", "1.5.3"),
])
def test_unknown_or_mismatched_versions_cannot_reuse_the_fixed_template(app_ver, result_ver):
    assert_failure({"sdk_context": sdk_context(app_ver=app_ver),
                    "version": VersionResult(result_ver, 62)},
                   "GAME_PROFILE_VERSION_UNSUPPORTED")


@pytest.mark.parametrize("hot_version", [-1, True, None, "62", 62.0, 2**31])
def test_hot_version_requires_an_explicit_nonnegative_int32_result(hot_version):
    assert_failure({"version": VersionResult("1.5.3", hot_version)})


@pytest.mark.parametrize("hot_version", [0, 62, 2**31 - 1])
def test_verified_hot_version_is_preserved_without_a_default(hot_version):
    assert build(version=VersionResult("1.5.3", hot_version)).hot_version == hot_version


@pytest.mark.parametrize("changes", [
    {"game_device_id": None},
    {"device_id_source": ""}, {"device_id_source": " "}, {"device_id_source": None},
    {"model": None}, {"os_version": None}, {"network": None}, {"platform": None},
    {"time_zone_hours": True}, {"time_zone_hours": -24}, {"time_zone_hours": 24},
    {"time_zone_hours": -5.5}, {"language_type": True}, {"language_type": 2**31},
])
def test_project_context_requires_declared_identity_and_typed_runtime_values(changes):
    assert_failure({"client_context": client_context(**changes)})


@pytest.mark.parametrize("changes", [
    {"game_uid": ""}, {"game_uid": None}, {"token": "synthetic-private-token"},
    {"token": SecretStr("")}, {"token": SecretStr(17)}, {"login_type": "1"},
])
def test_user_must_be_the_supported_game_password_projection(changes):
    user = SdkGameUser("synthetic-game-uid", SecretStr("synthetic-game-token"), "0")
    assert_failure({"user": replace(user, **changes)})


@pytest.mark.parametrize("field", ["sdk_context", "user", "version", "client_context"])
def test_wrong_context_types_use_a_fixed_private_safe_error(field):
    assert_failure({field: {"synthetic-private-data": "synthetic-private-data"}})


def test_malformed_nested_sdk_context_uses_a_fixed_private_safe_error():
    assert_failure({"sdk_context": replace(sdk_context(), app_config=None)})
    assert_failure({"sdk_context": object.__new__(SdkRequestContext)})


def test_client_identity_and_source_have_no_optional_default():
    from rumi_market.game.profile import ProjectClientContext

    with pytest.raises(TypeError):
        ProjectClientContext(model="model", os_version="os", network="network",
                             time_zone_hours=0, language_type=1, platform="platform")
