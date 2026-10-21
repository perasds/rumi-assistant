"""Removing administration must leave game and public APIs available."""

import importlib
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.models import Capabilities, Role, SessionView

from login_helpers import login_body


ADMIN_PATHS = [
    "/api/admin/session", "/api/admin/login-challenge", "/api/admin/login",
    "/api/admin/overview", "/api/admin/users", "/api/admin/unknown",
]


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("path", ADMIN_PATHS)
@pytest.mark.parametrize("legacy_cookie", [False, True])
def test_admin_routes_are_absent_with_or_without_legacy_cookie(catalog_path, method, path, legacy_cookie):
    app = create_app(catalog_path=catalog_path)
    with TestClient(app) as client:
        if legacy_cookie:
            client.cookies.set("rumi_admin", "synthetic-retired-session", path="/api/admin")
            client.cookies.set("rumi_admin_challenge", "synthetic-retired-challenge", path="/api/admin")
        response = client.request(method, path)
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"
        assert not any(path.startswith("/api/admin") for path in app.openapi()["paths"])


class LoginProvider:
    def capabilities(self):
        return Capabilities(integration_ready=True, methods=["account"], reason_code=None, message=None)

    async def login(self, account, password):
        role = Role(id="synthetic-role", name="", server_name="测试区服")
        return SessionView(status="connected", authenticated=True, role=role, roles=[role])

    async def select_role(self, role_id):
        return await self.login("", "")

    async def close(self):
        pass


def test_game_login_and_role_selection_need_no_admin_database(catalog_path, monkeypatch):
    original_connect, connections = sqlite3.connect, []

    def catalog_database_only(database, *args, **kwargs):
        assert database == ":memory:", "Game sessions must not open a persistent database"
        connections.append(database)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", catalog_database_only)
    app = create_app(catalog_path=catalog_path, provider_factory=LoginProvider)
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json=login_body(client, "synthetic-account", "synthetic-secret"))
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert client.get("/api/session").json()["status"] == "connected"
        assert client.post("/api/session/role", json={"role_id": "synthetic-role"}).status_code == 200
        assert client.delete("/api/session").json()["status"] == "anonymous"
        assert connections == [":memory:"]  # Only the public catalog uses SQLite.
        assert not hasattr(app.state, "admin_store")
        assert not hasattr(app.state.sessions, "login_store")


def test_public_catalog_and_game_task_routes_remain_available(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.get("/api/pets").status_code == 200
        assert client.get("/api/auth/login-challenge").status_code == 200
        for path in ["/api/market/listings", "/api/rush-purchase", "/api/home/orders/state", "/api/redbags/task"]:
            assert client.get(path).status_code == 401


def test_live_entry_ignores_retired_admin_configuration(tmp_path, monkeypatch):
    password_file = tmp_path / "admin-password.hash"
    password_file.write_text("synthetic-retired-hash", encoding="utf-8")
    monkeypatch.setenv("RUMI_ADMIN_PASSWORD_FILE", str(password_file))
    original_read = Path.read_text

    def reject_retired_password(path, *args, **kwargs):
        if path == password_file:
            raise AssertionError("Live entry must not read the retired administration password")
        return original_read(path, *args, **kwargs)

    original_connect = sqlite3.connect

    def catalog_database_only(database, *args, **kwargs):
        assert database == ":memory:", "Live entry must not open a persistent database"
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_retired_password)
    monkeypatch.setattr(sqlite3, "connect", catalog_database_only)
    module = importlib.import_module("rumi_market.live_app")
    importlib.reload(module)
    assert not hasattr(module.app.state, "admin_store")
