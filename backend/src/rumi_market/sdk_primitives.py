"""Source-verified SDK primitives; no authentication or network integration."""

import base64
import hashlib
import re
from collections.abc import Mapping

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


_EXCLUDED_NAMES = re.compile(
    r"(?:item_name|item_desc|feign_sign|token|sign)", re.IGNORECASE
)


def sdk_sign(parameters: Mapping[str, str], app_key: str) -> str:
    """Match CommonParamUtils.generateSign and MD5.sign for string inputs.

    SDK source uses Charset.defaultCharset(); this APK's Android test
    environment reports UTF-8. The key is consumed only in memory.
    """
    if not isinstance(parameters, Mapping) or not isinstance(app_key, str):
        raise TypeError("SDK parameters and key must use string inputs")
    if any(not isinstance(name, str) or not isinstance(value, str)
           for name, value in parameters.items()):
        raise TypeError("SDK parameter names and values must be strings")
    if not parameters:
        return ""

    # Big-endian UTF-16 bytes have Java String.compareTo's code-unit order.
    names = sorted(parameters, key=lambda name: name.encode("utf-16-be", "surrogatepass"))
    values = "".join(parameters[name] for name in names
                     if _EXCLUDED_NAMES.fullmatch(name) is None)
    return hashlib.md5((values + app_key).encode("utf-8")).hexdigest()


def sdk_encrypt_password(password: str, cipher_key: str, hash_prefix: str = "", *,
                         is_need_hash: bool = True) -> str:
    """Encrypt the SDK plaintext once with RSA PKCS1v1.5; return raw Base64.

    Only PUBLIC KEY / SPKI inputs are accepted. HTTP form encoding happens
    separately, once; this function performs no network or persistence.
    """
    if not all(isinstance(value, str) for value in (password, cipher_key, hash_prefix)):
        raise TypeError("SDK password, public key and hash prefix must be strings")
    if not isinstance(is_need_hash, bool):
        raise TypeError("SDK hash flag must be boolean")

    public_key = None
    try:
        encoded = cipher_key.encode("ascii")
        armored = re.fullmatch(
            rb"\s*-----BEGIN PUBLIC KEY-----([A-Za-z0-9+/=\s]+)-----END PUBLIC KEY-----\s*",
            encoded,
        )
        if armored is not None:
            encoded = armored.group(1)
        encoded = re.sub(rb"\s+", b"", encoded)
        der = base64.b64decode(encoded, validate=True)
        if base64.b64encode(der) != encoded:
            raise ValueError("Noncanonical public key encoding")
        # The fixed PUBLIC KEY label makes the loader require SPKI, rather
        # than accepting the PKCS1 public-key form also supported by DER load.
        pem = b"-----BEGIN PUBLIC KEY-----\n" + encoded + b"\n-----END PUBLIC KEY-----\n"
        public_key = serialization.load_pem_public_key(pem)
    except (ValueError, UnsupportedAlgorithm):
        pass
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise ValueError("Invalid SDK RSA public key")

    plaintext = None
    try:
        plaintext = ((hash_prefix if is_need_hash else "") + password).encode("utf-8")
    except UnicodeError:
        pass
    if plaintext is None:
        raise ValueError("Invalid SDK password text")
    if len(plaintext) > (public_key.key_size + 7) // 8 - 11:
        raise ValueError("SDK password plaintext is too long")

    ciphertext = None
    try:
        ciphertext = public_key.encrypt(plaintext, padding.PKCS1v15())
    except (ValueError, UnsupportedAlgorithm):
        pass
    if ciphertext is None:
        raise ValueError("SDK password encryption failed")
    return base64.b64encode(ciphertext).decode("ascii")
