"""One-use, session-bound login envelopes; keys and challenges stay in memory."""

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from time import monotonic

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Request
from pydantic import ValidationError

from .errors import ApiError
from .models import (LOGIN_PLAINTEXT_MAX_BYTES, EncryptedLoginRequest,
                     LoginChallengeView, LoginRequest, SmsLoginRequest,
                     SmsSendRequest, WebLoginRequest)

CHALLENGE_TTL = 120
MAX_ENVELOPE_BYTES = 45000
MAX_PLAINTEXT_BYTES = LOGIN_PLAINTEXT_MAX_BYTES


def invalid_payload() -> ApiError:
    return ApiError("LOGIN_PAYLOAD_INVALID", "登录请求无效，请重试", 400)


@lru_cache(maxsize=1)
def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass(frozen=True)
class LoginChallenge:
    challenge_id: str
    deadline: float
    expires_at: datetime

    def view(self) -> LoginChallengeView:
        public_key = _private_key().public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return LoginChallengeView(challenge_id=self.challenge_id,
                                  public_key=base64.b64encode(public_key).decode("ascii"),
                                  expires_at=self.expires_at)


def new_challenge() -> LoginChallenge:
    return LoginChallenge(secrets.token_urlsafe(24), monotonic() + CHALLENGE_TTL,
                          datetime.now(timezone.utc) + timedelta(seconds=CHALLENGE_TTL))


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError("Invalid JSON constant")


def _json(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise invalid_payload() from None


async def read_envelope(request: Request) -> dict:
    # Stream directly so Starlette does not retain a cached request body.
    data = bytearray()
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_ENVELOPE_BYTES:
                raise invalid_payload()
            data.extend(chunk)
        body = _json(data)
        if not isinstance(body, dict):
            raise invalid_payload()
        return body
    finally:
        data.clear()


def reject_plaintext_fields(body: dict) -> None:
    if any(key in body for key in ("account", "password", "mobile", "country_code", "otp")):
        raise ApiError("INVALID_INPUT", "输入格式不合法", 422)


def consume_challenge(challenge: LoginChallenge | None, challenge_id: object) -> None:
    if challenge is None or not isinstance(challenge_id, str) or challenge_id != challenge.challenge_id:
        raise invalid_payload()
    if monotonic() >= challenge.deadline:
        raise ApiError("LOGIN_CHALLENGE_EXPIRED", "登录请求已过期，请重试", 400)


def _decode(value: str, *, length: int | None = None) -> bytes:
    decoded = base64.b64decode(value, validate=True)
    if base64.b64encode(decoded).decode("ascii") != value or (length is not None and len(decoded) != length):
        raise ValueError("Invalid encrypted field")
    return decoded


def decrypt_login(body: dict, *, allow_remember: bool = False) -> LoginRequest:
    return _decrypt_credentials(body, WebLoginRequest if allow_remember else LoginRequest)


def decrypt_sms_send(body: dict) -> SmsSendRequest:
    return _decrypt_credentials(body, SmsSendRequest)


def decrypt_sms_login(body: dict) -> SmsLoginRequest:
    return _decrypt_credentials(body, SmsLoginRequest)


def _decrypt_credentials(body: dict, model):
    encrypted = plaintext = key = credentials = None
    try:
        try:
            encrypted = EncryptedLoginRequest.model_validate(body)
            wrapped = _decode(encrypted.encrypted_key.get_secret_value(), length=256)
            iv = _decode(encrypted.iv.get_secret_value(), length=12)
            ciphertext = _decode(encrypted.ciphertext.get_secret_value())
            if not 16 <= len(ciphertext) <= MAX_PLAINTEXT_BYTES + 16:
                raise ValueError("Invalid ciphertext length")
            key = _private_key().decrypt(wrapped, padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
            if len(key) != 32:
                raise ValueError("Invalid encrypted key length")
            aad = f"rumi-login:v1:{encrypted.challenge_id}".encode("utf-8")
            plaintext = AESGCM(key).decrypt(iv, ciphertext, aad)
        except Exception:
            # Neither validation details nor cryptographic exceptions cross HTTP.
            raise invalid_payload() from None
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise invalid_payload()
        credentials = _json(plaintext)
        try:
            return model.model_validate(credentials)
        except ValidationError:
            raise ApiError("INVALID_INPUT", "输入格式不合法", 422) from None
    finally:
        encrypted = plaintext = key = credentials = None
