import base64
import hashlib
import importlib
import importlib.util
import io
import struct
import warnings
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from rumi_market.sdk_rules import recover_signed_sdk_rule, sdk_dynamic_headers


MEMBER = "lib/arm64-v8a/libcustom_rules.so"
ELF_SIZE = 855416
PEM_VA, RULE_VA = 0xaa0c3, 0xaa9a3
BITS = b"0010010101000011"


def module():
    assert importlib.util.find_spec("rumi_market.sdk_assets") is not None, "SDK assets loader is missing"
    return importlib.import_module("rumi_market.sdk_assets")


@pytest.fixture(scope="module")
def materials():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(serialization.Encoding.DER,
                                      serialization.PublicFormat.SubjectPublicKeyInfo)
    pem = b"-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(der) + b"\n-----END PUBLIC KEY-----\n"
    block = b"\x00\x01" + b"\xff" * (256 - len(BITS) - 3) + b"\x00" + BITS
    numbers = key.private_numbers()
    signed = base64.b64encode(pow(int.from_bytes(block, "big"), numbers.d,
                                 numbers.public_numbers.n).to_bytes(256, "big"))
    assert (len(pem), len(signed)) == (445, 344)
    return pem, signed


def synthetic_elf(materials):
    pem, signed = materials
    data = bytearray(ELF_SIZE)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 3, 183, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 4, 4096, 0xaa000, 0xaa000, 4096, 4096, 4096)
    for address, value in ((PEM_VA, pem), (RULE_VA, signed)):
        offset = 4096 + address - 0xaa000
        data[offset:offset + len(value) + 1] = value + b"\x00"
    return data


def archive(tmp_path, monkeypatch, materials, data=None, *, names=(MEMBER,), declared_size=None):
    mod = module()
    data = synthetic_elf(materials) if data is None else data
    content = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name in names:
                target.writestr(name, bytes(data))
    raw = bytearray(content.getvalue())
    if declared_size is not None:
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<I", raw, central + 24, declared_size)
    path = tmp_path / "synthetic-private-name.apk"
    path.write_bytes(raw)
    monkeypatch.setattr(mod, "APK_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(mod, "ELF_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(mod, "PUBLIC_PEM_SHA256", hashlib.sha256(materials[0]).hexdigest())
    monkeypatch.setattr(mod, "BUILTIN_RULE_SHA256", hashlib.sha256(materials[1]).hexdigest())
    return mod, path


def rejected(mod, path):
    with pytest.raises(ValueError, match="^Invalid SDK native assets$") as failure:
        mod.load_sdk_native_assets(path)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert "private" not in str(failure.value)


def test_verified_loader_returns_private_native_materials_and_recovers_synthetic_rule(tmp_path, monkeypatch, materials):
    mod, path = archive(tmp_path, monkeypatch, materials)
    value = mod.load_sdk_native_assets(path)
    assert value.public_pem == materials[0]
    assert value.builtin_signed_rule == materials[1]
    assert "BEGIN" not in repr(value) and materials[1].decode() not in repr(value)
    assert recover_signed_sdk_rule(value.builtin_signed_rule, value.public_pem) == BITS
    headers = sdk_dynamic_headers(b"0123456789abcdef" * 2, b"synthetic-device-id", BITS, 0)
    assert len(headers) == 6 and headers["cversion"] == "0"


@pytest.mark.parametrize("changed", ["apk_hash", "elf_hash", "pem_hash", "rule_hash", "apk_size"])
def test_mismatched_pinned_materials_fail_with_fixed_error(tmp_path, monkeypatch, materials, changed):
    mod, path = archive(tmp_path, monkeypatch, materials)
    attribute = {"apk_hash": "APK_SHA256", "elf_hash": "ELF_SHA256", "pem_hash": "PUBLIC_PEM_SHA256",
                 "rule_hash": "BUILTIN_RULE_SHA256", "apk_size": "APK_MAX_BYTES"}[changed]
    monkeypatch.setattr(mod, attribute, 1 if changed == "apk_size" else "0" * 64)
    rejected(mod, path)


@pytest.mark.parametrize("names,size", [((MEMBER, MEMBER), None), (("other.so",), None),
                                        ((MEMBER,), ELF_SIZE + 1)])
def test_member_must_be_unique_exact_and_bounded(tmp_path, monkeypatch, materials, names, size):
    mod, path = archive(tmp_path, monkeypatch, materials, names=names, declared_size=size)
    rejected(mod, path)


@pytest.mark.parametrize("changed", ["class", "endianness", "machine", "program_bounds", "file_bounds",
                                     "filesz_gt_memsz", "writable", "unmapped", "ambiguous"])
def test_only_strict_readonly_elf64_load_mapping_can_supply_constants(tmp_path, monkeypatch, materials, changed):
    data = synthetic_elf(materials)
    if changed == "class":
        data[4] = 1
    elif changed == "endianness":
        data[5] = 2
    elif changed == "machine":
        struct.pack_into("<H", data, 18, 62)
    elif changed == "program_bounds":
        struct.pack_into("<Q", data, 32, ELF_SIZE - 1)
    elif changed == "file_bounds":
        struct.pack_into("<Q", data, 72, ELF_SIZE - 1)
    elif changed == "filesz_gt_memsz":
        struct.pack_into("<Q", data, 104, 1)
    elif changed == "writable":
        struct.pack_into("<I", data, 68, 6)
    elif changed == "unmapped":
        struct.pack_into("<Q", data, 80, 0xbb000)
    elif changed == "ambiguous":
        struct.pack_into("<H", data, 56, 2)
        data[120:176] = data[64:120]
    mod, path = archive(tmp_path, monkeypatch, materials, data)
    rejected(mod, path)


@pytest.mark.parametrize("changed", ["early_nul", "missing_terminator", "constant_crosses_segment"])
def test_constants_require_exact_length_terminator_and_load_boundary(tmp_path, monkeypatch, materials, changed):
    data = synthetic_elf(materials)
    offset = 4096 + PEM_VA - 0xaa000
    if changed == "early_nul":
        data[offset + 10] = 0
    elif changed == "missing_terminator":
        data[offset + len(materials[0])] = 1
    else:
        struct.pack_into("<Q", data, 96, PEM_VA - 0xaa000 + len(materials[0]))
    mod, path = archive(tmp_path, monkeypatch, materials, data)
    rejected(mod, path)


def test_missing_or_unreadable_archive_has_no_path_in_exception(tmp_path):
    rejected(module(), tmp_path / "synthetic-private-missing.apk")
