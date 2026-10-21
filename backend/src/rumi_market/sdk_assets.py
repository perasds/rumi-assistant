"""Read only two pinned SDK native constants from the verified local APK."""

import hashlib
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path


APK_SHA256 = "3c03c267d20644e47f8f66cae701664f1d5ed14218e2c6fe4fa1dca23cc13712"
APK_MAX_BYTES = 256 * 1024 * 1024
NATIVE_MEMBER = "lib/arm64-v8a/libcustom_rules.so"
ELF_SIZE = 855416
ELF_SHA256 = "34e76bed3320d51dd1c970a847591d00f9a4162de7e0761830047e7572316244"
PUBLIC_PEM_SHA256 = "8be8b400076b33aa37dfd5bacee835d20b49b1e9a672da355459965606263275"
BUILTIN_RULE_SHA256 = "a5305283c0bd4d6604bc1af443b3d34e3a18c68489dbc37ed24ed27033b9f79d"


@dataclass(frozen=True, repr=False)
class SdkNativeAssets:
    public_pem: bytes
    builtin_signed_rule: bytes


def _bad() -> ValueError:
    return ValueError("Invalid SDK native assets")


def _load_segments(data: bytes) -> list[tuple[int, int, int, int]]:
    if data[:7] != b"\x7fELF\x02\x01\x01":
        raise _bad()
    header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    kind, machine, version, _, phoff, _, _, ehsize, phsize, phcount, _, _, _ = header
    if (kind != 3 or machine != 183 or version != 1 or ehsize != 64 or phsize != 56
            or not 1 <= phcount <= 64 or phoff < 64 or phoff + phsize * phcount > len(data)):
        raise _bad()
    segments = []
    for index in range(phcount):
        typ, flags, offset, address, _, filesz, memsz, alignment = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phsize)
        if typ != 1:
            continue
        if (filesz > memsz or offset + filesz > len(data) or address + memsz > 2**64
                or (alignment not in (0, 1) and (alignment & (alignment - 1)
                    or offset % alignment != address % alignment))):
            raise _bad()
        segments.append((address, filesz, offset, flags))
    return segments


def _constant(data: bytes, segments: list[tuple[int, int, int, int]],
              address: int, size: int, digest: str) -> bytes:
    matching = [(offset + address - start, flags) for start, filesz, offset, flags in segments
                if start <= address and address + size + 1 <= start + filesz]
    if len(matching) != 1:
        raise _bad()
    begin, flags = matching[0]
    value = data[begin:begin + size]
    if (not flags & 4 or flags & 2 or data[begin + size] != 0 or b"\x00" in value
            or hashlib.sha256(value).hexdigest() != digest):
        raise _bad()
    return value


def load_sdk_native_assets(apk_path: str | Path) -> SdkNativeAssets:
    """Load pinned materials into RAM; no extraction, execution or network I/O."""
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
                members = [item for item in archive.infolist() if item.filename == NATIVE_MEMBER]
                if len(members) != 1:
                    raise _bad()
                member = members[0]
                if (member.is_dir() or member.file_size != ELF_SIZE or member.flag_bits & 1
                        or not 0 < member.compress_size <= ELF_SIZE
                        or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise _bad()
                with archive.open(member, "r") as source:
                    native = source.read(ELF_SIZE + 1)
            if len(native) != ELF_SIZE or hashlib.sha256(native).hexdigest() != ELF_SHA256:
                raise _bad()
            segments = _load_segments(native)
            result = SdkNativeAssets(
                _constant(native, segments, 0xaa0c3, 445, PUBLIC_PEM_SHA256),
                _constant(native, segments, 0xaa9a3, 344, BUILTIN_RULE_SHA256))
    except Exception:
        pass
    if result is None:
        raise _bad()
    return result
