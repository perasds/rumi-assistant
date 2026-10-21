"""Synthetic-only login notification collection and local snapshot boundaries."""

from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
import pytest

from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.proto import GameProtoCodec


def implementation():
    try:
        return importlib.import_module("rumi_market.game.profile_sync")
    except ModuleNotFoundError:
        pytest.fail("Login profile collector is not implemented")


def message(message_name, **fields):
    source = Path(__file__).parents[1] / "src/rumi_market/game/profile.descriptor.pb"
    descriptors = descriptor_pb2.FileDescriptorSet.FromString(source.read_bytes())
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
    pending = {file.name: file for file in descriptors.file}
    known = {"google/protobuf/descriptor.proto"}
    while pending:
        ready = [name for name, file in pending.items() if set(file.dependency) <= known]
        assert ready
        for item in ready:
            pool.AddSerializedFile(pending.pop(item).SerializeToString())
            known.add(item)
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("SNKProto." + message_name))
    return cls(**fields)


def response(command, data=b"", *, code=0, notify=1):
    return ResponseEnvelope(command, 0, notify, code, 0, data)


class MemoryArchive:
    def __init__(self, *, failure=None):
        self.calls = []
        self.failure = failure

    def save(self, role_id, zone_id, document, *, archive_id=None):
        if self.failure is not None:
            raise self.failure
        self.calls.append((role_id, zone_id, deepcopy(document)))
        return Path("synthetic-private-profile.json")


def finish(collector):
    collector.finalize(SimpleNamespace(role_id=2**63 + 7, zone_id=3))


def test_known_profile_keeps_presence_zero_and_64bit_strings():
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    payload = message("Cmd_Notify_RoleBase", role_id=2**63 + 7, name="合成角色", level=0)
    collector.observe(response(2931, payload.SerializeToString()))
    finish(collector)
    assert collector.archive_status == "saved"
    assert collector.archive_path == Path("synthetic-private-profile.json")
    role_id, zone_id, document = archive.calls[0]
    assert (role_id, zone_id) == (2**63 + 7, 3)
    assert document["source"] == "game_server"
    assert document["identity"] == {"role_id": str(role_id), "zone_id": zone_id}
    assert document["messages"][0]["fields"] == {
        "role_id": str(role_id), "name": "合成角色", "level": 0,
    }
    assert document["coverage"]["all_personal_information_decoded"] is False


def test_unknown_nonzero_and_invalid_messages_keep_metadata_without_wire_values():
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    private = b"synthetic-private-notification"
    collector.observe(response(6553, private))
    collector.observe(response(2931, private, code=-7))
    collector.observe(response(2931, b"\xff"))
    finish(collector)
    document = archive.calls[0][2]
    assert [item["status"] for item in document["messages"]] == [
        "unknown_command", "upstream_error", "decode_failed",
    ]
    assert [item["bytes"] for item in document["messages"]] == [len(private), len(private), 1]
    assert private.decode() not in json.dumps(document)
    assert all("fields" not in item for item in document["messages"])


def test_combined_profile_units_are_decoded_only_by_manifest_whitelist():
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    role = message("Cmd_Notify_RoleBase", name="合成组合角色")
    combine = message("Cmd_Notify_Combine_Info", unit_lst=[
        {"id": 2931, "msg": role.SerializeToString()},
        {"id": 6553, "msg": b"synthetic-unknown-child"},
    ])
    collector.observe(response(2016, combine.SerializeToString()))
    finish(collector)
    rows = archive.calls[0][2]["messages"]
    assert [row["command"] for row in rows] == [2016, 2931, 6553]
    assert rows[1]["fields"] == {"name": "合成组合角色"}
    assert rows[1]["parent_command"] == 2016
    assert rows[2]["status"] == "unknown_command"
    assert "synthetic-unknown-child" not in json.dumps(rows)
    assert rows[0]["fields"]["unit_lst"][0]["msg"]["omitted"] is True


def test_combined_depth_and_message_budgets_are_bounded():
    archive = MemoryArchive()
    module = implementation()
    collector = module.LoginProfileCollector(archive)
    data = message("Cmd_Notify_RoleBase", name="synthetic-deep").SerializeToString()
    for _ in range(module.MAX_COMBINE_DEPTH + 3):
        data = message("Cmd_Notify_Combine_Info", unit_lst=[{"id": 2016, "msg": data}]).SerializeToString()
    collector.observe(response(2016, data))
    for _ in range(module.MAX_MESSAGES + 5):
        collector.observe(response(6553))
    finish(collector)
    document = archive.calls[0][2]
    assert len(document["messages"]) <= module.MAX_MESSAGES
    assert document["coverage"]["skipped_depth"] > 0
    assert document["coverage"]["skipped_message_limit"] > 0


def test_item_full_snapshot_candidate_is_explicitly_unverified_and_changes_decode():
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    full = message("Cmd_Notify_ItemAll", item_list=[
        {"item_id": 10001, "count": 31}, {"item_id": 10001, "count": 2},
        {"item_id": 20002, "count": 500},
    ])
    changed = message("CmdItemReal", item_id=10001, count=1, item_uid=2**63 + 9)
    changes = message("Cmd_Notify_ItemChange", change_list=[
        {"change_type": 1, "data": changed.SerializeToString()},
        {"change_type": 2, "data": b"\xff"},
    ])
    collector.observe(response(2941, full.SerializeToString()))
    collector.observe(response(2942, changes.SerializeToString()))
    finish(collector)
    document = archive.calls[0][2]
    assert document["derived_candidates"]["raw_item_10001_sum"] == "33"
    assert document["derived_candidates"]["gold_balance_verified"] is False
    assert "gold_balance" not in document["derived_candidates"]
    changes_row = document["messages"][1]
    assert changes_row["decoded_item_changes"][0]["fields"]["count"] == "1"
    assert changes_row["decoded_item_changes"][0]["fields"]["item_uid"] == str(2**63 + 9)
    assert changes_row["decoded_item_changes"][1]["status"] == "decode_failed"


def test_only_self_guild_message_sets_self_guild_candidate():
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    collector.observe(response(5432, message("Cmd_Notify_Guild_Role_Data", data={"gid": 2**63 + 15}).SerializeToString()))
    finish(collector)
    candidate = archive.calls[0][2]["derived_candidates"]
    assert candidate["self_guild_id"] == str(2**63 + 15)
    assert candidate["self_guild_source_command"] == 5432


def test_handshake_explicit_allowlist_cannot_preserve_authentication_fields():
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    codec = GameProtoCodec()
    name = "SNKProto.Cmd_Net_LumiLogin_Ack"
    ack = codec.decode(name, codec.encode(name, {
        "role_id": 123, "reconnect_token": "synthetic-reconnect-secret",
    })).message
    collector.record_handshake("login_ack", ack, ("role_id", "reconnect_token", "session_info"))
    end_name = "SNKProto.Cmd_Notify_LoginEnd"
    end = codec.decode(end_name, codec.encode(end_name, {"use_cd_key": True, "cd_key": "synthetic-key"})).message
    collector.record_handshake("login_end", end, ("use_cd_key", "cd_key"))
    finish(collector)
    document = archive.calls[0][2]
    assert document["handshake"]["login_ack"] == {"role_id": "123"}
    # The shared exporter deliberately removes every cd-key-named field,
    # including this boolean gate. The root records derived activation state.
    assert document["handshake"]["login_end"] == {}
    assert "synthetic-reconnect-secret" not in json.dumps(document)
    assert "synthetic-key" not in json.dumps(document)


def test_finalize_is_once_and_stops_late_observation_and_close_clears_memory():
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    collector.observe(response(2931, message("Cmd_Notify_RoleBase", name="first").SerializeToString()))
    finish(collector)
    collector.observe(response(2931, message("Cmd_Notify_RoleBase", name="late").SerializeToString()))
    finish(collector)
    assert len(archive.calls) == 1
    assert len(archive.calls[0][2]["messages"]) == 1
    collector.close()
    collector.observe(response(6553, b"late"))
    assert len(archive.calls) == 1


def test_archive_failure_is_safe_and_does_not_escape_finalize(caplog):
    archive = MemoryArchive(failure=OSError("synthetic-private-error"))
    collector = implementation().LoginProfileCollector(archive)
    collector.observe(response(6553, b"synthetic-private-body"))
    finish(collector)
    assert collector.archive_status == "failed"
    assert collector.archive_path is None
    assert "synthetic-private-error" not in caplog.text
    assert "synthetic-private-body" not in caplog.text


@pytest.mark.parametrize("role_id,zone_id", [(None, 1), (0, 1), (True, 1), (1, -1), (1, True)])
def test_invalid_identity_does_not_write_or_claim_saved(role_id, zone_id):
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    collector.finalize(SimpleNamespace(role_id=role_id, zone_id=zone_id))
    assert collector.archive_status == "unavailable"
    assert collector.archive_path is None
    assert archive.calls == []


def test_schema_hash_mismatch_is_metadata_only_and_marked_unavailable(tmp_path, monkeypatch):
    module = implementation()
    source = Path(__file__).parents[1] / "src/rumi_market/game"
    manifest = json.loads((source / "profile-source.json").read_text(encoding="utf-8"))
    (tmp_path / "profile-source.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "profile.descriptor.pb").write_bytes(b"synthetic-tampered-schema")
    monkeypatch.setattr(module, "files", lambda package: tmp_path)
    archive = MemoryArchive()
    collector = module.LoginProfileCollector(archive)
    collector.observe(response(2931, message("Cmd_Notify_RoleBase", name="must-not-decode").SerializeToString()))
    finish(collector)
    document = archive.calls[0][2]
    assert document["coverage"]["schema_status"] == "unavailable"
    assert document["messages"][0]["status"] == "schema_unavailable"
    assert "must-not-decode" not in json.dumps(document)


@pytest.mark.parametrize("tamper", ["size", "duplicate_command", "unbound_root", "unknown_type"])
def test_manifest_binding_and_root_validation_is_strict(tmp_path, monkeypatch, tamper):
    module = implementation()
    source = Path(__file__).parents[1] / "src/rumi_market/game"
    manifest = json.loads((source / "profile-source.json").read_text(encoding="utf-8"))
    if tamper == "size":
        manifest["descriptor_size"] += 1
    elif tamper == "duplicate_command":
        manifest["rpc_bindings"][1]["rpc_id"] = manifest["rpc_bindings"][0]["rpc_id"]
    elif tamper == "unbound_root":
        manifest["selected_roots"].append("SNKProto.Cmd_Notify_Unbound")
    else:
        manifest["selected_roots"][0] = "SNKProto.Cmd_Notify_UnknownType"
        manifest["rpc_bindings"][0]["message_type"] = manifest["selected_roots"][0]
    (tmp_path / "profile-source.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "profile.descriptor.pb").write_bytes((source / "profile.descriptor.pb").read_bytes())
    monkeypatch.setattr(module, "files", lambda package: tmp_path)
    collector = module.LoginProfileCollector(MemoryArchive())
    assert collector._coverage["schema_status"] == "unavailable"


def test_incomplete_envelopes_are_ignored_and_wire_byte_budget_is_bounded(monkeypatch):
    module = implementation()
    monkeypatch.setattr(module, "MAX_CAPTURE_BYTES", 4)
    archive = MemoryArchive()
    collector = module.LoginProfileCollector(archive)
    collector.observe(ResponseEnvelope(2931, 0, 1, 0, 1, b"abc"))
    collector.observe(response(6553, b"abc"))
    collector.observe(response(6553, b"def"))
    finish(collector)
    document = archive.calls[0][2]
    assert len(document["messages"]) == 1
    assert document["coverage"]["captured_bytes"] == 3
    assert document["coverage"]["skipped_byte_limit"] == 1


def test_combined_wrapper_export_is_bounded_as_well_as_recursive_decode(monkeypatch):
    module = implementation()
    monkeypatch.setattr(module, "MAX_MESSAGES", 3)
    archive = MemoryArchive()
    collector = module.LoginProfileCollector(archive)
    combined = message("Cmd_Notify_Combine_Info", unit_lst=[{"id": 6553, "msg": b"x"}] * 12)
    collector.observe(response(2016, combined.SerializeToString()))
    finish(collector)
    document = archive.calls[0][2]
    assert len(document["messages"]) == 3
    assert len(document["messages"][0]["fields"]["unit_lst"]) <= 2
    assert document["coverage"]["skipped_message_limit"] >= 10


def test_other_guild_messages_are_archived_without_claiming_self_guild():
    archive = MemoryArchive()
    collector = implementation().LoginProfileCollector(archive)
    payload = message("Cmd_Notify_Guild_Base", base={"gid": 2**63 + 31}).SerializeToString()
    collector.observe(response(5425, payload))
    finish(collector)
    document = archive.calls[0][2]
    assert document["messages"][0]["status"] == "decoded"
    assert "self_guild_id" not in document["derived_candidates"]


def test_item_change_projection_and_decoding_are_both_bounded(monkeypatch):
    module = implementation()
    monkeypatch.setattr(module, "MAX_ITEM_CHANGES", 2)
    archive = MemoryArchive()
    collector = module.LoginProfileCollector(archive)
    changed = message("CmdItemReal", item_id=20002, count=3).SerializeToString()
    changes = message("Cmd_Notify_ItemChange", change_list=[{"change_type": 1, "data": changed}] * 7)
    collector.observe(response(2942, changes.SerializeToString()))
    finish(collector)
    document = archive.calls[0][2]
    row = document["messages"][0]
    assert len(row["fields"]["change_list"]) == 2
    assert len(row["decoded_item_changes"]) == 2
    assert document["coverage"]["skipped_item_change_limit"] == 5
