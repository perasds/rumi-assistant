"""Fixed public Home wire bindings, without target execution or network IO."""

import importlib
from importlib.resources import files
import hashlib
import json

import pytest


def implementation():
    try:
        return importlib.import_module("rumi_market.game.home_codec")
    except ModuleNotFoundError:
        pytest.fail("The fixed Home protocol codec is not implemented")


def test_home_codec_loads_exact_promoted_descriptor_and_confirmed_bindings():
    module = implementation()
    source = json.loads(files("rumi_market.game").joinpath("home-source.json").read_bytes())
    raw = files("rumi_market.game").joinpath("home.descriptor.pb").read_bytes()
    assert source["hot_version"] == 64 and source["client_version"] == "1.5.3"
    assert hashlib.sha256(raw).hexdigest() == source["descriptor_sha256"]
    assert len(raw) == source["descriptor_size"]
    codec = module.HomeProtoCodec()
    for command, name in ((3001, "Cmd_Notify_HomeAll"), (3998, "Cmd_Notify_HomeArchiAll"),
                          (3003, "Cmd_Notify_HomeArchitectureChange"),
                          (3004, "Cmd_Notify_HomeLumiList"),
                          (3005, "Cmd_Notify_HomeLumiWorkEnergyChange"),
                          (2971, "Cmd_Notify_LumiAll"), (2972, "Cmd_Notify_LumiChange"),
                          (3401, "Cmd_HomeOld_ArchiLumiOp_Req"),
                          (3404, "Cmd_HomeOld_ArchiRecipeOp_Ack")):
        assert codec.message_class(command).DESCRIPTOR.full_name == "SNKProto." + name


def test_home_codec_preserves_missing_optional_zero_and_uint64_precision():
    codec = implementation().HomeProtoCodec()
    assert not codec.decode(3401, b"").HasField("op")
    uid = 2**64 - 1
    message = codec.decode(3401, codec.encode(3401, archi_uid=9, archi_id=10101, lumi_uid=uid, op=0))
    assert message.HasField("op") and message.op == 0 and message.lumi_uid == uid
    assert codec.message_class(3005).DESCRIPTOR.fields_by_name["change_list"].number == 2


@pytest.mark.parametrize("command", [True, 0, -1, 3002, "3001"])
def test_home_codec_rejects_unbound_commands(command):
    with pytest.raises(ValueError, match="^GAME_HOME_PROTOCOL_INVALID$"):
        implementation().HomeProtoCodec().decode(command, b"")


def test_home_codec_bounds_and_sanitizes_wire_errors(monkeypatch):
    module = implementation()
    codec = module.HomeProtoCodec()
    for payload in (b"\xff", "synthetic-private-text"):
        with pytest.raises(ValueError, match="^GAME_HOME_PROTOCOL_INVALID$"):
            codec.decode(3001, payload)
    with pytest.raises(ValueError, match="^GAME_HOME_PROTOCOL_INVALID$"):
        codec.encode(3401, unknown_private_field="synthetic-private-text")
    monkeypatch.setattr(module, "BUSINESS_MESSAGE_LIMIT", 1)
    with pytest.raises(ValueError, match="^GAME_HOME_PROTOCOL_INVALID$"):
        codec.decode(3001, b"\x08\x01")


def test_home_codec_includes_source_bound_farm_dispatch_notifications_and_acks():
    codec = implementation().HomeProtoCodec()
    for command, name in ((10093, "Cmd_Notify_ActFam_All"), (10091, "Cmd_Notify_ActFarm_StealResult"),
                          (10083, "Cmd_Home_ActFarm_Steal_Ack"), (10089, "Cmd_Home_ActHome_GetStoreAward_Ack")):
        assert codec.message_class(command).DESCRIPTOR.full_name == "SNKProto." + name
