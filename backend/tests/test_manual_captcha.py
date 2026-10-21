"""Private callback pipe adapter, without a real account or official challenge."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from rumi_market.errors import ApiError

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/manual_captcha.py'

def load_adapter():
    spec = importlib.util.spec_from_file_location('manual_captcha_adapter', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class Challenge:
    def build_h5_url(self, config):
        return 'https://game.bilibili.com/sdk/geetest/?challenge=synthetic-private-url'

class Input:
    def __init__(self): self.data = bytearray(); self.closed = False
    def write(self, data): self.data.extend(data)
    async def drain(self): pass
    def close(self): self.closed = True

class Process:
    def __init__(self, result=None):
        self.stdin = Input()
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.killed = False
        if result is not None:
            self.stdout.feed_data(json.dumps(result).encode() + b'\n')
            self.stdout.feed_eof()
    async def wait(self): self.returncode = 0; return 0
    def kill(self): self.killed = True

def arrange(monkeypatch, module, process):
    calls = []
    async def spawn(*args, **kwargs): calls.append((args, kwargs)); return process
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(module.shutil, 'which', lambda name: 'synthetic-node')
    monkeypatch.setattr(module, 'browser_executable', lambda: 'synthetic-browser')
    # Spawning is synthetic; backend unit tests need no installed Node package.
    monkeypatch.setattr(module, 'PLAYWRIGHT', SCRIPT)
    return calls

@pytest.mark.asyncio
async def test_manual_result_stays_in_pipes_and_never_in_arguments_environment_or_logs(monkeypatch, capsys):
    module = load_adapter(); process = Process({'status':'completed','result':'synthetic-private-result'})
    calls = arrange(monkeypatch, module, process)
    assert await module.manual_captcha(Challenge(), object()) == 'synthetic-private-result'
    args, options = calls[0]
    assert options['stdin'] is asyncio.subprocess.PIPE
    assert options['stdout'] is asyncio.subprocess.PIPE
    assert options['stderr'] is asyncio.subprocess.DEVNULL
    assert options['env']['DEBUG'] == options['env']['PWDEBUG'] == options['env']['NODE_OPTIONS'] == ''
    assert 'synthetic-private' not in str(args) + str(options)
    assert json.loads(process.stdin.data.splitlines()[0])['url'].endswith('synthetic-private-url')
    assert json.loads(process.stdin.data.splitlines()[0])['diagnostics'] is True
    assert capsys.readouterr().out == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('valid', [True, False])
async def test_local_timing_precedes_result_without_exposing_private_fields(monkeypatch, caplog, valid):
    from test_web_captcha import timing_message
    monkeypatch.delenv('RUMI_CAPTCHA_MODE', raising=False)
    module = load_adapter()
    process = Process()
    value = timing_message()
    if not valid:
        value['url'] = 'synthetic-private-url'
    for message in [value, {'status': 'completed', 'result': 'synthetic-private-result'}]:
        process.stdout.feed_data(json.dumps(message).encode() + b'\n')
    process.stdout.feed_eof()
    arrange(monkeypatch, module, process)
    with caplog.at_level('INFO', logger='uvicorn.error'):
        assert await module.manual_captcha(Challenge(), object()) == 'synthetic-private-result'
    assert len(caplog.records) == int(valid)
    assert 'synthetic-private' not in caplog.text


@pytest.mark.asyncio
async def test_local_does_not_accept_unbounded_timing_messages(monkeypatch, caplog):
    monkeypatch.delenv('RUMI_CAPTCHA_MODE', raising=False)
    module = load_adapter()
    process = Process()
    for message in [{'kind': 'timing', 'private': 'synthetic-private'}] * 2:
        process.stdout.feed_data(json.dumps(message).encode() + b'\n')
    process.stdout.feed_eof()
    arrange(monkeypatch, module, process)
    with caplog.at_level('INFO', logger='uvicorn.error'):
        with pytest.raises(ApiError) as error:
            await module.manual_captcha(Challenge(), object())
    assert error.value.code == 'AUTH_CAPTCHA_INVALID'
    assert caplog.records == []

@pytest.mark.asyncio
@pytest.mark.parametrize('result,code', [
    ({'status':'cancelled'}, 'AUTH_CAPTCHA_CANCELLED'),
    ({'status':'failed','error_code':'AUTH_CAPTCHA_PAGE_UNAVAILABLE'}, 'AUTH_CAPTCHA_PAGE_UNAVAILABLE'),
    ({'status':'failed','error_code':'synthetic-private-error'}, 'AUTH_CAPTCHA_UNAVAILABLE'),
    ({'status':'completed','result':None}, 'AUTH_CAPTCHA_INVALID'),
    ({'status':'completed','result':'x'*65537}, 'AUTH_CAPTCHA_INVALID'),
])
async def test_non_completion_returns_only_fixed_errors(monkeypatch, result, code):
    module=load_adapter(); process=Process(result); arrange(monkeypatch,module,process)
    with pytest.raises(ApiError) as error: await module.manual_captcha(Challenge(),object())
    assert error.value.code == code
    assert 'synthetic-private' not in str(error.value.body())
    assert error.value.__context__ is error.value.__cause__ is None

@pytest.mark.asyncio
async def test_cancellation_sends_control_and_closes_only_owned_host(monkeypatch):
    module=load_adapter(); process=Process(); arrange(monkeypatch,module,process)
    task=asyncio.create_task(module.manual_captcha(Challenge(),object()))
    await asyncio.sleep(0); await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert process.stdin.closed
    assert process.stdin.data.endswith(b'cancel\n')
    assert process.returncode == 0

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['blocked_drain','write_error'])
async def test_cleanup_closes_and_waits_even_when_control_pipe_fails(monkeypatch, failure):
    module=load_adapter(); process=Process()
    monkeypatch.setattr(module,'CONTROL_TIMEOUT',0.01,raising=False)
    if failure == 'blocked_drain':
        async def drain(): await asyncio.Event().wait()
        process.stdin.drain=drain
    else:
        def write(data): raise RuntimeError('synthetic-private-write-error')
        process.stdin.write=write
    assert await asyncio.wait_for(module._close_host(process),0.1)
    assert process.stdin.closed and process.returncode == 0

@pytest.mark.asyncio
async def test_callback_observation_keeps_known_field_types_without_values(monkeypatch, capsys):
    module=load_adapter()
    raw=json.dumps({'status':1,'captcha_type':1,'validate':'synthetic-private-proof',
                    'userid':'synthetic-private-user','unknown-private-key':'synthetic-private-value'})
    process=Process({'status':'completed','result':raw})
    arrange(monkeypatch,module,process)
    assert await module.manual_captcha(Challenge(),object()) == raw
    observation=module.manual_captcha.observation
    assert observation['callback_received']
    assert observation['field_types']['status'] == 'number'
    assert observation['field_types']['captcha_type'] == 'number'
    assert observation['field_types']['validate'] == 'string'
    assert 'synthetic-private' not in json.dumps(observation)
    assert 'unknown-private-key' not in json.dumps(observation)
    assert capsys.readouterr().out == ''


def test_explicit_chromium_path_is_validated_before_launch(monkeypatch, tmp_path):
    module = load_adapter()
    browser = tmp_path / 'synthetic-chromium'
    browser.write_bytes(b'not-executed')
    monkeypatch.setenv('RUMI_CHROMIUM_EXECUTABLE', str(browser))
    assert module.browser_executable() == str(browser.resolve())
    monkeypatch.setenv('RUMI_CHROMIUM_EXECUTABLE', str(tmp_path / 'missing'))
    with pytest.raises(ValueError):
        module.browser_executable()


@pytest.mark.asyncio
async def test_web_mode_uses_context_channel_without_exposing_private_result(monkeypatch):
    from rumi_market.web_captcha import WebCaptchaChannel, current_channel
    module = load_adapter()
    process = Process({'status': 'completed', 'result': 'synthetic-private-callback'})
    calls = arrange(monkeypatch, module, process)
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    monkeypatch.setattr(module, 'browser_executable', lambda: None)
    channel = WebCaptchaChannel()
    token = current_channel.set(channel)
    try:
        assert await module.manual_captcha(Challenge(), object()) == 'synthetic-private-callback'
    finally:
        current_channel.reset(token)
    request = json.loads(process.stdin.data.splitlines()[0])
    assert request['mode'] == 'web'
    assert 'executablePath' not in request
    assert calls[0][1]['limit'] >= 4 * 1024 * 1024
    assert not channel.active and process.stdin.closed and process.returncode == 0


@pytest.mark.asyncio
async def test_web_mode_requires_login_context_before_starting_process(monkeypatch):
    module = load_adapter()
    process = Process({'status': 'completed', 'result': 'synthetic-private-callback'})
    calls = arrange(monkeypatch, module, process)
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    with pytest.raises(ApiError) as error:
        await module.manual_captcha(Challenge(), object())
    assert error.value.code == 'AUTH_CAPTCHA_UNAVAILABLE'
    assert calls == []


@pytest.mark.asyncio
async def test_linux_forced_cleanup_targets_owned_process_group(monkeypatch):
    module = load_adapter()
    process = Process()
    process.pid = 43123
    killed = []
    def kill_group(pid, signal):
        killed.append((pid, signal))
        process.returncode = -9
    async def wait():
        if process.returncode is None:
            await asyncio.Event().wait()
        return process.returncode
    process.wait = wait
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='posix', killpg=kill_group))
    monkeypatch.setattr(module, 'signal', SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(module, 'CLOSE_TIMEOUT', 0.01)
    assert await module._close_host(process)
    assert killed == [(43123, 9)] and process.stdin.closed


@pytest.mark.asyncio
async def test_web_api_uses_normal_live_adapter_without_blocking_event_loop(monkeypatch, catalog_path, capsys):
    import base64
    import httpx
    from rumi_market.app import create_app
    import rumi_market.live_runtime as runtime
    from providers import ScriptedProvider
    from login_helpers import async_login_body
    module = load_adapter()
    process = Process()
    process.stdout.feed_data(b'{"kind":"ready","width":520,"height":900}\n')
    original_write = process.stdin.write
    png = base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic').decode()
    def write(data):
        original_write(data)
        if data == b'cancel\n':
            return
        command = json.loads(data)
        if command.get('type') == 'screenshot':
            message = {'kind': 'frame', 'id': command['id'], 'width': 520, 'height': 900, 'image': png}
            process.stdout.feed_data(json.dumps(message).encode() + b'\n')
        elif command.get('type') == 'click':
            process.stdout.feed_data(json.dumps({'kind': 'ack', 'id': command['id']}).encode() + b'\n')
            process.stdout.feed_data(b'{"status":"completed","result":"synthetic-private-proof"}\n')
    process.stdin.write = write
    arrange(monkeypatch, module, process)
    monkeypatch.setenv('RUMI_CAPTCHA_MODE', 'web')
    monkeypatch.setattr(runtime, '_load_manual_captcha', lambda: module.manual_captcha)
    class AdapterProvider(ScriptedProvider):
        async def login(self, account, password):
            assert await runtime.manual_captcha(Challenge(), object()) == 'synthetic-private-proof'
            return await super().login(account, password)
    app = create_app(provider_factory=AdapterProvider, catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            body = await async_login_body(client, 'a', 'p')
            login = asyncio.create_task(client.post('/api/auth/login', json=body))
            for _ in range(20):
                response = await asyncio.wait_for(client.get('/api/auth/captcha'), 2)
                if response.json()['status'] == 'active':
                    break
                await asyncio.sleep(0)
            active = response.json()
            assert active['status'] == 'active'
            result = await client.post('/api/auth/captcha/' + active['challenge_id'] + '/input',
                                       json={'type': 'click', 'x': 1, 'y': 2})
            assert result.json() == {'status': 'ok', 'frame': {'status': 'waiting'}}
            completed = await asyncio.wait_for(login, 2)
            assert completed.status_code == 200
            assert 'synthetic-private' not in response.text + result.text + completed.text
            assert process.stdin.closed and process.returncode == 0
            assert app.state.sessions.get(client.cookies['rumi_session']).captcha is None
            assert capsys.readouterr().out == ''
