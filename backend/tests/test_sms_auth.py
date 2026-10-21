"""Encrypted SMS HTTP contracts; all credentials and providers are synthetic."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from login_helpers import encrypt_login
from rumi_market.app import create_app
from rumi_market.errors import ApiError
from rumi_market.models import Capabilities, Role, SessionView


CONTACT = {'mobile': '15500000000', 'country_code': '86'}


class SmsProvider:
    def __init__(self):
        self.sent, self.attempts, self.closed = [], [], False
        self.contact = None
        self.fail_send = self.fail_login = None

    def capabilities(self):
        return Capabilities(integration_ready=True, methods=['account', 'sms'])

    async def send_sms(self, mobile, country_code):
        self.sent.append((mobile, country_code))
        if self.fail_send:
            raise self.fail_send
        self.contact = (mobile, country_code)

    async def login_sms(self, mobile, country_code, otp):
        self.attempts.append((mobile, country_code, otp))
        if self.fail_login:
            raise self.fail_login
        if self.contact != (mobile, country_code):
            raise ApiError('AUTH_REJECTED', 'synthetic-private')
        if otp != '123456':
            raise ApiError('AUTH_SMS_REJECTED', otp)
        return self.connected()

    async def login(self, account, password):
        return self.connected()

    def connected(self):
        role = Role(id='synthetic-role', name='', server_name='synthetic-zone')
        return SessionView(status='connected', authenticated=True, role=role, roles=[role])

    async def close(self):
        self.closed = True
        self.contact = None


@pytest.fixture
def sms_app(catalog_path):
    providers = []
    def factory():
        value = SmsProvider()
        providers.append(value)
        return value
    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    with TestClient(app) as client:
        yield app, client, providers


def encrypted_post(client, path, payload):
    challenge = client.get('/api/auth/login-challenge').json()
    return client.post(path, json=encrypt_login(challenge, payload))


def send(client):
    return encrypted_post(client, '/api/auth/sms/send', CONTACT)


def login(client, otp='123456', **contact):
    return encrypted_post(client, '/api/auth/sms/login', {**CONTACT, **contact, 'otp': otp})


def test_send_uses_encrypted_payload_and_returns_no_ticket_or_contact(sms_app):
    app, client, providers = sms_app
    response = send(client)
    assert response.status_code == 200
    assert response.json() == {'resend_after_seconds': 60}
    assert response.headers['cache-control'] == 'no-store'
    assert providers[0].sent == [('15500000000', '86')]
    assert client.get('/api/session').json()['status'] == 'anonymous'
    assert '15500000000' not in response.text and 'captcha_key' not in response.text


def test_send_cooldown_prevents_upstream_duplicate(sms_app, monkeypatch):
    app, client, providers = sms_app
    now = 1000
    monkeypatch.setattr('rumi_market.auth.monotonic', lambda: now)
    assert send(client).status_code == 200
    response = send(client)
    assert response.status_code == 429 and response.json()['error']['code'] == 'AUTH_SMS_COOLDOWN'
    assert len(providers[0].sent) == 1
    now += 61
    assert send(client).status_code == 200
    assert len(providers[0].sent) == 2


def test_wrong_otp_can_be_corrected_without_sending_another_sms(sms_app):
    app, client, providers = sms_app
    assert send(client).status_code == 200
    response = login(client, '654321')
    assert response.status_code == 401 and response.json()['error']['code'] == 'AUTH_SMS_REJECTED'
    assert '654321' not in response.text and not providers[0].closed
    assert client.get('/api/session').json()['status'] == 'anonymous'
    assert login(client).json()['authenticated'] is True
    assert len(providers) == 1 and len(providers[0].sent) == 1


def test_sms_contact_binding_cannot_be_swapped(sms_app):
    app, client, providers = sms_app
    assert send(client).status_code == 200
    response = login(client, mobile='15500000001')
    assert response.status_code == 401
    assert providers[0].closed and len(providers) == 2


def test_unexpected_sms_failure_is_sanitized_and_resets_provider(sms_app):
    app, client, providers = sms_app
    assert send(client).status_code == 200
    providers[0].fail_login = RuntimeError('synthetic-private-response')
    response = login(client)
    assert response.status_code == 503
    assert 'synthetic-private' not in response.text
    assert providers[0].closed and len(providers) == 2


@pytest.mark.parametrize('path', ['/api/auth/sms/send', '/api/auth/sms/login'])
def test_sms_rejects_plaintext_without_creating_session(sms_app, path):
    app, client, providers = sms_app
    response = client.post(path, json={**CONTACT, 'otp': '123456'})
    assert response.status_code == 422
    assert providers == [] and not app.state.sessions.sessions
    assert CONTACT['mobile'] not in response.text and '123456' not in response.text


@pytest.mark.parametrize('payload', [
    {**CONTACT, 'mobile': 'synthetic-private'}, {**CONTACT, 'country_code': '0'},
    {**CONTACT, 'extra': 'synthetic-private'}, {**CONTACT, 'mobile': 15500000000},
])
def test_sms_validation_is_private_and_precedes_side_effects(sms_app, payload):
    app, client, providers = sms_app
    response = encrypted_post(client, '/api/auth/sms/send', payload)
    assert response.status_code == 422
    assert 'synthetic-private' not in response.text and providers[0].sent == []


def test_sms_challenge_is_one_use_and_session_bound(sms_app):
    app, first, providers = sms_app
    challenge = first.get('/api/auth/login-challenge').json()
    body = encrypt_login(challenge, CONTACT)
    with TestClient(app) as other:
        other.get('/api/session')
        assert other.post('/api/auth/sms/send', json=body).status_code == 400
        assert first.post('/api/auth/sms/send', json=body).status_code == 200
        assert first.post('/api/auth/sms/send', json=body).status_code == 400


def test_requesting_code_cannot_replace_connected_session(sms_app):
    app, client, providers = sms_app
    assert send(client).status_code == 200 and login(client).status_code == 200
    response = send(client)
    assert response.status_code == 409
    assert not providers[0].closed and client.get('/api/session').json()['authenticated']


def test_plaintext_otp_and_phone_cannot_be_added_to_envelope(sms_app):
    app, client, providers = sms_app
    challenge = client.get('/api/auth/login-challenge').json()
    body = {**encrypt_login(challenge, CONTACT), 'otp': '654321'}
    response = client.post('/api/auth/sms/send', json=body)
    assert response.status_code == 422
    assert providers[0].sent == []


def test_feature_disabled_provider_never_sends(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = send(client)
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'AUTH_PROTOCOL_UNVERIFIED'


@pytest.mark.parametrize('otp', ['12345', '1234567', '１２３４５６', '12345a', 123456])
def test_otp_is_exactly_six_ascii_digits_before_authentication(sms_app, otp):
    app, client, providers = sms_app
    assert send(client).status_code == 200
    assert login(client, otp).status_code == 422
    assert providers[0].attempts == [] and not providers[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['send', 'login'])
@pytest.mark.parametrize('ending', ['success', 'cancel', 'logout'])
async def test_sms_reuses_manual_captcha_and_cleans_up(catalog_path, monkeypatch, operation, ending):
    from test_web_captcha import Provider

    class CaptchaSmsProvider(Provider):
        def capabilities(self):
            return Capabilities(integration_ready=True, methods=['sms'])

        async def send_sms(self, mobile, country_code):
            await self.login(mobile, country_code)

        async def login_sms(self, mobile, country_code, otp):
            return await self.login(mobile, otp)

    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    provider = CaptchaSmsProvider()
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            challenge = (await client.get('/api/auth/login-challenge')).json()
            payload = CONTACT if operation == 'send' else {**CONTACT, 'otp': '123456'}
            pending = asyncio.create_task(client.post('/api/auth/sms/' + operation,
                json=encrypt_login(challenge, payload)))
            await asyncio.wait_for(provider.entered.wait(), 2)
            session = app.state.sessions.get(client.cookies['rumi_session'])
            assert session.login_in_progress
            active = (await client.get('/api/auth/captcha')).json()
            if ending == 'success':
                provider.host.emit({'status': 'completed', 'result': 'synthetic-private-callback'})
            elif ending == 'cancel':
                assert (await client.delete('/api/auth/captcha/' + active['challenge_id'])).status_code == 200
            else:
                assert (await asyncio.wait_for(client.delete('/api/session'), 2)).status_code == 200
            result = await asyncio.wait_for(pending, 2)
            assert result.status_code == (200 if ending == 'success' else 401)
            assert provider.host.closed and session.captcha is None
            assert not session.login_in_progress and not app.state.sessions.web_login_lock.locked()
            assert 'synthetic-private' not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['account', 'sms_login'])
async def test_logout_intent_cannot_commit_late_login(catalog_path, operation):
    entered, release = asyncio.Event(), asyncio.Event()
    class Waiting(SmsProvider):
        async def login(self, account, password):
            entered.set()
            await release.wait()
            return self.connected()

        async def login_sms(self, mobile, country_code, otp):
            return await self.login(mobile, otp)

    providers = []
    def factory():
        provider = Waiting()
        providers.append(provider)
        return provider

    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            challenge = (await client.get('/api/auth/login-challenge')).json()
            path = '/api/auth/login' if operation == 'account' else '/api/auth/sms/login'
            payload = {'account': 'synthetic', 'password': 'synthetic'} if operation == 'account' else {**CONTACT, 'otp': '123456'}
            pending = asyncio.create_task(client.post(path, json=encrypt_login(challenge, payload)))
            await asyncio.wait_for(entered.wait(), 2)
            session = app.state.sessions.get(client.cookies['rumi_session'])
            logout = asyncio.create_task(client.delete('/api/session'))
            while not session.logout_requested:
                await asyncio.sleep(0)
            release.set()
            assert (await asyncio.wait_for(pending, 2)).status_code == 401
            assert (await asyncio.wait_for(logout, 2)).status_code == 200
            assert providers[0].closed and not session.active and not session.view.authenticated


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['account', 'sms_send', 'sms_login'])
@pytest.mark.parametrize('swallow_cancel', [False, True])
async def test_explicit_cancel_ends_only_matching_authentication(catalog_path, operation, swallow_cancel):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Waiting(SmsProvider):
        async def wait(self):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                if not swallow_cancel:
                    raise
                await release.wait()
        async def send_sms(self, *args):
            await self.wait()
        async def login(self, *args):
            await self.wait()
            return self.connected()
        async def login_sms(self, *args):
            return await self.login(*args)
    providers = []
    def factory():
        value = Waiting()
        providers.append(value)
        return value
    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            challenge = (await client.get('/api/auth/login-challenge')).json()
            path = {'account': '/api/auth/login', 'sms_send': '/api/auth/sms/send', 'sms_login': '/api/auth/sms/login'}[operation]
            payload = {'account': 'a', 'password': 'p'} if operation == 'account' else CONTACT if operation == 'sms_send' else {**CONTACT, 'otp': '123456'}
            pending = asyncio.create_task(client.post(path, json=encrypt_login(challenge, payload)))
            await asyncio.wait_for(entered.wait(), 2)
            # A stale operation or another browser cannot cancel this authentication.
            assert (await client.delete('/api/auth/pending/' + 'x' * 32)).status_code == 200
            assert not cancelled.is_set()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as other:
                assert (await other.delete('/api/auth/pending/' + challenge['challenge_id'])).status_code == 200
            assert not cancelled.is_set()
            assert (await client.delete('/api/auth/pending/' + challenge['challenge_id'])).status_code == 200
            await asyncio.wait_for(cancelled.wait(), 2)
            release.set()
            response = await asyncio.wait_for(pending, 2)
            assert response.status_code == 401
            session = app.state.sessions.get(client.cookies['rumi_session'])
            assert not session.view.authenticated and providers[0].closed
            assert session.auth_task is None and not session.login_in_progress
            assert not session.lock.locked()


def test_cancel_old_challenge_cannot_logout_successful_session(sms_app):
    app, client, providers = sms_app
    assert send(client).status_code == 200
    challenge = client.get('/api/auth/login-challenge').json()
    assert client.post('/api/auth/sms/login', json=encrypt_login(challenge, {**CONTACT, 'otp': '123456'})).json()['authenticated']
    assert client.delete('/api/auth/pending/' + challenge['challenge_id']).status_code == 200
    assert client.get('/api/session').json()['authenticated'] and not providers[0].closed


def test_cancel_unused_challenge_prevents_late_submit(sms_app):
    app, client, providers = sms_app
    challenge = client.get('/api/auth/login-challenge').json()
    body = encrypt_login(challenge, CONTACT)
    assert client.delete('/api/auth/pending/' + challenge['challenge_id']).status_code == 200
    assert client.post('/api/auth/sms/send', json=body).status_code == 400
    assert providers[0].sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['account', 'sms_login'])
async def test_cancel_during_expired_provider_cleanup_never_starts_new_auth(catalog_path, operation):
    entered, release = asyncio.Event(), asyncio.Event()
    class SlowClose(SmsProvider):
        async def close(self):
            entered.set()
            await release.wait()
            await super().close()
    providers = []
    def factory():
        value = SlowClose() if not providers else SmsProvider()
        providers.append(value)
        return value
    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            challenge = (await client.get('/api/auth/login-challenge')).json()
            session = app.state.sessions.get(client.cookies['rumi_session'])
            session.view = SessionView(status='expired')
            path = '/api/auth/login' if operation == 'account' else '/api/auth/sms/login'
            payload = {'account': 'a', 'password': 'p'} if operation == 'account' else {**CONTACT, 'otp': '123456'}
            pending = asyncio.create_task(client.post(path, json=encrypt_login(challenge, payload)))
            await asyncio.wait_for(entered.wait(), 2)
            assert session.auth_task is None
            assert (await client.delete('/api/auth/pending/' + challenge['challenge_id'])).status_code == 200
            release.set()
            result = await asyncio.wait_for(pending, 2)
            assert result.status_code == 401 and result.json()['error']['code'] == 'AUTH_LOGIN_CANCELLED'
            assert providers[1].attempts == [] and not session.view.authenticated
