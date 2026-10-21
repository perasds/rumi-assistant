"""Build explicit browser-compatible login envelopes from synthetic credentials."""

import base64
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encrypt_login(challenge, credentials=None, *, plaintext=None, aad=None, key=None, iv=None):
    public_key = serialization.load_der_public_key(base64.b64decode(challenge["public_key"]))
    key = key if key is not None else AESGCM.generate_key(bit_length=256)
    iv = iv if iv is not None else os.urandom(12)
    if plaintext is None:
        plaintext = json.dumps(credentials, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    aad = aad if aad is not None else f"rumi-login:v1:{challenge['challenge_id']}".encode("utf-8")
    ciphertext = AESGCM(key).encrypt(iv, plaintext, aad)
    wrapped_key = public_key.encrypt(key, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return {"challenge_id": challenge["challenge_id"],
            "encrypted_key": base64.b64encode(wrapped_key).decode("ascii"),
            "iv": base64.b64encode(iv).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii")}


def login_body(client, account, password):
    challenge = client.get("/api/auth/login-challenge")
    assert challenge.status_code == 200
    return encrypt_login(challenge.json(), {"account": account, "password": password})


async def async_login_body(client, account, password):
    challenge = await client.get("/api/auth/login-challenge")
    assert challenge.status_code == 200
    return encrypt_login(challenge.json(), {"account": account, "password": password})
