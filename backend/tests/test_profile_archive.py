"""Synthetic-only contracts for private account profile snapshots."""

import importlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


def _module():
    try:
        return importlib.import_module("rumi_market.profile_archive")
    except ModuleNotFoundError:
        pytest.fail("The private account profile archive utility is not implemented")


@pytest.fixture
def profile_message():
    file = descriptor_pb2.FileDescriptorProto(name="archive_fixture.proto", package="archive_test", syntax="proto2")
    nested = file.message_type.add(name="Detail")
    nested.field.add(name="coins", number=1, label=1, type=3)
    nested.field.add(name="refresh_token", number=2, label=1, type=9)
    nested.field.add(name="child", number=3, label=1, type=11, type_name=".archive_test.Detail")
    message = file.message_type.add(name="Profile")
    fields = [
        ("name", 1, 9, 1), ("zero", 2, 5, 1), ("absent", 3, 5, 1),
        ("role_id", 4, 4, 1), ("signed_id", 5, 3, 1),
        ("fixed_id", 6, 6, 1), ("signed_fixed_id", 7, 16, 1),
        ("zigzag_id", 8, 18, 1), ("identifiers", 9, 4, 3),
        ("payload", 10, 12, 1), ("password", 11, 9, 1),
        ("accessToken", 12, 9, 1), ("sign", 13, 9, 1),
        ("designation", 14, 9, 1),
    ]
    for name, number, type_, label in fields:
        message.field.add(name=name, number=number, type=type_, label=label)
    message.field.add(name="detail", number=15, type=11, label=1, type_name=".archive_test.Detail")
    message.field.add(name="history", number=16, type=11, label=3, type_name=".archive_test.Detail")
    mapping = message.nested_type.add(name="CountersEntry")
    mapping.options.map_entry = True
    mapping.field.add(name="key", number=1, type=9, label=1)
    mapping.field.add(name="value", number=2, type=4, label=1)
    message.field.add(name="counters", number=17, type=11, label=3, type_name=".archive_test.Profile.CountersEntry")
    message_mapping = message.nested_type.add(name="DetailsEntry")
    message_mapping.options.map_entry = True
    message_mapping.field.add(name="key", number=1, type=5, label=1)
    message_mapping.field.add(name="value", number=2, type=11, label=1, type_name=".archive_test.Detail")
    message.field.add(name="details", number=18, type=11, label=3, type_name=".archive_test.Profile.DetailsEntry")
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("archive_test.Profile"))


def test_exports_only_present_fields_and_preserves_zero(profile_message):
    profile = profile_message(name="synthetic", zero=0)
    assert _module().export_profile_message(profile) == {"name": "synthetic", "zero": 0}


def test_all_64_bit_wire_integer_types_export_as_decimal_strings(profile_message):
    profile = profile_message(role_id=2**64 - 1, signed_id=-(2**63), fixed_id=2**63,
                              signed_fixed_id=0, zigzag_id=-5, identifiers=[0, 2**64 - 1])
    assert _module().export_profile_message(profile) == {
        "role_id": str(2**64 - 1), "signed_id": str(-(2**63)), "fixed_id": str(2**63),
        "signed_fixed_id": "0", "zigzag_id": "-5", "identifiers": ["0", str(2**64 - 1)],
    }


def test_exports_nested_repeated_maps_and_omits_secrets_and_binary(profile_message):
    profile = profile_message(password="fixture-password", accessToken="fixture-token", sign="fixture-signature",
                              designation="ordinary title", payload=b"fixture-binary")
    profile.detail.coins = 0
    profile.detail.refresh_token = "fixture-nested-token"
    profile.history.add(coins=9, refresh_token="fixture-history-token")
    profile.counters["wins"] = 2**63
    profile.details[5].coins = 4
    profile.details[5].refresh_token = "fixture-map-token"
    assert _module().export_profile_message(profile) == {
        "designation": "ordinary title", "payload": {"omitted": True, "bytes": 14},
        "detail": {"coins": "0"}, "history": [{"coins": "9"}],
        "counters": {"wins": str(2**63)}, "details": {"5": {"coins": "4"}},
    }


@pytest.mark.parametrize("value", [{}, None, b"wire", object()])
def test_proto_export_rejects_non_messages(value):
    module = _module()
    with pytest.raises(module.ProfileArchiveError, match="PROFILE_ARCHIVE_MESSAGE_INVALID"):
        module.export_profile_message(value)


@pytest.mark.parametrize("kind", ["repeated_scalar", "map_scalar", "repeated_message", "map_message"])
def test_proto_export_bounds_each_collection_with_one_node_budget(profile_message, monkeypatch, kind):
    module = _module()
    monkeypatch.setattr(module, "_MAX_PROFILE_EXPORT_NODES", 7, raising=False)
    profile = profile_message()
    if kind == "repeated_scalar":
        profile.identifiers.extend(range(8))
    elif kind == "map_scalar":
        profile.counters.update({str(index): index for index in range(4)})
    elif kind == "repeated_message":
        for index in range(4):
            profile.history.add(coins=index)
    else:
        for index in range(3):
            profile.details[index].coins = index
    with pytest.raises(module.ProfileArchiveError, match="^PROFILE_ARCHIVE_EXPORT_LIMIT$"):
        module.export_profile_message(profile)


def test_proto_export_shares_budget_between_sibling_fields(profile_message, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_MAX_PROFILE_EXPORT_NODES", 10, raising=False)
    profile = profile_message(identifiers=[1, 2, 3])
    profile.history.add(coins=4)
    profile.history.add(coins=5)
    profile.counters["wins"] = 6
    # Each collection fits on its own, but the combined traversal must not get
    # a fresh budget for every field, nested message or map value.
    with pytest.raises(module.ProfileArchiveError, match="^PROFILE_ARCHIVE_EXPORT_LIMIT$"):
        module.export_profile_message(profile)


def test_proto_export_rejects_excessive_nested_message_depth(profile_message, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_MAX_PROFILE_EXPORT_DEPTH", 5, raising=False)
    profile = profile_message()
    current = profile.detail
    for _ in range(8):
        current = current.child
    current.coins = 1
    with pytest.raises(module.ProfileArchiveError, match="^PROFILE_ARCHIVE_EXPORT_LIMIT$"):
        module.export_profile_message(profile)


def test_proto_export_accepts_budget_boundary_and_resets_between_calls(profile_message, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_MAX_PROFILE_EXPORT_NODES", 4, raising=False)
    profile = profile_message(identifiers=[0, 1])
    assert module.export_profile_message(profile) == {"identifiers": ["0", "1"]}
    assert module.export_profile_message(profile) == {"identifiers": ["0", "1"]}
    profile.identifiers.append(2)
    with pytest.raises(module.ProfileArchiveError, match="^PROFILE_ARCHIVE_EXPORT_LIMIT$"):
        module.export_profile_message(profile)
    profile.identifiers.pop()
    assert module.export_profile_message(profile) == {"identifiers": ["0", "1"]}


def test_proto_export_counts_filtered_map_keys_towards_traversal_budget(profile_message, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_MAX_PROFILE_EXPORT_NODES", 4, raising=False)
    profile = profile_message()
    profile.counters.update({"token" + str(index): index for index in range(5)})
    with pytest.raises(module.ProfileArchiveError, match="^PROFILE_ARCHIVE_EXPORT_LIMIT$"):
        module.export_profile_message(profile)


def test_private_archive_is_utf8_and_keeps_accounts_separate(tmp_path):
    module = _module()
    root = tmp_path / "profiles"
    archive = module.AccountProfileArchive(root)
    first = archive.save(424242, 3, {"name": "合成角色", "coins": "123"})
    second = archive.save(424243, 3, {"name": "other"})
    other_zone = archive.save(424242, 4, {"name": "another zone"})
    assert len({first.parent, second.parent, other_zone.parent}) == 3
    assert all(path.is_relative_to(root) for path in (first, second, other_zone))
    assert "424242" not in str(first.relative_to(root))
    assert "合成角色" in first.read_text(encoding="utf-8")
    assert json.loads(first.read_text(encoding="utf-8")) == {"name": "合成角色", "coins": "123"}
    if os.name != "nt":
        assert root.stat().st_mode & 0o777 == 0o700
        assert first.parent.stat().st_mode & 0o777 == 0o700
        assert first.stat().st_mode & 0o777 == 0o600


def test_same_archive_id_atomically_updates_one_login_snapshot(tmp_path):
    module = _module()
    archive = module.AccountProfileArchive(tmp_path / "profiles")
    first = archive.save(101, None, {"messages": [{"number": 1}]})
    updated = archive.save(101, None, {"messages": [{"number": 1}, {"number": 2}]}, archive_id=first.stem)
    assert updated == first
    assert len(list(first.parent.iterdir())) == 1
    assert json.loads(updated.read_text(encoding="utf-8"))["messages"] == [{"number": 1}, {"number": 2}]


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_archive_acl_allows_only_current_sid_and_system(tmp_path):
    module = _module()
    root = tmp_path / "profiles"
    result = module.AccountProfileArchive(root).save(101, 1, {"name": "synthetic"})
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::WriteLine([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
$rootAcl = [System.IO.Directory]::GetAccessControl($env:RUMI_PROFILE_TEST_ROOT)
[Console]::WriteLine($rootAcl.AreAccessRulesProtected)
foreach ($path in @($env:RUMI_PROFILE_TEST_ROOT, $env:RUMI_PROFILE_TEST_ACCOUNT, $env:RUMI_PROFILE_TEST_FILE)) {
    if ([System.IO.File]::Exists($path)) { $acl = [System.IO.File]::GetAccessControl($path) }
    else { $acl = [System.IO.Directory]::GetAccessControl($path) }
    $rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
    foreach ($rule in $rules) { [Console]::WriteLine($rule.IdentityReference.Value) }
    [Console]::WriteLine('END')
}
"""
    environment = dict(os.environ, RUMI_PROFILE_TEST_ROOT=str(root),
                       RUMI_PROFILE_TEST_ACCOUNT=str(result.parent), RUMI_PROFILE_TEST_FILE=str(result))
    completed = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                               env=environment, capture_output=True, check=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    lines = completed.stdout.decode("ascii").splitlines()
    current_sid = lines[0]
    assert lines[1] == "True"
    assert lines[2:] == [current_sid, "S-1-5-18", "END"] * 3 or lines[2:] == ["S-1-5-18", current_sid, "END"] * 3


@pytest.mark.parametrize("root", [None, object(), 4, "\x00invalid"])
def test_invalid_root_constructor_uses_only_safe_error_code(root):
    module = _module()
    with pytest.raises(module.ProfileArchiveError, match="^PROFILE_ARCHIVE_PATH_INVALID$"):
        module.AccountProfileArchive(root)


def test_save_sanitizes_nested_dictionary_keys_and_bytes(tmp_path):
    module = _module()
    archive = module.AccountProfileArchive(tmp_path / "profiles")
    document = {
        "profile": {"nick": "synthetic", "Password": "fixture"},
        "rows": [{"refresh-token": "fixture", "coins": 0}],
        "session_info": "fixture", "cd_key": "fixture", "appKey": "fixture",
        "raw": b"binary", "designation": "kept", "sign": "fixture",
    }
    result = archive.save(101, 1, document)
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "profile": {"nick": "synthetic"}, "rows": [{"coins": 0}],
        "raw": {"omitted": True, "bytes": 6}, "designation": "kept",
    }
    assert document["profile"]["Password"] == "fixture"


@pytest.mark.parametrize("name", [
    "PASSWORD", "pass-wd", "pwd", "accessToken", "secretKey", "credential",
    "captcha", "cookie", "authorization", "session_info", "cd_key",
    "accessKey", "refreshKey", "privateKey", "appKey", "signature", "sign",
])
def test_sensitive_field_variants_never_reach_disk(tmp_path, name):
    module = _module()
    result = module.AccountProfileArchive(tmp_path / "profiles").save(102, None, {name: "fixture", "level": 1})
    assert json.loads(result.read_text(encoding="utf-8")) == {"level": 1}


@pytest.mark.parametrize("role,zone", [
    (True, None), (0, None), (-1, None), (2**64, None), ("101", None),
    (101, True), (101, -1), (101, 2**31), (101, "2"),
])
def test_rejects_invalid_identity_without_creating_paths(tmp_path, role, zone):
    module = _module()
    root = tmp_path / "profiles"
    with pytest.raises(module.ProfileArchiveError, match="PROFILE_ARCHIVE_IDENTITY_INVALID"):
        module.AccountProfileArchive(root).save(role, zone, {})
    assert not root.exists()


@pytest.mark.parametrize("archive_id", ["../escape", "../" + "a" * 32, "a/b", "C:\\outside", "", "a" * 31,
                                        "A" * 32, "a" * 32 + ".json", 1, True])
def test_rejects_archive_path_escape_before_creating_paths(tmp_path, archive_id):
    module = _module()
    root = tmp_path / "profiles"
    with pytest.raises(module.ProfileArchiveError, match="PROFILE_ARCHIVE_ID_INVALID"):
        module.AccountProfileArchive(root).save(101, 1, {}, archive_id=archive_id)
    assert not root.exists()


def test_atomic_replace_failure_preserves_old_file_and_cleans_own_temporary_file(tmp_path, monkeypatch):
    module = _module()
    archive = module.AccountProfileArchive(tmp_path / "profiles")
    original = archive.save(101, 1, {"version": 1})
    unrelated = original.parent / "keep.tmp"
    unrelated.write_text("fixture", encoding="utf-8")
    def fail_replace(source, destination):
        raise OSError("fixture low-level details")
    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(module.ProfileArchiveError, match="^PROFILE_ARCHIVE_WRITE_FAILED$"):
        archive.save(101, 1, {"version": 2}, archive_id=original.stem)
    assert json.loads(original.read_text(encoding="utf-8")) == {"version": 1}
    assert sorted(path.name for path in original.parent.iterdir()) == sorted([original.name, "keep.tmp"])


@pytest.mark.parametrize("document", [[], {"odd": object()}, {"invalid": float("nan")}, {1: "bad key"}])
def test_unknown_or_non_json_values_are_not_formatted_or_written(tmp_path, document):
    module = _module()
    root = tmp_path / "profiles"
    with pytest.raises(module.ProfileArchiveError, match="PROFILE_ARCHIVE_DOCUMENT_INVALID"):
        module.AccountProfileArchive(root).save(101, 1, document)
    assert not root.exists()


def test_linked_account_directory_is_rejected_without_writing_outside_root(tmp_path):
    module = _module()
    archive = module.AccountProfileArchive(tmp_path / "profiles")
    first = archive.save(101, 1, {"version": 1})
    first.unlink()
    first.parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        first.parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation unavailable for this Windows account")
    with pytest.raises(module.ProfileArchiveError, match="PROFILE_ARCHIVE_PATH_INVALID"):
        archive.save(101, 1, {"version": 2})
    assert list(outside.iterdir()) == []
