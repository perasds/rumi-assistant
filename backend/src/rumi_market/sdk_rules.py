"""Verified SDK signed-rule recovery and bounded byte-rule subset."""

import base64
import hashlib
import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .sdk_rule_codes import CODEBOOK


_DECODE = {code.encode("ascii"): symbol for symbol, code in CODEBOOK}


def _bad() -> ValueError:
    return ValueError("Unsupported SDK rule")


def decode_sdk_rule(encoded: bytes) -> bytes:
    if not isinstance(encoded, bytes) or not 0 < len(encoded) <= 1048576:
        raise _bad()
    output, pending = bytearray(), b""
    for bit in encoded:
        if bit not in (48, 49):
            raise _bad()
        pending += bytes((bit,))
        if pending in _DECODE:
            output.append(_DECODE[pending])
            pending = b""
        elif len(pending) >= 9:
            raise _bad()
    if pending:
        raise _bad()
    return bytes(output)


def recover_signed_sdk_rule(signed_rule: bytes, public_pem: bytes) -> bytes:
    """Recover exactly one RSA PKCS#1 type-1 block, then validate its bit code."""
    recovered = None
    try:
        if (not isinstance(signed_rule, bytes) or not 0 < len(signed_rule) <= 16384
                or not isinstance(public_pem, bytes) or not 0 < len(public_pem) <= 16384
                or not public_pem.startswith(b"-----BEGIN PUBLIC KEY-----")):
            raise _bad()
        key = serialization.load_pem_public_key(public_pem)
        if not isinstance(key, rsa.RSAPublicKey) or not 1024 <= key.key_size <= 8192:
            raise _bad()
        size = (key.key_size + 7) // 8
        signature = base64.b64decode(signed_rule, validate=True)
        numbers = key.public_numbers()
        value = int.from_bytes(signature, "big")
        if len(signature) != size or value >= numbers.n:
            raise _bad()
        block = pow(value, numbers.e, numbers.n).to_bytes(size, "big")
        separator = block.find(b"\x00", 2)
        if (not block.startswith(b"\x00\x01") or separator < 10
                or block[2:separator] != b"\xff" * (separator - 2)):
            raise _bad()
        bits = block[separator + 1:]
        decode_sdk_rule(bits)
        recovered = bits
    except Exception:
        pass
    if recovered is None:
        raise _bad()
    return recovered


def _atoi(value: bytes) -> int:
    matched = re.match(rb"\s*([+-]?[0-9]+)", value)
    if matched is None:
        return 0
    token = matched.group(1)
    if len(token) > 11:
        raise _bad()
    number = int(token)
    if not -2147483648 <= number <= 2147483647:
        raise _bad()
    return number


def apply_sdk_rule(seed: bytes, rule: bytes) -> bytes:
    if (not isinstance(seed, bytes) or not isinstance(rule, bytes)
            or b"\0" in seed or b"\0" in rule or len(seed) > 1048576
            or not 3 <= len(rule) <= 1048576):
        raise _bad()
    groups = rule.split(b".*")
    if len(groups) > 256:
        raise _bad()
    depth = 0
    for char in rule:
        if char == 91:
            depth += 1
        elif char == 93:
            depth -= 1
            if depth < 0:
                raise _bad()
    if depth:
        raise _bad()
    current = seed
    for group in groups:
        if not group.startswith(b"[") or not group.endswith(b"]"):
            raise _bad()
        inner = group[1:-1]
        tokens = inner.split(b"|") if len(inner) >= 3 else [inner] if len(inner) == 1 else []
        if not tokens or len(tokens[0]) != 1:
            continue
        op = tokens[0]
        if op == b"d":
            current = current[::-1]
        elif op == b"r" and len(tokens) == 3:
            if current and tokens[1] and tokens[2]:
                if len(current) + current.count(tokens[1]) * (len(tokens[2]) - len(tokens[1])) > 1048576:
                    raise _bad()
                current = current.replace(tokens[1], tokens[2])
        elif op == b"a" and len(tokens) == 3:
            index = _atoi(tokens[1])
            if current and tokens[2] and 0 <= index < len(current):
                if len(current) + len(tokens[2]) > 1048576:
                    raise _bad()
                current = current[:index] + tokens[2] + current[index:]
        elif op in (b"t", b"m") and len(tokens) in ((2, 3) if op == b"t" else (3,)):
            start = _atoi(tokens[1])
            end = _atoi(tokens[2]) if len(tokens) == 3 else start
            if 0 <= start < len(current) and 0 <= end < len(current):
                if op == b"m":
                    current = current[start:end + 1]
                elif start <= end:
                    current = current[:start] + current[end + 1:]
        if len(current) > 1048576:
            raise _bad()
    return current


def sdk_dynamic_headers(sign_seed: bytes, udid: bytes, encoded_rule: bytes, version: int) -> dict[str, str]:
    if (not isinstance(sign_seed, bytes) or not isinstance(udid, bytes)
            or b"\0" in sign_seed or b"\0" in udid or len(sign_seed) < 16
            or len(udid) < 4 or max(len(sign_seed), len(udid)) > 1048576
            or type(version) is not int or not 0 <= version <= 2147483647):
        raise _bad()
    rule = decode_sdk_rule(encoded_rule)
    headers = {}
    for i in range(5):
        current = sign_seed if i == 1 else sign_seed + udid[i:i + i + 1]
        offsets = (i, (4 * i + 3) % 15, (i + 3) % 15, (8 * i + 1) % 15)
        raw_key = bytes(current[len(current) - 1 - offset] for offset in offsets)
        if not re.fullmatch(rb"[A-Za-z0-9!#$%&'*+.^_`|~-]{4}", raw_key):
            raise _bad()
        key = raw_key.decode("ascii")
        if key in headers:
            key += str(i)
        headers[key] = hashlib.md5(apply_sdk_rule(current, rule) + udid).hexdigest()
    headers["cversion"] = str(version)
    return headers
