import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.models import Capabilities, Role, SessionView

from login_helpers import async_login_body, login_body


class Provider:
    def __init__(self, *, failure=False, wait=None):
        self.failure, self.wait, self.closed = failure, wait, False
    def capabilities(self):
        return Capabilities(integration_ready=True, methods=["account"], reason_code=None, message=None)
    async def login(self, account, password):
        if self.wait is not None:
            self.wait.set()
            await asyncio.Event().wait()
        if self.failure:
            raise RuntimeError("synthetic-private-error")
        role = Role(id=str(id(self)), name="", server_name="synthetic zone")
        return SessionView(status="connected", authenticated=True, role=role, roles=[role])
    async def close(self):
        self.closed = True


def test_live_entry_is_ready_but_capability_and_session_get_are_lazy(catalog_path, monkeypatch):
    from rumi_market.live_app import create_live_app
    import rumi_market.live_runtime as module
    async def forbidden(*args):
        pytest.fail("public metadata must not create SDK clients")
    monkeypatch.setattr(module, "build_live_runtime", forbidden)
    with TestClient(create_live_app(catalog_path=catalog_path)) as client:
        for _ in range(2):
            assert client.get("/api/health").json()["game_integration_ready"]
        assert client.get("/api/auth/capabilities").json()["methods"] == ["account", "sms"]
        assert client.get("/api/session").json()["status"] == "anonymous"
        assert client.get("/api/auth/login-challenge").status_code == 200
        assert client.post("/api/auth/qr", json={}).status_code == 503


@pytest.mark.parametrize("timeout", [30, 300])
def test_login_wait_is_configurable_without_changing_default(catalog_path, monkeypatch, timeout):
    import rumi_market.auth as auth
    original, calls = asyncio.timeout, []
    def record(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(auth.asyncio, "timeout", record)
    options = {"login_timeout": timeout} if timeout != 30 else {}
    with TestClient(create_app(provider_factory=Provider, catalog_path=catalog_path, **options)) as client:
        assert client.post("/api/auth/login", json=login_body(client, "synthetic-a", "synthetic-p")).status_code == 200
        assert timeout in calls


@pytest.mark.parametrize("timeout", [0, -1, False, float("nan"), 301])
def test_invalid_login_timeout_is_rejected_before_serving(catalog_path, timeout):
    with pytest.raises(ValueError, match="^Invalid login timeout$"):
        create_app(catalog_path=catalog_path, login_timeout=timeout)


def test_failed_relogin_logout_and_new_browser_use_fresh_provider(catalog_path):
    providers = []
    def builder():
        provider = Provider(failure=not providers)
        providers.append(provider)
        return provider
    application = create_app(provider_factory=builder, catalog_path=catalog_path)
    with TestClient(application) as client:
        failed = client.post("/api/auth/login", json=login_body(client, "synthetic-a", "synthetic-p"))
        assert failed.status_code == 503 and "synthetic" not in failed.text
        assert providers[0].closed and len(providers) == 2
        first = client.post("/api/auth/login", json=login_body(client, "synthetic-a", "synthetic-p")).json()
        second = client.post("/api/auth/login", json=login_body(client, "synthetic-b", "synthetic-p")).json()
        assert providers[1].closed and first["role"]["id"] != second["role"]["id"]
        assert client.delete("/api/session").json()["status"] == "anonymous"
        assert providers[2].closed
        client.get("/api/session")
        assert len(providers) == 4 and not providers[3].closed
        client.cookies.clear()
        client.get("/api/session")
        assert len(providers) == 5 and not providers[4].closed


@pytest.mark.asyncio
async def test_cancelled_api_login_resets_anonymous_session_and_replaces_provider(catalog_path):
    entered, providers = asyncio.Event(), []
    def builder():
        provider = Provider(wait=entered)
        providers.append(provider)
        return provider
    application = create_app(provider_factory=builder, catalog_path=catalog_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver") as client:
        await client.get("/api/session")
        body = await async_login_body(client, "synthetic-a", "synthetic-p")
        task = asyncio.create_task(client.post("/api/auth/login", json=body))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert providers[0].closed and len(providers) == 2
        assert (await client.get("/api/session")).json()["status"] == "anonymous"
    await application.state.sessions.close()
