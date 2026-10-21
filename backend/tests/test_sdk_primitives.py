import base64
from urllib.parse import parse_qs, quote_plus

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa


SYNTHETIC_KEY = "synthetic-key"


def sign(parameters, app_key=SYNTHETIC_KEY):
    from rumi_market.sdk_primitives import sdk_sign

    return sdk_sign(parameters, app_key)


def test_sign_sorts_parameter_names_before_concatenating_values():
    parameters = {"z": "last", "a": "first"}
    expected = "b081bfb7da69c0faa0cbcea7bfa1a8ca"
    assert sign(parameters) == expected
    assert sign(dict(reversed(list(parameters.items())))) == expected


def test_sign_does_not_include_parameter_names_or_separators():
    expected = "5088a8f307604fac350bb765e0518f7c"
    assert sign({"a": "value"}) == expected
    assert sign({"renamed_parameter": "value"}) == expected


def test_sign_excludes_sdk_names_without_case_sensitivity():
    parameters = {
        "item_name": "ignored", "ITEM_DESC": "ignored",
        "FeIgN_SiGn": "ignored", "TOKEN": "ignored", "sign": "ignored",
        "a": "included",
    }
    assert sign(parameters) == "fd140539351363867eaa812780c2f9a5"


def test_sign_keeps_names_that_only_contain_an_excluded_name():
    parameters = {"token_version": "suffix", "prefixed_sign": "kept"}
    assert sign(parameters) == "b43787a4efe3def155f7b22146ff2304"


def test_sign_sorts_unicode_names_by_java_utf16_units():
    # Java puts the supplementary key's high surrogate before U+E000.
    parameters = {"\ue000": "bmp", "\U00010000": "supplement"}
    assert sign(parameters) == "9fd5b4b562968ec926d260f886da54f1"


def test_sign_encodes_non_ascii_values_and_key_as_utf8():
    assert sign({"a": "蜜桃"}, "合成-key") == "63f3b8d526b571bf6f741ffa89b9d7f4"


def test_sign_returns_empty_for_an_empty_parameter_map():
    assert sign({}) == ""


def test_sign_hashes_the_key_when_a_nonempty_map_has_only_excluded_names():
    assert sign({"sign": "ignored"}) == "52a2cfd3ff45fb738283a627b4becd6a"


@pytest.mark.parametrize("parameters,app_key", [
    ({1: "value"}, SYNTHETIC_KEY),
    ({"a": None}, SYNTHETIC_KEY),
    ({"a": 1}, SYNTHETIC_KEY),
    ({"token": None}, SYNTHETIC_KEY),
    ({"a": "value"}, None),
    (["not-a-map"], SYNTHETIC_KEY),
])
def test_sign_rejects_nonstring_inputs(parameters, app_key):
    with pytest.raises(TypeError):
        sign(parameters, app_key)


@pytest.fixture(scope="module")
def synthetic_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_key_text(key, armored=True):
    public = key.public_key()
    if armored:
        return public.public_bytes(serialization.Encoding.PEM,
                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return base64.b64encode(public.public_bytes(serialization.Encoding.DER,
                           serialization.PublicFormat.SubjectPublicKeyInfo)).decode("ascii")


def encrypt_password(password, key, hash_prefix="", **options):
    from rumi_market.sdk_primitives import sdk_encrypt_password

    return sdk_encrypt_password(password, key, hash_prefix, **options)


def decrypt_password(key, ciphertext):
    return key.decrypt(base64.b64decode(ciphertext, validate=True), padding.PKCS1v15())


def test_encrypt_password_prepends_hash_and_encodes_plaintext_as_utf8(synthetic_rsa_key):
    ciphertext = encrypt_password("蜜桃🔒", public_key_text(synthetic_rsa_key), "合成盐:")
    assert decrypt_password(synthetic_rsa_key, ciphertext) == "合成盐:蜜桃🔒".encode("utf-8")


def test_encrypt_password_without_hash_ignores_prefix(synthetic_rsa_key):
    ciphertext = encrypt_password("合成密码", public_key_text(synthetic_rsa_key),
                                  "must-not-be-in-plaintext", is_need_hash=False)
    assert decrypt_password(synthetic_rsa_key, ciphertext) == "合成密码".encode("utf-8")


@pytest.mark.parametrize("armored", [True, False], ids=["pem-spki", "base64-spki"])
def test_encrypt_password_accepts_only_spki_public_key_forms(synthetic_rsa_key, armored):
    ciphertext = encrypt_password("synthetic", public_key_text(synthetic_rsa_key, armored))
    assert decrypt_password(synthetic_rsa_key, ciphertext) == b"synthetic"


def test_encrypt_password_accepts_line_wrapped_raw_base64_spki(synthetic_rsa_key):
    encoded = public_key_text(synthetic_rsa_key, False)
    wrapped = "\n".join(encoded[index:index + 64] for index in range(0, len(encoded), 64))
    ciphertext = encrypt_password("synthetic", wrapped)
    assert decrypt_password(synthetic_rsa_key, ciphertext) == b"synthetic"


def test_encrypt_password_accepts_spki_without_optional_algorithm_null(synthetic_rsa_key):
    der = synthetic_rsa_key.public_key().public_bytes(serialization.Encoding.DER,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo)
    assert der[:6] == b"\x30\x82\x01\x22\x30\x0d"
    assert der[17:19] == b"\x05\x00"
    without_null = b"\x30\x82\x01\x20\x30\x0b" + der[6:17] + der[19:]
    key = base64.b64encode(without_null).decode("ascii")
    ciphertext = encrypt_password("synthetic", key)
    assert decrypt_password(synthetic_rsa_key, ciphertext) == b"synthetic"


def test_encrypt_password_allows_an_empty_plaintext(synthetic_rsa_key):
    ciphertext = encrypt_password("", public_key_text(synthetic_rsa_key))
    assert decrypt_password(synthetic_rsa_key, ciphertext) == b""


def test_encrypt_password_is_randomized_pkcs1_v1_5(synthetic_rsa_key):
    public = public_key_text(synthetic_rsa_key)
    first = encrypt_password("synthetic", public)
    second = encrypt_password("synthetic", public)
    assert first != second
    assert decrypt_password(synthetic_rsa_key, first) == b"synthetic"
    assert decrypt_password(synthetic_rsa_key, second) == b"synthetic"


def test_encrypt_password_returns_standard_base64_without_url_encoding(synthetic_rsa_key):
    ciphertext = encrypt_password("synthetic", public_key_text(synthetic_rsa_key))
    assert set(ciphertext) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    assert len(base64.b64decode(ciphertext, validate=True)) == synthetic_rsa_key.key_size // 8
    assert "%" not in ciphertext
    assert "\n" not in ciphertext


def test_encrypt_password_accepts_the_exact_pkcs1_plaintext_byte_limit(synthetic_rsa_key):
    limit = synthetic_rsa_key.key_size // 8 - 11
    password = "x" * (limit - len("盐".encode("utf-8")))
    ciphertext = encrypt_password(password, public_key_text(synthetic_rsa_key), "盐")
    assert decrypt_password(synthetic_rsa_key, ciphertext) == ("盐" + password).encode("utf-8")


def test_encrypt_password_uses_ceiling_bytes_for_a_2049_bit_rsa_key(synthetic_rsa_key):
    first = synthetic_rsa_key.private_numbers()
    second = rsa.generate_private_key(public_exponent=65537, key_size=2050).private_numbers()
    p, q = max(first.p, first.q), max(second.p, second.q)
    n, e = p * q, 65537
    d = pow(e, -1, (p - 1) * (q - 1))
    key = rsa.RSAPrivateNumbers(p, q, d, rsa.rsa_crt_dmp1(d, p),
        rsa.rsa_crt_dmq1(d, q), rsa.rsa_crt_iqmp(p, q), rsa.RSAPublicNumbers(e, n)).private_key()
    assert key.key_size == 2049
    ciphertext = encrypt_password("x" * 246, public_key_text(key))
    assert len(base64.b64decode(ciphertext)) == 257
    assert decrypt_password(key, ciphertext) == b"x" * 246
    with pytest.raises(ValueError, match="^SDK password plaintext is too long$"):
        encrypt_password("x" * 247, public_key_text(key))


@pytest.mark.parametrize("unicode_input", [False, True], ids=["ascii-limit", "utf8-limit"])
def test_encrypt_password_rejects_oversized_plaintext_without_splitting(synthetic_rsa_key, unicode_input):
    password = "蜜" * 82 if unicode_input else "x" * (synthetic_rsa_key.key_size // 8 - 10)
    with pytest.raises(ValueError, match="^SDK password plaintext is too long$") as failure:
        encrypt_password(password, public_key_text(synthetic_rsa_key))
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


@pytest.mark.parametrize("kind", ["empty", "invalid-base64", "non-ascii", "wrong-armor",
                                  "pkcs1-public", "private-pem", "private-der", "ec-public",
                                  "multiple-blocks", "der-trailing-data"])
def test_encrypt_password_rejects_invalid_public_keys_without_echo_or_cause(synthetic_rsa_key, kind):
    password, prefix = "synthetic-password-marker", "synthetic-hash-marker"
    if kind == "empty":
        key = ""
    elif kind == "invalid-base64":
        key = "synthetic-key-marker!"
    elif kind == "non-ascii":
        key = "synthetic-key-marker🧪"
    elif kind == "wrong-armor":
        key = "-----BEGIN RSA PUBLIC KEY-----\n" + public_key_text(synthetic_rsa_key, False) + "\n-----END RSA PUBLIC KEY-----"
    elif kind == "pkcs1-public":
        key = base64.b64encode(synthetic_rsa_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.PKCS1)).decode("ascii")
    elif kind == "private-pem":
        key = synthetic_rsa_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode("ascii")
    elif kind == "private-der":
        key = base64.b64encode(synthetic_rsa_key.private_bytes(serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode("ascii")
    elif kind == "multiple-blocks":
        key = public_key_text(synthetic_rsa_key) * 2
    elif kind == "der-trailing-data":
        der = synthetic_rsa_key.public_key().public_bytes(serialization.Encoding.DER,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo)
        key = base64.b64encode(der + b"synthetic-trailing-garbage").decode("ascii")
    else:
        key = public_key_text(ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(ValueError) as failure:
        encrypt_password(password, key, prefix)
    assert str(failure.value) == "Invalid SDK RSA public key"
    assert password not in str(failure.value)
    assert prefix not in str(failure.value)
    assert key not in str(failure.value) if key else True
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


@pytest.mark.parametrize("password,key,prefix,flag", [
    (None, "key", "", True), ("password", None, "", True),
    ("password", "key", None, True), ("password", "key", "", "true"),
])
def test_encrypt_password_rejects_wrong_argument_types(password, key, prefix, flag):
    with pytest.raises(TypeError):
        encrypt_password(password, key, prefix, is_need_hash=flag)


def test_encrypt_password_raw_base64_is_form_encoded_once_by_httpx(synthetic_rsa_key):
    ciphertext = encrypt_password("synthetic", public_key_text(synthetic_rsa_key))
    request = httpx.Request("POST", "https://example.invalid/synthetic", data={"pwd": ciphertext})
    body = request.content.decode("ascii")
    assert body == "pwd=" + quote_plus(ciphertext, safe="")
    assert parse_qs(body) == {"pwd": [ciphertext]}
    assert "%25" not in body
