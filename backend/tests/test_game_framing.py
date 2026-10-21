import pytest


def codec():
    from rumi_market.game.framing import FrameDecoder, encode_frame
    return FrameDecoder, encode_frame


def test_two_byte_big_endian_length_counts_only_payload_bytes():
    _, encode_frame=codec()
    assert encode_frame(b"\x08\xcd\x29")==b"\x00\x03\x08\xcd\x29"
    assert encode_frame(b"x"*256)[:2]==b"\x01\x00"


def test_arbitrary_stream_splits_and_coalesced_packets_preserve_payloads():
    FrameDecoder, encode_frame=codec()
    payloads=[b"a",b"\x08\xce\x29\x12\x00",b"b"*257]
    stream=b"".join(encode_frame(payload) for payload in payloads)
    for split in range(len(stream)+1):
        decoder=FrameDecoder()
        assert decoder.feed(stream[:split])+decoder.feed(stream[split:])==payloads
        assert decoder.pending_bytes==0


def test_byte_at_a_time_and_empty_chunks_do_not_drop_partial_header_or_body():
    FrameDecoder, encode_frame=codec()
    decoder=FrameDecoder()
    result=[]
    for byte in encode_frame(b"synthetic-payload"):
        assert decoder.feed(b"")==[]
        result+=decoder.feed(bytes([byte]))
    assert result==[b"synthetic-payload"]
    assert decoder.pending_bytes==0


def test_maximum_uint16_frame_is_supported_when_stream_is_split():
    FrameDecoder, encode_frame=codec()
    frame=encode_frame(b"x"*65535)
    decoder=FrameDecoder()
    assert decoder.feed(frame[:-1])==[]
    assert decoder.pending_bytes==65536
    assert decoder.feed(frame[-1:])==[b"x"*65535]


@pytest.mark.parametrize("payload",[b"",b"x"*65536,"synthetic-secret"],
                         ids=["empty", "oversized", "wrong-type"])
def test_invalid_outbound_payload_has_fixed_error(payload):
    _, encode_frame=codec()
    with pytest.raises(ValueError,match="^Invalid game frame$") as failure:
        encode_frame(payload)
    assert failure.value.__context__ is None


def test_zero_length_header_closes_and_clears_decoder():
    FrameDecoder,_=codec()
    decoder=FrameDecoder()
    with pytest.raises(ValueError,match="^Invalid game frame$"):
        decoder.feed(b"\x00\x00synthetic-secret")
    assert decoder.pending_bytes==0
    with pytest.raises(ValueError,match="^Invalid game frame$"):
        decoder.feed(b"\x00\x01a")


def test_explicit_close_discards_partial_bytes_and_prevents_reuse():
    FrameDecoder,_=codec()
    decoder=FrameDecoder()
    assert decoder.feed(b"\x00\x05xx")==[]
    assert decoder.pending_bytes==4
    decoder.close()
    assert decoder.pending_bytes==0
    with pytest.raises(ValueError,match="^Invalid game frame$"):
        decoder.feed(b"rest")


def test_invalid_chunk_type_closes_without_exposing_content():
    FrameDecoder,_=codec()
    decoder=FrameDecoder()
    with pytest.raises(ValueError,match="^Invalid game frame$") as failure:
        decoder.feed("synthetic-secret")
    assert failure.value.__context__ is None
    assert decoder.pending_bytes==0
