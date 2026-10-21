"""A single independent chat WebSocket, with no reconnect or claim replay."""
import asyncio
import logging
from urllib.parse import urlsplit

from ..errors import ApiError
from .redbag_codec import PAYLOAD_LIMIT


def validate_chat_url(url):
    try:
        parsed = urlsplit(url)
        valid = (type(url) is str and len(url) <= 2048 and parsed.scheme == "wss"
            and parsed.hostname is not None and parsed.hostname.endswith(".bilibiligame.net")
            and parsed.username is None and parsed.password is None and not parsed.fragment
            # 8917 is backed by the public hot71 VSC endpoint-shape evidence.
            and not parsed.query and parsed.port in (None, 443, 8917))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ApiError("REDBAG_UNAVAILABLE", "红包聊天地址尚未通过校验")


async def _connect(url, **kwargs):
    from websockets.asyncio.client import connect
    return await connect(url, **kwargs)


class RedbagChat:
    def __init__(self, codec, on_message, on_failure, *, timeout=15, connect_factory=None):
        self._codec, self._on_message, self._on_failure = codec, on_message, on_failure
        self._timeout, self._connect = timeout, connect_factory or _connect
        self._socket = self._reader = self._heartbeat = None
        self._pending = {}
        self._closed = False
        self._failure = None

    async def start(self, url):
        validate_chat_url(url)
        # A private logger suppresses library frame dumps containing auth material.
        logger = logging.Logger("rumi_market.redbag.websocket", level=logging.CRITICAL + 1)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        failed = False
        try:
            self._socket = await asyncio.wait_for(self._connect(url, proxy=None,
                compression=None, ping_interval=None, max_size=PAYLOAD_LIMIT, max_queue=16,
                open_timeout=self._timeout, close_timeout=2, logger=logger), self._timeout)
        except Exception:
            failed = True
        if failed:
            raise ApiError("REDBAG_UPSTREAM_FAILED", "红包聊天连接失败")
        self._reader = asyncio.create_task(self._read())

    def _fail(self, code):
        if self._failure is not None or self._closed:
            return
        self._failure = code
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ApiError(code, "红包聊天连接已中断"))
        self._on_failure(code)

    async def _read(self):
        failed = False
        try:
            while not self._closed:
                command, payload = self._codec.decode_ws(await self._socket.recv())
                future = self._pending.get(command)
                if future is not None and not future.done():
                    future.set_result(payload)
                else:
                    self._on_message(command, payload)
        except asyncio.CancelledError:
            return
        except Exception:
            failed = True
        if failed:
            self._fail("REDBAG_UPSTREAM_FAILED")

    async def send(self, command, payload=b""):
        if self._closed or self._failure is not None:
            raise ApiError("REDBAG_UPSTREAM_FAILED", "红包聊天连接已中断")
        failed = False
        try:
            await asyncio.wait_for(self._socket.send(self._codec.encode_ws(command, payload)), self._timeout)
        except Exception:
            failed = True
        if failed:
            self._fail("REDBAG_UPSTREAM_FAILED")
            raise ApiError("REDBAG_UPSTREAM_FAILED", "红包聊天发送失败")

    async def request(self, command, ack, payload):
        future = asyncio.get_running_loop().create_future()
        if ack in self._pending:
            raise ApiError("REDBAG_UNAVAILABLE", "红包聊天请求尚未完成")
        self._pending[ack] = future
        try:
            await self.send(command, payload)
            try:
                return await asyncio.wait_for(asyncio.shield(future), self._timeout)
            except TimeoutError:
                raise ApiError("REDBAG_UPSTREAM_FAILED", "红包聊天响应超时") from None
        finally:
            self._pending.pop(ack, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def heartbeat(self):
        async def run():
            try:
                while not self._closed:
                    # Local keepalive policy; not a claimed official timer default.
                    await asyncio.sleep(10)
                    await self.send(23924)
            except asyncio.CancelledError:
                return
            except Exception:
                self._fail("REDBAG_UPSTREAM_FAILED")
        self._heartbeat = asyncio.create_task(run())

    async def stop(self):
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        tasks = [t for t in (self._reader, self._heartbeat) if t is not None and t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await asyncio.wait_for(socket.close(), 2)
            except Exception:
                pass
