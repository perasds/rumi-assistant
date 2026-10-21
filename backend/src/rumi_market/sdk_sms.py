"""Fixed APK SMS signing material and the verified OTP-only RSA operation."""

import hashlib
import re
import zipfile
from pathlib import Path

from pydantic import SecretStr

from .sdk_app_config import DEX_MEMBER, DEX_SIZE, DEX_SHA256, _member
from .sdk_assets import APK_MAX_BYTES, APK_SHA256
from .sdk_primitives import sdk_encrypt_password


# ParamsSignInterceptors.setParams: classes7.dex string_id 4500. Values stay in RAM.
KEY_ASCII_OFFSET = 662954
KEY_SIZE = 216
KEY_SHA256 = "6fce52e85acd86a34ff10e4e219fa79ad16231241088bdc223a72e9c7f2029c7"


def load_sdk_sms_public_key(apk_path: str | Path) -> SecretStr:
    """Read the exact OTP public-key literal from the pinned APK without extraction."""
    result = None
    try:
        with Path(apk_path).open("rb") as stream:
            if not 0 < stream.seek(0, 2) <= APK_MAX_BYTES:
                raise ValueError()
            stream.seek(0)
            if hashlib.file_digest(stream, "sha256").hexdigest() != APK_SHA256:
                raise ValueError()
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                dex = _member(archive, DEX_MEMBER, DEX_SIZE, DEX_SHA256)
        key = dex[KEY_ASCII_OFFSET:KEY_ASCII_OFFSET + KEY_SIZE]
        if (len(key) != KEY_SIZE or dex[KEY_ASCII_OFFSET + KEY_SIZE] != 0
                or hashlib.sha256(key).hexdigest() != KEY_SHA256):
            raise ValueError()
        result = SecretStr(key.decode("ascii"))
    except Exception:
        pass
    if result is None:
        raise ValueError("Invalid SDK SMS signing material")
    return result


def sdk_sms_feign_sign(sign: str, public_key: str) -> str:
    """Rsa.encrypt(raw MD5 sign, fixed SPKI key), then identity params2Str.

    Native uses UTF-8, RSA/ECB/PKCS1Padding and raw Base64. Form encoding is
    performed exactly once by the HTTP layer, after this operation.
    """
    result = None
    try:
        if type(sign) is not str or re.fullmatch(r"[0-9a-f]{32}", sign) is None:
            raise ValueError()
        result = sdk_encrypt_password(sign, public_key, is_need_hash=False)
    except Exception:
        pass
    if result is None:
        raise ValueError("Invalid SDK SMS signature")
    return result
