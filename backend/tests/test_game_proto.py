"""Actual shipped public schemas with synthetic business values; no server login."""

import hashlib
import json
from importlib.resources import files

import pytest


QUERY = "SNKProto.Cmd_Market_Auction_QueryLumiDetail_Req"
ACK = "SNKProto.Cmd_Market_Auction_QueryLumiDetail_Ack"


def test_real_query_wire_tags_and_explicit_zero_presence():
    from rumi_market.game.proto import GameProtoCodec
    codec = GameProtoCodec()
    payload = codec.encode(QUERY, {"lumi_id": 0, "begin_index": 1, "end_index": 13,
                                   "order_type": 3, "course": "", "desc": False})
    assert payload == bytes.fromhex("08001001180d20032a003000")
    message = codec.decode(QUERY, payload).message
    assert message.HasField("lumi_id") and message.lumi_id == 0
    assert message.HasField("course") and message.course == ""
    assert codec.encode(QUERY, {}) == b""


def test_response_uses_listing_uid_and_pet_config_id_as_distinct_fields():
    from rumi_market.game.proto import GameProtoCodec
    codec = GameProtoCodec()
    raw = codec.encode(ACK, {"lumi_id": 0, "begin_index": 1, "end_index": 13,
                             "lumi_list": [{"uid": "synthetic-listing", "lumi_id": 1000101,
                                            "fixed_price": 220000, "bid_price": 210000}],
                             "course": "synthetic-cursor"})
    wrapped = codec.decode(ACK, raw)
    assert wrapped.message.lumi_list[0].uid == "synthetic-listing"
    assert wrapped.message.lumi_list[0].lumi_id == 1000101
    assert wrapped.message.lumi_list[0].fixed_price == 220000
    assert "synthetic" not in repr(wrapped)


def test_envelope_wraps_exact_business_protobuf_bytes():
    from rumi_market.game.proto import GameProtoCodec
    codec = GameProtoCodec()
    body = codec.encode(QUERY, {"lumi_id": 0, "order_type": 3})
    envelope = codec.encode("SNKProto.ReqProto", {"ReqCmdId": 5325, "ReqCmdSeq": 1, "ReqData": body})
    decoded = codec.decode("SNKProto.ReqProto", envelope).message
    assert decoded.ReqCmdId == 5325
    assert decoded.ReqCmdSeq == 1
    assert decoded.ReqData == body
    assert not decoded.HasField("ReqNotifySeq")


@pytest.mark.parametrize("data", [b"\xff", b"\x2a\x0a", "private-synthetic-value", b"a" * (4 * 1024 * 1024 + 1)],
                         ids=["invalid-tag", "truncated", "wrong-type", "oversized"])
def test_invalid_payload_errors_do_not_include_input_or_context(data):
    from rumi_market.game.proto import GameProtoCodec, GameProtoError
    with pytest.raises(GameProtoError) as caught:
        GameProtoCodec().decode(ACK, data)
    assert str(caught.value) == "GAME_PROTO_INVALID"
    assert caught.value.__context__ is None
    assert "private" not in repr(caught.value)


def test_unselected_message_and_unknown_field_are_rejected_safely():
    from rumi_market.game.proto import GameProtoCodec, GameProtoError
    codec = GameProtoCodec()
    with pytest.raises(GameProtoError):
        codec.encode("SNKProto.Unknown", {})
    with pytest.raises(GameProtoError) as caught:
        codec.encode(QUERY, {"unknown": "private-synthetic-value"})
    assert str(caught.value) == "GAME_PROTO_INVALID"
    assert caught.value.__context__ is None


def test_shipped_schema_has_source_hash_and_preserved_metadata_evidence():
    from rumi_market.game.proto import GameProtoCodec
    root = files("rumi_market.game")
    evidence = json.loads(root.joinpath("protocol-source.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(root.joinpath("protocol.descriptor.pb").read_bytes()).hexdigest() == evidence["schema_sha256"]
    assert evidence["selected_message_metadata_matches_source"] is True
    assert len(evidence["selected_messages"]) == 29
    assert evidence["schema_size"] == 10458
    assert evidence["schema_sha256"] == "8b4cf9cf1f35aaea159965131975d3f40dbb3010d3eaa3026522b63e6ad3145e"
    assert len(GameProtoCodec().message_types) == len(evidence["roots"]) == 19


def test_watch_schema_preserves_exact_optional_fields_and_exposes_only_buy_purchase_roots():
    from google.protobuf import descriptor_pb2
    from rumi_market.game.proto import GameProtoCodec
    codec = GameProtoCodec()
    prefix = "SNKProto.Cmd_Market_Auction_Watch_"
    for action in ("Start", "Stop"):
        name = prefix + action + "_Req"
        raw = codec.encode(name, {"uid": "uid"})
        assert raw == b"\x0a\x03uid"
        assert codec.decode(name, raw).message.HasField("uid")
    assert codec.encode(prefix + "Stop_Ack", {}) == b""
    ack_name = prefix + "Start_Ack"
    missing = codec.decode(ack_name, codec.encode(ack_name, {"uid": "uid"})).message
    assert not missing.HasField("lumi_info")
    explicit = codec.decode(ack_name, codec.encode(ack_name, {
        "uid": "uid", "lumi_info": {"uid": "uid", "is_sold_out": False,
                                    "fixed_price": 0, "sell_role_id": 0}})).message
    assert explicit.HasField("lumi_info")
    one = explicit.lumi_info
    assert one.HasField("is_sold_out") and one.is_sold_out is False
    assert one.HasField("fixed_price") and one.fixed_price == 0
    assert one.HasField("sell_role_id") and one.sell_role_id == 0
    assert not one.HasField("up_time")
    raw = files("rumi_market.game").joinpath("protocol.descriptor.pb").read_bytes()
    schema = descriptor_pb2.FileDescriptorSet.FromString(raw)
    selected = {m.name: m for f in schema.file for m in f.message_type}
    assert hashlib.sha256(selected["CmdAuctionLumiOne"].SerializeToString(deterministic=True)).hexdigest() == (
        "c745933c7c7f96f4a4a71464372bfb3c2c8b5e75e6c18acd14f07c61fa7806f9")
    assert {name for name in codec.message_types if "Buy" in name or "Bid" in name} == {
        "SNKProto.Cmd_Market_Auction_Buy_Req", "SNKProto.Cmd_Market_Auction_Buy_Ack"}


def test_buy_schema_preserves_native_tags_unsigned_fields_and_presence():
    from rumi_market.game.proto import GameProtoCodec
    codec = GameProtoCodec()
    name = "SNKProto.Cmd_Market_Auction_Buy_Req"
    raw = codec.encode(name, {"uid": "uid", "sell_role_id": 42, "price": 0})
    assert raw == bytes.fromhex("0a03756964102a1800")
    request = codec.decode(name, raw).message
    assert all(request.HasField(field) for field in ("uid", "sell_role_id", "price"))
    for field in ("sell_role_id", "price"):
        extreme = codec.decode(name, codec.encode(name, {field: (1 << 64) - 1})).message
        assert getattr(extreme, field) == (1 << 64) - 1
    ack_name = "SNKProto.Cmd_Market_Auction_Buy_Ack"
    assert codec.encode(ack_name, {"lumi_uid": 42, "lumi_id": 701, "uid": "uid"}) == bytes.fromhex(
        "082a10bd051a03756964")


def test_reassembled_business_payload_can_exceed_a_frame_but_envelope_cannot():
    from rumi_market.game.proto import GameProtoCodec, GameProtoError
    codec = GameProtoCodec()
    raw = codec.encode(ACK, {"course": "x" * 70000})
    assert len(raw) > 65535
    assert len(codec.decode(ACK, raw).message.course) == 70000
    with pytest.raises(GameProtoError):
        codec.encode("SNKProto.RspProto", {"RspCmdId": 5326, "RspData": raw})
    # Even a valid business protobuf cannot be accepted as an oversized frame.
    with pytest.raises(GameProtoError):
        codec.decode("SNKProto.RspProto", raw)


@pytest.mark.parametrize("name", [[], {}, None, 1], ids=["list", "mapping", "none", "integer"])
def test_invalid_message_name_type_has_fixed_error_for_both_directions(name):
    from rumi_market.game.proto import GameProtoCodec, GameProtoError
    codec = GameProtoCodec()
    for operation in (lambda: codec.encode(name, {}), lambda: codec.decode(name, b"")):
        with pytest.raises(GameProtoError) as caught:
            operation()
        assert str(caught.value) == "GAME_PROTO_INVALID"
        assert caught.value.__context__ is None
