"""Session-owned human browser controls; opaque login data stays in private pipes."""
import asyncio
import base64
from contextvars import ContextVar
import json
import logging
import math
import os
import secrets

from .errors import ApiError, provider_error

WIDTH, HEIGHT = 520, 900
MAX_IMAGE_BASE64 = 4 * 1024 * 1024
MAX_HOST_MESSAGE = MAX_IMAGE_BASE64 + 1024
CONTROL_TIMEOUT = 3
current_channel: ContextVar['WebCaptchaChannel | None'] = ContextVar('web_captcha', default=None)
TIMING_RESOURCE_TYPES = {'document', 'script', 'stylesheet', 'image', 'font', 'xhr', 'fetch', 'other'}
MAX_TIMING_COUNT = 100000
MAX_TIMING_DURATION_MS = 600000


def log_timing_summary(message) -> bool:
    """Log only one caller-bounded, fixed-schema numeric summary; never raw host data."""
    def counter(value, *, requests=False, image=False):
        count_keys = {'completed', 'failed', 'pending'} if requests else {'count'}
        expected = count_keys | {'total_ms', 'max_ms'}
        if image:
            expected |= {'total_bytes', 'max_bytes'}
        if type(value) is not dict or set(value) != expected:
            return False
        if any(type(value[key]) is not int or not 0 <= value[key] <= MAX_TIMING_COUNT for key in count_keys):
            return False
        count = sum(value[key] for key in count_keys)
        if count > MAX_TIMING_COUNT:
            return False
        if any(type(value[key]) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0
               for key in ('total_ms', 'max_ms')):
            return False
        if not value['max_ms'] <= min(value['total_ms'], MAX_TIMING_DURATION_MS):
            return False
        if value['total_ms'] > count * MAX_TIMING_DURATION_MS:
            return False
        if image:
            if any(type(value[key]) is not int or value[key] < 0 for key in ('total_bytes', 'max_bytes')):
                return False
            limit = MAX_IMAGE_BASE64 // 4 * 3
            if not value['max_bytes'] <= min(value['total_bytes'], limit) or value['total_bytes'] > count * limit:
                return False
        return True

    try:
        if type(message) is not dict or set(message) != {'kind', 'requests', 'screenshot', 'input'} or message['kind'] != 'timing':
            return False
        requests = message['requests']
        if type(requests) is not dict or set(requests) != TIMING_RESOURCE_TYPES:
            return False
        if not all(counter(value, requests=True) for value in requests.values()):
            return False
        if not counter(message['screenshot'], image=True) or not counter(message['input']):
            return False
        encoded = json.dumps(message, separators=(',', ':'), allow_nan=False)
        if len(encoded) > 4096:
            return False
    except (ValueError, TypeError, OverflowError):
        return False
    try:
        logging.getLogger('uvicorn.error').info('captcha_timing %s', encoded)
    except Exception:
        # Diagnostics are optional and cannot change an authentication result.
        pass
    return True


def web_mode() -> bool:
    return os.environ.get('RUMI_CAPTCHA_MODE', 'local') == 'web'


def _error(code='AUTH_CAPTCHA_INVALID') -> ApiError:
    return provider_error(ApiError(code, ''))


def not_found() -> ApiError:
    return ApiError('CAPTCHA_NOT_FOUND', '人工验证不存在或已结束', 404)


async def read_input(request) -> dict:
    data = bytearray()
    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError()
        return value
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > 1024:
                raise ValueError()
            data.extend(chunk)
        body = json.loads(data.decode('utf-8'), object_pairs_hook=unique_object)
        if type(body) is not dict or set(body) != {'type', 'x', 'y'} or body['type'] not in {'click', 'pointerdown', 'pointermove', 'pointerup'}:
            raise ValueError()
        for key, bound in [('x', WIDTH), ('y', HEIGHT)]:
            value = body[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < bound:
                raise ValueError()
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ApiError('INVALID_INPUT', '输入格式不合法', 422) from None
    finally:
        data.clear()


class WebCaptchaChannel:
    def __init__(self):
        self.challenge_id = secrets.token_urlsafe(24)
        self.active = False
        self._ended = False
        self._cancelled = asyncio.Event()
        self._operations = asyncio.Lock()
        self._pending = None
        self._send = self._close = self._closing = None
        self._failure = 'AUTH_CAPTCHA_CANCELLED'

    async def run(self, stdout, send, close) -> str:
        if self._ended or self._send is not None:
            raise _error('AUTH_CAPTCHA_CANCELLED')
        self._send, self._close = send, close
        reader = asyncio.create_task(self._read(stdout))
        cancel = asyncio.create_task(self._cancelled.wait())
        try:
            done, _ = await asyncio.wait([reader, cancel], return_when=asyncio.FIRST_COMPLETED)
            if cancel in done:
                raise _error(self._failure)
            return await reader
        finally:
            self.active = False
            self._ended = True
            self._fail_pending()
            for task in (reader, cancel):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, cancel, return_exceptions=True)
            await self.close()

    async def _read(self, stdout) -> str:
        timing_seen = False
        while True:
            line = await stdout.readline()
            if not line or len(line) > MAX_HOST_MESSAGE:
                raise _error()
            message = json.loads(line)
            if type(message) is not dict:
                raise _error()
            kind = message.get('kind')
            if kind == 'timing':
                if timing_seen:
                    raise _error()
                timing_seen = True
                log_timing_summary(message)
            elif kind == 'ready' and not self.active and set(message) == {'kind', 'width', 'height'}:
                if type(message['width']) is not int or type(message['height']) is not int or (message['width'], message['height']) != (WIDTH, HEIGHT):
                    raise _error()
                self.active = True
            elif kind in {'frame', 'ack'}:
                if not self.active or self._pending is None:
                    raise _error()
                command_id, expected, future = self._pending
                if message.get('id') != command_id or kind != expected or future.done():
                    raise _error()
                if kind == 'frame':
                    image = message.get('image')
                    if set(message) != {'kind', 'id', 'image', 'width', 'height'} or (message['width'], message['height']) != (WIDTH, HEIGHT) or type(image) is not str or len(image) > MAX_IMAGE_BASE64:
                        raise _error()
                    raw = base64.b64decode(image, validate=True)
                    if not raw.startswith(b'\x89PNG\r\n\x1a\n') or base64.b64encode(raw).decode('ascii') != image:
                        raise _error()
                elif set(message) != {'kind', 'id'}:
                    raise _error()
                future.set_result(message)
            elif message.get('status') == 'completed' and set(message) == {'status', 'result'}:
                result = message['result']
                if type(result) is not str or len(result.encode('utf-8')) > 65536:
                    raise _error()
                return result
            elif message.get('status') == 'cancelled':
                raise _error('AUTH_CAPTCHA_CANCELLED')
            elif message.get('status') == 'failed':
                code = message.get('error_code')
                if code not in {'AUTH_CAPTCHA_UNAVAILABLE', 'AUTH_CAPTCHA_PAGE_UNAVAILABLE', 'AUTH_CAPTCHA_TIMEOUT', 'AUTH_CAPTCHA_INVALID'}:
                    code = 'AUTH_CAPTCHA_UNAVAILABLE'
                raise _error(code)
            else:
                raise _error()

    def _fail_pending(self):
        if self._pending is not None and not self._pending[2].done():
            self._pending[2].set_exception(not_found())

    async def _command(self, body, expected):
        async with self._operations:
            if not self.active or self._ended:
                raise not_found()
            command_id = secrets.token_urlsafe(12)
            future = asyncio.get_running_loop().create_future()
            self._pending = (command_id, expected, future)
            try:
                async with asyncio.timeout(CONTROL_TIMEOUT):
                    await self._send({'id': command_id, **body})
                    return await future
            except asyncio.CancelledError:
                self._failure = 'AUTH_CAPTCHA_UNAVAILABLE'
                await self.close()
                raise
            except ApiError:
                raise
            except Exception:
                self._failure = 'AUTH_CAPTCHA_TIMEOUT'
                await self.close()
                raise _error(self._failure) from None
            finally:
                # Retrieve any failure set by close; a failed click is never replayed.
                if future.done() and not future.cancelled():
                    future.exception()
                else:
                    future.cancel()
                self._pending = None

    async def snapshot(self):
        frame = await self._command({'type': 'screenshot'}, 'frame')
        return {'status': 'active', 'challenge_id': self.challenge_id,
                'width': WIDTH, 'height': HEIGHT, 'image': 'data:image/png;base64,' + frame['image']}

    async def input(self, body):
        await self._command(body, 'ack')

    async def close(self):
        self.active = False
        self._ended = True
        self._cancelled.set()
        self._fail_pending()
        if self._close is not None:
            if self._closing is None:
                self._closing = asyncio.create_task(self._close())
            await asyncio.shield(self._closing)
