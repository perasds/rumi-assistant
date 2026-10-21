"""Synthetic browser pipes only; never contacts an official challenge."""
import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.models import Capabilities, SessionView

from login_helpers import async_login_body

PNG = base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic-image').decode()


def timing_message():
    return {'kind': 'timing', 'requests': {
        name: {'completed': 0, 'failed': 0, 'pending': 0, 'total_ms': 0, 'max_ms': 0}
        for name in ['document', 'script', 'stylesheet', 'image', 'font', 'xhr', 'fetch', 'other']},
        'screenshot': {'count': 1, 'total_ms': 12.5, 'max_ms': 12.5, 'total_bytes': 64, 'max_bytes': 64},
        'input': {'count': 1, 'total_ms': 2, 'max_ms': 2}}


def test_timing_log_accepts_only_fixed_bounded_numeric_summary(caplog):
    import rumi_market.web_captcha as module
    with caplog.at_level('INFO', logger='uvicorn.error'):
        assert module.log_timing_summary(timing_message())
    assert len(caplog.records) == 1
    assert caplog.records[0].name == 'uvicorn.error'
    assert caplog.records[0].getMessage().startswith('captcha_timing ')
    assert json.loads(caplog.records[0].getMessage().split(' ', 1)[1]) == timing_message()


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(url='synthetic-private-url'),
    lambda value: value['requests'].update({'synthetic-private-type': {}}),
    lambda value: value['requests']['font'].update(pending=True),
    lambda value: value['requests']['font'].update(completed=0.5),
    lambda value: value['requests']['font'].update(failed=-1),
    lambda value: value['requests']['font'].update(completed=100001),
    lambda value: value['requests']['font'].update(total_ms=1),
    lambda value: value['input'].update(total_ms=float('nan')),
    lambda value: value['input'].update(max_ms=float('inf')),
    lambda value: value['input'].update(total_ms=1),
    lambda value: value['input'].update(max_ms=600001, total_ms=600001),
    lambda value: value['screenshot'].update(max_bytes=4 * 1024 * 1024, total_bytes=4 * 1024 * 1024),
    lambda value: value['screenshot'].update(total_bytes=-1),
    lambda value: value['screenshot'].update(count=0),
    lambda value: value['screenshot'].update(image='synthetic-private-image'),
    lambda value: value.pop('input'),
])
def test_invalid_timing_is_never_logged(mutation, caplog):
    import rumi_market.web_captcha as module
    value = timing_message()
    mutation(value)
    with caplog.at_level('INFO', logger='uvicorn.error'):
        assert not module.log_timing_summary(value)
    assert caplog.records == []


def test_timing_logger_failure_does_not_change_login_behavior(monkeypatch):
    import logging
    import rumi_market.web_captcha as module
    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic-private-log-error')
    monkeypatch.setattr(logging.getLogger('uvicorn.error'), 'info', unavailable)
    assert module.log_timing_summary(timing_message())


class Host:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.commands = []
        self.closed = False
        self.stdout.feed_data(b'{"kind":"ready","width":520,"height":900}\n')

    async def send(self, message):
        self.commands.append(message)
        if message['type'] == 'screenshot':
            self.emit({'kind': 'frame', 'id': message['id'], 'image': PNG, 'width': 520, 'height': 900})
        else:
            self.emit({'kind': 'ack', 'id': message['id']})

    def emit(self, message):
        self.stdout.feed_data(json.dumps(message).encode() + b'\n')

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize('valid', [True, False])
async def test_web_timing_is_optional_and_does_not_interfere_with_frame_ack_or_result(valid, caplog):
    from rumi_market.web_captcha import WebCaptchaChannel
    channel, host = WebCaptchaChannel(), Host()
    value = timing_message()
    if not valid:
        value['url'] = 'synthetic-private-url'
    host.emit(value)
    with caplog.at_level('INFO', logger='uvicorn.error'):
        running = asyncio.create_task(channel.run(host.stdout, host.send, host.close))
        while not channel.active:
            await asyncio.sleep(0)
        frame = await channel.snapshot()
        await channel.input({'type': 'click', 'x': 1, 'y': 2})
        host.emit({'status': 'completed', 'result': 'synthetic-private-result'})
        assert await running == 'synthetic-private-result'
    assert frame['image'] == 'data:image/png;base64,' + PNG
    assert [command['type'] for command in host.commands] == ['screenshot', 'click']
    assert len(caplog.records) == int(valid)
    assert 'synthetic-private' not in caplog.text


@pytest.mark.asyncio
async def test_web_rejects_a_second_timing_message_without_logging_its_content(caplog):
    from rumi_market.web_captcha import WebCaptchaChannel
    channel, host = WebCaptchaChannel(), Host()
    host.emit({'kind': 'timing', 'private': 'synthetic-private-one'})
    host.emit({'kind': 'timing', 'private': 'synthetic-private-two'})
    with caplog.at_level('INFO', logger='uvicorn.error'):
        with pytest.raises(Exception):
            await channel.run(host.stdout, host.send, host.close)
    assert caplog.records == []
    assert host.closed


class Provider:
    def __init__(self):
        self.host = Host()
        self.entered = asyncio.Event()
        self.closed = False

    def capabilities(self):
        return Capabilities(integration_ready=True, methods=['account'], reason_code=None, message=None)

    async def login(self, account, password):
        from rumi_market.web_captcha import current_channel
        self.channel = current_channel.get()
        self.entered.set()
        await self.channel.run(self.host.stdout, self.host.send, self.host.close)
        return SessionView(status='role_selection', authenticated=True)

    async def close(self):
        self.closed = True


def test_captcha_waiting_requires_existing_cookie_and_never_creates_session(catalog_path):
    app = create_app(catalog_path=catalog_path)
    with TestClient(app) as client:
        response = client.get('/api/auth/captcha')
        assert response.status_code == 401
        assert app.state.sessions.sessions == {}
        assert client.post('/api/auth/captcha/missing/input', json={'type': 'click', 'x': 1, 'y': 1}).status_code == 401
        assert client.delete('/api/auth/captcha/missing').status_code == 401
        client.get('/api/session')
        assert client.get('/api/auth/captcha').json() == {'status': 'waiting'}
        assert client.get('/api/auth/captcha').headers['cache-control'] == 'no-store'
        app.state.sessions.get(client.cookies['rumi_session']).view = SessionView(status='expired')
        assert client.get('/api/auth/captcha').status_code == 401
        client.delete('/api/session')
        assert client.get('/api/auth/captcha').status_code == 401


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://foreign.invalid'}, {'Origin': 'null'},
    {'Origin': 'http://testserver:bad'}, {'Sec-Fetch-Site': 'cross-site'},
    {'Sec-Fetch-Site': 'same-site'},
])
def test_captcha_get_is_same_origin_and_uncacheable(catalog_path, headers):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        client.get('/api/session')
        response = client.get('/api/auth/captcha', headers=headers)
        assert response.status_code == 403
        assert response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
async def test_active_captcha_is_cookie_owned_and_input_does_not_deadlock_login(catalog_path, monkeypatch):
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    providers = []
    def factory():
        value = Provider()
        providers.append(value)
        return value
    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as first:
            body = await async_login_body(first, 'synthetic', 'private')
            provider = providers[0]
            login = asyncio.create_task(first.post('/api/auth/login', json=body))
            await asyncio.wait_for(provider.entered.wait(), 2)
            response = await asyncio.wait_for(first.get('/api/auth/captcha'), 2)
            active = response.json()
            assert response.status_code == 200
            assert set(active) == {'status', 'challenge_id', 'image', 'width', 'height'}
            assert active['status'] == 'active' and len(active['challenge_id']) == 32
            assert (active['width'], active['height'], active['image']) == (520, 900, 'data:image/png;base64,' + PNG)
            target = '/api/auth/captcha/' + active['challenge_id']
            foreign = {'Origin': 'https://foreign.invalid'}
            assert (await first.post(target + '/input', json={'type': 'click', 'x': 1, 'y': 2}, headers=foreign)).status_code == 403
            assert (await first.delete(target, headers=foreign)).status_code == 403
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as second:
                await second.get('/api/session')
                assert (await second.get('/api/auth/captcha')).json() == {'status': 'waiting'}
                assert (await second.post(target + '/input', json={'type': 'click', 'x': 1, 'y': 2})).status_code == 404
                assert (await second.delete(target)).status_code == 404
            for action in ['click', 'pointerdown', 'pointermove', 'pointerup']:
                result = await first.post(target + '/input', json={'type': action, 'x': 100, 'y': 200})
                expected = {'status': 'ok'}
                if action in {'click', 'pointerup'}:
                    expected['frame'] = active
                assert result.json() == expected
            assert [command['type'] for command in provider.host.commands] == [
                'screenshot', 'click', 'screenshot', 'pointerdown', 'pointermove', 'pointerup', 'screenshot']
            assert not login.done()
            provider.host.emit({'status': 'completed', 'result': 'synthetic-private-callback'})
            completed = await asyncio.wait_for(login, 2)
            assert completed.status_code == 200
            assert 'synthetic-private' not in completed.text + response.text
            assert provider.host.closed
            assert app.state.sessions.get(first.cookies['rumi_session']).captcha is None
            assert (await first.get('/api/auth/captcha')).json() == {'status': 'waiting'}
            assert (await first.post(target + '/input', json={'type': 'click', 'x': 1, 'y': 2})).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['click', 'pointerup'])
async def test_acknowledged_final_input_returns_waiting_when_verification_completes(catalog_path, monkeypatch, action):
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    provider = Provider()
    original_send = provider.host.send
    async def complete_after_ack(command):
        await original_send(command)
        if command['type'] == action:
            provider.host.emit({'status': 'completed', 'result': 'synthetic-private-callback'})
    provider.host.send = complete_after_ack
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            body = await async_login_body(client, 'a', 'p')
            login = asyncio.create_task(client.post('/api/auth/login', json=body))
            await asyncio.wait_for(provider.entered.wait(), 2)
            active = (await client.get('/api/auth/captcha')).json()
            response = await client.post('/api/auth/captcha/' + active['challenge_id'] + '/input',
                                        json={'type': action, 'x': 1, 'y': 2})
            assert response.status_code == 200
            assert response.json() == {'status': 'ok', 'frame': {'status': 'waiting'}}
            assert (await asyncio.wait_for(login, 2)).status_code == 200
            commands = [command['type'] for command in provider.host.commands]
            assert commands in (['screenshot', action], ['screenshot', action, 'screenshot'])
            assert 'synthetic-private' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['click', 'pointerup'])
@pytest.mark.parametrize('failure', ['unavailable', 'timeout'])
async def test_screenshot_failure_after_ack_does_not_fail_or_replay_input(catalog_path, monkeypatch, action, failure):
    from rumi_market.errors import ApiError
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    provider = Provider()
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            body = await async_login_body(client, 'a', 'p')
            login = asyncio.create_task(client.post('/api/auth/login', json=body))
            await asyncio.wait_for(provider.entered.wait(), 2)
            active = (await client.get('/api/auth/captcha')).json()
            attempts = []
            async def fail_snapshot():
                assert [command['type'] for command in provider.host.commands] == ['screenshot', action]
                attempts.append('snapshot')
                if failure == 'timeout':
                    raise TimeoutError('synthetic-private-timeout')
                raise ApiError('AUTH_CAPTCHA_UNAVAILABLE', 'synthetic-private-error', 503)
            monkeypatch.setattr(provider.channel, 'snapshot', fail_snapshot)
            target = '/api/auth/captcha/' + active['challenge_id']
            try:
                response = await client.post(target + '/input', json={'type': action, 'x': 1, 'y': 2})
                assert response.status_code == 200
                assert response.json() == {'status': 'ok'}
                assert attempts == ['snapshot']
                assert [command['type'] for command in provider.host.commands] == ['screenshot', action]
                assert 'synthetic-private' not in response.text
            finally:
                await client.delete(target)
                await login


@pytest.mark.asyncio
async def test_input_failure_is_not_hidden_by_optional_frame_response(catalog_path, monkeypatch):
    from rumi_market.errors import ApiError
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    provider = Provider()
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            body = await async_login_body(client, 'a', 'p')
            login = asyncio.create_task(client.post('/api/auth/login', json=body))
            await asyncio.wait_for(provider.entered.wait(), 2)
            active = (await client.get('/api/auth/captcha')).json()
            attempts = []
            async def fail_input(body):
                raise ApiError('AUTH_CAPTCHA_TIMEOUT', '人工验证已超时', 503)
            async def snapshot():
                attempts.append('snapshot')
            monkeypatch.setattr(provider.channel, 'input', fail_input)
            monkeypatch.setattr(provider.channel, 'snapshot', snapshot)
            target = '/api/auth/captcha/' + active['challenge_id']
            try:
                response = await client.post(target + '/input', json={'type': 'click', 'x': 1, 'y': 2})
                assert response.status_code == 503
                assert response.json()['error']['code'] == 'AUTH_CAPTCHA_TIMEOUT'
                assert attempts == []
                assert [command['type'] for command in provider.host.commands] == ['screenshot']
            finally:
                await client.delete(target)
                await login


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cancel', 'logout', 'timeout', 'failure'])
async def test_captcha_endings_close_owned_host_and_reset_login(catalog_path, monkeypatch, ending):
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    provider = Provider()
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path,
                     login_timeout=0.15 if ending == 'timeout' else 5)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            body = await async_login_body(client, 'a', 'p')
            session = app.state.sessions.get(client.cookies['rumi_session'])
            login = asyncio.create_task(client.post('/api/auth/login', json=body))
            await asyncio.wait_for(provider.entered.wait(), 2)
            active = (await client.get('/api/auth/captcha')).json()
            if ending == 'cancel':
                result = await asyncio.wait_for(client.delete('/api/auth/captcha/' + active['challenge_id']), 2)
                assert result.json() == {'status': 'ok'}
            elif ending == 'logout':
                assert (await asyncio.wait_for(client.delete('/api/session'), 2)).status_code == 200
            elif ending == 'failure':
                provider.host.emit({'status': 'failed', 'error_code': 'synthetic-private-error'})
            response = await asyncio.wait_for(login, 2)
            assert response.status_code in (401, 503)
            assert 'synthetic-private' not in response.text
            assert provider.host.closed and session.captcha is None
            assert not session.view.authenticated
            assert not app.state.sessions.web_login_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'type': 'click', 'x': True, 'y': 1}, {'type': 'click', 'x': -1, 'y': 0},
    {'type': 'click', 'x': 520, 'y': 0}, {'type': 'click', 'x': 0, 'y': 900},
    {'type': 'click', 'x': '1', 'y': 0}, {'type': 'type', 'x': 1, 'y': 0},
    {'type': 'click', 'x': 1, 'y': 0, 'extra': 'synthetic-private'},
    {'type': 'click', 'x': 1}, [], None,
    [['type', 'click'], ['x', 1], ['y', 1]],
    {'type': 'click', 'x': float('nan'), 'y': 1},
    {'type': 'click', 'x': float('inf'), 'y': 1},
])
async def test_captcha_input_is_strict_before_forwarding(catalog_path, monkeypatch, body):
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    provider = Provider()
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            login_body = await async_login_body(client, 'a', 'p')
            login = asyncio.create_task(client.post('/api/auth/login', json=login_body))
            await asyncio.wait_for(provider.entered.wait(), 2)
            active = (await client.get('/api/auth/captcha')).json()
            path = '/api/auth/captcha/' + active['challenge_id']
            response = await client.post(path + '/input', content=json.dumps(body), headers={'Content-Type': 'application/json'})
            assert response.status_code == 422
            assert 'synthetic-private' not in response.text
            assert [command['type'] for command in provider.host.commands] == ['screenshot']
            await client.delete(path)
            await login


@pytest.mark.asyncio
async def test_wrong_unicode_challenge_is_safe_not_found(catalog_path, monkeypatch):
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    provider = Provider()
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            body = await async_login_body(client, 'a', 'p')
            login = asyncio.create_task(client.post('/api/auth/login', json=body))
            await asyncio.wait_for(provider.entered.wait(), 2)
            active = (await client.get('/api/auth/captcha')).json()
            assert (await client.post('/api/auth/captcha/无效/input', json={'type': 'click', 'x': 1, 'y': 1})).status_code == 404
            await client.delete('/api/auth/captcha/' + active['challenge_id'])
            await login


@pytest.mark.asyncio
async def test_controls_are_serial_and_a_timeout_closes_without_replaying(monkeypatch):
    import rumi_market.web_captcha as module
    channel, host = module.WebCaptchaChannel(), Host()
    monkeypatch.setattr(module, 'CONTROL_TIMEOUT', 0.03)
    running = asyncio.create_task(channel.run(host.stdout, host.send, host.close))
    while not channel.active:
        await asyncio.sleep(0)
    await asyncio.gather(channel.snapshot(), channel.input({'type': 'click', 'x': 1, 'y': 1}))
    assert [item['type'] for item in host.commands] == ['screenshot', 'click']
    async def drop(message):
        host.commands.append(message)
    channel._send = drop
    with pytest.raises(Exception) as error:
        await channel.input({'type': 'click', 'x': 2, 'y': 2})
    assert error.value.code == 'AUTH_CAPTCHA_TIMEOUT'
    with pytest.raises(Exception):
        await running
    assert host.closed and not channel.active
    assert [item['type'] for item in host.commands] == ['screenshot', 'click', 'click']


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['image', 'size', 'width', 'id', 'extra'])
async def test_bad_screenshot_is_never_exposed(corruption):
    from rumi_market.web_captcha import MAX_IMAGE_BASE64, WebCaptchaChannel
    channel, host = WebCaptchaChannel(), Host()
    async def bad_frame(command):
        message = {'kind': 'frame', 'id': command['id'], 'image': PNG, 'width': 520, 'height': 900}
        message.update({'image': 'not-a-png'} if corruption == 'image' else
                       {'image': 'A' * (MAX_IMAGE_BASE64 + 1)} if corruption == 'size' else
                       {'width': 521} if corruption == 'width' else
                       {'id': 'foreign'} if corruption == 'id' else {'private': 'synthetic-private'})
        host.emit(message)
    running = asyncio.create_task(channel.run(host.stdout, bad_frame, host.close))
    while not channel.active:
        await asyncio.sleep(0)
    with pytest.raises(Exception):
        await channel.snapshot()
    with pytest.raises(Exception):
        await running
    assert host.closed and not channel.active


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [
    {'kind': 'frame', 'id': 'unsolicited', 'image': PNG, 'width': 520, 'height': 900},
    {'kind': 'ready', 'width': 521, 'height': 900},
    {'kind': 'unknown', 'private': 'synthetic-private'},
    [],
])
async def test_invalid_host_messages_close_channel_without_public_values(message):
    from rumi_market.web_captcha import WebCaptchaChannel
    channel, host = WebCaptchaChannel(), Host()
    host.emit(message)
    with pytest.raises(Exception):
        await channel.run(host.stdout, host.send, host.close)
    assert host.closed and not channel.active


@pytest.mark.asyncio
async def test_web_login_rejects_second_cookie_without_blocking_first_controls(catalog_path, monkeypatch):
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    providers = []
    def factory():
        provider = Provider()
        providers.append(provider)
        return provider
    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as first:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as second:
                first_body = await async_login_body(first, 'a', 'p')
                second_body = await async_login_body(second, 'b', 'p')
                first_provider, second_provider = providers
                login = asyncio.create_task(first.post('/api/auth/login', json=first_body))
                await asyncio.wait_for(first_provider.entered.wait(), 2)
                busy = await asyncio.wait_for(second.post('/api/auth/login', json=second_body), 2)
                assert busy.status_code == 429
                assert busy.json()['error']['code'] == 'AUTH_LOGIN_BUSY'
                assert not second_provider.entered.is_set()
                active = (await first.get('/api/auth/captcha')).json()
                path = '/api/auth/captcha/' + active['challenge_id']
                assert (await first.post(path + '/input', json={'type': 'click', 'x': 1, 'y': 2})).status_code == 200
                await first.delete(path)
                await login
                assert not app.state.sessions.web_login_lock.locked()
                retry_body = await async_login_body(second, 'b', 'p')
                retry = asyncio.create_task(second.post('/api/auth/login', json=retry_body))
                await asyncio.wait_for(second_provider.entered.wait(), 2)
                active = (await second.get('/api/auth/captcha')).json()
                await second.delete('/api/auth/captcha/' + active['challenge_id'])
                await retry
                assert not app.state.sessions.web_login_lock.locked()


@pytest.mark.asyncio
async def test_local_mode_keeps_parallel_logins(catalog_path, monkeypatch):
    monkeypatch.delenv('RUMI_CAPTCHA_MODE', raising=False)
    providers, entered, release = [], asyncio.Event(), asyncio.Event()
    class LocalProvider(Provider):
        async def login(self, account, password):
            from rumi_market.web_captcha import current_channel
            assert current_channel.get() is None
            self.entered.set()
            if len([item for item in providers if item.entered.is_set()]) == 2:
                entered.set()
            await release.wait()
            return SessionView(status='role_selection', authenticated=True)
    def factory():
        provider = LocalProvider()
        providers.append(provider)
        return provider
    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as first:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as second:
                bodies = [await async_login_body(client, 'a', 'p') for client in (first, second)]
                tasks = [asyncio.create_task(client.post('/api/auth/login', json=body))
                         for client, body in zip((first, second), bodies)]
                await asyncio.wait_for(entered.wait(), 2)
                release.set()
                assert all(response.status_code == 200 for response in await asyncio.gather(*tasks))


@pytest.mark.asyncio
async def test_expired_relogin_can_poll_while_old_provider_is_closing(catalog_path, monkeypatch):
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    closing, release = asyncio.Event(), asyncio.Event()
    providers = []
    class ClosingProvider(Provider):
        async def close(self):
            closing.set()
            await release.wait()
            self.closed = True
    def factory():
        provider = Provider() if providers else ClosingProvider()
        providers.append(provider)
        return provider
    app = create_app(provider_factory=factory, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            body = await async_login_body(client, 'a', 'p')
            session = app.state.sessions.get(client.cookies['rumi_session'])
            session.view = SessionView(status='expired')
            assert (await client.get('/api/auth/captcha')).status_code == 401
            login = asyncio.create_task(client.post('/api/auth/login', json=body))
            await asyncio.wait_for(closing.wait(), 2)
            try:
                response = await asyncio.wait_for(client.get('/api/auth/captcha'), 2)
            finally:
                release.set()
            assert response.status_code == 200 and response.json() == {'status': 'waiting'}
            while len(providers) < 2:
                await asyncio.sleep(0)
            await asyncio.wait_for(providers[1].entered.wait(), 2)
            active = (await client.get('/api/auth/captcha')).json()
            await client.delete('/api/auth/captcha/' + active['challenge_id'])
            await login
            assert not session.login_in_progress and session.captcha is None
