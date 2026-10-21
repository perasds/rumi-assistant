from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest


def success(**fields):
    return {"code": 0, "uid": "synthetic-account-uid",
            "access_key": "synthetic-private-access-key", **fields}


def test_password_callback_uses_account_uid_and_overrides_server_login_type():
    from rumi_market.sdk_user import project_password_login

    payload = success(game_open_id="synthetic-game-uid", login_type="9",
                      token="synthetic-wrong-token")
    before = dict(payload)
    user = project_password_login(MappingProxyType(payload))
    assert user.game_uid == "synthetic-account-uid"
    assert user.login_type == "0"
    assert user.token.get_secret_value() == "synthetic-private-access-key"
    assert payload == before
    assert "synthetic" not in repr(user)
    assert "synthetic-private-access-key" not in repr(user.token)
    with pytest.raises(FrozenInstanceError):
        user.game_uid = "changed"


@pytest.mark.parametrize("flag", [False, None])
def test_false_or_default_open_id_flag_uses_only_account_uid(flag):
    from rumi_market.sdk_user import project_password_login

    user = project_password_login(success(uid=123456,
        game_open_id_enable=flag, game_open_id="synthetic-ignored-game-uid"))
    assert user.game_uid == "123456"


def test_enabled_open_id_flag_selects_game_uid_and_preserves_token():
    from rumi_market.sdk_user import project_password_login

    user = project_password_login(success(game_open_id_enable=True,
        game_open_id="synthetic-game-uid"))
    assert user.game_uid == "synthetic-game-uid"
    assert user.token.get_secret_value() == "synthetic-private-access-key"
    assert user.login_type == "0"


@pytest.mark.parametrize("open_id", [None, "", False, {}])
def test_enabled_open_id_never_falls_back_to_raw_account_uid(open_id):
    from rumi_market.sdk_user import SdkUserError, project_password_login

    with pytest.raises(SdkUserError, match="^SDK_USER_INVALID$"):
        project_password_login(success(game_open_id_enable=True, game_open_id=open_id))


@pytest.mark.parametrize("uid", [None, "", False, {}])
def test_disabled_open_id_never_falls_back_to_unselected_game_uid(uid):
    from rumi_market.sdk_user import SdkUserError, project_password_login

    with pytest.raises(SdkUserError, match="^SDK_USER_INVALID$"):
        project_password_login(success(uid=uid, game_open_id="synthetic-game-uid"))


@pytest.mark.parametrize("flag", ["false", "true", 0, 1, [], {}])
def test_open_id_flag_requires_boolean_json_type_without_truthiness(flag):
    from rumi_market.sdk_user import SdkUserError, project_password_login

    with pytest.raises(SdkUserError, match="^SDK_USER_INVALID$"):
        project_password_login(success(game_open_id_enable=flag,
            game_open_id="synthetic-game-uid"))


@pytest.mark.parametrize("fields", [
    {"code": False}, {"code": 0.0}, {"code": 500101}, {"code": None},
    {"access_key": ""}, {"access_key": None}, {"access_key": False},
])
def test_invalid_success_data_raises_fixed_secret_safe_error(fields):
    from rumi_market.sdk_user import SdkUserError, project_password_login

    payload = success(message="synthetic-private-response", **fields)
    with pytest.raises(SdkUserError) as failure:
        project_password_login(payload)
    assert str(failure.value) == "SDK_USER_INVALID"
    assert "synthetic" not in repr(failure.value)
    assert failure.value.__context__ is None


def test_string_success_code_is_accepted():
    from rumi_market.sdk_user import project_password_login

    assert project_password_login(success(code="0")).game_uid == "synthetic-account-uid"


@pytest.mark.parametrize("payload", [None, [], "synthetic-private-response"])
def test_non_object_response_is_rejected_without_echo(payload):
    from rumi_market.sdk_user import SdkUserError, project_password_login

    with pytest.raises(SdkUserError, match="^SDK_USER_INVALID$") as failure:
        project_password_login(payload)
    assert "synthetic" not in repr(failure.value)


@pytest.mark.parametrize("flag,expected", [(True, "synthetic-game-uid"), (False, "synthetic-account-uid"),
                                           (None, "synthetic-account-uid")])
def test_sms_callback_has_its_own_game_type_and_same_selected_token_boundary(flag, expected):
    import rumi_market.sdk_user as module

    assert callable(getattr(module, "project_phone_sms_login", None)), "SMS projection is missing"
    payload = success(game_open_id_enable=flag, game_open_id="synthetic-game-uid", login_type="0")
    before = dict(payload)
    user = module.project_phone_sms_login(payload)
    assert user.game_uid == expected and user.login_type == "6"
    assert user.token.get_secret_value() == "synthetic-private-access-key"
    assert payload == before
    assert "synthetic" not in repr(user)


@pytest.mark.parametrize("fields", [{"code": 500001}, {"access_key": ""}, {"game_open_id_enable": "true"},
    {"game_open_id_enable": True, "game_open_id": None}, {"uid": False}])
def test_sms_projection_rejects_invalid_success_without_echo(fields):
    import rumi_market.sdk_user as module

    assert callable(getattr(module, "project_phone_sms_login", None)), "SMS projection is missing"
    with pytest.raises(module.SdkUserError, match="^SDK_USER_INVALID$"):
        module.project_phone_sms_login(success(**fields))
