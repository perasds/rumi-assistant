import base64
import hashlib
import importlib
import importlib.util
import io
import warnings
import zipfile
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from rumi_market.sdk_app_config import SdkAppConfig
from rumi_market.sdk_assets import SdkNativeAssets
from rumi_market.sdk_device import SdkDeviceIdentity
from rumi_market.sdk_http import SdkHttpClient
from rumi_market.sdk_primitives import sdk_sign
from rumi_market.sdk_rules import sdk_dynamic_headers
from rumi_market.sdk_rule_codes import CODEBOOK


CERT_MEMBER = "META-INF/BILIGAME.RSA"
CHANNEL_MEMBER = "assets/distributor.txt"
CIPHER_PATH = "/api/external/issue/cipher/v3"
LOGIN_PATH = "/api/external/login/v3"
BITS = b"0010010101000011"
CONFIG = SdkAppConfig("synthetic-merchant", "synthetic-game", "synthetic-server", "1.2.3",
                      SecretStr("0123456789abcdef" * 2), "2.0.0", "99")
IDENTITY = SdkDeviceIdentity("11111111-1111-4111-8111-111111111111", "synthetic-bdid")


def module():
    assert importlib.util.find_spec("rumi_market.sdk_parameters") is not None, "SDK context is missing"
    return importlib.import_module("rumi_market.sdk_parameters")


@pytest.fixture(scope="module")
def materials():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-certificate")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1).not_valid_before(now)
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_pkcs7 = pkcs7.serialize_certificates([cert], serialization.Encoding.DER)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    block = b"\x00\x01" + b"\xff" * (256 - len(BITS) - 3) + b"\x00" + BITS
    numbers = key.private_numbers()
    signed = base64.b64encode(pow(int.from_bytes(block, "big"), numbers.d,
                                 numbers.public_numbers.n).to_bytes(256, "big"))
    return cert, cert_der, cert_pkcs7, SdkNativeAssets(pem, signed), key


def archive(tmp_path, monkeypatch, materials, *, cert=None, channel=b"1",
            cert_names=(CERT_MEMBER,), channel_names=(CHANNEL_MEMBER,)):
    mod = module()
    cert = materials[2] if cert is None else cert
    content = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_STORED) as target:
            for name in cert_names:
                target.writestr(name, cert)
            for name in channel_names:
                target.writestr(name, channel)
    raw = content.getvalue()
    path = tmp_path / "synthetic-private-path.apk"
    path.write_bytes(raw)
    pins = {"APK_SHA256": hashlib.sha256(raw).hexdigest(), "CERT_SIZE": len(cert),
            "CERT_SHA256": hashlib.sha256(cert).hexdigest(), "CERT_DER_SIZE": len(materials[1]),
            "CERT_DER_SHA256": hashlib.sha256(materials[1]).hexdigest(), "CHANNEL_SIZE": len(channel),
            "CHANNEL_SHA256": hashlib.sha256(channel).hexdigest()}
    for name, value in pins.items():
        monkeypatch.setattr(mod, name, value)
    monkeypatch.setattr(mod, "load_sdk_app_config", lambda path: CONFIG)
    monkeypatch.setattr(mod, "load_sdk_native_assets", lambda path: materials[3])
    monkeypatch.setattr(mod, "load_sdk_sms_public_key", lambda path: SecretStr(materials[3].public_pem.decode()))
    return mod, path


def rejected(mod, path):
    with pytest.raises(ValueError, match="^Invalid SDK request context$") as failure:
        mod.SdkRequestContext.from_apk(path)
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert "private" not in str(failure.value)


def test_normal_context_creates_one_private_identity_and_pinned_apk_profile(tmp_path, monkeypatch, materials):
    mod, path = archive(tmp_path, monkeypatch, materials)
    calls = []
    monkeypatch.setattr(mod.SdkDeviceIdentity, "create", lambda: calls.append(True) or IDENTITY)
    context = mod.SdkRequestContext.from_apk(path)
    expected = {"app_id": "synthetic-game", "game_id": "synthetic-game",
                "merchant_id": "synthetic-merchant", "server_id": "synthetic-server",
                "sdk_ver": "1.2.3", "platform": "3", "platform_type": "3", "sdk_type": "1",
                "sdk_log_type": "1", "version": "1", "channel_id": "1", "current_env": "0",
                "udid": IDENTITY.udid, "bd_id": IDENTITY.bd_id,
                "apk_sign": hashlib.md5(materials[1]).hexdigest(), "app_ver": "2.0.0", "version_code": "99"}
    assert context.common_parameters(CIPHER_PATH) == expected
    assert context.common_parameters(LOGIN_PATH) == expected
    assert calls == [True]
    assert all(value not in repr(context) for value in [IDENTITY.udid, IDENTITY.bd_id,
               CONFIG.app_key.get_secret_value(), "synthetic-game", BITS.decode()])
    with pytest.raises(FrozenInstanceError):
        context.channel_id = "2"
    changed = context.common_parameters(CIPHER_PATH)
    changed["udid"] = "foreign"
    assert context.common_parameters(CIPHER_PATH)["udid"] == IDENTITY.udid
    assert calls == [True]


def test_config_uses_same_normal_after_consent_identity(tmp_path, monkeypatch, materials):
    mod, path = archive(tmp_path, monkeypatch, materials)
    context = mod.SdkRequestContext.from_apk(path)
    config = context.common_parameters("/api/external/config/v3")
    assert config == context.common_parameters(CIPHER_PATH)
    assert config["bd_id"] == context.identity.bd_id
    assert config["udid"] == context.identity.udid
    assert not {"imei", "android_id", "mac", "oaid", "user_id", "pwd"} & config.keys()


def test_sms_uses_same_identity_and_context_signs_only_raw_md5(tmp_path, monkeypatch, materials):
    from cryptography.hazmat.primitives.asymmetric import padding

    mod, path = archive(tmp_path, monkeypatch, materials)
    context = mod.SdkRequestContext.from_apk(path)
    common = context.common_parameters(CIPHER_PATH)
    assert context.common_parameters("/api/external/otp/send/v3") == common
    assert context.common_parameters("/api/external/login/otp/v3") == common
    signature = context.sms_feign_sign("0123456789abcdef" * 2)
    assert materials[4].decrypt(base64.b64decode(signature), padding.PKCS1v15()) == b"0123456789abcdef" * 2
    assert context.sms_public_key.get_secret_value() not in repr(context)


def test_configured_rule_keeps_identity_and_uses_verified_version(tmp_path, monkeypatch, materials):
    mod, path = archive(tmp_path, monkeypatch, materials)
    context = mod.SdkRequestContext.from_apk(path)
    codes = dict(CODEBOOK)
    changed_bits = "".join(codes[byte] for byte in b"[t|0]").encode("ascii")
    block = b"\x00\x01" + b"\xff" * (256 - len(changed_bits) - 3) + b"\x00" + changed_bits
    numbers = materials[4].private_numbers()
    signed = base64.b64encode(pow(int.from_bytes(block, "big"), numbers.d,
                                 numbers.public_numbers.n).to_bytes(256, "big"))
    configured = context.with_config({"crule": signed.decode("ascii"), "crule_ver": "7"})
    assert configured.identity is context.identity
    assert configured.app_config is context.app_config
    assert configured.encoded_rule == changed_bits and configured.encoded_rule != context.encoded_rule
    assert context.rule_version == 0 and configured.rule_version == 7
    sign, timestamp = "0123456789abcdef" * 2, "1234567890000"
    params = {**configured.common_parameters(CIPHER_PATH), "sign": sign, "timestamp": timestamp}
    headers = configured.extra_headers(timestamp, sign, params)
    assert headers["cversion"] == "7" and len(headers) == 6
    original_headers = context.extra_headers(timestamp, sign, params)
    assert all(headers[key] != original_headers[key] for key in headers if key != "cversion")
    assert "crule" not in configured.common_parameters(CIPHER_PATH)


@pytest.mark.parametrize("response", [None, {}, {"crule": "private-invalid", "crule_ver": "7"},
    {"crule_ver": "7"}, {"crule_ver": "0"}, {"crule_ver": "-1"}, {"crule_ver": "7x"},
    {"crule_ver": "2147483648"}, {"crule_ver": 7}])
def test_invalid_config_never_changes_live_rule_or_echoes_data(tmp_path, monkeypatch, materials, response):
    mod, path = archive(tmp_path, monkeypatch, materials)
    context = mod.SdkRequestContext.from_apk(path)
    if isinstance(response, dict) and "crule" not in response and response.get("crule_ver") != "7":
        response = {"crule": materials[3].builtin_signed_rule.decode("ascii"), **response}
    with pytest.raises(ValueError, match="^Invalid SDK request context$") as failure:
        context.with_config(response)
    assert context.rule_version == 0 and context.encoded_rule == BITS
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("name", ["APK_SHA256", "CERT_SHA256", "CERT_DER_SHA256", "CHANNEL_SHA256",
                                  "CERT_SIZE", "CERT_DER_SIZE", "CHANNEL_SIZE", "APK_MAX_BYTES"])
def test_wrong_pinned_materials_fail_before_identity_creation(tmp_path, monkeypatch, materials, name):
    mod, path = archive(tmp_path, monkeypatch, materials)
    wrong = "0" * 64 if name.endswith("SHA256") else 1 if name == "APK_MAX_BYTES" else getattr(mod, name) + 1
    monkeypatch.setattr(mod, name, wrong)
    monkeypatch.setattr(mod.SdkDeviceIdentity, "create", lambda: pytest.fail("identity created on invalid APK"))
    rejected(mod, path)


@pytest.mark.parametrize("cert_names,channel_names", [((CERT_MEMBER, CERT_MEMBER), (CHANNEL_MEMBER,)),
    (("foreign.RSA",), (CHANNEL_MEMBER,)), ((CERT_MEMBER,), (CHANNEL_MEMBER, CHANNEL_MEMBER)),
    ((CERT_MEMBER,), ("foreign.txt",))])
def test_only_unique_exact_members_are_used(tmp_path, monkeypatch, materials, cert_names, channel_names):
    mod, path = archive(tmp_path, monkeypatch, materials, cert_names=cert_names, channel_names=channel_names)
    rejected(mod, path)


@pytest.mark.parametrize("changed", ["invalid_pkcs7", "multiple_certificates", "channel", "der"])
def test_certificate_and_channel_structure_is_verified_after_member_hash(tmp_path, monkeypatch, materials, changed):
    cert, channel = materials[2], b"1"
    if changed == "invalid_pkcs7":
        cert = b"synthetic-private-invalid-certificate"
    elif changed == "multiple_certificates":
        cert = pkcs7.serialize_certificates([materials[0], materials[0]], serialization.Encoding.DER)
    elif changed == "channel":
        channel = b"2"
    mod, path = archive(tmp_path, monkeypatch, materials, cert=cert, channel=channel)
    if changed == "der":
        monkeypatch.setattr(mod, "CERT_DER_SIZE", len(materials[1]) + 1)
    rejected(mod, path)


def test_missing_path_and_dependency_errors_never_echo_raw_details(tmp_path, monkeypatch, materials):
    mod, path = archive(tmp_path, monkeypatch, materials)
    rejected(mod, tmp_path / "synthetic-private-missing.apk")
    def fail(path):
        raise RuntimeError("synthetic-app-key-private")
    monkeypatch.setattr(mod, "load_sdk_native_assets", fail)
    rejected(mod, path)


@pytest.mark.parametrize("path", ["/api/external/login/v3/", "/api/external/login/v3?x=1", "http://foreign/", None])
def test_common_parameters_require_exact_verified_paths(tmp_path, monkeypatch, materials, path):
    mod, apk = archive(tmp_path, monkeypatch, materials)
    context = mod.SdkRequestContext.from_apk(apk)
    with pytest.raises(ValueError, match="^Invalid SDK request context$"):
        context.common_parameters(path)


@pytest.mark.anyio
@pytest.mark.parametrize("token_path", ["/api/external/user.token.oauth.login/v3", "/api/external/token.renewal/v3"])
async def test_token_paths_use_normal_cache_miss_profile_and_configured_signature(
        tmp_path, monkeypatch, materials, token_path):
    mod, path = archive(tmp_path, monkeypatch, materials)
    context = mod.SdkRequestContext.from_apk(path)
    common = context.common_parameters(token_path)
    assert common == context.common_parameters(LOGIN_PATH) and common["version"] == "1"
    def handler(request):
        assert request.url.path == token_path
        form = {k: values[0] for k, values in parse_qs(request.content.decode()).items()}
        assert form["version"] == "1" and form["uid"] == "synthetic-uid"
        assert form["access_key"] == "synthetic-token" and form["udid"] == context.identity.udid
        assert form["sign"] == sdk_sign(form, CONFIG.app_key.get_secret_value())
        expected = context.extra_headers(form["timestamp"], form["sign"], form)
        assert all(request.headers[k] == v for k, v in expected.items())
        assert not {"user_id", "pwd", "refresh_token", "app_key"} & form.keys()
        return httpx.Response(200, json={"code": 0})
    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", CONFIG.app_key.get_secret_value(),
        common_parameters=context.common_parameters, extra_headers=context.extra_headers,
        transport=httpx.MockTransport(handler))
    try:
        await client._post(token_path, {"uid": "synthetic-uid", "access_key": "synthetic-token"})
    finally:
        await client.close()


def test_headers_use_same_identity_and_appkey_plus_sign_seed(tmp_path, monkeypatch, materials):
    mod, path = archive(tmp_path, monkeypatch, materials)
    monkeypatch.setattr(mod.SdkDeviceIdentity, "create", lambda: IDENTITY)
    context = mod.SdkRequestContext.from_apk(path)
    sign, timestamp = "fedcba9876543210" * 2, "1234567890000"
    params = {**context.common_parameters(CIPHER_PATH), "sign": sign, "timestamp": timestamp}
    expected = sdk_dynamic_headers((CONFIG.app_key.get_secret_value() + sign).encode(),
                                   IDENTITY.udid.encode(), BITS, 0)
    assert context.extra_headers(timestamp, sign, params) == expected
    assert len(expected) == 6 and expected["cversion"] == "0"
    assert params["sign"] == sign


@pytest.mark.parametrize("changed", ["udid", "timestamp", "sign", "sign_format"])
def test_header_callback_refuses_mixed_identity_or_sign_inputs(tmp_path, monkeypatch, materials, changed):
    mod, path = archive(tmp_path, monkeypatch, materials)
    context = mod.SdkRequestContext.from_apk(path)
    sign, timestamp = "fedcba9876543210" * 2, "1234567890000"
    params = {**context.common_parameters(CIPHER_PATH), "sign": sign, "timestamp": timestamp}
    if changed == "sign_format":
        sign = params["sign"] = "synthetic-private-foreign"
    else:
        params[changed] = "synthetic-private-foreign"
    with pytest.raises(ValueError, match="^Invalid SDK request context$") as failure:
        context.extra_headers(timestamp, sign, params)
    assert failure.value.__context__ is None and "private" not in str(failure.value)


@pytest.mark.anyio
async def test_cipher_business_uses_normal_profile_and_headers_with_one_form_encoding(tmp_path, monkeypatch, materials):
    mod, path = archive(tmp_path, monkeypatch, materials)
    monkeypatch.setattr(mod.SdkDeviceIdentity, "create", lambda: IDENTITY)
    context = mod.SdkRequestContext.from_apk(path)
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == CIPHER_PATH
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        assert form["cipher_type"] == "bili_login_rsa" and form["version"] == "3"
        assert form["udid"] == IDENTITY.udid and form["bd_id"] == IDENTITY.bd_id
        assert form["sign"] == sdk_sign(form, CONFIG.app_key.get_secret_value())
        assert not ({"user_id", "pwd", "uid", "access_key", "app_key", "userdata", "ad_info", "ad_ext"} & form.keys())
        expected = context.extra_headers(form["timestamp"], form["sign"], form)
        assert all(request.headers[key] == value for key, value in expected.items())
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        return httpx.Response(200, json={"code": 0, "cipher_key": "synthetic", "hash": "synthetic"})
    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", CONFIG.app_key.get_secret_value(),
        common_parameters=context.common_parameters, extra_headers=context.extra_headers,
        transport=httpx.MockTransport(handler), clock_ms=lambda: 1234567890000)
    try:
        assert (await client._post(CIPHER_PATH, {"cipher_type": "bili_login_rsa", "version": "3"}))["code"] == 0
        assert calls == [CIPHER_PATH]
    finally:
        await client.close()


@pytest.mark.anyio
async def test_legacy_captcha_uses_normal_version_one_profile_and_signed_headers(tmp_path, monkeypatch, materials):
    mod, path = archive(tmp_path, monkeypatch, materials)
    monkeypatch.setattr(mod.SdkDeviceIdentity, "create", lambda: IDENTITY)
    context = mod.SdkRequestContext.from_apk(path)
    captcha_path = "/api/client/start_captcha"
    common = context.common_parameters(captcha_path)
    assert common == context.common_parameters(CIPHER_PATH)
    assert common["version"] == "1" and context.identity is IDENTITY
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == captcha_path
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        expected = {**common, "timestamp": "1234567890000",
                    "domain": "line1-sdk-center-login-sh.biligame.net"}
        assert form == {**expected, "sign": sdk_sign(expected, CONFIG.app_key.get_secret_value())}
        headers = context.extra_headers(form["timestamp"], form["sign"], form)
        assert all(request.headers[key] == value for key, value in headers.items())
        return httpx.Response(200, json={"code": 0, "captcha_type": "synthetic-type"})

    client = SdkHttpClient("https://line1-sdk-center-login-sh.biligame.net", CONFIG.app_key.get_secret_value(),
        common_parameters=context.common_parameters, extra_headers=context.extra_headers,
        transport=httpx.MockTransport(handler), clock_ms=lambda: 1234567890000)
    try:
        assert await client.start_captcha() == {"code": 0, "captcha_type": "synthetic-type"}
        assert calls == [captcha_path]
    finally:
        await client.close()
