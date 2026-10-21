"""Load application SDK configuration from one verified APK, without extraction."""

import hashlib
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from .sdk_assets import APK_MAX_BYTES, APK_SHA256


ARSC_MEMBER = "resources.arsc"
ARSC_SIZE = 1022340
ARSC_SHA256 = "430175ff456d729c15b86640bc7a4a47eddce7f687117587b15bda753c66e397"
DEX_MEMBER = "classes7.dex"
DEX_SIZE = 2217836
DEX_SHA256 = "526775d838e8bd2105632d8d6fca4dca6cd9c3523e5008bde9aa14ea933b1383"


@dataclass(frozen=True, repr=False)
class SdkAppConfig:
    merchant_id: str
    app_id: str
    server_id: str
    sdk_ver: str
    app_key: SecretStr
    app_ver: str = "1.5.3"
    version_code: str = "42"


def _bad() -> ValueError:
    return ValueError("Invalid SDK app config")


def _member(archive: zipfile.ZipFile, name: str, size: int, digest: str) -> bytes:
    members = [item for item in archive.infolist() if item.filename == name]
    if len(members) != 1:
        raise _bad()
    member = members[0]
    if (member.is_dir() or member.file_size != size or member.flag_bits & 1
            or not 0 < member.compress_size <= size
            or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
        raise _bad()
    with archive.open(member, "r") as source:
        data = source.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise _bad()
    return data


def _ascii(data: bytes, offset: int, size: int) -> str:
    if not 0 <= offset < offset + size < len(data):
        raise _bad()
    value = data[offset:offset + size]
    if data[offset + size] != 0 or any(not 0 < byte < 128 for byte in value):
        raise _bad()
    return value.decode("ascii")


def _sdk_version(dex: bytes) -> str:
    if (dex[:8] != b"dex\n038\x00"
            or struct.unpack_from("<III", dex, 0x20) != (len(dex), 0x70, 0x12345678)):
        raise _bad()
    count, table = struct.unpack_from("<II", dex, 0x38)
    if count <= 502 or table < 0x70 or table + count * 4 > len(dex):
        raise _bad()
    offset = struct.unpack_from("<I", dex, table + 502 * 4)[0]
    if not table + count * 4 <= offset < len(dex):
        raise _bad()
    length = 0
    for index in range(5):
        if offset >= len(dex):
            raise _bad()
        byte = dex[offset]
        offset += 1
        if index == 4 and byte > 0x0f:
            raise _bad()
        length |= (byte & 0x7f) << (index * 7)
        if not byte & 0x80:
            break
    else:
        raise _bad()
    if length != 6:
        raise _bad()
    return _ascii(dex, offset, 6)


def load_sdk_app_config(apk_path: str | Path) -> SdkAppConfig:
    """Read pinned application fields into RAM; never read account or device data."""
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
                arsc = _member(archive, ARSC_MEMBER, ARSC_SIZE, ARSC_SHA256)
                dex = _member(archive, DEX_MEMBER, DEX_SIZE, DEX_SHA256)
            # Default UTF-8 string payload offsets in this exact resources.arsc.
            # ONE_MERCHANTID/ONE_APPID/ONE_SERVERID/ONE_APPKEY are android:value refs.
            result = SdkAppConfig(
                merchant_id=_ascii(arsc, 0x21811, 1),
                app_id=_ascii(arsc, 0x21858, 5),
                server_id=_ascii(arsc, 0x21850, 5),
                sdk_ver=_sdk_version(dex),
                app_key=SecretStr(_ascii(arsc, 0x217ee, 32)),
            )
    except Exception:
        pass
    if result is None:
        raise _bad()
    return result
