"""Only the source-verified Home capacity buff is retained from Adventure data."""

import importlib
from importlib.resources import files
import json

import pytest

from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.order_codec import OrderProtoCodec
from rumi_market.game.profile_sync import _load_schema


def implementation():
    try:
        return importlib.import_module("rumi_market.game.home_capacity_state")
    except ModuleNotFoundError:
        pytest.fail("The verified Home capacity projection is not implemented")


def response(command, data=b"", code=0, remaining=0):
    return ResponseEnvelope(command, 0, 0, code, remaining, data)


def full(*buffs):
    classes, _, _ = _load_schema()
    return response(3036, classes[3036](buff_list=buffs).SerializeToString())


def buff(stage=1, uid=0, buff_id=601):
    return {"buff_id": buff_id, "buff_uid": uid, "stage": stage}


def changes(*rows):
    return response(3040, OrderProtoCodec().encode(3040, change_list=[
        {"change_type": kind, "buff_info": value} for kind, value in rows]))


def combine(*notices):
    return response(2016, OrderProtoCodec().encode(2016, unit_lst=[
        {"id": notice.command, "msg": notice.data} for notice in notices]))


def test_capacity_starts_unknown_and_only_full_adventure_baseline_can_prove_no_bonus():
    state = implementation().HomeCapacityState(True)
    assert state.snapshot()["bonus"] is None and not state.snapshot()["valid"]
    state.observe(response(3001, OrderProtoCodec().encode(3001)))
    state.observe(changes((1, buff())))
    assert state.snapshot()["bonus"] is None and not state.snapshot()["valid"]
    state.observe(full())
    snapshot = state.snapshot()
    assert snapshot["valid"] and snapshot["bonus"] == 0 and snapshot["sequence"] == 1
    assert snapshot["observed_at"] and snapshot["unavailable_reason"] is None
    assert set(snapshot) == {"valid", "sequence", "bonus", "observed_at", "unavailable_reason"}


@pytest.mark.parametrize("stage, bonus", [(1, 300), (2, 200), (0, 100), (3, 100), (99, 100)])
def test_capacity_uses_server_stage_and_secondary_effect_parameters(stage, bonus):
    state = implementation().HomeCapacityState(True)
    state.observe(full(buff(stage)))
    assert state.snapshot()["valid"] and state.snapshot()["bonus"] == bonus


def test_capacity_keeps_no_unrelated_adventure_fields_or_buffs():
    state = implementation().HomeCapacityState(True)
    classes, _, _ = _load_schema()
    state.observe(response(3036, classes[3036](buff_list=[buff(2), buff(1, uid=20, buff_id=1001)],
                       current_map_id=999, gacha_color_miss_count=777).SerializeToString()))
    assert state.snapshot()["bonus"] == 200
    assert state._buffs == [(0, 2)]
    assert set(vars(state)) == {"_verified", "_closed", "_baseline", "_valid", "_sequence",
                                "_observed_at", "_bonus", "_buffs", "_codec", "_baseline_class",
                                "_unavailable_reason"}


def test_capacity_server_list_order_duplicates_and_first_match_change_semantics():
    state = implementation().HomeCapacityState(True)
    state.observe(full(buff(1)))
    state.observe(changes((1, buff(2))))
    assert state.snapshot()["bonus"] == 300
    assert state._buffs == [(0, 1), (0, 2)]
    state.observe(changes((3, buff(3))))
    assert state._buffs == [(0, 3), (0, 2)] and state.snapshot()["bonus"] == 100
    state.observe(changes((2, {"buff_id": 601, "buff_uid": 0})))
    assert state.snapshot()["bonus"] == 200
    state.observe(changes((2, {"buff_id": 601, "buff_uid": 0})))
    assert state.snapshot()["valid"] and state.snapshot()["bonus"] == 0


def test_capacity_absent_stage_is_unknown_and_can_be_repaired_by_matching_modify():
    state = implementation().HomeCapacityState(True)
    state.observe(full({"buff_id": 601, "buff_uid": 3}))
    assert not state.snapshot()["valid"] and state.snapshot()["bonus"] is None
    state.observe(changes((3, buff(2, uid=3))))
    assert state.snapshot()["valid"] and state.snapshot()["bonus"] == 200


def test_capacity_new_full_baseline_replaces_and_identical_or_unrelated_updates_do_not_advance_sequence():
    state = implementation().HomeCapacityState(True)
    state.observe(full(buff(1)))
    sequence = state.snapshot()["sequence"]
    state.observe(full(buff(1)))
    state.observe(changes((1, buff(1, buff_id=1001)), (3, buff(2, uid=99))))
    state.observe(response(9999, b"\xff"))
    assert state.snapshot()["sequence"] == sequence
    state.observe(full(buff(2)))
    assert state.snapshot()["bonus"] == 200


def test_capacity_combines_baseline_and_delta_and_never_locally_expires_server_stage():
    state = implementation().HomeCapacityState(True)
    row = buff(1)
    row.update(triggle_time=1, total_seconds=1)
    state.observe(combine(full(row), combine(changes((3, buff(2))))))
    assert state.snapshot()["valid"] and state.snapshot()["bonus"] == 200


@pytest.mark.parametrize("make_bad", [
    lambda: full({"buff_uid": 1}), lambda: full({"buff_id": 601, "buff_uid": -1, "stage": 1}),
    lambda: changes((4, buff())), lambda: changes((0, buff())),
    lambda: response(3036, b"\xff"), lambda: response(3040, b"\xff"),
    lambda: response(3036, code=7), lambda: response(3040, remaining=1),
    lambda: response(2016, b"\xff"),
    lambda: response(2016, OrderProtoCodec().encode(2016, unit_lst=[{"id": 3036}])),
])
def test_capacity_invalid_updates_clear_until_new_full_baseline(make_bad):
    state = implementation().HomeCapacityState(True)
    state.observe(full(buff()))
    state.observe(make_bad())
    assert not state.snapshot()["valid"] and state.snapshot()["bonus"] is None
    assert state.snapshot()["observed_at"] is None
    state.observe(changes((1, buff(2))))
    assert not state.snapshot()["valid"]
    state.observe(full(buff(2)))
    assert state.snapshot()["valid"] and state.snapshot()["bonus"] == 200


@pytest.mark.parametrize("limit", ["MAX_BUFF_ROWS", "MAX_BUFF_CHANGES", "MAX_COMBINE_UNITS", "MAX_COMBINE_DEPTH"])
def test_capacity_projection_is_bounded(monkeypatch, limit):
    module = implementation()
    state = module.HomeCapacityState(True)
    state.observe(full(buff()))
    monkeypatch.setattr(module, limit, 1)
    notice = {"MAX_BUFF_ROWS": full(buff(), buff(uid=2)),
              "MAX_BUFF_CHANGES": changes((1, buff(uid=2)), (1, buff(uid=3))),
              "MAX_COMBINE_UNITS": combine(full(), full()),
              "MAX_COMBINE_DEPTH": combine(combine(full()))}[limit]
    state.observe(notice)
    assert not state.snapshot()["valid"]


def test_capacity_unverified_version_and_close_retain_no_bonus():
    state = implementation().HomeCapacityState(False)
    state.observe(full(buff()))
    assert not state.snapshot()["valid"] and state.snapshot()["sequence"] == 0
    assert state.snapshot()["unavailable_reason"] == "HOME_CAPACITY_VERSION_UNVERIFIED"
    state = implementation().HomeCapacityState(True)
    state.observe(full(buff()))
    state.close()
    state.observe(full(buff()))
    assert not state.snapshot()["valid"] and state.snapshot()["bonus"] is None and state._buffs == []


@pytest.mark.parametrize("verified", [0, 1, None, "true"])
def test_capacity_version_flag_is_strict(verified):
    with pytest.raises(ValueError, match="^GAME_HOME_CAPACITY_STATE_INVALID$"):
        implementation().HomeCapacityState(verified)


def test_capacity_source_manifest_carries_exact_hot65_table_hashes_and_selected_effect():
    implementation()
    source = json.loads(files("rumi_market.game").joinpath("order-source.json").read_bytes())
    config = source["capacity_config"]
    assert config["table_sha256"] == {
        "AdventureBuff": "f0ddc5608ecfcafd88158a63dca18df7d8ce8eeb15713ffa24c26698ad67a769",
        "AdventureBuffEffect": "08a9303b0c3c482f3b9e4aa6e81d62e08fed190bede07110e5e6e48bc220b61a"}
    assert config["buff_id"] == 601 and config["secondary_effect_id"] == 602
    assert config["bonus_by_stage"] == {"1": 300, "2": 200, "other": 100}
    assert config["building_buff_capacity_bonus"] == 0


@pytest.mark.parametrize("changed", ["table", "bonus", "schema"])
def test_capacity_rejects_unverified_source_metadata(tmp_path, monkeypatch, changed):
    module = implementation()
    source = json.loads(files("rumi_market.game").joinpath("order-source.json").read_bytes())
    if changed == "table":
        source["capacity_config"]["table_sha256"]["AdventureBuff"] = "0" * 64
    elif changed == "bonus":
        source["capacity_config"]["bonus_by_stage"]["1"] = 999
    else:
        source["capacity_profile_message_sha256"]["SNKProto.CmdAdventureBuff"] = "0" * 64
    (tmp_path / "order-source.json").write_text(json.dumps(source), encoding="utf-8")
    monkeypatch.setattr(module, "files", lambda _: tmp_path)
    module._capacity_schema.cache_clear()
    try:
        state = module.HomeCapacityState(True)
        state.observe(full(buff()))
        assert not state.snapshot()["valid"] and state.snapshot()["bonus"] is None
        assert state.snapshot()["unavailable_reason"] == "HOME_CAPACITY_SCHEMA_UNAVAILABLE"
    finally:
        module._capacity_schema.cache_clear()
