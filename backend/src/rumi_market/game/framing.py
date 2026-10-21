"""Verified uint16 big-endian game framing, independent of protobuf semantics."""


def _bad() -> ValueError:
    return ValueError("Invalid game frame")


def encode_frame(payload: bytes) -> bytes:
    if not isinstance(payload, bytes) or not 0 < len(payload) <= 65535:
        raise _bad()
    return len(payload).to_bytes(2, "big") + payload


class FrameDecoder:
    def __init__(self):
        self._buffer = bytearray()
        self._closed = False

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def close(self) -> None:
        self._closed = True
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[bytes]:
        if self._closed or not isinstance(chunk, bytes):
            self.close()
            raise _bad()
        frames, offset = [], 0
        while offset < len(chunk):
            if len(self._buffer) < 2:
                take = min(2 - len(self._buffer), len(chunk) - offset)
                self._buffer.extend(chunk[offset:offset + take])
                offset += take
                if len(self._buffer) < 2:
                    break
            size = int.from_bytes(self._buffer[:2], "big")
            if size == 0:
                self.close()
                raise _bad()
            take = min(size + 2 - len(self._buffer), len(chunk) - offset)
            self._buffer.extend(chunk[offset:offset + take])
            offset += take
            if len(self._buffer) == size + 2:
                frames.append(bytes(self._buffer[2:]))
                self._buffer.clear()
        return frames
