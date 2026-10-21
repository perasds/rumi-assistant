"""Main-connection observer retaining only bounded chat authorization in RAM."""
import asyncio

from ..errors import ApiError
from .redbag_codec import RedbagProtoCodec


class RedbagState:
    def __init__(self, verified_version: bool):
        if type(verified_version) is not bool:
            raise ValueError("REDBAG_UNAVAILABLE")
        self.verified_version = verified_version
        self._auth = {}
        self._event = asyncio.Event()
        self.closed = False
        self.expired = False
        self._codec = RedbagProtoCodec()

    @property
    def has_auth(self):
        return self.verified_version and not self.closed and bool(self._auth)

    def observe(self, response):
        if self.closed or not self.verified_version:
            return
        if response.code in {-5, -8}:
            self.invalidate(expired=True)
            return
        if response.command not in {2919, 23939} or response.code:
            return
        valid = None
        try:
            params = dict(self._codec.decode(response.command, response.data).params)
            if (0 < len(params) <= 64 and sum(len(k) + len(v) for k, v in params.items()) <= 65536
                    and all(0 < len(k) <= 256 and len(v) <= 4096 for k, v in params.items())):
                valid = params
        except ValueError:
            pass
        if valid is None:
            self.invalidate()
            return
        self._auth.clear()
        self._auth.update(valid)
        self._event.set()

    __call__ = observe

    async def auth(self, *, timeout=15):
        if not self.closed and self.verified_version and not self.has_auth:
            try:
                await asyncio.wait_for(self._event.wait(), timeout)
            except TimeoutError:
                pass
        if not self.has_auth:
            raise ApiError("REDBAG_UNAVAILABLE", "红包聊天授权尚未就绪")
        return dict(self._auth)

    def clear_auth(self):
        self._auth.clear()
        self._event.clear()

    def invalidate(self, *, expired=False):
        self.verified_version = False
        self.expired = self.expired or expired
        self._auth.clear()
        self._event.set()

    def close(self):
        self.closed = True
        self.invalidate(expired=True)
