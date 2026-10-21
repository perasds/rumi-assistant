import hashlib
import importlib
import importlib.util
import io
import struct
import warnings
import zipfile

import pytest
from pydantic import SecretStr


NATIVE = "lib/arm64-v8a/libil2cpp.so"
METADATA = "assets/bin/Data/Managed/Metadata/global-metadata.dat"
PLAYER = "assets/bin/Data/globalgamemanagers"
SCENE = "assets/bin/Data/level0"
ORIGINS = (
    "https://l1-prod-cn-ob-vs-lumigo.bilibiligame.net",
    "https://l3-prod-cn-ob-vs-lumigo.bilibiligame.net",
    "https://lumigo-cn-prod-vs.bilibiligame.net",
)
SHORT_CIPHER = bytes.fromhex(
    "485450584800000063d35d2bdab8a2a0100469902013bb9a0e7466323dc8501c"
    "b11758f241913affa25ab27b1ccd1b53b81d1eb3931b841489892c35783cf3af39f8463d92312ada")
SHORT_BODY_SHA = "cadef29e6a077bcfeceae2dd4789eab6e3629abfa9057231cc4947d07064d757"


def module():
    assert importlib.util.find_spec("rumi_market.game.version_assets") is not None
    return importlib.import_module("rumi_market.game.version_assets")


def synthetic_native():
    data = bytearray(4096 + 1048576)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 3, 183, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 4096, 0x4700000, 0x4700000,
                     1048576, 1048576, 4096)

    def write(address, raw):
        start = 4096 + address - 0x4700000
        data[start:start + len(raw)] = raw

    write(74638120, struct.pack("<II", 0xedb88320, 0))
    permutation = (74626704, 74626768, 74626384, 74626720, 74626800, 74626752,
                   74626480, 74626368, 74626656, 74626624, 74626592, 74626640,
                   74626496, 74626512, 74626336, 74626400)
    for index, address in enumerate(permutation):
        write(address, bytes(range(index * 16, index * 16 + 16)))
    for first, second, value in ((0x47874f4, 0x47874f8, 0),
        (0x4787944, 0x4787954, 5), (0x4787980, 0x4787988, 6),
        (0x478794c, 0x47879a4, 7), (0x47879d8, 0x47879dc, 8)):
        write(first, struct.pack("<I", 0x52800000 | ((value & 65535) << 5)))
        write(second, struct.pack("<I", 0x72a00000 | ((value >> 16) << 5)))
    for first, second, value in ((0x4787984, 0x478798c, 1), (0x4787958, 0x4787964, 2),
        (0x4787968, 0x4787970, 3), (0x4787960, 0x478796c, 4)):
        write(first, struct.pack("<I", 0x11000000 | (value << 10)))
        write(second, struct.pack("<I", 0x11000000))
    return bytes(data)


def synthetic_members():
    player = bytearray(398308)
    struct.pack_into("<I", player, 5568 + 608, 5)
    player[5568 + 612:5568 + 617] = b"1.5.3"
    scene = bytearray(85164)
    start = 57352
    struct.pack_into("<I", scene, start + 44, 7)
    scene[start + 48:start + 55] = b"RELEASE"
    struct.pack_into("<III", scene, start + 56, 1, 0, 2)
    struct.pack_into("<I", scene, start + 68, 2)
    scene[start + 72:start + 74] = b"17"
    urls = ";".join(ORIGINS).encode()
    struct.pack_into("<I", scene, start + 76, len(urls))
    scene[start + 80:start + 80 + len(urls)] = urls
    return {NATIVE: synthetic_native(), METADATA: SHORT_CIPHER,
            PLAYER: bytes(player), SCENE: bytes(scene)}


def archive(tmp_path, monkeypatch, *, changes=None, names=None):
    mod = module()
    members = synthetic_members()
    members.update(changes or {})
    target = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name in names or members:
                z.writestr(name, members[name], compress_type=(
                    zipfile.ZIP_STORED if name == METADATA else zipfile.ZIP_DEFLATED))
    path = tmp_path / "synthetic-private-path.apk"
    raw = target.getvalue()
    path.write_bytes(raw)
    monkeypatch.setattr(mod, "APK_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(mod, "MEMBER_PINS", {
        name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in members.items()})
    monkeypatch.setattr(mod, "PLAYER_WINDOW", (5568, 756, hashlib.sha256(members[PLAYER][5568:6324]).hexdigest()))
    monkeypatch.setattr(mod, "ROOT_WINDOW", (57352, 240, hashlib.sha256(members[SCENE][57352:57592]).hexdigest()))
    monkeypatch.setattr(mod, "METADATA_BODY_SHA256", SHORT_BODY_SHA)
    monkeypatch.setattr(mod, "SECRET_LITERAL_INDEX", 0)
    return mod, path


def rejected(mod, path):
    with pytest.raises(ValueError, match="^Invalid game version assets$") as caught:
        mod.load_version_assets(path)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert "synthetic" not in str(caught.value)


def test_pinned_loader_returns_secretstr_and_actual_public_boot_fields(tmp_path, monkeypatch):
    mod, path = archive(tmp_path, monkeypatch)
    result = mod.load_version_assets(path)
    assert type(result.gm_secret) is SecretStr
    assert result.gm_secret.get_secret_value() == "synthetic-vsc-secret"
    assert "synthetic-vsc-secret" not in repr(result)
    assert result.origins == ORIGINS
    assert (result.app_id, result.channel, result.version, result.build_version) == (
        "1000069", "Android_CN_CDN", "1.5.3", "17")
    assert result.profile_source and not hasattr(result, "metadata")


def test_long_htpx_known_vector_covers_blocks_and_tail_without_native_execution():
    mod = module()
    encoded = b"HTPX" + struct.pack("<I", 1295) + bytes(i % 256 for i in range(1287))
    result = mod._recover_body(encoded, synthetic_native())
    assert hashlib.sha256(result).hexdigest() == "e853978f2c418cd80cca7305b6af6e3467555a6889d37d69444301847db96022"


@pytest.mark.parametrize("changed", ["apk_hash", "apk_size", "member_hash", "body_hash", "object_hash"])
def test_identity_hashes_and_sizes_fail_closed(tmp_path, monkeypatch, changed):
    mod, path = archive(tmp_path, monkeypatch)
    if changed == "apk_hash":
        monkeypatch.setattr(mod, "APK_SHA256", "0" * 64)
    elif changed == "apk_size":
        monkeypatch.setattr(mod, "APK_MAX_BYTES", 1)
    elif changed == "member_hash":
        monkeypatch.setattr(mod, "MEMBER_PINS", mod.MEMBER_PINS | {METADATA: (72, "0" * 64)})
    elif changed == "body_hash":
        monkeypatch.setattr(mod, "METADATA_BODY_SHA256", "0" * 64)
    else:
        monkeypatch.setattr(mod, "ROOT_WINDOW", (57352, 240, "0" * 64))
    rejected(mod, path)


@pytest.mark.parametrize("names", [
    (NATIVE, METADATA, PLAYER), (NATIVE, METADATA, PLAYER, SCENE, METADATA),
])
def test_required_members_are_unique(tmp_path, monkeypatch, names):
    mod, path = archive(tmp_path, monkeypatch, names=names)
    rejected(mod, path)


@pytest.mark.parametrize("changed", ["magic", "length", "mov_opcode", "writable"])
def test_native_operands_and_htpx_envelope_are_checked(tmp_path, monkeypatch, changed):
    native = bytearray(synthetic_native())
    metadata = bytearray(SHORT_CIPHER)
    if changed == "magic":
        metadata[0] = 0
    elif changed == "length":
        struct.pack_into("<I", metadata, 4, 71)
    elif changed == "mov_opcode":
        struct.pack_into("<I", native, 4096 + 0x47874f4 - 0x4700000, 0)
    else:
        struct.pack_into("<I", native, 68, 7)
    mod, path = archive(tmp_path, monkeypatch, changes={NATIVE: bytes(native), METADATA: bytes(metadata)})
    rejected(mod, path)


@pytest.mark.parametrize("pair,value", [(0, 0), (1, 7), (2, 999), (3, 999)])
def test_literal_table_requires_exact_bounds(pair, value):
    mod = module()
    body = bytearray(64)
    struct.pack_into("<4I", body, 0, 24, 8, 32, 20)
    struct.pack_into("<2I", body, 16, 20, 0)
    body[24:44] = b"synthetic-vsc-secret"
    struct.pack_into("<I", body, pair * 4, value)
    with pytest.raises(ValueError):
        mod._literal(bytes(body), 0)


@pytest.mark.parametrize("changed", ["application_version", "package", "sdk", "update", "origin"])
def test_unknown_boot_profile_is_not_guessed(tmp_path, monkeypatch, changed):
    members = synthetic_members()
    player, scene = bytearray(members[PLAYER]), bytearray(members[SCENE])
    if changed == "application_version":
        player[5568 + 612:5568 + 617] = b"9.9.9"
    elif changed == "package":
        scene[57352 + 48:57352 + 55] = b"UNKNOWN"
    elif changed == "sdk":
        struct.pack_into("<I", scene, 57352 + 60, 1)
    elif changed == "update":
        struct.pack_into("<I", scene, 57352 + 64, 0)
    else:
        scene[57352 + 80] = ord("x")
    mod, path = archive(tmp_path, monkeypatch, changes={PLAYER: bytes(player), SCENE: bytes(scene)})
    rejected(mod, path)


def test_unreadable_apk_does_not_leak_local_path(tmp_path):
    rejected(module(), tmp_path / "synthetic-private-missing.apk")
