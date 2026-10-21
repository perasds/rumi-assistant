"""Exact public hot65 order and production collection wire contracts."""

import hashlib
import importlib
from importlib.resources import files
import json

import pytest


BINDINGS = {2016: "Cmd_Notify_Combine_Info", 3001: "Cmd_Notify_HomeAll",
            3301: "Cmd_Notify_HomeOrderArchiChange", 3302: "Cmd_Home_Order_Complete_Req",
            3303: "Cmd_Home_Order_Complete_Ack", 3304: "Cmd_Home_Order_Refresh_Req",
            3305: "Cmd_Home_Order_Refresh_Ack", 3025: "Cmd_Home_ArchitecturePickWorkReward_Req",
            3026: "Cmd_Home_ArchitecturePickWorkReward_Ack",
            3027: "Cmd_Home_ArchitecturePickAllWorkReward_Req",
            3028: "Cmd_Home_ArchitecturePickAllWorkReward_Ack", 3029: "Cmd_Home_TryUpdateWork_Req",
            3030: "Cmd_Home_TryUpdateWork_Ack", 3040: "Cmd_Notify_AdventureBuffChange"}


def implementation():
    try:
        return importlib.import_module("rumi_market.game.order_codec")
    except ModuleNotFoundError:
        pytest.fail("The exact order codec is not implemented")


def test_order_codec_validates_fixed_resources_and_exact_dispatch_bindings():
    module = implementation()
    source = json.loads(files("rumi_market.game").joinpath("order-source.json").read_bytes())
    raw = files("rumi_market.game").joinpath("order.descriptor.pb").read_bytes()
    assert source["client_version"] == module.ORDER_CLIENT_VERSION == "1.5.3"
    assert source["hot_version"] == module.ORDER_HOT_VERSION == 65
    assert source["source_game_sha256"] == "ab029ae5433cbaa8b214a290387b40ae762a154cbc04096a26c886eba4eab4b3"
    assert hashlib.sha256(raw).hexdigest() == source["descriptor_sha256"]
    assert len(raw) == source["descriptor_size"]
    assert not source["network_requests_sent"] and not source["private_archives_read"]
    codec = module.OrderProtoCodec()
    assert codec.known_commands == frozenset(BINDINGS)
    for command, name in BINDINGS.items():
        assert codec.message_class(command).DESCRIPTOR.full_name == "SNKProto." + name


def test_order_codec_preserves_presence_and_exact_int64_quantities():
    codec = implementation().OrderProtoCodec()
    assert not codec.decode(3302, b"").HasField("slot_id")
    assert codec.decode(3302, codec.encode(3302, slot_id=0)).HasField("slot_id")
    result = codec.decode(3303, codec.encode(3303, reward=[{"item_id": 101, "count": 2**63 - 1}]))
    assert result.reward[0].count == 2**63 - 1
    assert list(codec.decode(3305, b"").ListFields()) == []
    assert set(codec.message_class(3025).DESCRIPTOR.fields_by_name) == {"archi_uid", "archi_id"}
    for command in (3027, 3028):
        assert not codec.message_class(command).DESCRIPTOR.fields
        assert codec.encode(command) == b""
        assert not codec.decode(command, b"").ListFields()


@pytest.mark.parametrize("command", [True, 0, -1, 3002, "3302"])
def test_order_codec_rejects_unbound_commands(command):
    with pytest.raises(ValueError, match="^GAME_ORDER_PROTOCOL_INVALID$"):
        implementation().OrderProtoCodec().decode(command, b"")


def test_order_codec_sanitizes_wire_errors_and_bounds_payloads(monkeypatch):
    module = implementation()
    codec = module.OrderProtoCodec()
    for payload in (b"\xff", "synthetic-private-text"):
        with pytest.raises(ValueError, match="^GAME_ORDER_PROTOCOL_INVALID$"):
            codec.decode(3301, payload)
    with pytest.raises(ValueError, match="^GAME_ORDER_PROTOCOL_INVALID$"):
        codec.encode(3302, unknown_private_field="synthetic-private-text")
    monkeypatch.setattr(module, "BUSINESS_MESSAGE_LIMIT", 1)
    with pytest.raises(ValueError, match="^GAME_ORDER_PROTOCOL_INVALID$"):
        codec.decode(3302, b"\x08\x01")


@pytest.mark.parametrize("changed", ["version", "source", "bindings", "descriptor"])
def test_order_codec_rejects_tampered_package_resources(tmp_path, monkeypatch, changed):
    module = implementation()
    root = files("rumi_market.game")
    source = json.loads(root.joinpath("order-source.json").read_bytes())
    raw = root.joinpath("order.descriptor.pb").read_bytes()
    if changed == "version":
        source["hot_version"] = 64
    elif changed == "source":
        source["source_game_sha256"] = "0" * 64
    elif changed == "bindings":
        source["rpc_bindings"][0]["rpc_id"] = 9999
    else:
        raw += b"\x00"
    (tmp_path / "order-source.json").write_text(json.dumps(source), encoding="utf-8")
    (tmp_path / "order.descriptor.pb").write_bytes(raw)
    monkeypatch.setattr(module, "files", lambda _: tmp_path)
    module._order_schema.cache_clear()
    try:
        with pytest.raises(ValueError, match="^GAME_ORDER_PROTOCOL_INVALID$"):
            module.OrderProtoCodec()
    finally:
        module._order_schema.cache_clear()
