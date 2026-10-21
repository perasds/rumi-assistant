"""One game receive loop, explicit ACK correlation, and selected notifications."""

import asyncio
from collections import deque

from .envelope import FragmentAssembler, GameEnvelopeError, RequestSequencer, decode_response
from .proto import GameProtoError
from .transport import GameTransport, GameTransportError


class GameClientError(Exception):
    def __init__(self, code: str, *, response_code: int | None = None):
        super().__init__(code)
        self.code = code
        self.response_code = response_code


class GameClient:
    def __init__(self, transport: GameTransport, *, timeout: float = 15.0,
                 notification_commands: frozenset[int] = frozenset(), response_observer=None):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 60):
            raise GameClientError("GAME_CLIENT_INVALID_TIMEOUT")
        if (not isinstance(notification_commands, frozenset)
                or any(type(cmd) is not int or not 0 < cmd < 1 << 32
                       for cmd in notification_commands)):
            raise GameClientError("GAME_CLIENT_INVALID_COMMAND")
        if response_observer is not None and not callable(response_observer):
            raise GameClientError("GAME_CLIENT_INVALID_OBSERVER")
        self._transport = transport
        self._timeout = float(timeout)
        self._sequencer = RequestSequencer()
        self._fragments = FragmentAssembler()
        self._pending = {}
        self._backlog = {cmd: deque() for cmd in notification_commands}
        self._notification_commands = notification_commands
        self._received_notices = set()
        self._notice_waiters = {}
        self._seen_notices = deque(maxlen=128)
        self._observed_notices = deque(maxlen=128)
        self._receiver = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._failure = "GAME_CLIENT_CLOSED"
        self._response_observer = response_observer
        self._observer_failed = False

    @property
    def observer_failed(self) -> bool:
        return self._observer_failed

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def closed(self) -> bool:
        return self._closed

    def received_notification(self, command: int) -> bool:
        if type(command) is not int or command not in self._notification_commands:
            raise GameClientError("GAME_NOTIFICATION_UNREGISTERED")
        return command in self._received_notices

    def _require_connected(self) -> None:
        if self._closed:
            raise GameClientError(self._failure)
        if self._receiver is None:
            raise GameClientError("GAME_CLIENT_NOT_CONNECTED")

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._closed:
                raise GameClientError(self._failure)
            if self._receiver is not None:
                return
            code = None
            try:
                await self._transport.connect()
            except asyncio.CancelledError:
                await self.close()
                raise
            except GameTransportError as error:
                code = error.code
            except Exception:
                code = "GAME_CONNECTION_FAILED"
            if code is not None:
                await self._shutdown(code)
                raise GameClientError(code)
            self._receiver = asyncio.create_task(self._read_loop())

    async def request(self, command: int, response_command: int, data: bytes, *, before_send=None):
        self._require_connected()
        if type(response_command) is not int or not 0 < response_command < 1 << 32:
            raise GameClientError("GAME_CLIENT_INVALID_COMMAND")
        future = None
        key = None
        try:
            async with self._send_lock:
                self._require_connected()
                if before_send is not None:
                    allowed = False
                    try:
                        allowed = before_send() is True
                    except Exception:
                        pass
                    if not allowed:
                        raise GameClientError("GAME_REQUEST_NOT_SENT")
                envelope, code = None, None
                try:
                    envelope = self._sequencer.encode(command, data)
                except (GameEnvelopeError, GameProtoError):
                    code = "GAME_PROTOCOL_INVALID"
                if code is not None:
                    raise GameClientError(code)
                key = (response_command, envelope.sequence)
                if key in self._pending:
                    raise GameClientError("GAME_REQUEST_SEQUENCE_IN_USE")
                future = asyncio.get_running_loop().create_future()
                self._pending[key] = future
                try:
                    await self._transport.send(envelope.payload)
                except asyncio.CancelledError:
                    await self.close()
                    raise
                except GameTransportError as error:
                    code = error.code
                except Exception:
                    code = "GAME_CONNECTION_FAILED"
                if code is not None:
                    await self._shutdown(code)
            return await self._wait(future, "GAME_REQUEST_TIMEOUT")
        finally:
            if key is not None and self._pending.get(key) is future:
                self._pending.pop(key, None)
            if future is not None and not future.done():
                future.cancel()
            elif future is not None and not future.cancelled():
                future.exception()  # Cancellation during send may precede awaiting this future.
            # Request bytes and decoded values are never included in exceptions/logs.
            data, envelope = None, None

    async def _wait(self, future, timeout_code):
        timed_out = False
        try:
            return await asyncio.wait_for(asyncio.shield(future), self._timeout)
        except TimeoutError:
            timed_out = True
        if timed_out:
            raise GameClientError(timeout_code)

    async def wait_notification(self, command: int):
        self._require_connected()
        if type(command) is not int or command not in self._backlog:
            raise GameClientError("GAME_NOTIFICATION_UNREGISTERED")
        if self._backlog[command]:
            response = self._backlog[command].popleft()
            if response.code:
                raise GameClientError("GAME_REQUEST_REJECTED", response_code=response.code)
            return response
        if command in self._notice_waiters:
            raise GameClientError("GAME_NOTIFICATION_BUSY")
        future = asyncio.get_running_loop().create_future()
        self._notice_waiters[command] = future
        try:
            return await self._wait(future, "GAME_NOTIFICATION_TIMEOUT")
        finally:
            self._notice_waiters.pop(command, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    @staticmethod
    def _deliver(future, response):
        if future.done():
            return
        if response.code:
            future.set_exception(GameClientError("GAME_REQUEST_REJECTED",
                                                 response_code=response.code))
        else:
            future.set_result(response)

    async def _read_loop(self):
        code = None
        try:
            while not self._closed:
                response = self._fragments.feed(decode_response(await self._transport.receive()))
                if response is None:
                    continue
                if self._response_observer is not None:
                    if response.notify_sequence <= 0 or response.identity not in self._observed_notices:
                        if response.notify_sequence > 0:
                            self._observed_notices.append(response.identity)
                        try:
                            self._response_observer(response)
                        except Exception:
                            # Optional local archiving cannot interrupt the game connection.
                            self._observer_failed = True
                future = self._pending.get((response.command, response.sequence))
                if future is not None:
                    self._deliver(future, response)
                elif response.notify_sequence > 0 and response.command in self._backlog:
                    if response.identity in self._seen_notices:
                        continue
                    self._seen_notices.append(response.identity)
                    if response.code == 0:
                        self._received_notices.add(response.command)
                    waiter = self._notice_waiters.get(response.command)
                    if waiter is not None and not waiter.done():
                        self._deliver(waiter, response)
                    else:
                        # Local memory policy, not a server protocol limit.
                        if sum(map(len, self._backlog.values())) >= 64:
                            raise GameClientError("GAME_NOTIFICATION_OVERFLOW")
                        self._backlog[response.command].append(response)
        except asyncio.CancelledError:
            return
        except GameTransportError as error:
            code = error.code
        except (GameProtoError, GameEnvelopeError):
            code = "GAME_PROTOCOL_INVALID"
        except GameClientError as error:
            code = error.code
        except Exception:
            code = "GAME_CONNECTION_FAILED"
        if code is not None:
            await self._shutdown(code)

    async def _shutdown(self, code):
        if self._closed:
            return
        self._closed = True
        self._failure = code
        for future in (*self._pending.values(), *self._notice_waiters.values()):
            if not future.done():
                future.set_exception(GameClientError(code))
        self._pending.clear()
        self._notice_waiters.clear()
        self._backlog.clear()
        self._seen_notices.clear()
        self._observed_notices.clear()
        self._received_notices.clear()
        self._response_observer = None
        self._fragments.close()
        receiver, self._receiver = self._receiver, None
        if receiver is not None and receiver is not asyncio.current_task():
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass
        try:
            await self._transport.close()
        except Exception:
            pass

    async def close(self) -> None:
        await self._shutdown("GAME_CLIENT_CLOSED")
