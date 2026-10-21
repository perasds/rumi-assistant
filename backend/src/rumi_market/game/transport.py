"""Unauthenticated TLS bytes for the verified gateway; no game message semantics.

The protocol owner serializes receive calls and correlates frames above this layer.
"""

import asyncio
import ssl

from .framing import encode_frame


GAME_HOST = "prod-cn-ob-gate-lumigo.bilibiligame.net"
GAME_PORT = 16880


class GameTransportError(Exception):
    """Only fixed transport classifications cross this boundary."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GameTransport:
    def __init__(self, *, timeout: float = 15.0, keep_idle: bool = False):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 60):
            raise GameTransportError("GAME_TRANSPORT_INVALID_TIMEOUT")
        if type(keep_idle) is not bool:
            raise GameTransportError("GAME_TRANSPORT_INVALID_IDLE_MODE")
        self._timeout = float(timeout)
        self._keep_idle = keep_idle
        self._reader = self._writer = None
        self._closed = False
        self._connect_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_connected(self):
        if self._closed:
            raise GameTransportError("GAME_TRANSPORT_CLOSED")
        if self._reader is None or self._writer is None:
            raise GameTransportError("GAME_TRANSPORT_NOT_CONNECTED")
        return self._reader, self._writer

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._closed:
                raise GameTransportError("GAME_TRANSPORT_CLOSED")
            if self._writer is not None:
                return
            code = None
            try:
                context = ssl.create_default_context()
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(GAME_HOST, GAME_PORT, ssl=context,
                                            server_hostname=GAME_HOST),
                    timeout=self._timeout)
            except asyncio.CancelledError:
                await self.close()
                raise
            except TimeoutError:
                code = "GAME_TRANSPORT_TIMEOUT"
            except Exception:
                code = "GAME_TRANSPORT_UNAVAILABLE"
            if code is not None:
                await self.close()
                # Raise outside the except block so private upstream context
                # (including incomplete plaintext frames) is not retained.
                raise GameTransportError(code)
            self._reader, self._writer = reader, writer
            if self._closed:
                await self.close()
                raise GameTransportError("GAME_TRANSPORT_CLOSED")

    async def send(self, payload: bytes) -> None:
        _, writer = self._require_connected()
        code = None
        try:
            frame = encode_frame(payload)
        except ValueError:
            code = "GAME_FRAME_INVALID"
        if code is not None:
            payload = None
            await self.close()
            raise GameTransportError(code)
        try:
            writer.write(frame)
            await asyncio.wait_for(writer.drain(), timeout=self._timeout)
        except asyncio.CancelledError:
            payload, frame = None, b""
            await self.close()
            raise
        except TimeoutError:
            code = "GAME_TRANSPORT_TIMEOUT"
        except Exception:
            code = "GAME_TRANSPORT_UNAVAILABLE"
        payload, frame = None, b""
        if code is not None:
            await self.close()
            raise GameTransportError(code)
        if self._closed:
            raise GameTransportError("GAME_TRANSPORT_CLOSED")

    @staticmethod
    async def _read_frame(reader, first_byte: bytes | None = None) -> bytes | None:
        if first_byte is None:
            header = await reader.readexactly(2)
        else:
            if not isinstance(first_byte, bytes) or len(first_byte) != 1:
                return None
            header = first_byte + await reader.readexactly(1)
        if not isinstance(header, bytes) or len(header) != 2:
            return None
        size = int.from_bytes(header, "big")
        if size == 0:
            return None
        payload = await reader.readexactly(size)
        return payload if isinstance(payload, bytes) and len(payload) == size else None

    async def receive(self) -> bytes:
        reader, _ = self._require_connected()
        code, payload = None, None
        try:
            # Idle connections need no heartbeat. Once a frame starts, its
            # remaining header and body share the normal bounded read budget.
            first_byte = await reader.readexactly(1) if self._keep_idle else None
            payload = await asyncio.wait_for(self._read_frame(reader, first_byte), timeout=self._timeout)
        except asyncio.CancelledError:
            await self.close()
            raise
        except TimeoutError:
            code = "GAME_TRANSPORT_TIMEOUT"
        except asyncio.IncompleteReadError:
            code = "GAME_TRANSPORT_CLOSED"
        except Exception:
            code = "GAME_TRANSPORT_UNAVAILABLE"
        if payload is None or code is not None:
            payload = None
            await self.close()
            raise GameTransportError(code or "GAME_FRAME_INVALID")
        if self._closed:
            payload = None
            raise GameTransportError("GAME_TRANSPORT_CLOSED")
        return payload

    @staticmethod
    def _abort(writer) -> None:
        try:
            writer.transport.abort()
        except Exception:
            pass

    async def close(self) -> None:
        self._closed = True
        writer, self._writer = self._writer, None
        reader, self._reader = self._reader, None
        if self._keep_idle and isinstance(reader, asyncio.StreamReader):
            reader.feed_eof()  # Wake an idle receive before waiting for TLS close.
        if writer is None:
            return
        failed = False
        try:
            writer.close()
        except Exception:
            failed = True
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=self._timeout)
        except asyncio.CancelledError:
            self._abort(writer)
            raise
        except Exception:
            failed = True
        if failed:
            self._abort(writer)
