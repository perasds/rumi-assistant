"""Synthetic HTTP contracts for encrypted login persistence and revocation."""

import asyncio
from copy import deepcopy
import inspect
import secrets
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from login_helpers import encrypt_login
from providers import ScriptedProvider
from rumi_market.app import create_app
from rumi_market.errors import ApiError
from rumi_market.models import Role, SessionView
from rumi_market.remember_store import RememberLoginStore


REMEMBER_COOKIE = 'rumi_remember'
PRIVATE = 'synthetic-sdk-token-not-for-http'
STATE = {'version': 1, 'access_key': PRIVATE, 'uid': 'synthetic-sdk-user'}


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.now = 1000
        self.revoked = 0

    def issue(self, payload):
        token = secrets.token_urlsafe(32)
        self.records[token] = (deepcopy(payload), self.now + 604800)
        return token

    def load(self, token):
        value = self.records.get(token)
        if not value or value[1] <= self.now:
            return None
        return SimpleNamespace(payload=deepcopy(value[0]), expires_at=value[1])

    def update(self, token, payload):
        if not self.load(token):
            return False
        self.records[token] = (deepcopy(payload), self.records[token][1])
        return True

    def revoke(self, token):
        self.records.pop(token, None)
        self.revoked += 1


class Factory:
    def __init__(self):
        self.providers = []
        self.restore_calls = 0
        self.restore_error = None
        self.started = self.release = None

    def __call__(self):
        factory = self

        class RememberProvider(ScriptedProvider):
            def export_login_state(self):
                return deepcopy(STATE)

            async def restore_login(self, state):
                assert state == STATE
                factory.restore_calls += 1
                if factory.started:
                    factory.started.set()
                    await factory.release.wait()
                if factory.restore_error:
                    raise factory.restore_error
                role = Role(id='synthetic-restored-role', name='', server_name='synthetic-zone')
                return SessionView(status='connected', authenticated=True, role=role, roles=[role])

        provider = RememberProvider()
        self.providers.append(provider)
        return provider


def app(catalog_path, store, factory):
    assert 'remember_store' in inspect.signature(create_app).parameters, 'Remembered login HTTP integration is missing'
    return create_app(catalog_path=catalog_path, provider_factory=factory, remember_store=store)


def login(client, remember=True):
    challenge = client.get('/api/auth/login-challenge')
    assert challenge.status_code == 200
    payload = {'account': 'synthetic-account', 'password': 'synthetic-password'}
    if remember is not None:
        payload['remember_login'] = remember
    return client.post('/api/auth/login', json=encrypt_login(challenge.json(), payload))


def new_client(application, token):
    client = TestClient(application, base_url='https://testserver')
    client.cookies.set(REMEMBER_COOKIE, token, domain='testserver.local', path='/')
    return client



@pytest.mark.parametrize('preference', [True, False, None])
def test_old_encrypted_preference_never_saves_or_restores(catalog_path, preference):
    store, factory = MemoryStore(), Factory()
    with TestClient(app(catalog_path, store, factory), base_url='https://testserver') as client:
        assert client.get('/api/auth/capabilities').json()['remember_login'] is False
        assert login(client, preference).json()['authenticated']
        assert store.records == {} and factory.restore_calls == 0
        assert client.cookies.get(REMEMBER_COOKIE) is None
        assert client.post('/api/auth/restore').status_code == 404


def test_existing_saved_cookie_is_cleared_without_loading_or_deleting_private_store(catalog_path):
    store, factory = MemoryStore(), Factory()
    token = store.issue(STATE)
    application = app(catalog_path, store, factory)
    with new_client(application, token) as client:
        response = client.get('/api/session')
        assert response.status_code == 200 and not response.json()['authenticated']
        assert client.cookies.get(REMEMBER_COOKIE) is None
        assert store.load(token) is not None and factory.restore_calls == 0
        assert client.post('/api/auth/restore').status_code == 404
        assert PRIVATE not in response.text and token not in response.text


def test_live_app_never_constructs_persistent_login_store():
    from pathlib import Path
    import rumi_market.live_app as module
    source = Path(module.__file__).read_text(encoding='utf-8')
    assert 'RememberLoginStore' not in source
    assert 'remember-logins.sqlite3' not in source


def test_ignored_store_is_never_accessed_even_when_broken(catalog_path):
    class UnusedStore:
        def __getattr__(self, name):
            raise AssertionError('Persistent login store must not be used')
    with TestClient(app(catalog_path, UnusedStore(), Factory())) as client:
        assert login(client).json()['authenticated']
        assert client.delete('/api/session').status_code == 200


def test_normal_session_survives_refresh_and_logout_clears_it(catalog_path):
    store, factory = MemoryStore(), Factory()
    application = app(catalog_path, store, factory)
    with TestClient(application) as client:
        assert login(client).json()['authenticated']
        session_id = client.cookies.get('rumi_session')
        assert client.get('/api/session').json()['authenticated']
        assert client.cookies.get('rumi_session') == session_id
        assert client.delete('/api/session').status_code == 200
        assert not client.get('/api/session').json()['authenticated']
        assert factory.restore_calls == 0 and not store.records


def test_password_preference_type_is_still_validated_before_auth(catalog_path):
    store, factory = MemoryStore(), Factory()
    with TestClient(app(catalog_path, store, factory)) as client:
        assert login(client, 'true').status_code == 422
        assert not any(provider.account for provider in factory.providers)
