import hashlib
import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from rumi_market.sdk_rule_codes import CODEBOOK
from rumi_market.sdk_rules import apply_sdk_rule, decode_sdk_rule, sdk_dynamic_headers


def test_public_prefix_table_is_unique_and_decodes_independent_known_bit_string():
    assert len(CODEBOOK)==67
    codes=[code for _,code in CODEBOOK]
    assert len(set(codes))==67
    assert all(not b.startswith(a) for a in codes for b in codes if a!=b)
    encoded=b"00100101010000110000100000001001011010001011010100010111111001111110100011"
    assert decode_sdk_rule(encoded)==b"[d].*[a|2|XY]"


@pytest.mark.parametrize("source,rule,expected",[
    (b"abc",b"[d]",b"cba"),
    (b"abcabc",b"[r|ab|Z]",b"ZcZc"),
    (b"abc",b"[r|a|]",b"abc"),
    (b"abc",b"[a|1|XY]",b"aXYbc"),
    (b"abc",b"[a|3|XY]",b"abc"),
    (b"abcdef",b"[t|1|3]",b"aef"),
    (b"abcdef",b"[t|2]",b"abdef"),
    (b"abcdef",b"[m|1|3]",b"bcd"),
    (b"abcdef",b"[m|3|1]",b""),
    (b"abc",b"[m|1|5]",b"abc"),
    (b"abcdef",b"[d].*[a|2|XY]",b"feXYdcba"),
    ("蜜桃".encode(),b"[t|0|2]","桃".encode()),
])
def test_verified_operations_use_native_byte_and_inclusive_range_semantics(source,rule,expected):
    assert apply_sdk_rule(source,rule)==expected


def test_atoi_decimal_prefix_and_unknown_opcode_match_normal_native_dispatch():
    assert apply_sdk_rule(b"abc",b"[a|1extra|XY].*[z|ignored]")==b"aXYbc"


@pytest.mark.parametrize("encoded",[b"secret",b"0010",b"1"*1048577],
                         ids=["invalid-symbol", "truncated-prefix", "oversized-input"])
def test_bad_encoded_rule_has_fixed_error_and_no_context(encoded):
    with pytest.raises(ValueError,match="^Unsupported SDK rule$") as failure:
        decode_sdk_rule(encoded)
    assert failure.value.__context__ is None


def encode(rule):
    codes={symbol:code.encode() for symbol,code in CODEBOOK}
    return b"".join(codes[symbol] for symbol in rule)


@pytest.fixture
def synthetic_rule_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_block(key, block):
    numbers=key.private_numbers()
    size=(key.key_size+7)//8
    return base64.b64encode(pow(int.from_bytes(block,"big"), numbers.d,
                               numbers.public_numbers.n).to_bytes(size,"big"))


def rule_public_pem(key):
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def test_signed_rule_recovery_checks_type_one_padding_before_prefix_decode(synthetic_rule_key):
    from rumi_market.sdk_rules import recover_signed_sdk_rule
    bits=encode(b"[d].*[t|1|2]")
    block=b"\x00\x01"+b"\xff"*(256-len(bits)-3)+b"\x00"+bits
    recovered=recover_signed_sdk_rule(signed_block(synthetic_rule_key,block),
                                      rule_public_pem(synthetic_rule_key))
    assert recovered==bits
    assert decode_sdk_rule(recovered)==b"[d].*[t|1|2]"


@pytest.mark.parametrize("kind",["type-two", "short-padding", "mixed-padding", "missing-zero",
                                 "bad-prefix", "short-block", "multiple-blocks", "bad-base64",
                                 "bad-public-key", "out-of-range"])
def test_signed_rule_failure_is_fixed_and_never_guesses_fallback(synthetic_rule_key,kind):
    from rumi_market.sdk_rules import recover_signed_sdk_rule
    bits=encode(b"[d]")
    block=b"\x00\x01"+b"\xff"*(256-len(bits)-3)+b"\x00"+bits
    pem=rule_public_pem(synthetic_rule_key)
    if kind=="type-two":
        block=b"\x00\x02"+block[2:]
    elif kind=="short-padding":
        block=b"\x00\x01"+b"\xff"*7+b"\x00"+b"0"*246
    elif kind=="mixed-padding":
        block=block[:4]+b"\xfe"+block[5:]
    elif kind=="missing-zero":
        block=b"\x00\x01"+b"\xff"*254
    elif kind=="bad-prefix":
        block=b"\x00\x01"+b"\xff"*250+b"\x00"+b"001"
    signature=signed_block(synthetic_rule_key,block)
    if kind=="short-block":
        signature=base64.b64encode(base64.b64decode(signature)[:-1])
    elif kind=="multiple-blocks":
        signature=base64.b64encode(base64.b64decode(signature)*2)
    elif kind=="bad-base64":
        signature=b"synthetic-secret!"
    elif kind=="bad-public-key":
        pem=b"synthetic-secret-key"
    elif kind=="out-of-range":
        signature=base64.b64encode(synthetic_rule_key.public_key().public_numbers().n.to_bytes(256,"big"))
    with pytest.raises(ValueError,match="^Unsupported SDK rule$") as failure:
        recover_signed_sdk_rule(signature,pem)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None


def test_five_header_wrapper_uses_udid_fragments_and_native_key_positions():
    seed=b"0123456789abcdef0123456789abcdef"
    udid=b"synthetic-device-id"
    headers=sdk_dynamic_headers(seed,udid,encode(b"[d]"),7)
    assert headers["cversion"]=="7"
    assert len(headers)==6
    for i in range(5):
        current=seed if i==1 else seed+udid[i:i+i+1]
        n=len(current)
        key=bytes(current[n-1-offset] for offset in [i,(4*i+3)%15,(i+3)%15,(8*i+1)%15]).decode()
        if key not in headers:
            key+=str(i)
        assert headers[key]==hashlib.md5(current[::-1]+udid).hexdigest()


def test_duplicate_dynamic_names_get_round_suffix():
    headers=sdk_dynamic_headers(b"a"*32,b"a"*16,encode(b"[d]"),1)
    assert set(headers)=={"aaaa","aaaa1","aaaa2","aaaa3","aaaa4","cversion"}


def test_unsupported_rule_integer_overflow_does_not_guess_native_undefined_behavior():
    with pytest.raises(ValueError,match="^Unsupported SDK rule$"):
        apply_sdk_rule(b"abc",b"[a|999999999999999|Z]")


@pytest.mark.parametrize("rule",[b"[d]]", b"[[d]", b"[a|1|X]],[[d]"])
def test_unbalanced_rules_are_rejected_before_generating_headers(rule):
    with pytest.raises(ValueError,match="^Unsupported SDK rule$"):
        apply_sdk_rule(b"synthetic-seed",rule)


def test_oversized_transformation_is_rejected():
    with pytest.raises(ValueError,match="^Unsupported SDK rule$"):
        apply_sdk_rule(b"aaaaaaaa",b"[r|a|"+b"X"*300000+b"]")
