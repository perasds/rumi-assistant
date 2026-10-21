"""Source-confirmed request envelopes and response fragment countdown handling."""

from dataclasses import dataclass, replace

from .proto import BUSINESS_MESSAGE_LIMIT, GameProtoCodec


class GameEnvelopeError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class RequestEnvelope:
    command: int
    sequence: int
    payload: bytes


class RequestSequencer:
    def __init__(self, *, last_sequence: int = 0):
        if type(last_sequence) is not int or not 0 <= last_sequence < 1 << 32:
            raise GameEnvelopeError("GAME_ENVELOPE_INVALID")
        self._sequence = last_sequence
        self._codec = GameProtoCodec()

    def encode(self, command: int, data: bytes) -> RequestEnvelope:
        if type(command) is not int or not 0 < command < 1 << 32 or not isinstance(data, bytes):
            raise GameEnvelopeError("GAME_ENVELOPE_INVALID")
        sequence = (self._sequence + 1) & 0xffffffff
        payload = self._codec.encode("SNKProto.ReqProto", {
            "ReqCmdId": command, "ReqCmdSeq": sequence, "ReqData": data})
        self._sequence = sequence
        return RequestEnvelope(command, sequence, payload)


@dataclass(frozen=True, repr=False)
class ResponseEnvelope:
    command: int
    sequence: int
    notify_sequence: int
    code: int
    remaining: int
    data: bytes

    def __post_init__(self):
        for value, bits in ((self.command, 32), (self.sequence, 32),
                            (self.notify_sequence, 64), (self.remaining, 32)):
            if type(value) is not int or not 0 <= value < 1 << bits:
                raise GameEnvelopeError("GAME_ENVELOPE_INVALID")
        if (type(self.code) is not int or not -(1 << 31) <= self.code < 1 << 31
                or not isinstance(self.data, bytes)):
            raise GameEnvelopeError("GAME_ENVELOPE_INVALID")

    @property
    def identity(self) -> tuple[int, int, int]:
        return self.command, self.notify_sequence, self.sequence


def decode_response(payload: bytes) -> ResponseEnvelope:
    message = GameProtoCodec().decode("SNKProto.RspProto", payload).message
    return ResponseEnvelope(message.RspCmdId, message.RspCmdSeq, message.RspNotifySeq,
                            message.RspCode, message.Coalescing, message.RspData)


class FragmentAssembler:
    def __init__(self, *, max_bytes: int = BUSINESS_MESSAGE_LIMIT):
        if type(max_bytes) is not int or not 0 < max_bytes <= BUSINESS_MESSAGE_LIMIT:
            raise GameEnvelopeError("GAME_FRAGMENT_INVALID_LIMIT")
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._identity = None
        self._expected = 0
        self._closed = False

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def _clear(self) -> None:
        self._identity = None
        self._expected = 0
        self._buffer.clear()

    def close(self) -> None:
        self._clear()
        self._closed = True

    def _append(self, data: bytes) -> None:
        if len(self._buffer) + len(data) > self._max_bytes:
            self._clear()
            raise GameEnvelopeError("GAME_FRAGMENT_TOO_LARGE")
        self._buffer.extend(data)

    def feed(self, response: ResponseEnvelope) -> ResponseEnvelope | None:
        if self._closed or not isinstance(response, ResponseEnvelope):
            self._clear()
            raise GameEnvelopeError("GAME_ENVELOPE_INVALID")
        if len(response.data) > self._max_bytes:
            self._clear()
            raise GameEnvelopeError("GAME_FRAGMENT_TOO_LARGE")
        if self._identity is not None and response.identity != self._identity:
            self._clear()  # The source retries the current envelope as new idle input.
        if self._identity is None:
            if response.remaining == 0:
                return response
            self._identity = response.identity
            self._expected = response.remaining - 1
            self._append(response.data)
            return None
        if response.remaining != self._expected:
            self._clear()
            return response if response.remaining == 0 else None
        self._append(response.data)
        if response.remaining > 0:
            self._expected = response.remaining - 1
            return None
        complete = replace(response, data=bytes(self._buffer))
        self._clear()
        return complete
