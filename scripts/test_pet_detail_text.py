"""Public localization behavior using synthetic strings only."""

import importlib

import pytest


def render(key, params, texts):
    return importlib.import_module("pet_detail_text").render_text(key, params, texts)


def test_numeric_parameters_and_localized_references_become_plain_text():
    texts = {"skill": "攻击敌人{0}次，每次有{1}%概率添加[burn]。",
             "burn": "<color=#ff0000>灼烧</color>"}
    assert render("skill", [2.0, 20.0], texts) == "攻击敌人2次，每次有20%概率添加灼烧。"


def test_single_precision_parameters_do_not_expose_binary_float_noise():
    texts = {"skill": "攻击回能{0}，间隔{1}秒。"}
    assert render("skill", [0.699999988079071, 0.8999999761581421], texts) == "攻击回能0.7，间隔0.9秒。"


def test_chinese_numeric_display_groups_integer_part_and_preserves_decimals():
    texts = {"skill": "伤害{0}，生命{1}，倍率{2}。"}
    assert render("skill", [12345, 1234.5, -1234.25], texts) == "伤害12,345，生命1,234.5，倍率-1,234.25。"


def test_numeric_format_specifier_does_not_reformat_preformatted_string():
    assert render("skill", [1200.5], {"skill": "数值{0:F2}"}) == "数值1,200.5"


def test_negative_fraction_keeps_its_sign_when_integer_part_is_zero():
    assert render("skill", [-0.5], {"skill": "间隔变化{0}秒"}) == "间隔变化-0.5秒"


def test_known_cosmetic_tags_and_line_breaks_leave_readable_text():
    texts = {"skill": "<b>潜行</b><br><size=18><color=#F00>暴击</color></size>\n继续"}
    assert render("skill", [], texts) == "潜行\n暴击\n继续"


def test_escaped_braces_and_numeric_alignment_follow_composite_format():
    assert render("skill", [2.0], {"skill": "{{数值}}：{0,3}"}) == "{数值}：  2"


@pytest.mark.parametrize("key", [None, "", "unknown"])
def test_missing_localization_is_not_presented_as_a_description(key):
    assert render(key, [], {"skill": "有效说明"}) is None


@pytest.mark.parametrize("template, texts", [
    ("效果[unknown]", {}),
    ("[loop]", {"loop": "[loop]"}),
    ("[outer]", {"outer": "[inner]", "inner": "最终文本"}),
    ("缺少{1}", {}),
    ("{0.__class__}", {}),
    ("{name}", {}),
    ("{0,1000000000}", {}),
    ("{0", {}),
    ("<script>private-code</script>", {}),
    ("<img src=x onerror=private-code>", {}),
])
def test_unsupported_or_unresolved_templates_fail_closed(template, texts):
    assert render("skill", [2.0], {"skill": template, **texts}) is None


@pytest.mark.parametrize("params", [[True], ["2"], [float("nan")], [float("inf")], [1e100], None])
def test_non_numeric_or_nonfinite_parameters_are_rejected(params):
    assert render("skill", params, {"skill": "伤害{0}"}) is None


def test_input_size_is_bounded_and_original_catalog_is_not_modified():
    texts = {"skill": "说明" * 10000}
    original = dict(texts)
    assert render("skill", [], texts) is None
    assert texts == original
