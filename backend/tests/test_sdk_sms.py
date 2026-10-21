"""Synthetic SMS protocol coverage: never sends an SMS or uses an account."""

import base64
import importlib
import importlib.util
import json
import hashlib
import zipfile
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr

from rumi_market.errors import ApiError
from rumi_market.sdk_captcha import SdkCaptchaConfig
from rumi_market.sdk_http import SdkHttpClient, SdkResponseError
from rumi_market.sdk_primitives import sdk_sign


SEND = "/api/external/otp/send/v3"
LOGIN = "/api/external/login/otp/v3"
CAPTCHA = "/api/client/start_captcha"
MOBILE = "18800000000"
TICKET = "synthetic-private-sms-ticket"
OTP = "123456"


def sms_module():
    assert importlib.util.find_spec("rumi_market.sdk_sms") is not None, "SMS signing implementation is missing"
    return importlib.import_module("rumi_market.sdk_sms")


@pytest.fixture(scope="module")
def signing_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return key, pem


def client_for(handler, signing_material, **kwargs):
    assert callable(getattr(SdkHttpClient, "send_phone_sms", None)), "SDK SMS entry is missing"
    mod = sms_module()
    origin = ("https://line3-sdk-center-login-sh.biligame.net" if kwargs.get("purpose") == "config"
              else "https://line1-sdk-center-login-sh.biligame.net")
    return SdkHttpClient(origin, "synthetic-app-key",
        common_parameters=lambda path: {"app_id": "synthetic-game", "version": "1"},
        extra_headers=lambda timestamp, sign, params: {"x-synthetic-rule": sign},
        sms_feign_sign=lambda sign: mod.sdk_sms_feign_sign(sign, signing_material[1]),
        transport=httpx.MockTransport(handler), clock_ms=lambda: 1234567890000, **kwargs)


def form_of(request):
    return {key: value[0] for key, value in parse_qs(request.content.decode()).items()}


def test_feign_sign_encrypts_only_raw_md5_with_pkcs1_and_returns_raw_base64(signing_material):
    mod = sms_module()
    sign = "0123456789abcdef" * 2
    encoded = mod.sdk_sms_feign_sign(sign, signing_material[1])
    assert signing_material[0].decrypt(base64.b64decode(encoded, validate=True), padding.PKCS1v15()) == sign.encode()
    assert "%" not in encoded


@pytest.mark.parametrize("sign", ["", "SYNTHETIC-PRIVATE", "A" * 32, "0" * 31, None, False])
def test_feign_sign_rejects_unverified_sign_without_echo(sign, signing_material):
    mod = sms_module()
    with pytest.raises(ValueError, match="^Invalid SDK SMS signature$") as failure:
        mod.sdk_sms_feign_sign(sign, signing_material[1])
    assert failure.value.__context__ is None
    assert "PRIVATE" not in str(failure.value)


@pytest.mark.anyio
async def test_send_uses_official_fields_and_returns_only_private_ticket(signing_material):
    calls = []
    def handler(request):
        form = form_of(request)
        calls.append((request.url.path, form))
        assert request.url.path == SEND
        assert form["otp_channel_no"] == MOBILE
        assert form["country_code"] == "86"
        assert form["otp_channel_category"] == "tel" and form["otp_type"] == "login"
        assert form["version"] == "3"
        assert not {"pwd", "user_id", "otp", "feign_sign", "captcha_key"} & form.keys()
        assert form["sign"] == sdk_sign(form, "synthetic-app-key")
        assert request.headers["x-synthetic-rule"] == form["sign"]
        return httpx.Response(200, json={"code": 0, "captcha_key": TICKET})
    client = client_for(handler, signing_material)
    try:
        ticket = await client.send_phone_sms(MOBILE, "86")
        assert isinstance(ticket, SecretStr) and ticket.get_secret_value() == TICKET
        assert TICKET not in repr(ticket) and len(calls) == 1
    finally:
        await client.close()


@pytest.mark.anyio
async def test_sms_login_has_extra_signature_and_projects_game_type_six(signing_material):
    calls = []
    def handler(request):
        form = form_of(request)
        calls.append(request.url.path)
        assert request.url.path == LOGIN
        assert {key: form[key] for key in ("mobile", "country_code", "captcha_key", "otp", "version")} == {
            "mobile": MOBILE, "country_code": "86", "captcha_key": TICKET, "otp": OTP, "version": "3"}
        assert not {"user_id", "pwd", "otp_channel_no"} & form.keys()
        assert form["sign"] == sdk_sign(form, "synthetic-app-key")
        assert signing_material[0].decrypt(base64.b64decode(form["feign_sign"]), padding.PKCS1v15()) == form["sign"].encode()
        assert b"%25" not in request.content
        return httpx.Response(200, json={"code": "0", "uid": "synthetic-account-uid",
            "access_key": "synthetic-private-access-key", "game_open_id_enable": True,
            "game_open_id": "synthetic-game-uid", "login_type": "0"})
    client = client_for(handler, signing_material)
    try:
        credentials = await client.login_phone_sms(MOBILE, "86", OTP, SecretStr(TICKET))
        assert credentials.game_user.login_type == "6"
        assert credentials.game_user.game_uid == "synthetic-game-uid"
        assert credentials.access_key.get_secret_value() == "synthetic-private-access-key"
        assert all(value not in repr(credentials) for value in (MOBILE, OTP, TICKET, "synthetic-private"))
        assert calls == [LOGIN]
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("stage,path", [("sdk_sms_send", SEND), ("sdk_sms_login", LOGIN)])
@pytest.mark.parametrize("code", [500001, "200007", -1, "not-a-public-code"])
async def test_business_error_keeps_only_bounded_code_and_stage(stage, path, code, signing_material):
    client = client_for(lambda request: httpx.Response(200, json={"code": code,
        "message": MOBILE + OTP + TICKET, "access_key": TICKET}), signing_material)
    try:
        with pytest.raises(SdkResponseError) as failure:
            if path == SEND:
                await client.send_phone_sms(MOBILE, "86")
            else:
                await client.login_phone_sms(MOBILE, "86", OTP, SecretStr(TICKET))
        error = failure.value
        assert error.request_stage == stage
        assert error.captcha_required is (code == "200007")
        assert all(value not in str(error.body()) + repr(error) for value in (MOBILE, OTP, TICKET))
        assert error.__context__ is None and error.__cause__ is None
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("path", [SEND, LOGIN])
async def test_human_captcha_retries_same_sms_business_once(path, signing_material):
    calls = []
    attempts = 0
    async def human_handler(challenge, config):
        return json.dumps({"challenge": "synthetic-human-challenge", "validate": "synthetic-human-validate"})
    def handler(request):
        nonlocal attempts
        form = form_of(request)
        calls.append((request.url.path, form))
        if request.url.path == CAPTCHA:
            return httpx.Response(200, json={"code": 0, "captcha_type": 1,
                "challenge": "synthetic-start-challenge", "gt": "synthetic-gt", "gt_user_id": "synthetic-userid"})
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"code": 200007})
        assert form["validate"] == "synthetic-human-validate"
        assert form["challenge"] == "synthetic-human-challenge"
        assert form["sign"] == sdk_sign(form, "synthetic-app-key")
        if path == SEND:
            return httpx.Response(200, json={"code": 0, "captcha_key": TICKET})
        return httpx.Response(200, json={"code": 0, "uid": "synthetic-uid", "access_key": "synthetic-key"})
    client = client_for(handler, signing_material)
    config = SdkCaptchaConfig.from_response({"code": 0, "config_login_android_https": "1",
        "config_login_https": "https://line1-sdk-center-login-sh.biligame.net",
        "config_risk_verify_url": "https://synthetic.biligame.com/verify"})
    try:
        options = {"captcha_config": config, "captcha_handler": human_handler}
        if path == SEND:
            await client.send_phone_sms(MOBILE, "86", **options)
        else:
            await client.login_phone_sms(MOBILE, "86", OTP, SecretStr(TICKET), **options)
        assert [call[0] for call in calls] == [path, CAPTCHA, path]
        assert calls[0][1].get("otp_channel_no", calls[0][1].get("mobile")) == MOBILE
        if path == LOGIN:
            assert calls[2][1]["captcha_key"] == TICKET and calls[2][1]["otp"] == OTP
            assert signing_material[0].decrypt(base64.b64decode(calls[2][1]["feign_sign"]), padding.PKCS1v15()) == calls[2][1]["sign"].encode()
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("ticket", [None, "", False, {}, "synthetic\nprivate"])
async def test_missing_or_invalid_send_ticket_is_upstream_failure(ticket, signing_material):
    client = client_for(lambda request: httpx.Response(200, json={"code": 0, "captcha_key": ticket}), signing_material)
    try:
        with pytest.raises(ApiError) as failure:
            await client.send_phone_sms(MOBILE, "86")
        assert failure.value.code == "AUTH_UPSTREAM_FAILED"
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("path", [SEND, LOGIN])
async def test_config_and_closed_clients_never_send_sms(path, signing_material):
    for closed in (False, True):
        calls = []
        client = client_for(lambda request: calls.append(request), signing_material,
            purpose="config" if not closed else "login")
        if closed:
            await client.close()
        try:
            with pytest.raises(ApiError) as failure:
                if path == SEND:
                    await client.send_phone_sms(MOBILE, "86")
                else:
                    await client.login_phone_sms(MOBILE, "86", OTP, SecretStr(TICKET))
            assert failure.value.code == "AUTH_PROTOCOL_UNVERIFIED" and not calls
        finally:
            await client.close()


@pytest.mark.anyio
async def test_missing_signer_blocks_sms_login_before_http():
    assert callable(getattr(SdkHttpClient, "login_phone_sms", None)), "SDK SMS login is missing"
    calls = []
    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", "synthetic-app-key",
        common_parameters=lambda path: {}, extra_headers=lambda *args: {},
        transport=httpx.MockTransport(lambda request: calls.append(request)))
    try:
        with pytest.raises(ApiError) as failure:
            await client.login_phone_sms(MOBILE, "86", OTP, SecretStr(TICKET))
        assert failure.value.code == "AUTH_PROTOCOL_UNVERIFIED" and not calls
    finally:
        await client.close()


def synthetic_archive(tmp_path, monkeypatch, signing_material):
    mod = sms_module()
    key = signing_material[0].public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    encoded = base64.b64encode(key)
    dex = bytes(256) + encoded + b"\0" + bytes(16)
    path = tmp_path / "synthetic-sms.apk"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("classes7.dex", dex)
    for name, value in {"APK_SHA256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "DEX_SIZE": len(dex), "DEX_SHA256": hashlib.sha256(dex).hexdigest(),
        "KEY_ASCII_OFFSET": 256, "KEY_SIZE": len(encoded),
        "KEY_SHA256": hashlib.sha256(encoded).hexdigest()}.items():
        monkeypatch.setattr(mod, name, value)
    return mod, path, encoded.decode()


def test_pinned_apk_key_loader_returns_only_private_memory_value(tmp_path, monkeypatch, signing_material):
    mod, path, expected = synthetic_archive(tmp_path, monkeypatch, signing_material)
    value = mod.load_sdk_sms_public_key(path)
    assert type(value) is SecretStr and value.get_secret_value() == expected
    assert expected not in repr(value)
    signature = mod.sdk_sms_feign_sign("0" * 32, value.get_secret_value())
    assert signing_material[0].decrypt(base64.b64decode(signature), padding.PKCS1v15()) == b"0" * 32


@pytest.mark.parametrize("name", ["APK_SHA256", "DEX_SHA256", "KEY_SHA256", "KEY_SIZE", "KEY_ASCII_OFFSET"])
def test_key_loader_rejects_changed_material_with_fixed_error(tmp_path, monkeypatch, signing_material, name):
    mod, path, _ = synthetic_archive(tmp_path, monkeypatch, signing_material)
    current = getattr(mod, name)
    monkeypatch.setattr(mod, name, "0" * 64 if type(current) is str else current + 1)
    with pytest.raises(ValueError, match="^Invalid SDK SMS signing material$") as failure:
        mod.load_sdk_sms_public_key(path)
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert "synthetic" not in str(failure.value)


@pytest.mark.anyio
async def test_wrong_otp_business_rejection_does_not_close_client_or_invalidate_ticket(signing_material):
    calls = []
    def handler(request):
        form = form_of(request)
        calls.append(form)
        if len(calls) == 1:
            return httpx.Response(200, json={"code": 500002})
        return httpx.Response(200, json={"code": 0, "uid": "synthetic-uid", "access_key": "synthetic-key"})
    client = client_for(handler, signing_material)
    try:
        with pytest.raises(SdkResponseError) as failure:
            await client.login_phone_sms(MOBILE, "86", "000000", SecretStr(TICKET))
        assert failure.value.request_stage == "sdk_sms_login" and not client._closed
        result = await client.login_phone_sms(MOBILE, "86", OTP, SecretStr(TICKET))
        assert result.game_user.login_type == "6"
        assert [form["captcha_key"] for form in calls] == [TICKET, TICKET]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_close_releases_private_sms_signing_context(signing_material):
    client = client_for(lambda request: httpx.Response(200, json={"code": 0}), signing_material)
    await client.close()
    assert client._sms_feign_sign is None


@pytest.mark.anyio
@pytest.mark.parametrize("path", [SEND, LOGIN])
async def test_repeated_captcha_is_returned_without_reopening_human_flow(path, signing_material):
    calls = []
    human_calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == CAPTCHA:
            return httpx.Response(200, json={"code": 0, "captcha_type": 1,
                "challenge": "synthetic-start-challenge", "gt": "synthetic-gt", "gt_user_id": "synthetic-userid"})
        return httpx.Response(200, json={"code": 200007})
    async def human_handler(challenge, config):
        human_calls.append(True)
        return '{"validate":"synthetic-human-validate"}'
    client = client_for(handler, signing_material)
    config = SdkCaptchaConfig.from_response({"code": 0, "config_login_android_https": "1",
        "config_login_https": "https://line1-sdk-center-login-sh.biligame.net",
        "config_risk_verify_url": "https://synthetic.biligame.com/verify"})
    try:
        with pytest.raises(SdkResponseError) as failure:
            if path == SEND:
                await client.send_phone_sms(MOBILE, "86", captcha_config=config, captcha_handler=human_handler)
            else:
                await client.login_phone_sms(MOBILE, "86", OTP, SecretStr(TICKET),
                    captcha_config=config, captcha_handler=human_handler)
        assert failure.value.response_code == 200007
        assert calls == [path, CAPTCHA, path] and human_calls == [True]
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("path", [SEND, LOGIN])
async def test_cancelled_human_check_does_not_resubmit_sms_or_echo(path, signing_material):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == CAPTCHA:
            return httpx.Response(200, json={"code": 0, "captcha_type": 1,
                "challenge": "synthetic-start-challenge", "gt": "synthetic-gt", "gt_user_id": "synthetic-userid"})
        return httpx.Response(200, json={"code": 200007})
    async def human_handler(challenge, config):
        raise ApiError("AUTH_CAPTCHA_CANCELLED", MOBILE + OTP + TICKET)
    client = client_for(handler, signing_material)
    config = SdkCaptchaConfig.from_response({"code": 0, "config_login_android_https": "1",
        "config_login_https": "https://line1-sdk-center-login-sh.biligame.net",
        "config_risk_verify_url": "https://synthetic.biligame.com/verify"})
    try:
        with pytest.raises(ApiError) as failure:
            if path == SEND:
                await client.send_phone_sms(MOBILE, "86", captcha_config=config, captcha_handler=human_handler)
            else:
                await client.login_phone_sms(MOBILE, "86", OTP, SecretStr(TICKET),
                    captcha_config=config, captcha_handler=human_handler)
        assert failure.value.code == "AUTH_CAPTCHA_CANCELLED" and calls == [path, CAPTCHA]
        assert all(value not in str(failure.value.body()) for value in (MOBILE, OTP, TICKET))
    finally:
        await client.close()
