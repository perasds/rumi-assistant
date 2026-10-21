"""Synthetic checks for the pinned hot71 chat and redbag wire contract."""
import importlib
import json
from importlib.resources import files

import pytest


def implementation():
    try:
        return importlib.import_module("rumi_market.game.redbag_codec")
    except ModuleNotFoundError:
        pytest.fail("The hot71 redbag codec is not implemented")


def test_exact_current_bindings_and_private_chat_envelope():
    module = implementation()
    codec = module.RedbagProtoCodec()
    assert {4009, 4010, 4011, 4012, 23903, 23904, 23909, 23910, 23920, 23921,
            23938, 23939, 23940, 2919} <= codec.known_commands
    assert codec.message_class(4012).DESCRIPTOR.full_name == "SNKProto.Cmd_Home_VisitGetShareAward_Ack"
    raw = codec.encode_ws(23924, b"")
    package = codec.message_class("WsClientPkg").FromString(raw)
    assert package.api == "23924" and package.seq_id == "0" and package.data == b""
    assert codec.decode_ws(raw) == (23924, b"")
    source = json.loads(files("rumi_market.game").joinpath("redbag-source.json").read_bytes())
    assert source["hot_version"] == 71
    assert source["source_game_sha256"] == "0867970fcfd9a33984120c85ad91644b1e17352c4ddae85e083cea5f59c089f8"
    assert not source["network_requests_sent"] and not source["private_archives_read"]


def test_auth_map_and_card_bytes_are_exact_protobuf_not_json():
    codec = implementation().RedbagProtoCodec()
    auth = codec.decode(2919, codec.encode(2919, params={"test": "synthetic-secret"}))
    assert dict(auth.params) == {"test": "synthetic-secret"}
    card = codec.decode("HomeVisitShareCardParam", codec.encode("HomeVisitShareCardParam",
        share_str_id="redbag-A", sender_uid=2, expire_time=2**53 + 1))
    assert card.share_str_id == "redbag-A" and card.expire_time == 2**53 + 1


@pytest.mark.parametrize("payload", [b"\xff", "synthetic-private", bytearray(b"x")])
def test_codec_rejects_malformed_input_without_exporting_it(payload):
    module = implementation()
    with pytest.raises(ValueError, match="^REDBAG_PROTOCOL_INVALID$"):
        module.RedbagProtoCodec().decode_ws(payload)


def test_source_report_provenance_cannot_be_changed_while_reusing_descriptor(monkeypatch):
    module = implementation()
    root = files("rumi_market.game")
    source = json.loads(root.joinpath("redbag-source.json").read_bytes())
    source["source_report_sha256"] = "0" * 64
    class Resource:
        def joinpath(self, name):
            from types import SimpleNamespace
            data = json.dumps(source).encode() if name == "redbag-source.json" else root.joinpath(name).read_bytes()
            return SimpleNamespace(read_bytes=lambda: data)
    module._schema.cache_clear()
    monkeypatch.setattr(module, "files", lambda package: Resource())
    try:
        with pytest.raises(ValueError, match="^REDBAG_PROTOCOL_INVALID$"):
            module.RedbagProtoCodec()
    finally:
        module._schema.cache_clear()
