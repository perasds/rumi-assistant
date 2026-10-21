"""Temporary manual H5 callback adapter. Private values use memory pipes only."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

from rumi_market.errors import ApiError
from rumi_market.web_captcha import MAX_HOST_MESSAGE, current_channel, log_timing_summary, web_mode

PROJECT = Path(__file__).resolve().parents[1]
HOST = PROJECT / 'scripts/manual_captcha_host.mjs'
PLAYWRIGHT = PROJECT / 'scripts/node_modules/playwright-core/index.mjs'
CONTROL_TIMEOUT = 2
CLOSE_TIMEOUT = 5
_MESSAGES = {
    'AUTH_CAPTCHA_UNAVAILABLE': '本机验证窗口暂不可用',
    'AUTH_CAPTCHA_PAGE_UNAVAILABLE': '官方验证页暂不可用',
    'AUTH_CAPTCHA_TIMEOUT': '人工验证已超时',
    'AUTH_CAPTCHA_INVALID': '验证码返回数据未通过校验',
    'AUTH_CAPTCHA_CANCELLED': '已取消人工验证',
}

def _callback_observation(raw: str) -> dict:
    result = {'callback_received': True, 'field_types': {}}
    parsed = None
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        pass
    if type(parsed) is dict:
        names = ('captcha', 'captcha_type', 'challenge', 'code', 'ctoken',
                 'seccode', 'status', 'token', 'userid', 'validate', 'data')
        types = {str:'string', int:'number', float:'number', bool:'boolean',
                 dict:'object', list:'array', type(None):'null'}
        result['field_types'] = {name:types.get(type(parsed[name]), 'unknown')
                                 for name in names if name in parsed}
    return result

def browser_executable() -> str | None:
    """Use an installed browser; this helper never installs or changes one."""
    explicit = os.environ.get('RUMI_CHROMIUM_EXECUTABLE')
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError('Browser unavailable')
        return str(path)
    root = os.environ.get('LOCALAPPDATA')
    if not root:
        return None
    paths = list((Path(root) / 'ms-playwright').glob('chromium-*/chrome-win64/chrome.exe'))
    paths = [path for path in paths if path.is_file() and path.parents[1].name[9:].isdigit()]
    return str(max(paths, key=lambda path: int(path.parents[1].name[9:]))) if paths else None

async def _close_host(process) -> bool:
    try:
        if process.returncode is None:
            try:
                process.stdin.write(b'cancel\n')
                await asyncio.wait_for(process.stdin.drain(), CONTROL_TIMEOUT)
            except Exception:
                pass
            finally:
                try:
                    process.stdin.close()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(process.wait(), CLOSE_TIMEOUT)
            except TimeoutError:
                if os.name == 'nt':
                    # Only this still-owned Node PID and its browser descendants.
                    tool = shutil.which('taskkill')
                    if not tool:
                        return False
                    killer = await asyncio.create_subprocess_exec(tool, '/PID', str(process.pid), '/T', '/F',
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    await asyncio.wait_for(killer.wait(), CLOSE_TIMEOUT)
                else:
                    # start_new_session below isolates only this host and its descendants.
                    os.killpg(process.pid, signal.SIGKILL)
                await asyncio.wait_for(process.wait(), CLOSE_TIMEOUT)
        return True
    except Exception:
        return False

async def manual_captcha(challenge, config) -> str:
    """Open a human-operated official page; return its opaque callback string."""
    process = None
    manual_captcha.observation = {'callback_received': False}
    result = None
    error_code = 'AUTH_CAPTCHA_UNAVAILABLE'
    cancelled = False
    channel = current_channel.get() if web_mode() else None
    url = line = payload = None
    try:
        if web_mode() and channel is None:
            raise ValueError()
        node = shutil.which('node')
        if not node or not HOST.is_file() or not PLAYWRIGHT.is_file():
            raise ValueError()
        url = challenge.build_h5_url(config)
        process = await asyncio.create_subprocess_exec(node, str(HOST), str(PLAYWRIGHT),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=MAX_HOST_MESSAGE if channel else 131072,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt',
            env={**os.environ, 'DEBUG':'', 'PWDEBUG':'', 'NODE_OPTIONS':''})
        request = {'url':url, 'diagnostics': True}
        executable = browser_executable()
        if executable is not None:
            request['executablePath'] = executable
        if channel is not None:
            request['mode'] = 'web'
        process.stdin.write((json.dumps(request) + '\n').encode('utf-8'))
        await asyncio.wait_for(process.stdin.drain(), CONTROL_TIMEOUT)
        error_code = 'AUTH_CAPTCHA_INVALID'
        if channel is not None:
            async def send(message):
                process.stdin.write((json.dumps(message) + '\n').encode('utf-8'))
                await asyncio.wait_for(process.stdin.drain(), CONTROL_TIMEOUT)
            raw = await asyncio.wait_for(channel.run(process.stdout, send, lambda: _close_host(process)), 310)
            payload = {'status': 'completed', 'result': raw}
        else:
            # Local mode keeps the original one-result private pipe protocol.
            timing_seen = False
            async with asyncio.timeout(310):
                while True:
                    line = await process.stdout.readline()
                    if len(line) > 131072:
                        raise ValueError()
                    payload = json.loads(line)
                    if type(payload) is dict and payload.get('kind') == 'timing':
                        if timing_seen:
                            raise ValueError()
                        timing_seen = True
                        log_timing_summary(payload)
                        continue
                    break
            await asyncio.wait_for(process.wait(), 5)
        if type(payload) is dict and payload.get('status') == 'completed':
            raw = payload.get('result')
            if type(raw) is str and len(raw.encode('utf-8')) <= 65536:
                result = raw
                manual_captcha.observation = _callback_observation(raw)
            else:
                error_code = 'AUTH_CAPTCHA_INVALID'
        elif type(payload) is dict and payload.get('status') == 'cancelled':
            error_code = 'AUTH_CAPTCHA_CANCELLED'
        elif type(payload) is dict and payload.get('error_code') in _MESSAGES:
            error_code = payload['error_code']
        else:
            error_code = 'AUTH_CAPTCHA_UNAVAILABLE'
    except asyncio.CancelledError:
        cancelled = True
    except TimeoutError:
        error_code = 'AUTH_CAPTCHA_TIMEOUT'
    except ApiError as error:
        if error.code in _MESSAGES:
            error_code = error.code
    except Exception:
        pass
    finally:
        url = line = payload = request = None
        if process is not None:
            closing = asyncio.create_task(_close_host(process))
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            if not closing.result():
                result = None
                error_code = 'AUTH_CAPTCHA_UNAVAILABLE'
    if cancelled:
        raise asyncio.CancelledError()
    if result is None:
        raise ApiError(error_code, _MESSAGES[error_code], 400 if error_code == 'AUTH_CAPTCHA_CANCELLED' else 503)
    return result
