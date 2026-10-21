import base64
import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from rumi_market.errors import ApiError
from rumi_market.sdk_http import SdkHttpClient
from rumi_market.sdk_primitives import sdk_sign


@pytest.fixture
def synthetic_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_pem(key):
    return key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


def client_for(handler, *, extra_headers=None):
    return SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        common_parameters=lambda path: {"app_id":"synthetic-game", "bd_id":"synthetic-device"},
        extra_headers=extra_headers or (lambda timestamp, sign, params: {"x-synthetic-rule":sign}),
        transport=httpx.MockTransport(handler), clock_ms=lambda:1234567890000)


@pytest.mark.anyio
async def test_normal_password_flow_signs_raw_base64_and_returns_private_credentials(synthetic_key):
    calls=[]
    def handler(request):
        form={k:v[0] for k,v in parse_qs(request.content.decode()).items()}
        calls.append(request.url.path)
        assert form["timestamp"]=="1234567890000"
        assert form["domain"]=="line1-sdk-center-login-sh.biligame.net"
        assert form["sign"]==sdk_sign(form,"synthetic-app-key")
        assert request.headers["x-synthetic-rule"]==form["sign"]
        if request.url.path.endswith("/issue/cipher/v3"):
            assert form["cipher_type"]=="bili_login_rsa"
            return httpx.Response(200,json={"code":0,"hash":"synthetic-prefix:","cipher_key":public_pem(synthetic_key)})
        assert request.url.path=="/api/external/login/v3"
        assert form["user_id"]=="synthetic-account"
        assert b"%25" not in request.content
        assert synthetic_key.decrypt(base64.b64decode(form["pwd"]),padding.PKCS1v15())==b"synthetic-prefix:synthetic-password"
        return httpx.Response(200,json={"code":0,"uid":123456,"access_key":"synthetic-access-token","game_open_id":None})
    client=client_for(handler)
    try:
        session=await client.login("synthetic-account","synthetic-password")
        assert session.uid=="123456"
        assert session.access_key.get_secret_value()=="synthetic-access-token"
        assert "synthetic-access-token" not in repr(session)
        assert "synthetic-app-key" not in repr(client)
        assert len(calls)==2
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("enabled,expected", [(False, "123456"), (True, "synthetic-game-id")])
async def test_password_login_returns_verified_game_callback_user(synthetic_key, enabled, expected):
    def handler(request):
        if request.url.path.endswith("/issue/cipher/v3"):
            return httpx.Response(200, json={"code": 0, "hash": "",
                                            "cipher_key": public_pem(synthetic_key)})
        return httpx.Response(200, json={"code": 0, "uid": 123456,
            "access_key": "synthetic-access-token", "game_open_id_enable": enabled,
            "game_open_id": "synthetic-game-id", "login_type": "untrusted-upstream-type"})
    client = client_for(handler)
    try:
        credentials = await client.login("synthetic-account", "synthetic-password")
        assert credentials.uid == "123456"
        assert credentials.game_user.game_uid == expected
        assert credentials.game_user.login_type == "0"
        assert credentials.game_user.token.get_secret_value() == "synthetic-access-token"
        assert "synthetic-access-token" not in repr(credentials)
        assert "synthetic-game-id" not in repr(credentials.game_user)
    finally:
        await client.close()


@pytest.mark.anyio
async def test_enabled_open_id_without_selected_identifier_is_not_a_game_login(synthetic_key):
    def handler(request):
        if request.url.path.endswith("/issue/cipher/v3"):
            return httpx.Response(200, json={"code": 0, "hash": "",
                                            "cipher_key": public_pem(synthetic_key)})
        return httpx.Response(200, json={"code": 0, "uid": 123456,
            "access_key": "synthetic-access-token", "game_open_id_enable": True})
    client = client_for(handler)
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account", "synthetic-password")
        assert failure.value.code == "AUTH_UPSTREAM_FAILED"
        assert failure.value.__context__ is failure.value.__cause__ is None
        assert "synthetic-access-token" not in str(failure.value.body())
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("code",[1,"-1"])
async def test_cipher_rejection_stops_before_password_login_without_echo(code):
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200,json={"code":code,"message":"synthetic-password-secret"})
    client=client_for(handler)
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account","synthetic-password-secret")
        assert failure.value.code=="AUTH_REJECTED"
        assert "secret" not in failure.value.message
        assert len(calls)==1
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("body",[{"code":False},{"code":0,"hash":"prefix"},[],{"code":"0","hash":"prefix","cipher_key":"bad"}])
async def test_invalid_cipher_responses_fail_before_login(body):
    client=client_for(lambda request:httpx.Response(200,json=body))
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account","synthetic-password")
        assert failure.value.code=="AUTH_UPSTREAM_FAILED"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_transport_errors_do_not_keep_raw_error_as_exception_context():
    def handler(request):
        raise httpx.ConnectError("synthetic-token-secret",request=request)
    client=client_for(handler)
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account","synthetic-password")
        assert failure.value.code=="AUTH_UPSTREAM_FAILED"
        assert failure.value.__context__ is None
        assert failure.value.__cause__ is None
    finally:
        await client.close()


@pytest.mark.anyio
async def test_redirect_never_sends_credentials_to_another_origin():
    calls=[]
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(307,headers={"location":"https://example.invalid/credential-sink"})
    client=client_for(handler)
    try:
        with pytest.raises(ApiError):
            await client.login("synthetic-account","synthetic-password")
        assert calls==["line1-sdk-center-login-sh.biligame.net"]
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("result",[{"code":1,"message":"synthetic-secret"},
                                  {"code":0,"uid":True,"access_key":"synthetic-secret"},
                                  {"code":0,"uid":123}, {"code":0,"uid":123,"access_key":""}])
async def test_login_rejection_or_invalid_credentials_never_become_success(synthetic_key,result):
    def handler(request):
        if request.url.path.endswith("/issue/cipher/v3"):
            return httpx.Response(200,json={"code":"0","hash":"","cipher_key":public_pem(synthetic_key)})
        return httpx.Response(200,json=result)
    client=client_for(handler)
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account","synthetic-password")
        assert failure.value.code in {"AUTH_REJECTED","AUTH_UPSTREAM_FAILED"}
        assert "synthetic-secret" not in str(failure.value.body())
    finally:
        await client.close()


@pytest.mark.anyio
async def test_header_generation_failure_prevents_all_http_calls():
    calls=[]
    def headers(*args):
        raise ValueError("synthetic-app-key-secret")
    client=client_for(lambda request:calls.append(request),extra_headers=headers)
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account","synthetic-password")
        assert failure.value.code=="AUTH_PROTOCOL_UNVERIFIED"
        assert failure.value.__context__ is None
        assert calls==[]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_close_clears_cookie_and_prevents_new_auth_requests():
    client=client_for(lambda request:httpx.Response(200,json={"code":1}))
    client._http.cookies.set("synthetic-session","synthetic-secret-token")
    await client.close()
    assert len(client._http.cookies)==0
    assert client._app_key.get_secret_value()==""
    with pytest.raises(ApiError) as failure:
        await client.login("synthetic-account","synthetic-password")
    assert failure.value.code=="AUTH_PROTOCOL_UNVERIFIED"


@pytest.mark.anyio
async def test_close_failure_clears_secrets_without_raw_exception_context():
    class BrokenClose(httpx.AsyncBaseTransport):
        async def aclose(self):
            raise RuntimeError("synthetic-secret-token")

    client=SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
                         common_parameters=lambda path:{}, extra_headers=lambda *args:{},
                         transport=BrokenClose())
    client._http.cookies.set("synthetic-session", "synthetic-secret-token")
    with pytest.raises(ApiError) as failure:
        await client.close()
    assert failure.value.code=="AUTH_UPSTREAM_FAILED"
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert "synthetic-secret" not in str(failure.value.body())
    assert len(client._http.cookies)==0
    assert client._app_key.get_secret_value()==""
    assert client._parameters is None and client._headers is None


@pytest.mark.parametrize("origin",["http://line1-sdk-center-login-sh.biligame.net","https://example.invalid", "https://line1-sdk-center-login-sh.biligame.net/?token=secret"])
def test_rejects_unverified_origin_before_any_network(origin):
    with pytest.raises(ValueError,match="^Invalid SDK login origin$"):
        SdkHttpClient(origin,"synthetic-app-key",common_parameters=lambda path:{},extra_headers=lambda *args:{})


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["p.biligame.com", "line3-sdk-center-login-sh.biligame.net", "line3-sdkcenter-login.bilibiligame.net"])
async def test_config_client_signs_verified_request_and_cannot_send_login(host):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert request.url.path == "/api/external/config/v3"
        assert form["version"] == "3" and form["domain"] == host
        assert form["sign"] == sdk_sign(form, "synthetic-app-key")
        assert "user_id" not in form and "pwd" not in form
        return httpx.Response(200, json={"code": 0, "crule": "synthetic-config-rule"})
    client = SdkHttpClient("https://" + host, "synthetic-app-key", purpose="config",
        common_parameters=lambda path: {"app_id": "synthetic-game", "version": "1"},
        extra_headers=lambda *args: {}, transport=httpx.MockTransport(handler))
    try:
        assert (await client.fetch_config())["code"] == 0
        with pytest.raises(ApiError):
            await client.login("synthetic-account", "synthetic-password")
        with pytest.raises(ApiError):
            await client._post("/api/external/login/v3", {"pwd": "synthetic-password"})
        assert calls == ["/api/external/config/v3"]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_login_client_cannot_fetch_config():
    calls = []
    client = client_for(lambda request: calls.append(request))
    try:
        with pytest.raises(ApiError):
            await client.fetch_config()
        assert calls == []
    finally:
        await client.close()


@pytest.mark.parametrize("origin", ["https://line1-sdk-center-login-sh.biligame.net", "http://p.biligame.com", "https://p.biligame.com.evil.invalid"])
def test_config_purpose_rejects_other_origins(origin):
    with pytest.raises(ValueError):
        SdkHttpClient(origin, "synthetic-app-key", purpose="config",
            common_parameters=lambda path: {}, extra_headers=lambda *args: {})


@pytest.mark.parametrize("raw, expected", [
    (-2147483648, -2147483648), (2147483647, 2147483647),
    ("-00123", -123), ("+17", 17), ("00031", 31),
    (None, None), (True, None), (1.5, None),
    ("synthetic-private-response", None), ("１", None), (" 17", None),
    ("17 ", None), ("0x17", None), ("17.0", None),
    (2147483648, None), (-2147483649, None), ("9" * 5000, None),
], ids=[f"code-{index}" for index in range(17)])
def test_sdk_response_error_keeps_only_bounded_public_code_and_fixed_body(raw, expected):
    from rumi_market.sdk_http import SdkResponseError

    error = SdkResponseError(response_code=raw, request_stage="sdk_password")
    assert isinstance(error, ApiError) and error.code == "AUTH_REJECTED"
    assert error.response_code == expected and error.request_stage == "sdk_password"
    assert error.status_code == 401 and error.retryable is False
    assert error.body() == {"error": {"code": "AUTH_REJECTED",
        "message": "游戏认证信息未通过验证", "retryable": False}}
    assert "synthetic" not in str(error) and "synthetic" not in repr(error)
    assert not any(hasattr(error, name) for name in
                   ("response", "request", "path", "password", "payload", "raw_code"))


@pytest.mark.parametrize("stage", ["", "synthetic-private-path", None])
def test_sdk_response_error_cannot_keep_a_freeform_request_stage(stage):
    from rumi_market.sdk_http import SdkResponseError

    with pytest.raises(ValueError, match="^Invalid SDK response stage$"):
        SdkResponseError(response_code=-1, request_stage=stage)


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["sdk_config", "sdk_cipher", "sdk_password", "sdk_captcha"])
@pytest.mark.parametrize("raw, expected", [
    (-17, -17), ("-0017", -17), ("synthetic-private-response", None),
])
async def test_sdk_business_rejection_identifies_only_the_fixed_request_stage(synthetic_key, stage, raw, expected):
    from rumi_market.sdk_http import SdkResponseError

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if stage == "sdk_password" and request.url.path.endswith("/issue/cipher/v3"):
            return httpx.Response(200, json={"code": 0, "hash": "",
                                            "cipher_key": public_pem(synthetic_key)})
        return httpx.Response(200, json={"code": raw, "message": "synthetic-private-response",
            "uid": "synthetic-private-response", "pwd": "synthetic-private-response"})

    if stage == "sdk_config":
        client = SdkHttpClient("https://line3-sdk-center-login-sh.biligame.net", "synthetic-app-key",
            purpose="config", common_parameters=lambda path: {}, extra_headers=lambda *args: {},
            transport=httpx.MockTransport(handler))
    else:
        client = client_for(handler)
    try:
        with pytest.raises(SdkResponseError) as failure:
            if stage == "sdk_config":
                await client.fetch_config()
            elif stage == "sdk_captcha":
                await client.start_captcha()
            else:
                await client.login("synthetic-account", "synthetic-private-password")
        assert failure.value.response_code == expected and failure.value.request_stage == stage
        assert failure.value.code == "AUTH_REJECTED"
        assert failure.value.__context__ is None and failure.value.__cause__ is None
        assert "synthetic" not in str(failure.value.body())
        assert len(calls) == (2 if stage == "sdk_password" else 1)
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("code", [0, "0"])
async def test_start_captcha_returns_top_level_payload_with_empty_business(code):
    calls = []
    payload = {"code": code, "url": "https://example.invalid/synthetic-captcha",
               "captcha_type": "synthetic-type", "data": {"synthetic": "nested"}}

    def handler(request):
        calls.append(request.url.path)
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        unsigned = {"app_id": "synthetic-game", "version": "1",
                    "timestamp": "1234567890000", "domain": "line1-sdk-center-login-sh.biligame.net"}
        assert form == {**unsigned, "sign": sdk_sign(unsigned, "synthetic-app-key")}
        assert request.headers["x-synthetic-rule"] == form["sign"]
        return httpx.Response(200, json=payload)

    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        common_parameters=lambda path: {"app_id": "synthetic-game", "version": "1"},
        extra_headers=lambda timestamp, sign, params: {"x-synthetic-rule": sign},
        transport=httpx.MockTransport(handler), clock_ms=lambda: 1234567890000)
    try:
        assert await client.start_captcha() == payload
        assert calls == ["/api/client/start_captcha"]
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("blocked_by", ["config-purpose", "closed"])
async def test_start_captcha_is_blocked_before_parameters_and_http(blocked_by):
    calls = []
    client = SdkHttpClient("https://line3-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        purpose="config" if blocked_by == "config-purpose" else "login",
        common_parameters=lambda path: calls.append("parameters") or {},
        extra_headers=lambda *args: calls.append("headers") or {},
        transport=httpx.MockTransport(lambda request: calls.append("http") or httpx.Response(200, json={"code": 0})))
    try:
        if blocked_by == "closed":
            await client.close()
        with pytest.raises(ApiError) as failure:
            await client.start_captcha()
        assert failure.value.code == "AUTH_PROTOCOL_UNVERIFIED"
        assert failure.value.__context__ is failure.value.__cause__ is None
        assert calls == []
    finally:
        if blocked_by != "closed":
            await client.close()


def captcha_flow(synthetic_key, *, first_code=200007, cipher_code=0, retry_code=0, bootstrap_wait=None):
    from rumi_market.sdk_captcha import SdkCaptchaConfig

    config = SdkCaptchaConfig("https://line1-sdk-center-login-sh.biligame.net",
                             "https://example.invalid/verify",
                             ("https://line1-sdk-center-login-sh.biligame.net",))
    calls = []
    completion = {"challenge": "synthetic-human-challenge", "validate": "synthetic-human-validate",
        "seccode": "synthetic-human-seccode", "code": "synthetic-human-code",
        "captcha": "synthetic-human-captcha", "userid": "synthetic-human-userid", "token": "synthetic-human-token",
        "user_id": "synthetic-injected-account", "pwd": "synthetic-injected-pwd",
        "sign": "synthetic-injected-sign", "timestamp": "synthetic-injected-time",
        "captcha_type": "synthetic-injected-type", "ctoken": "synthetic-injected-ctoken"}

    async def handler(request):
        form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        calls.append((request.url.path, form))
        assert form["sign"] == sdk_sign(form, "synthetic-app-key")
        assert request.headers["x-synthetic-rule"] == form["sign"]
        if request.url.path.endswith("/issue/cipher/v3"):
            return httpx.Response(200, json={"code": cipher_code, "hash": "prefix:", "cipher_key": public_pem(synthetic_key)})
        if request.url.path == "/api/client/start_captcha":
            assert form["version"] == "1" and not {"pwd", "user_id"} & form.keys()
            if bootstrap_wait is not None:
                await bootstrap_wait()
            return httpx.Response(200, json={"code": 0, "captcha_type": "geetest",
                "challenge": "synthetic-start-challenge", "gt": "synthetic-start-gt", "ctoken": "synthetic-start-ctoken"})
        assert form["version"] == "3" and form["user_id"] == "synthetic-account"
        assert synthetic_key.decrypt(base64.b64decode(form["pwd"]), padding.PKCS1v15()) == b"prefix:synthetic-password"
        number = sum(path.endswith("/login/v3") for path, _ in calls)
        code = first_code if number == 1 else retry_code
        return httpx.Response(200, json={"code": code, "uid": "synthetic-uid", "access_key": "synthetic-token",
                                        "message": "synthetic-private-message"})

    ticks = iter(range(1234567890000, 1234567890010))
    client = SdkHttpClient(config.login_origin, "synthetic-app-key",
        common_parameters=lambda path: {"app_id": "synthetic-game", "version": "1"},
        extra_headers=lambda timestamp, sign, params: {"x-synthetic-rule": sign},
        transport=httpx.MockTransport(handler), clock_ms=lambda: next(ticks))
    return client, config, calls, json.dumps(completion), completion


@pytest.mark.anyio
@pytest.mark.parametrize("code", [200007, "200007"])
async def test_human_captcha_retries_once_with_core_projection_same_cipher_and_new_signature(synthetic_key, code):
    client, config, calls, raw, completion = captcha_flow(synthetic_key, first_code=code)
    humans = []

    async def human(challenge, supplied_config):
        assert supplied_config is config and challenge.ctoken == "synthetic-start-ctoken"
        humans.append(True)
        return raw

    try:
        result = await client.login("synthetic-account", "synthetic-password",
                                    captcha_config=config, captcha_handler=human)
        assert result.uid == "synthetic-uid" and humans == [True]
        assert [path for path, _ in calls] == ["/api/external/issue/cipher/v3", "/api/external/login/v3",
                                              "/api/client/start_captcha", "/api/external/login/v3"]
        first, retry = calls[1][1], calls[3][1]
        assert retry["pwd"] == first["pwd"] and retry["user_id"] == first["user_id"]
        assert retry["timestamp"] != first["timestamp"] and retry["sign"] != first["sign"]
        added = {"captcha_type": "geetest", "ctoken": "synthetic-start-ctoken", "challenge": completion["challenge"],
            "validate": completion["validate"], "seccode": completion["seccode"], "img_code": completion["code"],
            "gt_user_id": completion["userid"], "captcha_code": completion["captcha"], "image_token": completion["token"]}
        assert {key: retry[key] for key in added} == added
        assert retry.keys() - first.keys() == added.keys()
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("code,cipher_code,enabled", [(200006, 0, True), ("+200007", 0, True),
    ("0200007", 0, True), (200007, 0, False), (0, 200007, True)])
async def test_only_exact_password_captcha_code_and_enabled_handler_start_human_flow(synthetic_key, code, cipher_code, enabled):
    from rumi_market.sdk_http import SdkResponseError
    client, config, calls, raw, _ = captcha_flow(synthetic_key, first_code=code, cipher_code=cipher_code)
    async def human(*args):
        pytest.fail("unexpected human callback")
    try:
        with pytest.raises(SdkResponseError):
            await client.login("synthetic-account", "synthetic-password", captcha_config=config,
                               captcha_handler=human if enabled else None)
        assert len(calls) == (1 if cipher_code else 2)
    finally:
        await client.close()


@pytest.mark.anyio
async def test_repeated_captcha_code_does_not_start_a_second_human_flow(synthetic_key):
    from rumi_market.sdk_http import SdkResponseError
    client, config, calls, raw, _ = captcha_flow(synthetic_key, retry_code=200007)
    humans = []
    async def human(*args):
        humans.append(True)
        return raw
    try:
        with pytest.raises(SdkResponseError) as failure:
            await client.login("synthetic-account", "synthetic-password", captcha_config=config, captcha_handler=human)
        assert failure.value.request_stage == "sdk_password" and failure.value.response_code == 200007
        assert len(calls) == 4 and humans == [True]
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("raw,code", [("", "AUTH_CAPTCHA_CANCELLED"), ("null", "AUTH_CAPTCHA_CANCELLED"),
    ("synthetic-private-invalid-json", "AUTH_CAPTCHA_FAILED"), ({"validate": "synthetic-private"}, "AUTH_CAPTCHA_FAILED")])
async def test_cancelled_or_invalid_completion_never_reposts_or_echoes_private_fields(synthetic_key, raw, code):
    client, config, calls, _, _ = captcha_flow(synthetic_key)
    async def human(*args):
        return raw
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account", "synthetic-password", captcha_config=config, captcha_handler=human)
        assert failure.value.code == code and len(calls) == 3
        assert failure.value.__context__ is failure.value.__cause__ is None
        assert "synthetic" not in str(failure.value.body())
        from rumi_market.errors import provider_error
        safe = provider_error(failure.value)
        assert safe.code == code and safe.status_code == 401 and not safe.retryable
    finally:
        await client.close()


@pytest.mark.anyio
async def test_unknown_human_exception_is_fixed_and_never_reposts(synthetic_key):
    client, config, calls, _, _ = captcha_flow(synthetic_key)
    async def human(*args):
        raise RuntimeError("synthetic-private-handler")
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account", "synthetic-password", captcha_config=config, captcha_handler=human)
        assert failure.value.code == "AUTH_CAPTCHA_FAILED" and len(calls) == 3
        assert failure.value.__context__ is failure.value.__cause__ is None
        assert "synthetic" not in str(failure.value.body())
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["AUTH_CAPTCHA_CANCELLED", "AUTH_CAPTCHA_UNAVAILABLE",
    "AUTH_CAPTCHA_PAGE_UNAVAILABLE", "AUTH_CAPTCHA_TIMEOUT", "AUTH_CAPTCHA_INVALID"])
async def test_owned_handler_error_keeps_only_whitelisted_code_and_fixed_safe_copy(synthetic_key, code):
    from rumi_market.errors import provider_error
    client, config, calls, _, _ = captcha_flow(synthetic_key)
    original = ApiError(code, "synthetic-private-handler-message", 599, True)
    async def human(*args):
        raise original
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account", "synthetic-password", captcha_config=config, captcha_handler=human)
        assert failure.value is not original and failure.value.code == code
        assert failure.value.status_code != 599 and "synthetic" not in str(failure.value.body())
        assert failure.value.__context__ is failure.value.__cause__ is None and len(calls) == 3
        assert provider_error(failure.value).body() == failure.value.body()
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["unknown-api", "str-raises"])
async def test_untrusted_handler_error_cannot_escape_fixed_error_projection(synthetic_key, kind):
    from rumi_market.sdk_captcha import SdkCaptchaError
    class BadString(SdkCaptchaError):
        def __str__(self):
            raise RuntimeError("synthetic-private-error-str")
    client, config, calls, _, _ = captcha_flow(synthetic_key)
    async def human(*args):
        raise ApiError("synthetic-private-code", "synthetic-private-message") if kind == "unknown-api" else BadString()
    try:
        with pytest.raises(ApiError) as failure:
            await client.login("synthetic-account", "synthetic-password", captcha_config=config, captcha_handler=human)
        assert failure.value.code == "AUTH_CAPTCHA_FAILED"
        assert failure.value.__context__ is failure.value.__cause__ is None and len(calls) == 3
        assert "synthetic" not in str(failure.value.body())
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["close", "cancel-task"])
async def test_human_wait_cannot_send_after_close_or_keep_cancellation_details(synthetic_key, action):
    client, config, calls, raw, _ = captcha_flow(synthetic_key)
    entered, release = asyncio.Event(), asyncio.Event()
    async def human(*args):
        entered.set()
        await release.wait()
        return raw
    task = asyncio.create_task(client.login("synthetic-account", "synthetic-password",
                              captcha_config=config, captcha_handler=human))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if action == "close":
            await client.close()
            release.set()
            with pytest.raises(ApiError) as failure:
                await task
            assert failure.value.code == "AUTH_PROTOCOL_UNVERIFIED"
        else:
            task.cancel("synthetic-private-cancel")
            with pytest.raises(asyncio.CancelledError) as failure:
                await task
            assert "synthetic" not in str(failure.value)
        assert len(calls) == 3 and failure.value.__context__ is failure.value.__cause__ is None
    finally:
        release.set()
        await client.close()


@pytest.mark.anyio
async def test_close_during_bootstrap_does_not_open_a_late_human_window(synthetic_key):
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait_bootstrap():
        entered.set()
        await release.wait()
    client, config, calls, raw, _ = captcha_flow(synthetic_key, bootstrap_wait=wait_bootstrap)
    humans = []
    async def human(*args):
        humans.append(True)
        return raw
    task = asyncio.create_task(client.login("synthetic-account", "synthetic-password",
                              captcha_config=config, captcha_handler=human))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await client.close()
        release.set()
        with pytest.raises(ApiError) as failure:
            await task
        assert failure.value.code == "AUTH_PROTOCOL_UNVERIFIED"
        assert humans == [] and len(calls) == 3
    finally:
        release.set()
        await client.close()
