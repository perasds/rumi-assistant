"""Verified request sequence and actual Protobuf response fragment behavior."""

import pytest

from rumi_market.game.proto import GameProtoCodec


def response(data, remaining=0, seq=1, cmd=5326, notify=0, code=0):
    from rumi_market.game.envelope import decode_response
    codec = GameProtoCodec()
    return decode_response(codec.encode("SNKProto.RspProto", {
        "RspCmdId": cmd, "RspCmdSeq": seq, "RspNotifySeq": notify,
        "RspCode": code, "RspData": data, "Coalescing": remaining}))


def test_request_sequence_starts_one_and_does_not_add_unobserved_notify_field():
    from rumi_market.game.envelope import RequestSequencer
    sender = RequestSequencer()
    first = sender.encode(5325, bytes(range(256)))
    second = sender.encode(3522, b"synthetic-private-data")
    assert (first.sequence, second.sequence) == (1, 2)
    decoded = GameProtoCodec().decode("SNKProto.ReqProto", first.payload).message
    assert decoded.ReqCmdId == 5325 and decoded.ReqData == bytes(range(256))
    assert not decoded.HasField("ReqNotifySeq")
    assert "synthetic" not in repr(second)


def test_request_sequence_wrap_matches_uint32_add():
    from rumi_market.game.envelope import RequestSequencer
    value = RequestSequencer(last_sequence=0xffffffff).encode(5325, b"")
    assert value.sequence == 0
    decoded = GameProtoCodec().decode("SNKProto.ReqProto", value.payload).message
    assert decoded.HasField("ReqCmdSeq") and decoded.ReqCmdSeq == 0


def test_three_fragments_merge_data_and_keep_terminal_status():
    from rumi_market.game.envelope import FragmentAssembler
    merger = FragmentAssembler()
    assert merger.feed(response(b"a", 2, notify=9)) is None
    assert merger.feed(response(b"b", 1, notify=9)) is None
    complete = merger.feed(response(b"c", 0, notify=9, code=-1))
    assert complete.data == b"abc" and complete.code == -1
    assert (complete.command, complete.sequence, complete.notify_sequence) == (5326, 1, 9)
    assert merger.pending_bytes == 0


def test_identity_mismatch_retries_current_message_as_new_group():
    from rumi_market.game.envelope import FragmentAssembler
    merger = FragmentAssembler()
    merger.feed(response(b"old", 2))
    assert merger.feed(response(b"new", 1, seq=2)) is None
    assert merger.feed(response(b"last", seq=2)).data == b"newlast"


def test_countdown_mismatch_drops_group_and_keeps_current_terminal_only():
    from rumi_market.game.envelope import FragmentAssembler
    merger = FragmentAssembler()
    merger.feed(response(b"old", 3))
    assert merger.feed(response(b"current")).data == b"current"
    merger.feed(response(b"old", 3))
    assert merger.feed(response(b"wrong", 1)) is None
    assert merger.pending_bytes == 0
    assert merger.feed(response(b"terminal")).data == b"terminal"


def test_assembled_business_data_can_exceed_single_frame():
    from rumi_market.game.envelope import FragmentAssembler
    merger = FragmentAssembler()
    assert merger.feed(response(b"a" * 40000, 1)) is None
    assert len(merger.feed(response(b"b" * 40000)).data) == 80000


def test_memory_limit_discards_partial_state_with_fixed_error():
    from rumi_market.game.envelope import FragmentAssembler, GameEnvelopeError
    merger = FragmentAssembler(max_bytes=4)
    merger.feed(response(b"123", 1))
    with pytest.raises(GameEnvelopeError) as caught:
        merger.feed(response(b"456"))
    assert str(caught.value) == "GAME_FRAGMENT_TOO_LARGE"
    assert caught.value.__context__ is None
    assert merger.pending_bytes == 0


def test_close_clears_fragment_data_and_prevents_reuse():
    from rumi_market.game.envelope import FragmentAssembler, GameEnvelopeError
    merger = FragmentAssembler()
    merger.feed(response(b"synthetic-private-token", 1))
    merger.close()
    assert merger.pending_bytes == 0
    with pytest.raises(GameEnvelopeError):
        merger.feed(response(b"last"))
