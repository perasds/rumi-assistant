"""Reopening a page must observe local disconnects without losing saved login."""

import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request
from starlette.responses import Response

from rumi_market.auth import COOKIE_NAME
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.live_runtime import LiveProviderFactory
from rumi_market.models import SessionView
from rumi_market.remember_auth import RememberLoginService
from rumi_market.sessions import SessionStore
from test_game_provider import Runtime as GameRuntime
from test_remember_auth import Factory, MemoryStore, REMEMBER_COOKIE, app, login, new_client
from test_saved_login_provider import Runtime as SavedRuntime
from test_sdk_saved_login import saved_state


class ConnectionFactory(Factory):
    def __call__(self):
        provider = super().__call__()
        provider.connection_alive = True
        return provider


class ObservedLock(asyncio.Lock):
    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.waiting.set()
        return await super().acquire()


def restore_request(token=None):
    headers = [] if token is None else [(b'cookie', (REMEMBER_COOKIE + '=' + token).encode())]
    return Request({'type': 'http', 'method': 'POST', 'path': '/api/auth/restore',
                    'scheme': 'https', 'server': ('testserver', 443), 'headers': headers})


async def remembered_pair():
    store, factory = MemoryStore(), ConnectionFactory()
    sessions = SessionStore(factory)
    service = RememberLoginService(sessions, store)
    owner, visitor = sessions.create(), sessions.create()
    owner.view = await owner.provider.login('synthetic-account', 'synthetic-password')
    await service.save(owner, restore_request(), Response(), True)
    return store, factory, sessions, service, owner, visitor, owner.remember_token


def current_session(application, client):
    return application.state.sessions.get(client.cookies.get(COOKIE_NAME))


def assert_credential_retained(store, client, token):
    assert client.cookies.get(REMEMBER_COOKIE) == token
    assert store.load(token) is not None












@pytest.mark.asyncio
async def test_independent_connection_flag_reads_only_current_runtime_snapshot():
    runtime = GameRuntime()
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=()), login_verified=True)
    try:
        assert provider.connection_alive is False
        assert not runtime.started and runtime.calls == []
        await provider.login('synthetic-account', 'synthetic-password')
        assert provider.connection_alive is True and runtime.calls == []
        runtime.can_query = False
        assert provider.connection_alive is False and runtime.calls == []
        runtime.can_query = True
        assert provider.connection_alive is True and runtime.calls == []
    finally:
        await provider.close()
    assert provider.connection_alive is False and runtime.calls == []


@pytest.mark.asyncio
async def test_lazy_connection_flag_never_builds_or_logs_in_by_itself():
    runtime, builds = SavedRuntime(), []

    async def builder(*, login_state=None):
        builds.append(True)
        return runtime, SimpleNamespace(items=())

    provider = LiveProviderFactory(runtime_builder=builder)()
    try:
        assert provider.connection_alive is False
        assert builds == [] and runtime.calls == []
        await provider.restore_login(saved_state())
        assert provider.connection_alive is True
        assert builds == [True] and runtime.calls == ['restore']
        runtime.can_query = False
        assert provider.connection_alive is False
        assert builds == [True] and runtime.calls == ['restore']
    finally:
        await provider.close()
    assert provider.connection_alive is False
    assert builds == [True] and runtime.calls == ['restore']


@pytest.mark.asyncio
async def test_logout_while_restore_waits_for_owner_does_not_reuse_revoked_connection():
    store, factory, sessions, service, owner, visitor, token = await remembered_pair()
    owner.lock = ObservedLock()
    await owner.lock.acquire()
    pending = asyncio.create_task(service.restore(visitor, restore_request(token), Response()))
    try:
        await asyncio.wait_for(owner.lock.waiting.wait(), 2)
        # DELETE /api/session sets this intent and revokes before taking locks.
        # It cannot remove visitor yet: restore owns visitor.lock while waiting.
        owner.logout_requested = visitor.logout_requested = True
        await service.forget(restore_request(token), Response(), visitor)
        assert store.load(token) is None
        owner.lock.release()
        returned = await asyncio.wait_for(pending, 2)
        assert not returned.view.authenticated
        assert factory.restore_calls == 0
    finally:
        if owner.lock.locked():
            owner.lock.release()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await sessions.close()


@pytest.mark.asyncio
async def test_logout_during_visitor_cleanup_cannot_return_authenticated_owner():
    store, factory, sessions, service, owner, visitor, token = await remembered_pair()
    started, release = asyncio.Event(), asyncio.Event()
    original_remove = sessions.remove

    async def yielding_remove(session):
        if session is visitor:
            started.set()
            await release.wait()
        await original_remove(session)

    sessions.remove = yielding_remove
    pending = asyncio.create_task(service.restore(visitor, restore_request(token), Response()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        owner.logout_requested = visitor.logout_requested = True
        await service.forget(restore_request(token), Response(), visitor)
        release.set()
        returned = await asyncio.wait_for(pending, 2)
        assert not returned.view.authenticated
        assert store.load(token) is None and factory.restore_calls == 0
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await sessions.close()


@pytest.mark.asyncio
async def test_credential_expiring_while_waiting_for_owner_does_not_reuse_connection():
    store, factory, sessions, service, owner, visitor, token = await remembered_pair()
    owner.lock = ObservedLock()
    await owner.lock.acquire()
    pending = asyncio.create_task(service.restore(visitor, restore_request(token), Response()))
    try:
        await asyncio.wait_for(owner.lock.waiting.wait(), 2)
        store.now += 604801
        owner.lock.release()
        returned = await asyncio.wait_for(pending, 2)
        assert not returned.view.authenticated
        assert store.load(token) is None and factory.restore_calls == 0
    finally:
        if owner.lock.locked():
            owner.lock.release()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await sessions.close()


@pytest.mark.asyncio
async def test_logout_during_dead_owner_refresh_does_not_start_saved_login():
    store, factory, sessions, service, owner, visitor, token = await remembered_pair()
    owner.provider.connection_alive = False
    started, release = asyncio.Event(), asyncio.Event()
    original_close = owner.provider.close

    async def yielding_close():
        started.set()
        await release.wait()
        await original_close()

    owner.provider.close = yielding_close
    pending = asyncio.create_task(service.restore(visitor, restore_request(token), Response()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        owner.logout_requested = visitor.logout_requested = True
        await service.forget(restore_request(token), Response(), visitor)
        release.set()
        returned = await asyncio.wait_for(pending, 2)
        assert not returned.view.authenticated
        assert store.load(token) is None
        assert factory.restore_calls == 0
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await sessions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidation', ['logout', 'expiry'])
async def test_credential_invalidated_during_provider_reset_does_not_start_saved_login(invalidation):
    store, factory, sessions, service, owner, visitor, token = await remembered_pair()
    owner.view = SessionView(status='expired')
    owner.provider.connection_alive = False
    started, release = asyncio.Event(), asyncio.Event()
    original_close = visitor.provider.close

    async def yielding_close():
        started.set()
        await release.wait()
        await original_close()

    visitor.provider.close = yielding_close
    pending = asyncio.create_task(service.restore(visitor, restore_request(token), Response()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if invalidation == 'logout':
            owner.logout_requested = visitor.logout_requested = True
            await service.forget(restore_request(token), Response(), visitor)
        else:
            store.now += 604801
        release.set()
        returned = await asyncio.wait_for(pending, 2)
        assert not returned.view.authenticated
        assert store.load(token) is None and factory.restore_calls == 0
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await sessions.close()

def test_dead_connection_expires_without_automatic_relogin(catalog_path):
    store, factory = MemoryStore(), ConnectionFactory()
    application = app(catalog_path, store, factory)
    with TestClient(application) as client:
        assert login(client).json()['authenticated']
        session = current_session(application, client)
        original = session.provider
        original.connection_alive = False
        response = client.get('/api/session')
        assert response.json()['status'] == 'expired'
        assert original.closed and factory.restore_calls == 0
        assert client.post('/api/auth/restore').status_code == 404
        assert not store.records


@pytest.mark.parametrize('connection', [True, None, 'missing'])
def test_active_session_refresh_does_not_start_new_login(catalog_path, connection):
    store, factory = MemoryStore(), Factory()
    application = app(catalog_path, store, factory)
    with TestClient(application) as client:
        assert login(client).json()['authenticated']
        provider = current_session(application, client).provider
        if connection != 'missing':
            provider.connection_alive = connection
        assert client.get('/api/session').json()['authenticated']
        assert factory.restore_calls == 0 and not provider.closed


def test_role_selection_refresh_preserves_current_session(catalog_path):
    store, factory = MemoryStore(), ConnectionFactory()
    application = app(catalog_path, store, factory)
    with TestClient(application) as client:
        assert login(client).json()['authenticated']
        session = current_session(application, client)
        session.provider.connection_alive = False
        session.view = SessionView(status='role_selection', authenticated=True)
        assert client.get('/api/session').json()['status'] == 'role_selection'
        assert not session.provider.closed and factory.restore_calls == 0
