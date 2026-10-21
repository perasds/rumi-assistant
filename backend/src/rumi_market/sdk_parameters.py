"""Normal, private SDK request context from the verified APK and a new identity."""

import hashlib
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import SecretStr

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs7

from .sdk_app_config import SdkAppConfig, load_sdk_app_config
from .sdk_assets import APK_MAX_BYTES, APK_SHA256, load_sdk_native_assets
from .sdk_device import SdkDeviceIdentity
from .sdk_rules import recover_signed_sdk_rule, sdk_dynamic_headers
from .sdk_sms import load_sdk_sms_public_key, sdk_sms_feign_sign


CERT_MEMBER = "META-INF/BILIGAME.RSA"
CERT_SIZE = 1105
CERT_SHA256 = "cd19dcb2b76100e46ecbd18d6a0bb0d30a379d2c5f58f43ff0845fd930e16fe6"
CERT_DER_SIZE = 715
CERT_DER_SHA256 = "8d9975b308c79d52b1703900afe35f39afd5d40a4d01c8d29064288078a7af7d"
CHANNEL_MEMBER = "assets/distributor.txt"
CHANNEL_SIZE = 1
CHANNEL_SHA256 = "6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b"
SDK_PATHS = ("/api/external/issue/cipher/v3", "/api/external/login/v3", "/api/external/config/v3",
             "/api/client/start_captcha", "/api/external/user.token.oauth.login/v3",
             "/api/external/token.renewal/v3", "/api/external/otp/send/v3",
             "/api/external/login/otp/v3")


def _bad() -> ValueError:
    return ValueError("Invalid SDK request context")


def _member(archive: zipfile.ZipFile, name: str, size: int, digest: str) -> bytes:
    members = [item for item in archive.infolist() if item.filename == name]
    if len(members) != 1:
        raise _bad()
    member = members[0]
    if (member.is_dir() or member.file_size != size or member.flag_bits & 1
            or not 0 < member.compress_size <= size + 64
            or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
        raise _bad()
    with archive.open(member) as source:
        data = source.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise _bad()
    return data


def _apk_profile(apk_path: str | Path) -> tuple[str, str]:
    with Path(apk_path).open("rb") as source:
        if not 0 < source.seek(0, 2) <= APK_MAX_BYTES:
            raise _bad()
        source.seek(0)
        if hashlib.file_digest(source, "sha256").hexdigest() != APK_SHA256:
            raise _bad()
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            certificate = _member(archive, CERT_MEMBER, CERT_SIZE, CERT_SHA256)
            channel = _member(archive, CHANNEL_MEMBER, CHANNEL_SIZE, CHANNEL_SHA256)
    certificates = pkcs7.load_der_pkcs7_certificates(certificate)
    if len(certificates) != 1 or channel != b"1":
        raise _bad()
    der = certificates[0].public_bytes(serialization.Encoding.DER)
    if len(der) != CERT_DER_SIZE or hashlib.sha256(der).hexdigest() != CERT_DER_SHA256:
        raise _bad()
    # ApkSignatureUtil: first signature DER -> MD5 -> lower, zero-padded hex.
    return hashlib.md5(der).hexdigest(), channel.decode("ascii")


@dataclass(frozen=True, repr=False)
class SdkRequestContext:
    app_config: SdkAppConfig
    identity: SdkDeviceIdentity
    encoded_rule: bytes
    apk_sign: str
    channel_id: str
    public_pem: bytes
    rule_version: int = 0
    sms_public_key: SecretStr | None = None

    @classmethod
    def from_apk(cls, apk_path: str | Path) -> "SdkRequestContext":
        result = None
        try:
            apk_sign, channel = _apk_profile(apk_path)
            config = load_sdk_app_config(apk_path)
            native = load_sdk_native_assets(apk_path)
            rule = recover_signed_sdk_rule(native.builtin_signed_rule, native.public_pem)
            sms_key = load_sdk_sms_public_key(apk_path)
            result = cls(config, SdkDeviceIdentity.create(), rule, apk_sign, channel, native.public_pem,
                         sms_public_key=sms_key)
        except Exception:
            pass
        if result is None:
            raise _bad()
        return result

    def with_config(self, response: Mapping) -> "SdkRequestContext":
        configured = None
        try:
            if not isinstance(response, Mapping):
                raise _bad()
            version = response.get("crule_ver")
            signed = response.get("crule")
            if (not isinstance(version, str) or not re.fullmatch(r"[+-]?[0-9]{1,10}", version)
                    or not isinstance(signed, str) or not signed):
                raise _bad()
            parsed = int(version)
            if not 1 <= parsed <= 2147483647:
                raise _bad()
            rule = recover_signed_sdk_rule(signed.encode("ascii"), self.public_pem)
            configured = replace(self, encoded_rule=rule, rule_version=parsed)
        except Exception:
            pass
        if configured is None:
            raise _bad()
        return configured

    def common_parameters(self, path: str) -> dict[str, str]:
        # Normal after-consent profile; CONFIG re-adds the same BD identity.
        # Token endpoints are not filterByPath privacy routes. Without an Attri
        # cache, getCodeListByPath returns null: the same UpDataModel profile and
        # configured dynamic rule apply. RN supplies uid/access_key, no version.
        if not isinstance(path, str) or path not in SDK_PATHS:
            raise _bad()
        config = self.app_config
        return {"app_id": config.app_id, "game_id": config.app_id,
                "merchant_id": config.merchant_id, "server_id": config.server_id,
                "sdk_ver": config.sdk_ver, "platform": "3", "platform_type": "3",
                "sdk_type": "1", "sdk_log_type": "1", "version": "1",
                "channel_id": self.channel_id, "current_env": "0",
                "udid": self.identity.udid, "bd_id": self.identity.bd_id,
                "apk_sign": self.apk_sign, "app_ver": config.app_ver,
                "version_code": config.version_code}

    def extra_headers(self, timestamp: str, sign: str, params: Mapping[str, str]) -> dict[str, str]:
        headers = None
        try:
            if (not isinstance(timestamp, str) or not timestamp.isascii() or not timestamp.isdigit()
                    or not isinstance(sign, str) or not re.fullmatch(r"[0-9a-f]{32}", sign)
                    or not isinstance(params, Mapping) or params.get("timestamp") != timestamp
                    or params.get("sign") != sign or params.get("udid") != self.identity.udid):
                raise _bad()
            seed = (self.app_config.app_key.get_secret_value() + sign).encode("utf-8")
            headers = sdk_dynamic_headers(seed, self.identity.udid.encode("utf-8"), self.encoded_rule, self.rule_version)
        except Exception:
            pass
        if headers is None:
            raise _bad()
        return headers

    def sms_feign_sign(self, sign: str) -> str:
        if type(self.sms_public_key) is not SecretStr:
            raise _bad()
        return sdk_sms_feign_sign(sign, self.sms_public_key.get_secret_value())
