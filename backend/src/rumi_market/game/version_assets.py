"""Pinned APK version materials, recovered in RAM without executing target code.

HTPX semantics come from the fixed libil2cpp loader. Its operands are read from
the verified ELF, never embedded as signing material. Metadata body offsets
include an unknown eight-byte prefix; no metadata magic/version is invented.
Public boot windows are PlayerSettings path 1 and active level0 Root path 449.
"""

import hashlib
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from ..sdk_assets import APK_MAX_BYTES, APK_SHA256, _load_segments
from .version import VERSION_ORIGINS


NATIVE = "lib/arm64-v8a/libil2cpp.so"
METADATA = "assets/bin/Data/Managed/Metadata/global-metadata.dat"
PLAYER = "assets/bin/Data/globalgamemanagers"
SCENE = "assets/bin/Data/level0"
MEMBER_PINS = {
    NATIVE: (79070848, "623ad7c84bd79f30e20f03f57e84e2d48b90e00175700fb633bfdc7685369a6e"),
    METADATA: (11635420, "3d61b61c7230ad6783925126c1e9313f3091f5f55a2b15f5c7b4261d59bea23d"),
    PLAYER: (398308, "19f1931d9b29670c5af8dc397b0ab26f06acd411ac656ead1960a9c6a4034122"),
    SCENE: (85164, "25f5ef156e0fa74970393212c8a1165f1de5fb9e230d7532be45128a19559c3f"),
}
METADATA_BODY_SHA256 = "8fcc1c2322718972cd83b548fe0f71bb194f101bdfb9f20ecad0247b75ce19c6"
SECRET_LITERAL_INDEX = 1641
PLAYER_WINDOW = (5568, 756, "e020a3641cf69a05a890b418297a7869628363630119b664ca3a4c4ccc2a0fef")
ROOT_WINDOW = (57352, 240, "a1236928d001a640012dbc6e7b487848832ad62dd3d3884d671ebef667cdd53d")
_MASK = 0xffffffff
_PERMUTATION_ADDRESSES = (
    74626704, 74626768, 74626384, 74626720, 74626800, 74626752, 74626480, 74626368,
    74626656, 74626624, 74626592, 74626640, 74626496, 74626512, 74626336, 74626400,
)


@dataclass(frozen=True, repr=False)
class VersionAssets:
    gm_secret: SecretStr
    origins: tuple[str, ...]
    channel: str
    version: str
    build_version: str
    app_id: str
    profile_source: str


def _bad() -> ValueError:
    return ValueError("Invalid game version assets")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_va(data: bytes, segments: list, address: int, size: int) -> bytes:
    matches = [(offset + address - start, flags) for start, length, offset, flags in segments
               if start <= address and address + size <= start + length]
    if len(matches) != 1 or size <= 0:
        raise _bad()
    offset, flags = matches[0]
    if not flags & 4 or flags & 2:
        raise _bad()
    return data[offset:offset + size]


def _operands(native: bytes) -> tuple[tuple, tuple, tuple]:
    segments = _load_segments(native)

    def word(address):
        return struct.unpack("<I", _read_va(native, segments, address, 4))[0]

    def moved(first, second):
        a, b = word(first), word(second)
        if (a & 0xff800000 != 0x52800000 or b & 0xff800000 != 0x72800000
                or a & 31 != b & 31 or (a >> 21) & 3 > 1 or (b >> 21) & 3 > 1):
            raise _bad()
        value = ((a >> 5) & 65535) << (((a >> 21) & 3) * 16)
        shift = ((b >> 21) & 3) * 16
        return (value & ~(65535 << shift)) | (((b >> 5) & 65535) << shift)

    def added(first, second):
        total = 0
        for address in (first, second):
            op = word(address)
            if op & 0xff800000 != 0x11000000:
                raise _bad()
            total += ((op >> 10) & 4095) << (12 if op & (1 << 22) else 0)
        return total & _MASK

    permutation = b"".join(_read_va(native, segments, address, 16)
                           for address in _PERMUTATION_ADDRESSES)
    if permutation != bytes(range(256)):
        raise _bad()
    polynomial, step = struct.unpack("<II", _read_va(native, segments, 74638120, 8))
    crc = (polynomial, step, moved(0x47874f4, 0x47874f8))
    biases = tuple(added(*pair) for pair in ((0x4787984, 0x478798c), (0x4787958, 0x4787964),
                                            (0x4787968, 0x4787970), (0x4787960, 0x478796c)))
    salts = tuple(moved(*pair) for pair in ((0x4787944, 0x4787954), (0x4787980, 0x4787988),
                                           (0x478794c, 0x47879a4), (0x47879d8, 0x47879dc)))
    return crc, biases, salts


def _seed(length: int, polynomial: int, step: int, final: int) -> int:
    table = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (polynomial if value & 1 else 0)
        table.append(value & _MASK)
    state = _MASK
    for byte in struct.pack("<I", length):
        state = (((state >> 8) ^ table[(state ^ byte) & 255]) + step) & _MASK
    return ((~state) + final) & _MASK


def _stream(data: bytes, key: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 255
        state[i], state[j] = state[j], state[i]
    result = bytearray(data)
    i = j = 0
    for index, byte in enumerate(data):
        i = (i + 1) & 255
        j = (j + state[i]) & 255
        state[i], state[j] = state[j], state[i]
        value = state[(state[i] + state[j]) & 255]
        rotated = ((value >> 2) | (value << 6)) & 255
        result[index] = byte ^ ((rotated + 0x3a) & 255)
    return bytes(result)


def _recover_body(encoded: bytes, native: bytes) -> bytes:
    if (len(encoded) < 8 or encoded[:4] != b"HTPX"
            or struct.unpack_from("<I", encoded, 4)[0] != len(encoded) or len(encoded) >= 2**31):
        raise _bad()
    crc, biases, salts = _operands(native)
    body = encoded[8:]
    seed = _seed(len(body), *crc)
    key = struct.pack("<I", seed)
    if len(body) <= 128:
        return _stream(body, key)
    original = lambda position: struct.unpack_from("<I", encoded, position)[0]
    template = _stream(body[:128], struct.pack("<I", original(0x20) ^ seed))
    words = struct.unpack("<32I", template)
    mixed = original(0x20) ^ seed
    b0 = ((original(0x28) + biases[0]) & _MASK) ^ seed
    b1 = ((original(0x54) + biases[1]) & _MASK) ^ mixed
    b2 = ((words[11] + biases[2]) & _MASK) ^ seed
    b3 = ((words[15] + biases[3]) & _MASK) ^ mixed
    a0, a1, a5 = words[3] ^ salts[0], b3 ^ salts[1], b1 ^ salts[2]
    a7 = a1 ^ b2
    selectors = (a0, a1, words[7] ^ a0, words[8] ^ a7, b0 ^ a5,
                 a5, original(0x18) ^ salts[3], a7, original(0x20) ^ (b0 ^ a5))
    masks = (b0, b1, b2, b3)
    result = bytearray(body)
    for block in range((len(body) - 128) // 128):
        mode = selectors[block % 9] & 3
        start = 128 + block * 128
        for index, value in enumerate(words):
            current = struct.unpack_from("<I", body, start + index * 4)[0]
            if mode == 0:
                current ^= index ^ value ^ masks[value & 3]
            elif mode == 1:
                current ^= index ^ value ^ masks[selectors[index % 9] & 3]
            elif mode == 2:
                current ^= (32 - index) ^ value ^ selectors[index % 9]
            else:
                current ^= value ^ masks[value & 3]
            struct.pack_into("<I", result, start + index * 4, current & _MASK)
    tail = len(body) & ~127
    for index in range(len(body) & 127):
        value = selectors[masks[index & 3] % 9]
        mask = (value + value // 255) & _MASK
        result[tail + index] = (body[tail + index] ^ index ^ template[index] ^ mask) & 255
    result[:128] = _stream(body[:128], key)
    return bytes(result)


def _literal(body: bytes, index: int) -> bytes:
    if len(body) < 16 or type(index) is not int or index < 0:
        raise _bad()
    records, size, payload, payload_size = struct.unpack_from("<4I", body)
    if (size % 8 or index >= size // 8 or records < 8 or records + size > len(body) + 8
            or payload < 8 or payload + payload_size > len(body) + 8):
        raise _bad()
    length, offset = struct.unpack_from("<II", body, records - 8 + index * 8)
    if offset > payload_size or not 0 < length <= min(4096, payload_size - offset):
        raise _bad()
    return body[payload - 8 + offset:payload - 8 + offset + length]


def _window(data: bytes, pin: tuple) -> bytes:
    offset, size, digest = pin
    if offset < 0 or size <= 0 or offset + size > len(data):
        raise _bad()
    raw = data[offset:offset + size]
    if _digest(raw) != digest:
        raise _bad()
    return raw


def _string(raw: bytes, position: int) -> tuple[str, int]:
    if position < 0 or position + 4 > len(raw):
        raise _bad()
    size = struct.unpack_from("<I", raw, position)[0]
    start, end = position + 4, position + 4 + size
    if size > 1024 or end > len(raw):
        raise _bad()
    return raw[start:end].decode("utf-8"), (end + 3) & ~3


def _boot(player: bytes, scene: bytes) -> tuple[str, str, tuple[str, ...]]:
    version, _ = _string(_window(player, PLAYER_WINDOW), 608)
    root = _window(scene, ROOT_WINDOW)
    package, position = _string(root, 44)
    if (version != "1.5.3" or package != "RELEASE" or position != 56
            or struct.unpack_from("<III", root, 56) != (1, 0, 2)):
        raise _bad()
    build, position = _string(root, 68)
    gm_url, _ = _string(root, position)
    origins = tuple(part.strip() for part in gm_url.split(";") if part.strip())
    if not build or not build.isascii() or not build.isdecimal() or len(origins) != 3 or frozenset(origins) != VERSION_ORIGINS:
        raise _bad()
    return version, build, origins


def load_version_assets(apk_path: str | Path) -> VersionAssets:
    """Load only pinned local members into RAM; no extraction, execution or HTTP."""
    result = None
    try:
        with Path(apk_path).open("rb") as stream:
            if not 0 < stream.seek(0, 2) <= APK_MAX_BYTES:
                raise _bad()
            stream.seek(0)
            if hashlib.file_digest(stream, "sha256").hexdigest() != APK_SHA256:
                raise _bad()
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                data = {}
                for name, (size, digest) in MEMBER_PINS.items():
                    matches = [member for member in archive.infolist() if member.filename == name]
                    if len(matches) != 1:
                        raise _bad()
                    member = matches[0]
                    if (member.is_dir() or member.file_size != size or member.flag_bits & 1
                            or not 0 < member.compress_size <= size
                            or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                        raise _bad()
                    with archive.open(member) as source:
                        value = source.read(size + 1)
                    if len(value) != size or _digest(value) != digest:
                        raise _bad()
                    data[name] = value
            version, build, origins = _boot(data[PLAYER], data[SCENE])
            body = _recover_body(data[METADATA], data[NATIVE])
            if _digest(body) != METADATA_BODY_SHA256:
                raise _bad()
            secret = _literal(body, SECRET_LITERAL_INDEX).decode("utf-8")
            if not secret or "\x00" in secret:
                raise _bad()
            result = VersionAssets(SecretStr(secret), origins, "Android_CN_CDN", version, build,
                "1000069", "fixed APK PlayerSettings.bundleVersion / active level0 Root / metadata literal1641")
    except Exception:
        pass
    if result is None:
        raise _bad()
    return result
