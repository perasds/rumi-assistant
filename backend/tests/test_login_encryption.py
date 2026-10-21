import asyncio
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.login_encryption import MAX_PLAINTEXT_BYTES
from rumi_market.models import Capabilities, Role, SessionView

from login_helpers import async_login_body, encrypt_login, login_body


class RecordingProvider:
    def __init__(self):
        self.logins, self.closed = [], False

    def capabilities(self):
        return Capabilities(integration_ready=True, methods=["account"], reason_code=None, message=None)

    async def login(self, account, password):
        self.logins.append((account, password))
        role = Role(id="synthetic-role", name="", server_name="synthetic-zone")
        return SessionView(status="connected", authenticated=True, role=role, roles=[role])

    async def close(self):
        self.closed = True


@pytest.fixture
def harness(catalog_path):
    providers = []

    def factory():
        provider = RecordingProvider()
        providers.append(provider)
        return provider

    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    with TestClient(app) as client:
        yield app, client, providers


def challenge(client):
    response = client.get("/api/auth/login-challenge")
    assert response.status_code == 200
    return response.json()


def assert_invalid(response, *, code="LOGIN_PAYLOAD_INVALID", status=400):
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "synthetic-private" not in response.text
    assert "input" not in response.text


def session_for(app, client):
    return app.state.sessions.get(client.cookies["rumi_session"])


def test_challenge_is_public_ephemeral_crypto_metadata_with_private_cookie(harness):
    app, client, providers = harness
    before = datetime.now(timezone.utc)
    response = client.get("/api/auth/login-challenge")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"challenge_id", "public_key", "algorithm", "expires_at"}
    assert len(body["challenge_id"]) == 32
    assert body["algorithm"] == "RSA-OAEP-256+A256GCM"
    key = serialization.load_der_public_key(base64.b64decode(body["public_key"], validate=True))
    assert isinstance(key, rsa.RSAPublicKey) and key.key_size == 2048
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert 115 < (expires - before).total_seconds() < 125
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert len(providers) == 1 and providers[0].logins == []
    assert session_for(app, client).view.status == "anonymous"


def test_framework_mode_can_issue_challenge_without_game_login(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        body = challenge(client)
        response = client.post("/api/auth/login", json=encrypt_login(body, {"account": "a", "password": "p"}))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AUTH_PROTOCOL_UNVERIFIED"


@pytest.mark.parametrize("headers", [
    {"Origin": "https://other.example"}, {"Origin": "null"},
    {"Origin": "http://testserver:bad"}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_challenge_rejects_foreign_browser_origin_before_session_creation(harness, headers):
    app, client, providers = harness
    response = client.get("/api/auth/login-challenge", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_REJECTED"
    assert providers == [] and app.state.sessions.sessions == {}


@pytest.mark.parametrize("account,password", [
    ("synthetic-account", "synthetic-private-password"),
    ("合成🙂账户", "测试🔐口令"), ("🙂" * 256, "🔐" * 4096),
], ids=["ascii", "unicode", "max-unicode-lengths"])
def test_valid_envelope_passes_only_decrypted_credentials_to_provider(harness, account, password):
    app, client, providers = harness
    body = login_body(client, account, password)
    response = client.post("/api/auth/login", json=body)
    assert response.status_code == 200 and response.json()["authenticated"]
    assert providers[0].logins == [(account, password)]
    assert password not in response.text and account not in response.text
    assert session_for(app, client).login_challenge is None


def test_plaintext_login_is_rejected_without_creating_a_provider(harness):
    app, client, providers = harness
    response = client.post("/api/auth/login", json={"account": "synthetic-private-account", "password": "synthetic-private-password"})
    assert_invalid(response, code="INVALID_INPUT", status=422)
    assert providers == [] and app.state.sessions.sessions == {}
    assert "set-cookie" not in response.headers


def test_envelope_without_cookie_or_challenge_creates_no_session(harness):
    app, client, providers = harness
    body = encrypt_login(challenge(client), {"account": "a", "password": "p"})
    client.cookies.clear()
    before = len(app.state.sessions.sessions)
    assert_invalid(client.post("/api/auth/login", json=body))
    assert len(providers) == before == len(app.state.sessions.sessions)
    client.get("/api/session")
    assert_invalid(client.post("/api/auth/login", json=body))
    assert providers[-1].logins == []


def test_challenge_is_one_use_and_replay_preserves_connected_provider(harness):
    app, client, providers = harness
    body = login_body(client, "a", "synthetic-private-password")
    assert client.post("/api/auth/login", json=body).status_code == 200
    assert_invalid(client.post("/api/auth/login", json=body))
    assert len(providers) == 1 and len(providers[0].logins) == 1 and not providers[0].closed
    assert session_for(app, client).view.status == "connected"


def test_overwritten_challenge_does_not_consume_newer_challenge(harness):
    app, client, providers = harness
    old = login_body(client, "old", "p")
    new = login_body(client, "new", "p")
    assert old["challenge_id"] != new["challenge_id"]
    assert_invalid(client.post("/api/auth/login", json=old))
    assert client.post("/api/auth/login", json=new).status_code == 200
    assert providers[0].logins == [("new", "p")]


def test_foreign_challenge_does_not_consume_either_cookie_challenge(harness):
    app, first, providers = harness
    first_body = login_body(first, "one", "p")
    with TestClient(app) as second:
        second_body = login_body(second, "two", "p")
        assert_invalid(second.post("/api/auth/login", json=first_body))
        assert second.post("/api/auth/login", json=second_body).status_code == 200
        assert first.post("/api/auth/login", json=first_body).status_code == 200
        assert [provider.logins for provider in providers] == [[("one", "p")], [("two", "p")]]


def test_expired_challenge_is_consumed_before_provider_side_effects(harness, monkeypatch):
    app, client, providers = harness
    body = login_body(client, "a", "p")
    session = session_for(app, client)
    deadline = session.login_challenge.deadline
    monkeypatch.setattr("rumi_market.login_encryption.monotonic", lambda: deadline + 1)
    assert_invalid(client.post("/api/auth/login", json=body), code="LOGIN_CHALLENGE_EXPIRED")
    assert session.login_challenge is None and providers[0].logins == [] and not providers[0].closed


@pytest.mark.parametrize("mutation", [
    {"encrypted_key": "!"}, {"iv": "!"}, {"ciphertext": "!"},
    {"encrypted_key": base64.b64encode(b"x" * 255).decode()},
    {"iv": base64.b64encode(b"x" * 11).decode()},
    {"ciphertext": base64.b64encode(b"x" * 15).decode()},
    {"ciphertext": "A" * 32769},
    {"encrypted_key": base64.b64encode(b"x" * 256).decode()},
    {"iv": 123}, {"encrypted_key": []}, {"ciphertext": None},
    {"extra": "synthetic-private"},
])
def test_malformed_envelope_consumes_matching_challenge_without_login(harness, mutation):
    app, client, providers = harness
    valid = login_body(client, "a", "synthetic-private-password")
    malformed = {**valid, **mutation}
    assert_invalid(client.post("/api/auth/login", json=malformed))
    assert session_for(app, client).login_challenge is None
    assert_invalid(client.post("/api/auth/login", json=valid))
    assert providers[0].logins == [] and not providers[0].closed and len(providers) == 1


@pytest.mark.parametrize("kind", ["tag", "wrong-aad", "aes128", "noncanonical-base64"])
def test_crypto_authentication_failures_are_safe_and_consume_challenge(harness, kind):
    app, client, providers = harness
    issued = challenge(client)
    options = {"aad": b"wrong-context"} if kind == "wrong-aad" else {"key": b"x" * 16} if kind == "aes128" else {}
    body = encrypt_login(issued, {"account": "a", "password": "synthetic-private"}, **options)
    if kind == "tag":
        data = bytearray(base64.b64decode(body["ciphertext"]))
        data[-1] ^= 1
        body["ciphertext"] = base64.b64encode(data).decode()
    elif kind == "noncanonical-base64":
        body["iv"] += "\n"
    assert_invalid(client.post("/api/auth/login", json=body))
    assert session_for(app, client).login_challenge is None and providers[0].logins == []


@pytest.mark.parametrize("plaintext,status", [
    (b'{"account":"a","password":"p","password":"synthetic-private"}', 400),
    (b'{"account":"a","account":"b","password":"p"}', 400),
    (b'not json synthetic-private', 400), (b'\xff', 400),
    (b'{"account":"a","password":"p"}' + b' ' * MAX_PLAINTEXT_BYTES, 400),
    (b'[]', 422), (b'null', 422), (b'{"account":"a","password":7}', 422),
    (b'{"account":false,"password":"p"}', 422),
    (b'{"account":"a","password":"p","extra":"synthetic-private"}', 422),
    (b'{"account":"","password":"p"}', 422),
    (b'{"account":"a","password":""}', 422),
    (b'{"account":"a","password":"p","extra":NaN}', 400),
], ids=["duplicate-password", "duplicate-account", "malformed-json", "invalid-utf8", "oversized-plaintext",
        "array", "null", "numeric-password", "boolean-account", "extra-field", "empty-account",
        "empty-password", "nonfinite-json"])
def test_decrypted_json_is_strict_bounded_and_never_echoed(harness, plaintext, status):
    app, client, providers = harness
    body = encrypt_login(challenge(client), plaintext=plaintext)
    assert_invalid(client.post("/api/auth/login", json=body),
                   code="INVALID_INPUT" if status == 422 else "LOGIN_PAYLOAD_INVALID", status=status)
    assert session_for(app, client).login_challenge is None and providers[0].logins == []


@pytest.mark.parametrize("credentials", [
    {"account": "a" * 257, "password": "p"},
    {"account": "a", "password": "p" * 4097},
])
def test_decrypted_credentials_retain_existing_character_limits(harness, credentials):
    _, client, providers = harness
    body = encrypt_login(challenge(client), credentials)
    assert_invalid(client.post("/api/auth/login", json=body), code="INVALID_INPUT", status=422)
    assert providers[0].logins == []


def test_invalid_relogin_does_not_close_or_reset_connected_provider(harness):
    app, client, providers = harness
    assert client.post("/api/auth/login", json=login_body(client, "a", "p")).status_code == 200
    body = login_body(client, "b", "synthetic-private-password")
    body["ciphertext"] = "!"
    assert_invalid(client.post("/api/auth/login", json=body))
    session = session_for(app, client)
    assert session.view.status == "connected" and session.view.authenticated
    assert providers[0].logins == [("a", "p")] and not providers[0].closed and len(providers) == 1


def test_envelope_with_plaintext_field_consumes_matching_challenge(harness):
    app, client, providers = harness
    body = login_body(client, "a", "p")
    body["password"] = "synthetic-private"
    assert_invalid(client.post("/api/auth/login", json=body), code="INVALID_INPUT", status=422)
    assert session_for(app, client).login_challenge is None
    assert providers[0].logins == [] and not providers[0].closed


def test_decrypted_utf16_is_not_reinterpreted_as_utf8_credentials(harness):
    _, client, providers = harness
    body = encrypt_login(challenge(client), plaintext='{"account":"a","password":"p"}'.encode("utf-16"))
    assert_invalid(client.post("/api/auth/login", json=body))
    assert providers[0].logins == []


@pytest.mark.parametrize("kind", ["array", "null", "bad-json", "oversized", "duplicate-key", "missing-field"])
def test_outer_json_parsing_is_strict_and_bounded(harness, kind):
    _, client, providers = harness
    body = login_body(client, "a", "p")
    if kind == "duplicate-key":
        content = json.dumps(body)[:-1] + ',"ciphertext":"synthetic-private"}'
    elif kind == "missing-field":
        body.pop("ciphertext")
        content = json.dumps(body)
    else:
        content = {"array": "[]", "null": "null", "bad-json": "{", "oversized": " " * 45001}[kind]
    assert_invalid(client.post("/api/auth/login", content=content, headers={"Content-Type": "application/json"}))
    assert providers[0].logins == [] and not providers[0].closed


def test_logout_removes_challenge_and_old_cookie_cannot_create_provider(harness):
    app, client, providers = harness
    body = login_body(client, "a", "p")
    session = session_for(app, client)
    cookie = client.cookies["rumi_session"]
    assert client.delete("/api/session").status_code == 200
    assert session.login_challenge is None and providers[0].closed
    client.cookies.set("rumi_session", cookie)
    assert_invalid(client.post("/api/auth/login", json=body))
    assert len(providers) == 1


async def test_session_expiry_and_close_clear_challenge(harness):
    app, client, _ = harness
    challenge(client)
    session = session_for(app, client)
    await app.state.sessions.expire(session)
    assert session.login_challenge is None
    challenge(client)
    await app.state.sessions.close()
    assert session.login_challenge is None


def test_login_models_hide_opaque_secrets_and_credential_repr(harness):
    from rumi_market.models import EncryptedLoginRequest, LoginRequest

    _, client, _ = harness
    body = login_body(client, "synthetic-private-account", "synthetic-private-password")
    encrypted = EncryptedLoginRequest.model_validate(body)
    for field in ["encrypted_key", "iv", "ciphertext"]:
        assert body[field] not in repr(encrypted)
        assert encrypted.model_dump(mode="json")[field] == "**********"
    credentials = LoginRequest(account="synthetic-private-account", password="synthetic-private-password")
    assert "synthetic-private" not in repr(credentials)


async def test_challenge_expiry_does_not_cancel_already_started_captcha_wait(catalog_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(RecordingProvider):
        async def login(self, account, password):
            entered.set()
            await release.wait()
            return await super().login(account, password)

    provider = WaitingProvider()
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path, login_timeout=300)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            body = await async_login_body(client, "a", "p")
            session = session_for(app, client)
            deadline = session.login_challenge.deadline
            task = asyncio.create_task(client.post("/api/auth/login", json=body))
            await asyncio.wait_for(entered.wait(), 2)
            monkeypatch.setattr("rumi_market.login_encryption.monotonic", lambda: deadline + 1000)
            release.set()
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 200 and response.json()["authenticated"]
