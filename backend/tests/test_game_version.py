import hashlib

import httpx
import pytest
from pydantic import SecretStr

from rumi_market.errors import ApiError
from rumi_market.sdk_device import SdkDeviceIdentity


ORIGINS = (
    "https://l1-prod-cn-ob-vs-lumigo.bilibiligame.net",
    "https://l3-prod-cn-ob-vs-lumigo.bilibiligame.net",
    "https://lumigo-cn-prod-vs.bilibiligame.net",
)


def context_for(**changes):
    from rumi_market.game.version import VersionRequestContext

    values = {
        "gm_secret": SecretStr("synthetic-secret-雪"),
        "identity": SdkDeviceIdentity("synthetic-sdk-udid-雪", "synthetic-sdk-bd-id"),
        "device_id": "synthetic-game-buvid-雪",
        "channel": "Android_CN_CDN", "version": "1.5.3", "build_version": "17",
        "profile_source": "synthetic game DeviceId; independent SDK identity",
    }
    return VersionRequestContext(**(values | changes))


def client_for(handler, *, context=None, origin=ORIGINS[0], clock_seconds=None):
    from rumi_market.game.version import VersionHttpClient

    return VersionHttpClient(origin, context or context_for(),
        transport=httpx.MockTransport(handler),
        clock_seconds=clock_seconds or (lambda: 1700000000))


def success_payload(hot_version="62", *, version="1.5.3", code=0):
    return {"code": code, "data": {
        "version": version, "hotVersion": {"version": hot_version},
        "deviceId": "synthetic-private-response", "token": "synthetic-private-response",
    }}


def assert_safe_failure(error):
    assert error.code == "AUTH_UPSTREAM_FAILED"
    assert error.status_code == 503 and error.retryable
    assert "synthetic-private-response" not in str(error.body())
    assert error.__context__ is None and error.__cause__ is None


@pytest.mark.anyio
async def test_exact_compact_utf8_request_and_signature_keep_null_and_identity():
    context = context_for()
    expected = ('{"channel":"Android_CN_CDN","version":"1.5.3","buildVersion":"17",'
                '"timestamp":1700000000,"aid":"","deviceId":"synthetic-game-buvid-雪",'
                '"nation_code":"CN","queueToken":null}').encode("utf-8")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == ORIGINS[0] + "/api/v1/vs/client/VscQueryVersion"
        assert request.content == expected
        assert context.identity.udid.encode("utf-8") not in request.content
        assert context.identity.bd_id.encode("utf-8") not in request.content
        assert request.headers["X-App-Id"] == "1000069"
        assert request.headers["X-App-Client-Sign"] == hashlib.sha256(
            expected + "synthetic-secret-雪".encode("utf-8")).hexdigest()
        assert request.headers["Content-Type"] == "application/json;charset=utf-8"
        assert request.headers["sessionId"] == "0"
        return httpx.Response(200, json=success_payload())

    client = client_for(handler, context=context)
    try:
        result = await client.fetch()
        assert vars(result) == {"version": "1.5.3", "hot_version": 62, "chat_servers": ()}
        assert client.identity is context.identity
        assert len({context.device_id, context.identity.udid, context.identity.bd_id}) == 3
        assert len(calls) == 1
        assert "synthetic-private-response" not in repr(result)
        for private in ("synthetic-secret-雪", "synthetic-game-buvid-雪",
                        "synthetic-sdk-udid-雪", "synthetic-sdk-bd-id"):
            assert private not in repr(context) and private not in repr(client)
    finally:
        await client.close()
    assert client.identity is context.identity


@pytest.mark.anyio
async def test_fresh_project_identity_is_reused_and_session_id_returns_old_counter():
    identity = SdkDeviceIdentity.create()
    ids = []

    def handler(request):
        assert b'"deviceId":""' in request.content
        assert identity.udid.encode() not in request.content
        assert identity.bd_id.encode() not in request.content
        ids.append(request.headers["sessionId"])
        return httpx.Response(200, json=success_payload())

    client = client_for(handler, context=context_for(identity=identity, device_id="",
        profile_source="synthetic SDKDataCenter constructor-initial DeviceId; no native fallback asserted"))
    try:
        await client.fetch()
        await client.fetch()
        assert ids == ["0", "1"] and client.identity is identity
    finally:
        await client.close()
    assert client.identity is identity


def test_device_id_is_required_without_sdk_identity_fallback():
    from rumi_market.game.version import VersionRequestContext

    values = vars(context_for()).copy()
    values.pop("device_id")
    with pytest.raises(TypeError):
        VersionRequestContext(**values)


@pytest.mark.anyio
@pytest.mark.parametrize("origin", ORIGINS)
async def test_only_source_root_origins_are_accepted(origin):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=success_payload())

    client = client_for(handler, origin=origin + "/")
    try:
        await client.fetch()
        assert calls == [origin + "/api/v1/vs/client/VscQueryVersion"]
    finally:
        await client.close()


@pytest.mark.parametrize("origin", [
    "http://l1-prod-cn-ob-vs-lumigo.bilibiligame.net",
    "https://example.invalid", ORIGINS[0] + ".example.invalid",
    ORIGINS[0] + ":8443", ORIGINS[0] + "/other",
    ORIGINS[0] + "?synthetic-private-response", ORIGINS[0] + "#private",
    "https://synthetic-private-response@l1-prod-cn-ob-vs-lumigo.bilibiligame.net",
    None,
])
def test_invalid_origins_are_rejected_before_http(origin):
    with pytest.raises(ValueError) as caught:
        client_for(lambda request: pytest.fail("unexpected HTTP"), origin=origin)
    assert "synthetic-private-response" not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("changes", [
    {"gm_secret": "synthetic-private-response"}, {"gm_secret": SecretStr("")},
    {"gm_secret": SecretStr(123)},
    {"identity": None}, {"identity": SdkDeviceIdentity("", "synthetic-bd-id")},
    {"device_id": None}, {"device_id": 0}, {"device_id": False},
    {"channel": ""}, {"version": "synthetic-private-response"},
    {"build_version": 17}, {"profile_source": ""},
])
def test_incomplete_or_untyped_context_is_rejected_before_http(changes):
    with pytest.raises(ValueError) as caught:
        client_for(lambda request: pytest.fail("unexpected HTTP"), context=context_for(**changes))
    assert "synthetic-private-response" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize("hot,code,expected", [
    ("62", 0, 62), (62, "0", 62), (0, 0, 0), ("0", 0, 0),
    (2147483647, 0, 2147483647), ("2147483647", 0, 2147483647), ("00062", 0, 62),
])
async def test_success_uses_exact_hot_version_path_and_nonnegative_int32(hot, code, expected):
    client = client_for(lambda request: httpx.Response(200,
        json=success_payload(hot, code=code)))
    try:
        result = await client.fetch()
        assert result.version == "1.5.3" and result.hot_version == expected
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("payload", [
    [], None, {"code": False, "data": {"hotVersion": {"version": "62"}}},
    success_payload(code=True), success_payload(code=1), success_payload(code="00"),
    {"code": 0}, {"code": 0, "data": None}, {"code": 0, "data": []},
    {"code": 0, "data": {"version": "1.5.3", "hotVersion": None}},
    {"code": 0, "data": {"version": "1.5.3", "hotVersion": {}}},
    {"code": 0, "data": {"version": "1.5.3", "result": {"hotVersion": {"version": "62"}}}},
    {"code": 0, "data": {"hotVersion": {"version": "62"}}},
    success_payload(version="synthetic-private-response"),
    *[success_payload(value) for value in
      (None, True, False, -1, 62.0, "", "-1", "+62", " 62", "62 ", "６２", "0x3e",
       "62.0", 2147483648, "2147483648", "9" * 5000)],
])
async def test_invalid_business_or_version_values_fail_without_fallback_or_response(payload):
    client = client_for(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ApiError) as caught:
            await client.fetch()
        assert_safe_failure(caught.value)
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("status,body", [
    (500, b"synthetic-private-response"),
    (302, b"synthetic-private-response"),
    (200, b"synthetic-private-response"),
    (200, b"synthetic-private-response" * 60000),
], ids=["server-error", "redirect", "invalid-json", "oversized"])
async def test_bad_http_invalid_json_and_oversized_response_are_sanitized(status, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=body, headers={"Location": ORIGINS[1]})

    client = client_for(handler)
    try:
        with pytest.raises(ApiError) as caught:
            await client.fetch()
        assert_safe_failure(caught.value)
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.anyio
async def test_transport_error_cannot_expose_request_or_upstream_exception():
    def handler(request):
        raise httpx.ConnectError("synthetic-private-response", request=request)

    client = client_for(handler)
    try:
        with pytest.raises(ApiError) as caught:
            await client.fetch()
        assert_safe_failure(caught.value)
    finally:
        await client.close()


@pytest.mark.anyio
@pytest.mark.parametrize("timestamp", [True, 1.5, "1700000000", -(2**63)-1, 2**63])
async def test_timestamp_must_be_signed_int64_seconds_before_http(timestamp):
    client = client_for(lambda request: pytest.fail("unexpected HTTP"),
                        clock_seconds=lambda: timestamp)
    try:
        with pytest.raises(ApiError) as caught:
            await client.fetch()
        assert caught.value.code == "AUTH_PROTOCOL_UNVERIFIED"
        assert caught.value.__context__ is None and caught.value.__cause__ is None
    finally:
        await client.close()


@pytest.mark.anyio
async def test_closed_client_does_not_send_and_keeps_identity_for_sdk():
    context = context_for()
    client = client_for(lambda request: pytest.fail("unexpected HTTP"), context=context)
    await client.close()
    with pytest.raises(ApiError) as caught:
        await client.fetch()
    assert caught.value.code == "AUTH_PROTOCOL_UNVERIFIED"
    assert client.identity is context.identity


@pytest.mark.anyio
@pytest.mark.parametrize("device_id", ["synthetic-private-declared-game-device", ""])
async def test_declared_game_device_property_is_read_only_and_survives_context_release(device_id):
    context = context_for(device_id=device_id)
    client = client_for(lambda request: pytest.fail("unexpected HTTP"), context=context)
    try:
        assert client.device_id == device_id
        with pytest.raises(AttributeError):
            client.device_id = "replacement"
        assert "synthetic-private-declared-game-device" not in repr(client)
    finally:
        await client.close()
    assert client._context is None
    assert client.device_id == device_id and client.identity is context.identity
