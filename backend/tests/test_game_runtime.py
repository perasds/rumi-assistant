import asyncio
import base64
import importlib
import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr

from rumi_market.errors import ApiError
from rumi_market.game.client import GameClientError
from rumi_market.game.profile import ProjectClientContext
from rumi_market.game.proto import GameProtoCodec
from rumi_market.game.version import VersionHttpClient, VersionRequestContext
from rumi_market.sdk_app_config import SdkAppConfig
from rumi_market.sdk_device import SdkDeviceIdentity
from rumi_market.sdk_parameters import SdkRequestContext
from rumi_market.sdk_primitives import sdk_sign
from rumi_market.sdk_rule_codes import CODEBOOK
from test_game_client import MemoryTransport


PRIVATE = "synthetic-private-runtime-password"
TOKEN = "synthetic-private-runtime-token"
CODEC = GameProtoCodec()


def implementation():
    return importlib.import_module("rumi_market.game.runtime")


def bits(rule):
    codes = dict(CODEBOOK)
    return "".join(codes[byte] for byte in rule).encode("ascii")


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class TrackingHttp(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.closes = 0
        self.fail_close = False

    async def aclose(self):
        self.closes += 1
        if self.fail_close == "cancel":
            raise asyncio.CancelledError(PRIVATE)
        if self.fail_close:
            raise RuntimeError(PRIVATE)
        await super().aclose()


def materials(key):
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    rule = bits(b"[t|0]")
    block = b"\x00\x01" + b"\xff" * (256-len(rule)-3) + b"\x00" + rule
    numbers = key.private_numbers()
    signed = base64.b64encode(pow(int.from_bytes(block, "big"), numbers.d,
                                  numbers.public_numbers.n).to_bytes(256, "big")).decode()
    sdk = SdkRequestContext(
        SdkAppConfig("synthetic-merchant", "synthetic-app", "synthetic-server",
                     "synthetic-sdk-version", SecretStr("synthetic-app-key")),
        SdkDeviceIdentity("synthetic-http-udid", "synthetic-http-bdid"),
        bits(b"[d]"), "synthetic-apk-sign", "1", pem,
    )
    client = ProjectClientContext(game_device_id="synthetic-game-buvid",
        device_id_source="synthetic project identity policy", model="model", os_version="os",
        network="network", time_zone_hours=8, language_type=1, platform="project-platform")
    return sdk, client, {"code": 0, "crule": signed, "crule_ver": "7"}, pem


def setup(key, *, failure=None, foreign_identity=False, version_device_id=None,
          rejection_code=None, timeout=2, captcha_handler=None, profile_archive=None):
    module = implementation()
    sdk, client, config, pem = materials(key)
    if captcha_handler is not None:
        config.update({"config_login_android_https": "1",
            "config_login_https": "https://line3-sdk-center-login-sh.biligame.net,https://line1-sdk-center-login-sh.biligame.net",
            "config_risk_verify_url": "https://example.invalid/synthetic-verify"})
        if failure == "captcha_config":
            config.pop("config_risk_verify_url")
    declared_version_device = client.game_device_id if version_device_id is None else version_device_id
    events = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle_version(request):
        events.append("version")
        assert request.read().find(declared_version_device.encode()) >= 0
        if failure == "cancel_version":
            entered.set()
            await release.wait()
        if failure == "version":
            return httpx.Response(500, content=PRIVATE.encode())
        if failure == "unexpected":
            raise TypeError(PRIVATE)
        return httpx.Response(200, json={"code": 0, "data": {
            "version": "1.5.3", "hotVersion": {"version": "62"}}})

    async def handle_sdk(request):
        form = {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
        assert form["udid"] == sdk.identity.udid and form["bd_id"] == sdk.identity.bd_id
        assert form["sign"] == sdk_sign(form, sdk.app_config.app_key.get_secret_value())
        if request.url.path.endswith("/config/v3"):
            events.append("config")
            assert request.url.host == "line3-sdk-center-login-sh.biligame.net"
            assert request.headers["cversion"] == "0"
            assert form["version"] == "3" and "user_id" not in form and "pwd" not in form
            if failure == "config":
                return httpx.Response(200, json={"code": -5 if rejection_code is None else rejection_code,
                                                "message": PRIVATE})
            return httpx.Response(200, json={"code": 0} if failure == "rule" else config)
        assert request.url.host == ("line3-sdk-center-login-sh.biligame.net" if captcha_handler is not None
                                    else "line1-sdk-center-login-sh.biligame.net")
        assert request.headers["cversion"] == "7"
        configured = sdk.with_config(config)
        for name, value in configured.extra_headers(form["timestamp"], form["sign"], form).items():
            assert request.headers[name] == value
        if request.url.path == "/api/client/start_captcha":
            events.append("captcha_start")
            assert form["version"] == "1" and not {"user_id", "pwd"} & form.keys()
            return httpx.Response(200, json={"code": -17 if failure == "captcha_start" else 0,
                "captcha_type": "geetest", "challenge": "synthetic-start-challenge",
                "gt": "synthetic-start-gt", "ctoken": "synthetic-start-ctoken"})
        if request.url.path.endswith("/issue/cipher/v3"):
            events.append("cipher")
            if failure == "cipher":
                return httpx.Response(200, json={"code": -11 if rejection_code is None else rejection_code,
                                                "message": PRIVATE})
            return httpx.Response(200, json={"code": 0, "cipher_key": pem.decode(), "hash": "prefix:"})
        events.append("login")
        assert form["user_id"] == "synthetic-account"
        assert key.decrypt(base64.b64decode(form["pwd"]), padding.PKCS1v15()) == ("prefix:"+PRIVATE).encode()
        if failure == "cancel_login":
            entered.set()
            await release.wait()
        if captcha_handler is not None and events.count("login") == 1:
            return httpx.Response(200, json={"code": 200007, "message": PRIVATE})
        return httpx.Response(200, json={"code": -1 if rejection_code is None else rejection_code,
                                        "message": PRIVATE} if failure == "login" else {
            "code": 0, "uid": "synthetic-raw-sdk-uid", "access_key": TOKEN,
            "game_open_id_enable": True, "game_open_id": "synthetic-projected-game-uid"})

    version_transport, config_transport, login_transport = (TrackingHttp(handle_version),
        TrackingHttp(handle_sdk), TrackingHttp(handle_sdk))
    identity = replace(sdk.identity) if foreign_identity else sdk.identity
    context = VersionRequestContext(gm_secret=SecretStr("synthetic-gm-secret"), identity=identity,
        device_id=declared_version_device, channel="Android_CN_CDN", version="1.5.3",
        build_version="17", profile_source="synthetic fixed source")
    version = VersionHttpClient("https://l1-prod-cn-ob-vs-lumigo.bilibiligame.net", context,
                                transport=version_transport, clock_seconds=lambda: 1700000000)
    game = MemoryTransport()
    runtime = module.GameRuntime(sdk, version, client, game,
        sdk_config_transport=config_transport, sdk_login_transport=login_transport,
        timeout=timeout, page_window=2, profile_archive=profile_archive,
        **({"captcha_handler": captcha_handler} if captcha_handler is not None else {}))
    return runtime, game, events, (version_transport, config_transport, login_transport), version, entered


async def game_server(game, events, *, activation=False, selection=False, rejected=False,
                      server_time_ms=1700000000123, login_type="0"):
    request = await game.next_request()
    events.append("game_login")
    assert request.ReqCmdId == 2901
    msg = CODEC.decode("SNKProto.Cmd_Net_LumiLogin_Req", request.ReqData).message
    assert msg.param_bili.uid == "synthetic-projected-game-uid"
    assert msg.param_bili.access_token == TOKEN and msg.game_info.udid == "synthetic-game-buvid"
    assert msg.client_version == msg.game_info.sdk_version == "1.5.3"
    assert msg.game_info.login_channel == login_type
    assert msg.hot_version == 62 and msg.game_info.ad_channel_id == ""
    game.respond(2902, request.ReqCmdSeq,
                 CODEC.encode("SNKProto.Cmd_Net_LumiLogin_Ack", {"role_id": 123}), code=-9 if rejected else 0)
    if rejected:
        return
    enter = await game.next_request()
    events.append("enter")
    assert enter.ReqCmdId == 2911 and not enter.HasField("ReqNotifySeq")
    assert CODEC.decode("SNKProto.Cmd_Role_EnterGame_Req", enter.ReqData).message.login_channel == login_type
    if selection:
        game.respond(2934, 0, b"", notify=1)
    game.respond(2913, 0, CODEC.encode("SNKProto.Cmd_Notify_LoginEnd", {
        "use_cd_key": activation, "cd_key": ""}), notify=2)
    fields = {"zone_id": 7, "zone_name": "synthetic-zone"}
    if server_time_ms is not None:
        fields["server_time_ms"] = server_time_ms
    game.respond(2912, enter.ReqCmdSeq, CODEC.encode("SNKProto.Cmd_Role_EnterGame_Ack", fields))


def assert_released(runtime, version):
    assert version._context is None
    for name in ("_sdk_context", "_client_context", "_version_client", "_config_client", "_login_client"):
        assert getattr(runtime, name) is None
    assert not {"account", "password", "credentials", "profile"} & vars(runtime).keys()
    assert PRIVATE not in repr(runtime) and TOKEN not in repr(runtime)


async def test_real_crypto_config_rule_projection_handshake_and_quote_pipeline(key):
    runtime, game, events, transports, version, _ = setup(key)
    server = asyncio.create_task(game_server(game, events))
    try:
        facts = await runtime.start("synthetic-account", PRIVATE)
        await server
        assert events == ["version", "config", "cipher", "login", "game_login", "enter"]
        assert (facts.role_id, facts.zone_id, facts.server_time_ms) == (123, 7, 1700000000123)
        assert runtime.result is facts and runtime.can_query and not runtime.closed
        assert runtime.export_login_state() is None
        assert not game.closed and [t.closes for t in transports] == [1, 1, 1]
        assert_released(runtime, version)
        call = asyncio.create_task(runtime.market.query_color_page())
        query = await game.next_request()
        assert query.ReqCmdId == 5325
        msg = CODEC.decode("SNKProto.Cmd_Market_Auction_QueryLumiDetail_Req", query.ReqData).message
        assert (msg.begin_index, msg.end_index, msg.order_type) == (1, 3, 3)
        game.respond(5326, query.ReqCmdSeq, CODEC.encode("SNKProto.Cmd_Market_Auction_QueryLumiDetail_Ack", {
            "course": "", "lumi_list": [{"uid": "synthetic-listing", "lumi_id": 701, "fixed_price": 0}]}))
        page = await call
        assert page.quotes[0].fixed_price == 0 and page.next_cursor is None
        assert not hasattr(facts, "market_success")
    finally:
        server.cancel()
        await runtime.close()
    assert runtime.closed and not runtime.can_query and game.closed and game.closes == 1
    await runtime.close()
    assert game.closes == 1 and [t.closes for t in transports] == [1, 1, 1]


@pytest.mark.parametrize("activation,selection", [(True, False), (False, True), (True, True)])
async def test_handshake_gates_return_facts_without_query_access(key, activation, selection):
    module = implementation()
    runtime, game, events, transports, version, _ = setup(key)
    server = asyncio.create_task(game_server(game, events, activation=activation, selection=selection))
    try:
        facts = await runtime.start("synthetic-account", PRIVATE)
        await server
        assert facts.needs_activation is activation and facts.needs_initial_pet_selection is selection
        assert not runtime.can_query
        with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_QUERY_UNAVAILABLE$"):
            runtime.market
        assert game.outgoing.empty() and [t.closes for t in transports] == [1, 1, 1]
        assert_released(runtime, version)
    finally:
        server.cancel()
        await runtime.close()


@pytest.mark.parametrize("phase,expected_events", [
    ("version", ["version"]), ("config", ["version", "config"]),
    ("rule", ["version", "config"]), ("login", ["version", "config", "cipher", "login"]),
    ("unexpected", ["version"]),
])
async def test_failure_stops_next_phase_and_closes_created_clients_without_context(key, phase, expected_events):
    runtime, game, events, transports, version, _ = setup(key, failure=phase)
    with pytest.raises((ApiError, implementation().GameRuntimeError)) as failure:
        await runtime.start("synthetic-account", PRIVATE)
    assert events == expected_events and runtime.closed and game.closed
    assert transports[0].closes == 1
    assert PRIVATE not in str(failure.value) and TOKEN not in str(failure.value)
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert_released(runtime, version)
    assert not runtime.can_query and runtime.result is None


@pytest.mark.parametrize("phase, raw, code, stage, expected_events", [
    ("config", "-321", -321, "sdk_config", ["version", "config"]),
    ("cipher", -17, -17, "sdk_cipher", ["version", "config", "cipher"]),
    ("login", "synthetic-private-response", None, "sdk_password",
     ["version", "config", "cipher", "login"]),
])
async def test_runtime_copies_only_public_sdk_rejection_facts_and_still_cleans_up(
        key, phase, raw, code, stage, expected_events):
    from rumi_market.sdk_http import SdkResponseError

    runtime, game, events, transports, version, _ = setup(key, failure=phase, rejection_code=raw)
    with pytest.raises(SdkResponseError) as failure:
        await runtime.start("synthetic-account", PRIVATE)
    assert failure.value.code == "AUTH_REJECTED"
    assert failure.value.response_code == code and failure.value.request_stage == stage
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert "synthetic" not in str(failure.value.body())
    frames = []
    traceback = failure.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "_post" not in frames and "login" not in frames
    assert events == expected_events and runtime.closed and game.closed and game.outgoing.empty()
    assert [transport.closes for transport in transports] == ([1, 1, 0] if phase == "config" else [1, 1, 1])
    assert_released(runtime, version)


async def test_game_business_rejection_is_preserved_and_transport_closed(key):
    runtime, game, events, transports, version, _ = setup(key)
    server = asyncio.create_task(game_server(game, events, rejected=True))
    try:
        with pytest.raises(GameClientError) as failure:
            await runtime.start("synthetic-account", PRIVATE)
        await server
        assert failure.value.code == "GAME_REQUEST_REJECTED" and failure.value.response_code == -9
        assert failure.value.__context__ is None and failure.value.__cause__ is None
        assert game.closed and runtime.closed and events[-1] == "game_login"
        assert [t.closes for t in transports] == [1, 1, 1]
        assert_released(runtime, version)
    finally:
        server.cancel()
        await runtime.close()


async def test_equal_but_different_identity_is_rejected_before_any_request(key):
    module = implementation()
    runtime, game, events, transports, version, _ = setup(key, foreign_identity=True)
    with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_IDENTITY_MISMATCH$"):
        await runtime.start("synthetic-account", PRIVATE)
    assert events == [] and transports[0].closes == 1 and game.closed
    assert_released(runtime, version)


async def test_same_sdk_identity_but_different_game_device_is_rejected_before_any_http(key):
    module = implementation()
    runtime, game, events, transports, version, _ = setup(
        key, version_device_id="synthetic-different-game-device",
    )
    assert version.identity is runtime._sdk_context.identity
    with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_IDENTITY_MISMATCH$") as failure:
        await runtime.start("synthetic-account", PRIVATE)
    assert events == [] and runtime.closed and game.closed and game.closes == 1
    assert [transport.closes for transport in transports] == [1, 0, 0]
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert PRIVATE not in str(failure.value)
    assert_released(runtime, version)


@pytest.mark.parametrize("phase", ["cancel_version", "cancel_login", "game"])
async def test_cancellation_closes_every_created_client_and_clears_contexts(key, phase):
    runtime, game, events, transports, version, entered = setup(key, failure=phase)
    call = asyncio.create_task(runtime.start("synthetic-account", PRIVATE))
    if phase == "game":
        await game.next_request()
    else:
        await asyncio.wait_for(entered.wait(), 2)
    call.cancel(PRIVATE)
    with pytest.raises(asyncio.CancelledError) as failure:
        await call
    assert PRIVATE not in str(failure.value)
    assert runtime.closed and game.closed and transports[0].closes == 1
    assert_released(runtime, version)


async def test_http_close_failure_still_closes_game_and_does_not_keep_secrets(key):
    module = implementation()
    runtime, game, events, transports, version, _ = setup(key)
    transports[1].fail_close = True
    server = asyncio.create_task(game_server(game, events))
    try:
        with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_CLOSE_FAILED$") as failure:
            await runtime.start("synthetic-account", PRIVATE)
        await server
        assert failure.value.__context__ is None and PRIVATE not in str(failure.value)
        assert game.closed and [t.closes for t in transports] == [1, 1, 1]
        assert_released(runtime, version)
    finally:
        server.cancel()
        await runtime.close()


async def test_one_cancelled_http_close_does_not_skip_remaining_closes_or_context_clear(key):
    module = implementation()
    runtime, game, events, transports, version, _ = setup(key)
    transports[1].fail_close = "cancel"
    server = asyncio.create_task(game_server(game, events))
    try:
        with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_CLOSE_FAILED$"):
            await runtime.start("synthetic-account", PRIVATE)
        await server
        assert [t.closes for t in transports] == [1, 1, 1]
        assert game.closed and runtime.closed
        assert_released(runtime, version)
    finally:
        server.cancel()
        await runtime.close()


async def test_runtime_is_single_use_and_query_is_unavailable_before_start_or_after_close(key):
    module = implementation()
    runtime, game, events, _, version, _ = setup(key)
    with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_QUERY_UNAVAILABLE$"):
        runtime.market
    server = asyncio.create_task(game_server(game, events))
    try:
        await runtime.start("synthetic-account", PRIVATE)
        await server
        with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_ALREADY_STARTED$"):
            await runtime.start("synthetic-account", PRIVATE)
        assert not game.closed and runtime.can_query
    finally:
        server.cancel()
        await runtime.close()
    with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_QUERY_UNAVAILABLE$"):
        runtime.market
    assert_released(runtime, version)


async def test_runtime_uses_verified_config_route_and_passes_private_captcha_models(key):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaConfig
    humans = []
    async def human(challenge, config):
        assert type(challenge) is SdkCaptchaChallenge and type(config) is SdkCaptchaConfig
        assert config.login_origin == "https://line3-sdk-center-login-sh.biligame.net"
        assert config.h5_base_url == "https://example.invalid/synthetic-verify"
        humans.append(True)
        return json.dumps({"challenge": "synthetic-human-challenge", "validate": "synthetic-human-validate"})
    runtime, game, events, transports, version, _ = setup(key, captcha_handler=human)
    server = asyncio.create_task(game_server(game, events))
    try:
        facts = await runtime.start("synthetic-account", PRIVATE)
        await server
        assert facts.role_id == 123 and humans == [True]
        assert events == ["version", "config", "cipher", "login", "captcha_start", "login", "game_login", "enter"]
        assert [transport.closes for transport in transports] == [1, 1, 1]
        assert runtime._captcha_handler is None
        assert_released(runtime, version)
    finally:
        server.cancel()
        await runtime.close()


@pytest.mark.parametrize("phase", ["rule", "captcha_config"])
async def test_runtime_verifies_config_and_captcha_route_before_login_or_human_callback(key, phase):
    async def human(*args):
        pytest.fail("human callback before valid config")
    runtime, game, events, transports, version, _ = setup(key, failure=phase, captcha_handler=human)
    with pytest.raises(implementation().GameRuntimeError) as failure:
        await runtime.start("synthetic-account", PRIVATE)
    assert failure.value.code == "GAME_RUNTIME_PROTOCOL_INVALID"
    assert failure.value.__context__ is failure.value.__cause__ is None
    assert events == ["version", "config"] and game.closed
    assert [transport.closes for transport in transports] == [1, 1, 0]
    assert_released(runtime, version)


async def test_runtime_keeps_safe_bootstrap_rejection_stage_and_does_not_call_human(key):
    from rumi_market.sdk_http import SdkResponseError
    async def human(*args):
        pytest.fail("human callback after bootstrap rejection")
    runtime, game, events, transports, version, _ = setup(key, failure="captcha_start", captcha_handler=human)
    with pytest.raises(SdkResponseError) as failure:
        await runtime.start("synthetic-account", PRIVATE)
    assert failure.value.response_code == -17 and failure.value.request_stage == "sdk_captcha"
    assert failure.value.__context__ is failure.value.__cause__ is None
    assert events == ["version", "config", "cipher", "login", "captcha_start"] and game.closed
    assert_released(runtime, version)


@pytest.mark.parametrize("action", ["cancel-human", "close-runtime", "cancel-task"])
async def test_runtime_captcha_wait_cancel_and_close_never_enter_game_or_repost(key, action):
    entered, release = asyncio.Event(), asyncio.Event()
    async def human(*args):
        entered.set()
        await release.wait()
        return "" if action == "cancel-human" else '{"validate":"synthetic-human-validate"}'
    runtime, game, events, transports, version, _ = setup(key, captcha_handler=human)
    task = asyncio.create_task(runtime.start("synthetic-account", PRIVATE))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if action == "close-runtime":
            await runtime.close()
        elif action == "cancel-task":
            task.cancel(PRIVATE)
        release.set()
        with pytest.raises(asyncio.CancelledError if action == "cancel-task" else ApiError) as failure:
            await task
        if action == "cancel-human":
            assert failure.value.code == "AUTH_CAPTCHA_CANCELLED"
        assert failure.value.__context__ is failure.value.__cause__ is None
        assert PRIVATE not in str(failure.value) and TOKEN not in str(failure.value)
        assert events == ["version", "config", "cipher", "login", "captcha_start"]
        assert game.closed and game.outgoing.empty() and runtime.closed
        assert [transport.closes for transport in transports] == [1, 1, 1]
        assert runtime._captcha_handler is None
        assert_released(runtime, version)
    finally:
        release.set()
        await runtime.close()


async def test_runtime_clock_requires_query_gate_and_clears_on_close(key, monkeypatch):
    module = implementation()
    handshake_module = importlib.import_module("rumi_market.game.handshake")
    now = [10.0]
    monkeypatch.setattr(handshake_module, "monotonic", lambda: now[0], raising=False)
    runtime, game, events, _, _, _ = setup(key)
    with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_QUERY_UNAVAILABLE$"):
        runtime.server_time_seconds
    server = asyncio.create_task(game_server(game, events))
    try:
        await runtime.start("synthetic-account", PRIVATE)
        await server
        assert runtime.server_time_seconds == 1700000000
        now[0] = 12.0
        assert runtime.server_time_seconds == 1700000002
    finally:
        server.cancel()
        await runtime.close()
    with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_QUERY_UNAVAILABLE$"):
        runtime.server_time_seconds


async def test_runtime_unknown_clock_returns_fixed_safe_error(key):
    module = implementation()
    runtime, game, events, _, _, _ = setup(key)
    server = asyncio.create_task(game_server(game, events, server_time_ms=None))
    try:
        await runtime.start("synthetic-account", PRIVATE)
        await server
        with pytest.raises(module.GameRuntimeError, match="^GAME_RUNTIME_CLOCK_UNAVAILABLE$") as failure:
            runtime.server_time_seconds
        assert failure.value.__context__ is failure.value.__cause__ is None
    finally:
        server.cancel()
        await runtime.close()


@pytest.mark.parametrize("hot_version", [61, 62, 63, 64, 65, 66, 69, 70, 71, 72, 73])
async def test_runtime_live_gold_accepts_hot_versions_and_clears_on_close(key, hot_version):
    from test_game_profile_sync import message
    runtime, game, _, _, version, _ = setup(key)
    assert runtime.gold_balance is None and runtime.account_state is None
    original_fetch = version.fetch
    async def fetch_version():
        return replace(await original_fetch(), hot_version=hot_version)
    version.fetch = fetch_version

    async def serve():
        login = await game.next_request()
        game.respond(2902, login.ReqCmdSeq, CODEC.encode("SNKProto.Cmd_Net_LumiLogin_Ack", {"role_id": 123}))
        enter = await game.next_request()
        game.respond(2912, enter.ReqCmdSeq, CODEC.encode("SNKProto.Cmd_Role_EnterGame_Ack", {
            "zone_id": 7, "zone_name": "synthetic-zone", "server_time_ms": 1700000000123}))
        game.respond(2941, 0, message("Cmd_Notify_ItemAll", item_list=[
            {"item_id": 10001, "count": 80}]).SerializeToString(), notify=1)
        game.respond(2913, 0, CODEC.encode("SNKProto.Cmd_Notify_LoginEnd", {}), notify=2)

    server = asyncio.create_task(serve())
    try:
        await runtime.start("synthetic-account", PRIVATE)
        await server
        assert runtime.gold_balance == 80
        tracker = runtime.account_state
        assert tracker is not None
        revision = tracker.gold_revision
        changed = message("CmdItemReal", item_id=10001, count=65)
        game.respond(2942, 0, message("Cmd_Notify_ItemChange", change_list=[{
            "change_type": 3, "data": changed.SerializeToString()}]).SerializeToString(), notify=3)
        assert await tracker.wait_for_gold_balance(65, revision, timeout=1)
        assert runtime.gold_balance == 65
        assert game.outgoing.empty()  # Observing currency sends no game request.
    finally:
        server.cancel()
        await runtime.close()
    assert runtime.gold_balance is None and runtime.account_state is None
    assert tracker.gold_balance is None


async def test_runtime_close_invalidates_gold_before_awaiting_other_cleanup(key, monkeypatch):
    from rumi_market.game.account_state import GoldItemState
    from test_game_account_state import full, item
    runtime, _, _, _, _, _ = setup(key)
    tracker = GoldItemState(verified_version=True)
    tracker.observe(full(item(30)))
    runtime._account_state = tracker
    entered, release = asyncio.Event(), asyncio.Event()
    original_close_http = runtime._close_http
    async def held_close_http():
        entered.set()
        await release.wait()
        return await original_close_http()
    monkeypatch.setattr(runtime, "_close_http", held_close_http)
    closing = asyncio.create_task(runtime.close())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert tracker.gold_balance is None
    finally:
        release.set()
        await closing


@pytest.mark.parametrize('hot_version', [64, 65, 66])
async def test_runtime_home_tracks_login_and_later_notifications_across_hot_versions(key, monkeypatch, hot_version):
    from rumi_market.game.home_codec import HomeProtoCodec
    from rumi_market.game.version import VersionResult
    from test_home_state import arch

    runtime, game, _, _, version, _ = setup(key)
    with pytest.raises(implementation().GameRuntimeError, match="^GAME_RUNTIME_QUERY_UNAVAILABLE$"):
        runtime.home_client
    async def fixed_version():
        return VersionResult("1.5.3", hot_version)
    monkeypatch.setattr(version, "fetch", fixed_version)
    codec = HomeProtoCodec()
    async def serve():
        login = await game.next_request()
        assert runtime.home_state is not None
        game.respond(2902, login.ReqCmdSeq, CODEC.encode("SNKProto.Cmd_Net_LumiLogin_Ack", {"role_id": 123}))
        enter = await game.next_request()
        game.respond(2912, enter.ReqCmdSeq, CODEC.encode("SNKProto.Cmd_Role_EnterGame_Ack", {"zone_id": 7}))
        game.respond(3998, 0, codec.encode(3998, architecture_list=[arch()]), notify=1)
        game.respond(3001, 0, codec.encode(3001, lumi_list=[{'lumi_uid': 11, 'work_energy': 90}]), notify=2)
        game.respond(2971, 0, codec.encode(2971, lumi_list=[{"lumi_uid": 11, "lumi_id": 701}]), notify=3)
        game.respond(2913, 0, CODEC.encode("SNKProto.Cmd_Notify_LoginEnd", {}), notify=4)
    server = asyncio.create_task(serve())
    try:
        await runtime.start("synthetic-account", PRIVATE)
        await server
        tracker = runtime.home_state
        assert runtime.home_client is runtime._handshake.client
        assert tracker.snapshot()["valid"] and tracker.snapshot()["roster_ready"]
        assert tracker.snapshot()['architectures'][0]['uid'] == 9
        assert tracker.snapshot()['workers'][0]['work_energy'] == 90
        sequence = tracker.sequence
        game.respond(3003, 0, codec.encode(3003, change_list=[{"change_type": 5, "architecture_info": arch(9, 4)}]), notify=5)
        assert await tracker.wait_for_update(sequence, timeout=1)
        assert tracker.snapshot()["architectures"][0]["level"] == 4
    finally:
        server.cancel()
        await runtime.close()
    assert runtime.home_state is None and not tracker.snapshot()["valid"]


def test_runtime_disconnected_client_invalidates_home_on_access(key):
    from types import SimpleNamespace
    from rumi_market.game.home_state import HomeState
    from test_home_state import full, arch
    runtime, _, _, _, _, _ = setup(key)
    tracker = HomeState(verified_version=True)
    tracker.observe(full(arch()))
    runtime._home_state = tracker
    runtime._handshake = SimpleNamespace(client=SimpleNamespace(closed=True))
    assert runtime.home_state is tracker and not tracker.snapshot()["valid"]


async def test_runtime_close_invalidates_home_before_waiting_for_other_cleanup(key, monkeypatch):
    from rumi_market.game.home_state import HomeState
    from test_home_state import full, arch
    runtime, _, _, _, _, _ = setup(key)
    tracker = HomeState(verified_version=True)
    tracker.observe(full(arch()))
    runtime._home_state = tracker
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime._close_http
    async def held_close():
        entered.set()
        await release.wait()
        return await original()
    monkeypatch.setattr(runtime, "_close_http", held_close)
    closing = asyncio.create_task(runtime.close())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert tracker._closed and not tracker.snapshot()["valid"]
    finally:
        release.set()
        await closing
