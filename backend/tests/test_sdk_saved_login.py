import copy
import json
from urllib.parse import parse_qs

import httpx
import pytest

from rumi_market.errors import ApiError
from rumi_market.sdk_http import SdkHttpClient
from rumi_market.sdk_primitives import sdk_sign


TOKEN_PATH = "/api/external/user.token.oauth.login/v3"
RENEW_PATH = "/api/external/token.renewal/v3"


def saved_state():
    return {"version": 1, "uid": "synthetic-sdk-uid", "access_key": "synthetic-saved-token",
        "game_user": {"game_uid": "synthetic-game-uid", "login_type": "0"},
        "expires": None, "renewal_expires": None,
        "identity": {"udid": "synthetic-udid", "bd_id": "synthetic-bdid",
                     "game_device_id": "synthetic-game-device"}}


def test_saved_state_validation_copies_only_fixed_schema_and_hides_bad_payload():
    from rumi_market.sdk_saved_login import copy_login_state
    state = saved_state()
    copied = copy_login_state(state)
    assert copied == state and copied is not state and copied["identity"] is not state["identity"]
    for bad in [None, {}, {**state, "password": "synthetic-private-password"},
                {**state, "access_key": ""}, {**state, "expires": True},
                {**state, "uid": "x" * 129}, {**state, "access_key": "secret\nheader"},
                {**state, "identity": {**state["identity"], "game_device_id": 7}},
                {**state, "game_user": {**state["game_user"], "login_type": "6"}}]:
        with pytest.raises(ApiError) as caught:
            copy_login_state(bad)
        assert caught.value.code == "AUTH_REJECTED" and caught.value.status_code == 401
        assert "synthetic" not in str(caught.value.body())
        assert caught.value.__context__ is caught.value.__cause__ is None
    json.dumps(copied)


@pytest.mark.anyio
@pytest.mark.parametrize("expiry,renew_failure", [(None, None), ("9999999999999", None),
                                                  ("1000", None), ("1000", -1), ("1000", "network")])
async def test_token_login_uses_same_signed_identity_and_optional_renewal(expiry, renew_failure):
    calls = []
    state = saved_state()
    state["expires"] = expiry
    before = copy.deepcopy(state)
    def handler(request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append(request.url.path)
        assert form["uid"] == state["uid"] and form["access_key"] == state["access_key"]
        assert form["udid"] == state["identity"]["udid"] and form["version"] == "1"
        assert form["sign"] == sdk_sign(form, "synthetic-app-key")
        assert request.headers["x-synthetic-rule"] == form["sign"]
        assert not {"user_id", "pwd", "app_key", "refresh_token"} & form.keys()
        if request.url.path == RENEW_PATH:
            if renew_failure == "network":
                raise httpx.ConnectError("synthetic-private-network", request=request)
            return httpx.Response(200, json={"code": renew_failure or 0, "expires": "9999999999999"})
        assert request.url.path == TOKEN_PATH
        return httpx.Response(200, json={"code": 0, "uid": state["uid"],
            "access_key": "synthetic-new-token", "game_open_id_enable": True,
            "game_open_id": state["game_user"]["game_uid"], "login_type": "untrusted-response-type"})
    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        common_parameters=lambda path: {"udid": state["identity"]["udid"], "version": "1"},
        extra_headers=lambda timestamp, sign, params: {"x-synthetic-rule": sign},
        transport=httpx.MockTransport(handler), clock_ms=lambda: 2000)
    try:
        credentials = await client.login_token(state)
        assert calls == ([RENEW_PATH, TOKEN_PATH] if expiry == "1000" else [TOKEN_PATH])
        assert credentials.game_user.game_uid == state["game_user"]["game_uid"]
        assert credentials.game_user.login_type == "0"
        assert credentials.access_key.get_secret_value() == "synthetic-new-token"
        assert credentials.expires == ("9999999999999" if expiry == "1000" and renew_failure is None else expiry)
        expected_threshold = (None if credentials.expires is None else
                              "1334" if credentials.expires == "1000" else "6666666667332")
        assert credentials.renewal_expires == expected_threshold
        assert state == before and "synthetic-new-token" not in repr(credentials)
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("response,code", [({"code": -1, "message": "synthetic-secret"}, "AUTH_REJECTED"),
    ({"code": 0, "uid": "other", "access_key": "synthetic-secret"}, "AUTH_REJECTED"),
    ({"code": 0, "uid": "synthetic-sdk-uid", "access_key": "synthetic-secret",
      "game_open_id_enable": True, "game_open_id": "other"}, "AUTH_REJECTED")])
async def test_token_rejection_and_account_switch_never_restore(response, code):
    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        common_parameters=lambda path: {"version": "1"}, extra_headers=lambda *args: {},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    try:
        with pytest.raises(ApiError) as caught:
            await client.login_token(saved_state())
        assert caught.value.code == code
        assert "synthetic" not in str(caught.value.body())
        assert caught.value.__context__ is caught.value.__cause__ is None
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [60005, "60006", "http", "network"])
async def test_token_temporary_failures_remain_retryable_without_offline_login(failure):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError("synthetic-private-network", request=request)
        return httpx.Response(503 if failure == "http" else 200,
                              json={"code": failure, "message": "synthetic-private-upstream"})
    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        common_parameters=lambda path: {"version": "1"}, extra_headers=lambda *args: {},
        transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as caught:
            await client.login_token(saved_state())
        assert caught.value.code == "AUTH_UPSTREAM_FAILED" and caught.value.status_code == 503
        assert caught.value.retryable and "synthetic" not in str(caught.value.body())
        assert caught.value.__context__ is caught.value.__cause__ is None
    finally:
        await client.close()


@pytest.mark.parametrize("expiry,now,expected", [(None, 1000, None), ("10000", 1000, "7000"),
    ("1000", 2000, "1334"), ("1700000001000", 1700000000000, "1700000000666")])
def test_native_expiry_threshold_uses_absolute_milliseconds_and_java_truncation(expiry, now, expected):
    from rumi_market.sdk_saved_login import renewal_threshold
    assert renewal_threshold(expiry, now) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("now,renewal", [(6999, False), (7000, False), (7001, True)])
async def test_saved_native_threshold_renews_before_expiry_and_recomputes_after_success(now, renewal):
    state, calls = saved_state(), []
    state["expires"], state["renewal_expires"] = "10000", "7000"
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == RENEW_PATH:
            return httpx.Response(200, json={"code": 0, "expires": "20000"})
        return httpx.Response(200, json={"code": 0, "uid": state["uid"],
            "access_key": "synthetic-new-token", "game_open_id_enable": True,
            "game_open_id": state["game_user"]["game_uid"]})
    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        common_parameters=lambda path: {"version": "1"}, extra_headers=lambda *args: {},
        transport=httpx.MockTransport(handler), clock_ms=lambda: now)
    try:
        credentials = await client.login_token(state)
        assert calls == ([RENEW_PATH, TOKEN_PATH] if renewal else [TOKEN_PATH])
        assert credentials.expires == ("20000" if renewal else "10000")
        if renewal:
            assert credentials.renewal_expires == "15667"
        assert state["expires"] == "10000" and state["renewal_expires"] == "7000"
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("changed,missing", [({}, "uid"), ({"uid": None}, None),
    ({"uid": True}, None), ({"uid": ""}, None), ({"uid": "x" * 129}, None),
    ({"uid": "bad\nuid"}, None), ({"uid": 1.5}, None),
    ({}, "access_key"), ({"access_key": None}, None), ({"access_key": ""}, None),
    ({"access_key": "x" * 4097}, None), ({"access_key": "bad\ntoken"}, None),
    ({}, "game_open_id"), ({"game_open_id": []}, None), ({"game_open_id": ""}, None),
    ({"game_open_id": "x" * 129}, None), ({"game_open_id": "bad\ngame-id"}, None),
    ({"game_open_id_enable": "true"}, None), ({"expires": True}, None),
    ({"uid": "other-legal-id", "access_key": None}, None)])
async def test_malformed_success_payload_is_retryable_and_never_revokes_saved_identity(changed, missing):
    state = saved_state()
    response = {"code": 0, "uid": state["uid"], "access_key": "synthetic-token",
        "game_open_id_enable": True, "game_open_id": state["game_user"]["game_uid"], **changed}
    if missing is not None:
        response.pop(missing)
    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        common_parameters=lambda path: {"version": "1"}, extra_headers=lambda *args: {},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    try:
        with pytest.raises(ApiError) as caught:
            await client.login_token(state)
        assert caught.value.code == "AUTH_UPSTREAM_FAILED" and caught.value.status_code == 503
        assert caught.value.retryable and "synthetic" not in str(caught.value.body())
        assert caught.value.__context__ is caught.value.__cause__ is None
        assert state == saved_state()
    finally:
        await client.close()
