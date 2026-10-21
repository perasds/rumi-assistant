"""SMS orchestration with synthetic SDK results; no real phone operation."""

import asyncio
import base64
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from pydantic import SecretStr

from rumi_market.errors import ApiError
from rumi_market.sdk_http import SdkCredentials, SdkHttpClient, SdkResponseError
from rumi_market.sdk_primitives import sdk_sign
from rumi_market.sdk_user import SdkGameUser
from test_game_runtime import (PRIVATE, TOKEN, TrackingHttp, assert_released, game_server,
                               key, setup)
from test_game_provider import Runtime, provider


MOBILE = "13800000000"
OTP = "123456"
TICKET = "synthetic-private-sms-ticket"


def sms_sdk(monkeypatch, events, *, failure=None, entered=None, release=None):
    clients = []

    async def send(client, mobile, country_code, **kwargs):
        events.append("sms_send")
        clients.append(client)
        assert mobile == MOBILE and country_code == "86"
        if entered is not None and failure == "wait_send":
            entered.set()
            await release.wait()
        if failure == "send" or (failure == "resend" and events.count("sms_send") > 1):
            raise ApiError("AUTH_REJECTED", TICKET, 401)
        return SecretStr(TICKET)

    async def login(client, mobile, country_code, otp, captcha_key, **kwargs):
        events.append("sms_login")
        assert client is clients[-1]
        assert (mobile, country_code, otp) == (MOBILE, "86", OTP)
        assert captcha_key.get_secret_value() == TICKET
        if entered is not None and failure == "wait_login":
            entered.set()
            await release.wait()
        if failure == "otp" and events.count("sms_login") == 1:
            raise SdkResponseError(response_code=-1, request_stage="sdk_sms_login")
        return SdkCredentials("synthetic-raw-sdk-uid", SecretStr(TOKEN),
            SdkGameUser("synthetic-projected-game-uid", SecretStr(TOKEN), "6"))

    monkeypatch.setattr(SdkHttpClient, "send_phone_sms", send, raising=False)
    monkeypatch.setattr(SdkHttpClient, "login_phone_sms", login, raising=False)
    return clients


async def test_sms_uses_same_device_config_client_and_normal_game_handshake(monkeypatch, key):
    runtime, game, events, transports, version, _ = setup(key)
    sms_sdk(monkeypatch, events)
    server = None
    try:
        assert await runtime.send_sms(MOBILE, "86") is None
        assert events == ["version", "config", "sms_send"]
        assert not runtime.can_query and game.outgoing.empty()
        assert [transport.closes for transport in transports] == [0, 0, 0]
        server = asyncio.create_task(game_server(game, events, login_type="6"))
        facts = await runtime.start_sms(MOBILE, "86", OTP)
        await server
        assert facts.role_id == 123 and runtime.can_query
        assert events == ["version", "config", "sms_send", "sms_login", "game_login", "enter"]
        assert [transport.closes for transport in transports] == [1, 1, 1]
        assert runtime.export_login_state() is None and runtime._sms_ticket is None
        assert_released(runtime, version)
    finally:
        if server is not None:
            server.cancel()
        await runtime.close()


async def test_runtime_wires_real_sms_crypto_and_type_six_into_both_game_requests(key):
    runtime, game, events, _, version, _ = setup(key)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    runtime._sdk_context = replace(runtime._sdk_context, sms_public_key=SecretStr(pem))
    attempts = 0

    def sms_http(request):
        nonlocal attempts
        form = {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
        assert form["udid"] == "synthetic-http-udid" and form["bd_id"] == "synthetic-http-bdid"
        assert form["sign"] == sdk_sign(form, "synthetic-app-key")
        assert request.headers["cversion"] == "7"
        if request.url.path == "/api/external/otp/send/v3":
            events.append("sms_send")
            assert form["otp_channel_no"] == MOBILE and form["country_code"] == "86"
            return httpx.Response(200, json={"code": 0, "captcha_key": TICKET})
        assert request.url.path == "/api/external/login/otp/v3"
        events.append("sms_login")
        assert (form["mobile"], form["country_code"], form["otp"], form["captcha_key"]) == (MOBILE, "86", OTP, TICKET)
        assert key.decrypt(base64.b64decode(form["feign_sign"]), padding.PKCS1v15()) == form["sign"].encode()
        attempts += 1
        return httpx.Response(200, json={"code": -1} if attempts == 1 else {
            "code": 0, "uid": "synthetic-raw-sdk-uid", "access_key": TOKEN,
            "game_open_id_enable": True, "game_open_id": "synthetic-projected-game-uid"})

    runtime._login_transport = TrackingHttp(sms_http)
    await runtime.send_sms(MOBILE, "86")
    with pytest.raises(ApiError, match="^AUTH_SMS_REJECTED$"):
        await runtime.start_sms(MOBILE, "86", OTP)
    assert not runtime.closed and game.outgoing.empty()
    server = asyncio.create_task(game_server(game, events, login_type="6"))
    try:
        assert (await runtime.start_sms(MOBILE, "86", OTP)).role_id == 123
        await server
        assert runtime.export_login_state() is None
        assert_released(runtime, version)
    finally:
        server.cancel()
        await runtime.close()


@pytest.mark.parametrize("mobile,country_code,send_first", [
    (MOBILE, "86", False), ("13900000000", "86", True), (MOBILE, "1", True),
])
async def test_sms_requires_successful_send_for_same_phone_and_country(monkeypatch, key, mobile, country_code, send_first):
    runtime, game, events, _, version, _ = setup(key)
    sms_sdk(monkeypatch, events)
    if send_first:
        await runtime.send_sms(MOBILE, "86")
    with pytest.raises(ApiError) as caught:
        await runtime.start_sms(mobile, country_code, OTP)
    assert caught.value.code == "AUTH_REJECTED"
    assert "sms_login" not in events and game.outgoing.empty() and game.closed
    assert runtime.closed and runtime._sms_ticket is None
    assert_released(runtime, version)


@pytest.mark.parametrize("failure", ["send", "resend"])
async def test_sms_failure_drops_ticket_credentials_and_never_enters_game(monkeypatch, key, failure):
    runtime, game, events, _, version, _ = setup(key)
    sms_sdk(monkeypatch, events, failure=failure)
    if failure != "send":
        await runtime.send_sms(MOBILE, "86")
    with pytest.raises(ApiError) as caught:
        await runtime.send_sms(MOBILE, "86")
    assert caught.value.code == "AUTH_REJECTED"
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert TICKET not in str(caught.value.body()) and OTP not in str(caught.value.body())
    assert runtime.closed and runtime._sms_ticket is None and game.closed and game.outgoing.empty()
    assert_released(runtime, version)


async def test_failed_sms_send_still_closes_every_resource_when_http_close_fails(monkeypatch, key):
    runtime, game, events, transports, version, _ = setup(key)
    sms_sdk(monkeypatch, events, failure="send")
    transports[1].fail_close = True
    with pytest.raises(ApiError, match="^AUTH_REJECTED$"):
        await runtime.send_sms(MOBILE, "86")
    assert runtime.closed and game.closed and runtime._sms_ticket is None
    assert [transport.closes for transport in transports] == [1, 1, 1]
    assert_released(runtime, version)


async def test_wrong_otp_keeps_same_ticket_and_client_for_user_retry(monkeypatch, key):
    runtime, game, events, transports, version, _ = setup(key)
    sms_sdk(monkeypatch, events, failure="otp")
    await runtime.send_sms(MOBILE, "86")
    with pytest.raises(ApiError) as caught:
        await runtime.start_sms(MOBILE, "86", OTP)
    assert caught.value.code == "AUTH_SMS_REJECTED"
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert not runtime.closed and not runtime._started and runtime._sms_ticket is not None
    assert [transport.closes for transport in transports] == [0, 0, 0]
    assert game.outgoing.empty()
    server = asyncio.create_task(game_server(game, events, login_type="6"))
    try:
        result = await runtime.start_sms(MOBILE, "86", OTP)
        await server
        assert runtime.can_query and result.role_id == 123
        assert events == ["version", "config", "sms_send", "sms_login", "sms_login", "game_login", "enter"]
        assert runtime._sms_ticket is None and runtime.export_login_state() is None
        assert_released(runtime, version)
    finally:
        server.cancel()
        await runtime.close()


@pytest.mark.parametrize("phase,action", [("wait_send", "cancel"), ("wait_send", "close"),
                                          ("wait_login", "cancel"), ("wait_login", "close")])
async def test_sms_cancel_and_close_clear_pending_ticket(monkeypatch, key, phase, action):
    runtime, game, events, _, version, _ = setup(key)
    entered, release = asyncio.Event(), asyncio.Event()
    sms_sdk(monkeypatch, events, failure=phase, entered=entered, release=release)
    if phase == "wait_login":
        await runtime.send_sms(MOBILE, "86")
    task = asyncio.create_task(runtime.send_sms(MOBILE, "86") if phase == "wait_send"
                               else runtime.start_sms(MOBILE, "86", OTP))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if action == "cancel":
            task.cancel(TICKET)
        else:
            await runtime.close()
            assert runtime._sms_ticket is None
        release.set()
        with pytest.raises(asyncio.CancelledError if action == "cancel" else Exception) as caught:
            await task
        assert caught.value.__context__ is caught.value.__cause__ is None
        assert TICKET not in str(caught.value) and OTP not in str(caught.value)
        assert runtime.closed and runtime._sms_ticket is None and game.closed and game.outgoing.empty()
        assert_released(runtime, version)
    finally:
        release.set()
        await runtime.close()


async def test_switch_to_password_consumes_no_sms_ticket_and_uses_normal_password_path(monkeypatch, key):
    runtime, game, events, _, version, _ = setup(key)
    sms_sdk(monkeypatch, events)
    await runtime.send_sms(MOBILE, "86")
    server = asyncio.create_task(game_server(game, events))
    try:
        await runtime.start("synthetic-account", PRIVATE)
        await server
        assert events == ["version", "config", "sms_send", "cipher", "login", "game_login", "enter"]
        assert runtime._sms_ticket is None and runtime.export_login_state() is None
        assert_released(runtime, version)
    finally:
        server.cancel()
        await runtime.close()


class SmsRuntime(Runtime):
    async def send_sms(self, mobile, country_code):
        self.calls.append(("sms_send", mobile, country_code))

    async def start_sms(self, mobile, country_code, otp):
        self.calls.append(("sms_login", mobile, country_code, otp))
        if getattr(self, "reject_once", False):
            self.reject_once = False
            raise ApiError("AUTH_SMS_REJECTED", OTP, 401)
        self.started = True
        return self.result


async def test_provider_sms_shares_role_projection_and_version_notice():
    runtime = SmsRuntime()
    runtime.version_warning = "工具适配版本与游戏实际版本不同"
    adapter = provider(runtime)
    try:
        assert adapter.capabilities().methods == ["account", "sms"]
        assert await adapter.send_sms(MOBILE, "86") is None
        view = await adapter.login_sms(MOBILE, "86", OTP)
        assert view.authenticated and view.message == runtime.version_warning
        assert view.role.server_name == "真实响应区服" and "123456789" not in view.model_dump_json()
        assert runtime.calls == [("sms_send", MOBILE, "86"), ("sms_login", MOBILE, "86", OTP)]
    finally:
        await adapter.close()


async def test_unverified_provider_sms_is_blocked_before_runtime_calls():
    runtime = SmsRuntime()
    adapter = provider(runtime, verified=False)
    try:
        with pytest.raises(ApiError, match="^AUTH_PROTOCOL_UNVERIFIED$"):
            await adapter.send_sms(MOBILE, "86")
        with pytest.raises(ApiError, match="^AUTH_PROTOCOL_UNVERIFIED$"):
            await adapter.login_sms(MOBILE, "86", OTP)
        assert runtime.calls == []
    finally:
        await adapter.close()


async def test_live_sms_builds_once_and_preserves_provider_for_wrong_otp_retry():
    from rumi_market.live_runtime import LiveProviderFactory
    from test_live_runtime import shop
    runtime = SmsRuntime()
    runtime.reject_once = True
    builds = []

    async def builder():
        builds.append(True)
        return runtime, shop()

    adapter = LiveProviderFactory(runtime_builder=builder)()
    try:
        assert adapter.capabilities().methods == ["account", "sms"]
        await adapter.send_sms(MOBILE, "86")
        with pytest.raises(ApiError) as caught:
            await adapter.login_sms(MOBILE, "86", OTP)
        assert caught.value.code == "AUTH_SMS_REJECTED" and not runtime.closed
        assert OTP not in str(caught.value.body()) and not runtime.started
        assert (await adapter.login_sms(MOBILE, "86", OTP)).authenticated
        assert builds == [True]
    finally:
        await adapter.close()


async def test_live_sms_pending_can_switch_to_password_without_rebuilding_or_leaking_runtime():
    from rumi_market.live_runtime import LiveProviderFactory
    from test_live_runtime import shop
    runtime = SmsRuntime()
    builds = []

    async def builder():
        builds.append(True)
        return runtime, shop()

    adapter = LiveProviderFactory(runtime_builder=builder)()
    try:
        await adapter.send_sms(MOBILE, "86")
        assert (await adapter.login("synthetic-account", PRIVATE)).authenticated
        assert builds == [True] and runtime.started
    finally:
        await adapter.close()


@pytest.mark.parametrize("action", ["cancel", "close"])
async def test_live_sms_pending_send_cancel_or_close_cannot_report_late_success(action):
    from rumi_market.live_runtime import LiveProviderFactory
    from test_live_runtime import shop
    runtime = SmsRuntime()
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending_send(mobile, country_code):
        entered.set()
        await release.wait()

    runtime.send_sms = pending_send

    async def builder():
        return runtime, shop()

    adapter = LiveProviderFactory(runtime_builder=builder)()
    task = asyncio.create_task(adapter.send_sms(MOBILE, "86"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if action == "cancel":
            task.cancel(TICKET)
        else:
            await adapter.close()
        release.set()
        with pytest.raises(asyncio.CancelledError if action == "cancel" else ApiError) as caught:
            await task
        assert runtime.closed and not adapter.connection_alive
        assert TICKET not in str(caught.value)
        assert caught.value.__context__ is caught.value.__cause__ is None
    finally:
        release.set()
        await adapter.close()
