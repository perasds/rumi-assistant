from dataclasses import FrozenInstanceError
import json
from types import MappingProxyType

import pytest


def config_payload(**updates):
    return {"code": 0, "config_login_android_https": "1",
            "config_login_https": "https://synthetic-a.biligame.net,,https://synthetic-b.biligame.net,https://synthetic-a.biligame.net,",
            "config_risk_verify_url": "https://synthetic.biligame.com/verify", **updates}


def challenge_payload(captcha_type="geetest", **updates):
    return {"code": 0, "captcha_type": captcha_type,
            "challenge": "synthetic-start-challenge", "gt": "synthetic-gt",
            "ctoken": "synthetic-start-ctoken", "gt_user_id": "synthetic-start-userid",
            "gs": "synthetic-gs", "url": "https://synthetic.biligame.com/image/a b+中.png?x=1&y=2",
            "image_token": "synthetic-start-image-token", **updates}


def completion_payload(**updates):
    return {"captcha_type": "synthetic-ignored-type", "ctoken": "synthetic-ignored-ctoken",
            "challenge": "synthetic-human-challenge", "validate": "synthetic-human-validate",
            "seccode": "synthetic-human-seccode", "code": "synthetic-human-code",
            "captcha": "synthetic-human-captcha", "userid": "synthetic-human-userid",
            "token": "synthetic-human-image-token", **updates}


def test_new_https_config_preserves_comma_order_and_removes_empty_duplicates():
    from rumi_market.sdk_captcha import SdkCaptchaConfig

    raw = config_payload(message="synthetic-private-message")
    before = dict(raw)
    value = SdkCaptchaConfig.from_response(MappingProxyType(raw))
    assert value.login_origins == ("https://synthetic-a.biligame.net", "https://synthetic-b.biligame.net")
    assert value.login_origin == value.login_origins[0]
    assert value.h5_base_url == raw["config_risk_verify_url"]
    assert raw == before
    assert "synthetic" not in repr(value)
    with pytest.raises(FrozenInstanceError):
        value.login_origin = "changed"


@pytest.mark.parametrize("updates", [
    {"code": False}, {"code": 0.0}, {"code": 7},
    {"config_login_android_https": "0"}, {"config_login_android_https": 1},
    {"config_login_https": ""}, {"config_login_https": None},
    {"config_login_https": "http://synthetic.biligame.net"},
    {"config_login_https": " https://synthetic.biligame.net"},
    {"config_login_https": "https://synthetic.biligame.net/path"},
    {"config_login_https": "https://synthetic.biligame.net?secret=synthetic"},
    {"config_login_https": "https://synthetic-user:synthetic-pass@synthetic.biligame.net"},
    {"config_risk_verify_url": "javascript:synthetic-private"},
    {"config_risk_verify_url": "https://[synthetic-private"},
    {"config_risk_verify_url": "https://synthetic-user@synthetic.biligame.com/verify"},
], ids=lambda value: "invalid-config")
def test_config_rejects_unknown_or_unsafe_values_with_fixed_error(updates):
    from rumi_market.sdk_captcha import SdkCaptchaConfig, SdkCaptchaError

    with pytest.raises(SdkCaptchaError) as failure:
        SdkCaptchaConfig.from_response(config_payload(**updates))
    assert str(failure.value) == "SDK_CAPTCHA_CONFIG_INVALID"
    assert "synthetic" not in repr(failure.value)
    assert failure.value.__context__ is None


@pytest.mark.parametrize("payload", [None, [], "synthetic-private"])
def test_non_object_config_is_rejected(payload):
    from rumi_market.sdk_captcha import SdkCaptchaConfig, SdkCaptchaError

    with pytest.raises(SdkCaptchaError, match="^SDK_CAPTCHA_CONFIG_INVALID$"):
        SdkCaptchaConfig.from_response(payload)


@pytest.mark.parametrize("captcha_type,suffix", [
    ("geetest", "?captcha_type=geetest&challenge=synthetic-start-challenge&gt=synthetic-gt"),
    ("1", "?captcha_type=1&challenge=synthetic-start-challenge&gt=synthetic-gt&userid=synthetic-start-userid&gs=synthetic-gs"),
    ("2", "?captcha_type=2&url=https%3A%2F%2Fsynthetic.biligame.com%2Fimage%2Fa+b%2B%E4%B8%AD.png%3Fx%3D1%26y%3D2&token=synthetic-start-image-token"),
])
def test_h5_query_follows_all_supported_source_branches(captcha_type, suffix):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaConfig

    raw = challenge_payload(captcha_type)
    value = SdkCaptchaChallenge.from_response(raw)
    config = SdkCaptchaConfig.from_response(config_payload(code="0"))
    assert value.captcha_type == captcha_type
    assert value.build_h5_url(config) == config.h5_base_url + suffix
    assert "failed=" not in value.build_h5_url(config)
    assert "synthetic" not in repr(value)
    with pytest.raises(FrozenInstanceError):
        value.ctoken = "changed"


@pytest.mark.parametrize("response_code", [200005, "200005", 200001, "200001"])
def test_only_source_failed_login_codes_add_failed_one(response_code):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaConfig

    value = SdkCaptchaChallenge.from_response(challenge_payload())
    config = SdkCaptchaConfig.from_response(config_payload())
    assert value.build_h5_url(config, response_code=response_code).endswith("&failed=1")


@pytest.mark.parametrize("updates", [{"code": False}, {"code": 0.0}, {"code": 7},
    {"captcha_type": "synthetic-unknown"}, {"captcha_type": True}, {"challenge": False},
    {"gt": {}}, {"ctoken": ""}])
def test_challenge_validation_does_not_echo_response(updates):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaError

    with pytest.raises(SdkCaptchaError) as failure:
        SdkCaptchaChallenge.from_response(challenge_payload(**updates))
    assert str(failure.value) == "SDK_CAPTCHA_RESPONSE_INVALID"
    assert "synthetic" not in repr(failure.value)
    assert failure.value.__context__ is None


@pytest.mark.parametrize("captcha_type,ctoken", [
    ("geetest", "synthetic-start-ctoken"), ("1", "synthetic-start-userid"),
    ("2", "synthetic-start-image-token")])
def test_human_completion_uses_start_type_and_ctoken_and_source_nine_fields(captcha_type, ctoken):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge

    value = SdkCaptchaChallenge.from_response(challenge_payload(captcha_type))
    raw = completion_payload()
    before = dict(raw)
    projected = value.project_completion(json.dumps(raw))
    assert projected == {"captcha_type": captcha_type, "ctoken": ctoken,
        "challenge": raw["challenge"], "validate": raw["validate"], "seccode": raw["seccode"],
        "img_code": raw["captcha"] if captcha_type == "2" else raw["code"],
        "gt_user_id": raw["userid"], "captcha_code": raw["captcha"], "image_token": raw["token"]}
    assert raw == before
    assert "synthetic-ignored-ctoken" not in projected.values()
    assert len(projected) == 9


def test_completion_missing_optional_strings_become_source_empty_strings():
    from rumi_market.sdk_captcha import SdkCaptchaChallenge

    value = SdkCaptchaChallenge.from_response(challenge_payload())
    projected = value.project_completion('{"validate":"synthetic-human-validate","challenge":null}')
    assert projected["challenge"] == ""
    assert projected["validate"] == "synthetic-human-validate"
    assert projected["seccode"] == projected["img_code"] == ""
    assert projected["ctoken"] == "synthetic-start-ctoken"


@pytest.mark.parametrize("gs", [0, 7])
def test_actual_anonymous_integer_type_one_and_gs_shape_is_projected(gs):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaConfig

    # Matches the reported public JSON types; all challenge values are synthetic.
    raw = challenge_payload(1, gs=gs, ctoken=None, image_token=None, url=None)
    value = SdkCaptchaChallenge.from_response(raw)
    config = SdkCaptchaConfig.from_response(config_payload())
    assert value.captcha_type == "1"
    assert value.gs == str(gs)
    assert value.build_h5_url(config) == (config.h5_base_url
        + "?captcha_type=1&challenge=synthetic-start-challenge&gt=synthetic-gt"
        + f"&userid=synthetic-start-userid&gs={gs}")
    assert value.project_completion(json.dumps(completion_payload()))["ctoken"] == "synthetic-start-userid"


def test_missing_gs_follows_native_string_builder_null_append():
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaConfig

    value = SdkCaptchaChallenge.from_response(challenge_payload("1", gs=None))
    config = SdkCaptchaConfig.from_response(config_payload())
    assert value.build_h5_url(config).endswith("&gs=null")


def test_java_image_url_encoder_preserves_star_and_encodes_tilde():
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaConfig

    value = SdkCaptchaChallenge.from_response(challenge_payload("2", url="synthetic*~ +"))
    config = SdkCaptchaConfig.from_response(config_payload())
    assert "&url=synthetic*%7E+%2B&token=" in value.build_h5_url(config)


@pytest.mark.parametrize("raw", ["", "null"])
def test_native_null_completion_is_cancelled(raw):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaError

    value = SdkCaptchaChallenge.from_response(challenge_payload())
    with pytest.raises(SdkCaptchaError, match="^SDK_CAPTCHA_CANCELLED$"):
        value.project_completion(raw)


@pytest.mark.parametrize("raw", ["synthetic-private-not-json", '"synthetic-private"', "[]",
    '{"validate":"synthetic-private",', '{"validate":[]}', '{"captcha_type":{}}'])
def test_invalid_completion_error_retains_no_json_decode_exception(raw):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaError

    value = SdkCaptchaChallenge.from_response(challenge_payload())
    with pytest.raises(SdkCaptchaError) as failure:
        value.project_completion(raw)
    assert str(failure.value) == "SDK_CAPTCHA_COMPLETION_INVALID"
    assert "synthetic" not in repr(failure.value)
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None


def test_native_string_scalars_in_human_callback_do_not_reject_unused_numeric_fields():
    from rumi_market.sdk_captcha import SdkCaptchaChallenge

    value = SdkCaptchaChallenge.from_response(challenge_payload("1", gs=1))
    raw = json.dumps(completion_payload(captcha_type=1, ctoken=17, status=1, code=0,
                                        userid=123456))
    fields = value.project_completion(raw)
    assert fields["captcha_type"] == "1"
    assert fields["ctoken"] == "synthetic-start-userid"
    assert fields["img_code"] == "0"
    assert fields["gt_user_id"] == "123456"
    assert "status" not in fields
    assert len(fields) == 9


def test_completion_numeric_lexeme_and_boolean_follow_native_string_reader():
    from rumi_market.sdk_captcha import SdkCaptchaChallenge

    value = SdkCaptchaChallenge.from_response(challenge_payload())
    fields = value.project_completion('{"code":1e3,"userid":1.00,"status":false,"validate":true}')
    assert fields["img_code"] == "1e3"
    assert fields["gt_user_id"] == "1.00"
    assert fields["validate"] == "true"


@pytest.mark.parametrize("raw", ['{"code":{}}', '{"userid":[]}', '{"status":{}}'])
def test_native_string_model_still_rejects_complex_known_fields(raw):
    from rumi_market.sdk_captcha import SdkCaptchaChallenge, SdkCaptchaError

    value = SdkCaptchaChallenge.from_response(challenge_payload())
    with pytest.raises(SdkCaptchaError, match="^SDK_CAPTCHA_COMPLETION_INVALID$") as failure:
        value.project_completion(raw)
    assert failure.value.__context__ is None
