"""Actual Web Crypto -> Python API interoperability using synthetic credentials."""

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.models import Capabilities, Role, SessionView


NODE_ENCRYPT = r"""
import { webcrypto } from 'node:crypto';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const { challenge, credentials } = JSON.parse(input);
const subtle = webcrypto.subtle;
const publicKey = await subtle.importKey('spki', Buffer.from(challenge.public_key, 'base64'),
  { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']);
const key = await subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt']);
const iv = webcrypto.getRandomValues(new Uint8Array(12));
const ciphertext = await subtle.encrypt({ name: 'AES-GCM', iv, tagLength: 128,
  additionalData: new TextEncoder().encode(`rumi-login:v1:${challenge.challenge_id}`) },
  key, new TextEncoder().encode(JSON.stringify(credentials)));
const rawKey = await subtle.exportKey('raw', key);
const encryptedKey = await subtle.encrypt({ name: 'RSA-OAEP' }, publicKey, rawKey);
const b64 = bytes => Buffer.from(bytes).toString('base64');
process.stdout.write(JSON.stringify({ challenge_id: challenge.challenge_id,
  encrypted_key: b64(encryptedKey), iv: b64(iv), ciphertext: b64(ciphertext) }));
"""


class SyntheticProvider:
    def __init__(self):
        self.received = []

    def capabilities(self):
        return Capabilities(integration_ready=True, methods=["account"], reason_code=None, message=None)

    async def login(self, account, password):
        self.received.append((account, password))
        role = Role(id="synthetic-role", name="synthetic", server_name="synthetic")
        return SessionView(status="connected", authenticated=True, role=role, roles=[role])

    async def close(self):
        pass


@pytest.mark.parametrize("account,password", [
    ("synthetic-user", "synthetic-password"),
    ("中文账号😀", "中文密码😀"),
    ("账" * 256, "密" * 4096),
    ("\x01" * 256, "\x01" * 4096),
], ids=["ascii", "unicode", "previous-maximum-lengths", "maximum-json-escape-lengths"])
def test_web_crypto_envelope_is_accepted_without_plaintext_on_wire(catalog_path, account, password):
    node = os.environ.get("RUMI_NODE_EXE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web Crypto interoperability")
    provider = SyntheticProvider()
    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path)
    with TestClient(app) as client:
        response = client.get("/api/auth/login-challenge")
        assert response.status_code == 200
        result = subprocess.run([node, "--input-type=module", "-e", NODE_ENCRYPT],
                                input=json.dumps({"challenge": response.json(),
                                                  "credentials": {"account": account, "password": password}}),
                                text=True, encoding="utf-8", capture_output=True, check=True, timeout=15)
        envelope = json.loads(result.stdout)
        assert set(envelope) == {"challenge_id", "encrypted_key", "iv", "ciphertext"}
        assert account not in result.stdout and password not in result.stdout
        login = client.post("/api/auth/login", json=envelope)
        assert login.status_code == 200 and login.json()["authenticated"] is True
        assert provider.received == [(account, password)]
