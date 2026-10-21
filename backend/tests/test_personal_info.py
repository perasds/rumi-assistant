"""Only the current session's four safe personal fields cross the API boundary."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.auth import COOKIE_NAME
from rumi_market.errors import ApiError
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.models import SessionView
from rumi_market.providers import UnverifiedGameProvider
from rumi_market.sessions import Session


class Runtime:
    def __init__(self, role_id=9007199254740993, balance=0, server_name='测试区服'):
        self.can_query = True
        self.gold_balance = balance
        self.result = SimpleNamespace(role_id=role_id, zone_name=server_name,
            needs_activation=False, needs_initial_pet_selection=False)

    async def start(self, account, password):
        return self.result

    async def close(self):
        self.can_query = False


def connected(runtime=None):
    runtime = runtime or Runtime()
    provider = IndependentGameProvider(runtime, SimpleNamespace(), login_verified=True)
    view = asyncio.run(provider.login('synthetic-account', 'synthetic-password'))
    return provider, view, runtime


def add_session(app, provider, view):
    session = Session(f'synthetic-session-{len(app.state.sessions.sessions)}', provider, view=view)
    app.state.sessions.sessions[session.id] = session
    return session


@pytest.mark.parametrize('status', ['anonymous', 'authenticating', 'expired'])
def test_personal_info_rejects_unauthenticated_session(catalog_path, status):
    app = create_app(catalog_path=catalog_path)
    with TestClient(app) as client:
        if status != 'anonymous':
            session = add_session(app, UnverifiedGameProvider(), SessionView(status=status))
            client.cookies.set(COOKIE_NAME, session.id)
        response = client.get('/api/me')
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'AUTH_REQUIRED'


def test_personal_info_requires_selected_role(catalog_path):
    app = create_app(catalog_path=catalog_path)
    session = add_session(app, UnverifiedGameProvider(),
        SessionView(status='role_selection', authenticated=True))
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, session.id)
        assert client.get('/api/me').status_code == 409


@pytest.mark.parametrize(('balance', 'expected'), [
    (None, None), (0, '0'), (2**63 - 1, str(2**63 - 1)),
    (True, None), (-1, None), (2**64, None), ('123', None), (1.5, None),
])
def test_personal_info_has_only_current_safe_fields_and_preserves_gold(catalog_path, balance, expected):
    provider, view, runtime = connected(Runtime(balance=balance))
    app = create_app(catalog_path=catalog_path)
    session = add_session(app, provider, view)
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, session.id)
        response = client.get('/api/me')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json() == {
            'role_id': str(runtime.result.role_id), 'nickname': None,
            'server_name': '测试区服', 'balance_gold': expected,
        }
        assert view.role.id not in response.text
        assert session.id not in response.text
        assert 'synthetic-account' not in response.text
        assert 'synthetic-password' not in response.text
        runtime.gold_balance = 12
        assert client.get('/api/me').json()['balance_gold'] == '12'


def test_personal_info_uses_each_cookie_session_and_logout_clears_access(catalog_path):
    app = create_app(catalog_path=catalog_path)
    first, first_view, _ = connected(Runtime(role_id=111, balance=10, server_name='测试甲区'))
    second, second_view, _ = connected(Runtime(role_id=222, balance=20, server_name='测试乙区'))
    sessions = [add_session(app, first, first_view), add_session(app, second, second_view)]
    with TestClient(app) as client:
        for session, role_id, balance in [(sessions[0], '111', '10'), (sessions[1], '222', '20')]:
            client.cookies.set(COOKIE_NAME, session.id)
            data = client.get('/api/me').json()
            assert data['role_id'] == role_id and data['balance_gold'] == balance
        assert client.delete('/api/session').status_code == 200
        assert client.get('/api/me').status_code == 401
        client.cookies.set(COOKIE_NAME, sessions[0].id)
        assert client.get('/api/me').json()['role_id'] == '111'


def test_disconnected_personal_info_expires_session_without_stale_data(catalog_path):
    provider, view, runtime = connected()
    app = create_app(catalog_path=catalog_path)
    session = add_session(app, provider, view)
    runtime.can_query = False
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, session.id)
        response = client.get('/api/me')
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'GAME_SESSION_EXPIRED'
        assert 'role_id' not in response.json()
        assert client.get('/api/session').json()['status'] == 'expired'
        assert provider.gold_balance is None


@pytest.mark.parametrize('invalid', [False, True])
def test_personal_info_does_not_leak_provider_failures_or_extra_fields(catalog_path, invalid):
    class FailedProvider:
        async def personal_info(self):
            if invalid:
                return {'role_id': '123', 'token': 'synthetic-private-marker'}
            raise RuntimeError('synthetic-private-marker')

        async def close(self):
            pass

    _, view, _ = connected()
    app = create_app(catalog_path=catalog_path)
    session = add_session(app, FailedProvider(), view)
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, session.id)
        response = client.get('/api/me')
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'UPSTREAM_UNAVAILABLE'
        assert 'synthetic-private-marker' not in response.text
        assert 'token' not in response.text


@pytest.mark.asyncio
async def test_live_personal_info_delegates_after_login_and_rejects_closed_session():
    from rumi_market.live_runtime import LiveProviderFactory

    builds = []

    async def builder():
        builds.append(True)
        return Runtime(balance=321), SimpleNamespace(items=())

    provider = LiveProviderFactory(runtime_builder=builder)()
    with pytest.raises(ApiError):
        await provider.personal_info()
    assert builds == []
    await provider.login('synthetic-account', 'synthetic-password')
    assert (await provider.personal_info()).balance_gold == '321'
    await provider.close()
    with pytest.raises(ApiError):
        await provider.personal_info()


@pytest.mark.asyncio
async def test_unverified_provider_personal_info_is_unavailable():
    with pytest.raises(ApiError) as caught:
        await UnverifiedGameProvider().personal_info()
    assert caught.value.code == 'AUTH_PROTOCOL_UNVERIFIED'
