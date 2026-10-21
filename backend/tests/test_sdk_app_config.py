import dataclasses
import hashlib
import importlib
import importlib.util
import io
import struct
import warnings
import zipfile

import pytest
from pydantic import SecretStr


ARSC_MEMBER = "resources.arsc"
DEX_MEMBER = "classes7.dex"
ARSC_SIZE = 1022340
DEX_SIZE = 2217836
APP_KEY = "synthetic-key-0123456789abcdef00"
RESOURCE_FIELDS = {
    "merchant_id": (0x21811, b"7"),
    "app_id": (0x21858, b"12345"),
    "server_id": (0x21850, b"54321"),
    "app_key": (0x217ee, APP_KEY.encode()),
}
DEX_STRING_TABLE = 0x70
DEX_STRING_DATA = 0x1000


def module():
    assert importlib.util.find_spec("rumi_market.sdk_app_config") is not None, "SDK app config loader is missing"
    return importlib.import_module("rumi_market.sdk_app_config")


def synthetic_members():
    arsc = bytearray(ARSC_SIZE)
    for offset, value in RESOURCE_FIELDS.values():
        arsc[offset - 2:offset] = bytes([len(value), len(value)])
        arsc[offset:offset + len(value) + 1] = value + b"\x00"
    dex = bytearray(DEX_SIZE)
    dex[:8] = b"dex\n038\x00"
    struct.pack_into("<III", dex, 0x20, DEX_SIZE, 0x70, 0x12345678)
    struct.pack_into("<II", dex, 0x38, 503, DEX_STRING_TABLE)
    struct.pack_into("<I", dex, DEX_STRING_TABLE + 4 * 502, DEX_STRING_DATA)
    dex[DEX_STRING_DATA:DEX_STRING_DATA + 8] = b"\x063.2.10\x00"
    return arsc, dex


def archive(tmp_path, monkeypatch, *, arsc=None, dex=None, names=(ARSC_MEMBER, DEX_MEMBER), altered=None):
    mod = module()
    original_arsc, original_dex = synthetic_members()
    arsc = original_arsc if arsc is None else arsc
    dex = original_dex if dex is None else dex
    contents = {ARSC_MEMBER: bytes(arsc), DEX_MEMBER: bytes(dex)}
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name in names:
                target.writestr(name, contents.get(name, b"irrelevant"))
    raw = bytearray(output.getvalue())
    if altered:
        selected, changed = altered
        offset = raw.index(b"PK\x01\x02")
        for name in names:
            filename_size, extra_size, comment_size = struct.unpack_from("<HHH", raw, offset + 28)
            if name == selected:
                if changed == "size":
                    struct.pack_into("<I", raw, offset + 24, len(contents[name]) + 1)
                elif changed == "encrypted":
                    struct.pack_into("<H", raw, offset + 8, 1)
                elif changed == "compression":
                    struct.pack_into("<H", raw, offset + 10, zipfile.ZIP_BZIP2)
                elif changed == "compressed_size":
                    struct.pack_into("<I", raw, offset + 20, len(contents[name]) + 1)
                break
            offset += 46 + filename_size + extra_size + comment_size
    path = tmp_path / "synthetic-private-name.apk"
    path.write_bytes(raw)
    monkeypatch.setattr(mod, "APK_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(mod, "ARSC_SHA256", hashlib.sha256(arsc).hexdigest())
    monkeypatch.setattr(mod, "DEX_SHA256", hashlib.sha256(dex).hexdigest())
    return mod, path


def rejected(mod, path):
    with pytest.raises(ValueError, match="^Invalid SDK app config$") as failure:
        mod.load_sdk_app_config(path)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert "private" not in str(failure.value)
    assert APP_KEY not in str(failure.value)


def test_source_contract_is_bound_to_same_apk_as_native_assets():
    from rumi_market.sdk_assets import APK_SHA256
    mod = module()
    assert mod.APK_SHA256 == APK_SHA256
    assert mod.ARSC_SIZE == ARSC_SIZE and mod.DEX_SIZE == DEX_SIZE
    assert mod.ARSC_SHA256 == "430175ff456d729c15b86640bc7a4a47eddce7f687117587b15bda753c66e397"
    assert mod.DEX_SHA256 == "526775d838e8bd2105632d8d6fca4dca6cd9c3523e5008bde9aa14ea933b1383"


def test_verified_archive_returns_fields_and_secret_is_not_in_repr(tmp_path, monkeypatch):
    mod, path = archive(tmp_path, monkeypatch)
    value = mod.load_sdk_app_config(path)
    assert (value.merchant_id, value.app_id, value.server_id, value.sdk_ver) == ("7", "12345", "54321", "3.2.10")
    assert (value.app_ver, value.version_code) == ("1.5.3", "42")
    assert isinstance(value.app_key, SecretStr)
    assert value.app_key.get_secret_value() == APP_KEY
    assert APP_KEY not in repr(value) and APP_KEY not in str(value.app_key)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.app_id = "changed"


@pytest.mark.parametrize("attribute", ["APK_SHA256", "ARSC_SHA256", "DEX_SHA256", "APK_MAX_BYTES"])
def test_untrusted_archive_or_member_hash_is_rejected(tmp_path, monkeypatch, attribute):
    mod, path = archive(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, attribute, 1 if attribute == "APK_MAX_BYTES" else "0" * 64)
    rejected(mod, path)


@pytest.mark.parametrize("names", [
    (ARSC_MEMBER,), (DEX_MEMBER,), (ARSC_MEMBER, ARSC_MEMBER, DEX_MEMBER),
    (ARSC_MEMBER, DEX_MEMBER, DEX_MEMBER),
])
def test_each_target_member_must_be_present_exactly_once(tmp_path, monkeypatch, names):
    mod, path = archive(tmp_path, monkeypatch, names=names)
    rejected(mod, path)


@pytest.mark.parametrize("member", [ARSC_MEMBER, DEX_MEMBER])
@pytest.mark.parametrize("changed", ["size", "encrypted", "compression", "compressed_size"])
def test_zip_member_metadata_must_be_exact_and_bounded(tmp_path, monkeypatch, member, changed):
    mod, path = archive(tmp_path, monkeypatch, altered=(member, changed))
    rejected(mod, path)


@pytest.mark.parametrize("field", list(RESOURCE_FIELDS))
@pytest.mark.parametrize("changed", ["early_nul", "non_ascii", "missing_terminator"])
def test_resource_strings_require_exact_ascii_and_terminator(tmp_path, monkeypatch, field, changed):
    arsc, dex = synthetic_members()
    offset, value = RESOURCE_FIELDS[field]
    if changed == "early_nul":
        arsc[offset] = 0
    elif changed == "non_ascii":
        arsc[offset] = 0xff
    else:
        arsc[offset + len(value)] = 1
    mod, path = archive(tmp_path, monkeypatch, arsc=arsc, dex=dex)
    rejected(mod, path)


@pytest.mark.parametrize("changed", [
    "magic", "file_size", "header_size", "endianness", "missing_string_id", "table_before_header",
    "table_out_of_bounds", "data_out_of_bounds", "uleb_overflow", "wrong_utf16_length", "non_ascii",
    "early_nul", "missing_terminator",
])
def test_sdk_version_requires_bounded_dex_string_id_502(tmp_path, monkeypatch, changed):
    arsc, dex = synthetic_members()
    if changed == "magic":
        dex[0] = 0
    elif changed == "file_size":
        struct.pack_into("<I", dex, 0x20, DEX_SIZE - 1)
    elif changed == "header_size":
        struct.pack_into("<I", dex, 0x24, 0x6f)
    elif changed == "endianness":
        struct.pack_into("<I", dex, 0x28, 0x78563412)
    elif changed == "missing_string_id":
        struct.pack_into("<I", dex, 0x38, 502)
    elif changed == "table_before_header":
        struct.pack_into("<I", dex, 0x3c, 0x60)
    elif changed == "table_out_of_bounds":
        struct.pack_into("<I", dex, 0x3c, DEX_SIZE - 4)
    elif changed == "data_out_of_bounds":
        struct.pack_into("<I", dex, DEX_STRING_TABLE + 4 * 502, DEX_SIZE)
    elif changed == "uleb_overflow":
        dex[DEX_STRING_DATA:DEX_STRING_DATA + 5] = b"\x80" * 5
    elif changed == "wrong_utf16_length":
        dex[DEX_STRING_DATA] = 5
    elif changed == "non_ascii":
        dex[DEX_STRING_DATA + 1] = 0xff
    elif changed == "early_nul":
        dex[DEX_STRING_DATA + 1] = 0
    else:
        dex[DEX_STRING_DATA + 7] = 1
    mod, path = archive(tmp_path, monkeypatch, arsc=arsc, dex=dex)
    rejected(mod, path)


def test_missing_or_unreadable_archive_has_no_path_in_exception(tmp_path):
    rejected(module(), tmp_path / "synthetic-private-missing.apk")
