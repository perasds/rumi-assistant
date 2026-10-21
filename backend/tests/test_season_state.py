"""Synthetic hot65 season notifications; no account or network fixtures."""
import importlib
from importlib.resources import files
import hashlib
import json

import pytest

from rumi_market.game.envelope import ResponseEnvelope


def implementation(name="season_state"):
    try:
        return importlib.import_module("rumi_market.game." + name)
    except ModuleNotFoundError:
        pytest.fail("The source-bound season state is not implemented")


def activity(*, key=11, system=700010, begin=100, end=200):
    return {"act_comm": {"system_open_id": key, "system_id": system,
                         "begin_time": begin, "end_time": end}}


def notification(command=8001, rows=None, *, payload=None, code=0, remaining=0):
    if payload is None:
        codec = implementation("season_codec").SeasonProtoCodec()
        payload = codec.encode(command, activity_info={"season_list": rows or []})
    return ResponseEnvelope(command, 0, 0, code, remaining, payload)


def ready_state(rows=None):
    state = implementation().SeasonState()
    state.observe(notification(rows=[activity()] if rows is None else rows))
    state.mark_login_complete()
    return state


def test_season_codec_is_bound_to_public_hot65_resource_and_two_commands():
    codec = implementation("season_codec").SeasonProtoCodec()
    source = json.loads(files("rumi_market.game").joinpath("season-source.json").read_bytes())
    raw = files("rumi_market.game").joinpath("season.descriptor.pb").read_bytes()
    assert source["source_game_sha256"] == "ab029ae5433cbaa8b214a290387b40ae762a154cbc04096a26c886eba4eab4b3"
    assert source["hot_version"] == 65
    assert hashlib.sha256(raw).hexdigest() == source["descriptor_sha256"]
    for command, suffix in ((8001, "All"), (8002, "Change")):
        assert codec.message_class(command).DESCRIPTOR.full_name == "SNKProto.Cmd_Notify_ACT_" + suffix
    message = codec.decode(8001, codec.encode(8001, activity_info={"season_list": [activity()]}))
    assert message.DESCRIPTOR.fields_by_name["activity_info"].number == 1
    assert message.activity_info.DESCRIPTOR.fields_by_name["season_list"].number == 15
    comm = message.activity_info.season_list[0].act_comm
    assert [comm.DESCRIPTOR.fields_by_name[n].number for n in
            ("system_open_id", "system_id", "begin_time", "end_time")] == [1, 2, 3, 4]
    assert all(comm.HasField(n) for n in ("system_open_id", "system_id", "begin_time", "end_time"))
    assert not codec.decode(8001, b"").HasField("activity_info")


@pytest.mark.parametrize("command,payload", [(True, b""), (8003, b""), (8001, b"\xff"), (8001, "private")])
def test_season_codec_rejects_invalid_or_unbound_wire(command, payload):
    with pytest.raises(ValueError, match="^GAME_SEASON_PROTOCOL_INVALID$"):
        implementation("season_codec").SeasonProtoCodec().decode(command, payload)


def test_season_waits_for_current_login_baseline_and_login_completion():
    state = implementation().SeasonState()
    assert state.snapshot(150)["valid"] is False
    state.observe(notification(8002, [activity()]))
    state.mark_login_complete()
    assert state.snapshot(150)["unavailable_reason"] == "baseline_pending"
    state.observe(notification(rows=[activity()]))
    assert state.snapshot(150)["current_season_id"] == 1
    pending = implementation().SeasonState()
    pending.observe(notification(rows=[activity()]))
    assert pending.snapshot(150)["unavailable_reason"] == "login_pending"


@pytest.mark.parametrize("now,valid", [(99, False), (100, True), (200, True), (201, False)])
def test_active_season_uses_source_inclusive_server_time_bounds(now, valid):
    snapshot = ready_state().snapshot(now)
    assert snapshot["valid"] is valid
    if valid:
        assert snapshot["current_season_id"] == 1 and snapshot["current_lumi_tag"] == 2
        assert snapshot["system_open_id"] == 11
        assert snapshot["begin_time"] == 100 and snapshot["end_time"] == 200


@pytest.mark.parametrize("now", [None, True, "150", -1, 2**63])
def test_season_requires_valid_current_server_clock(now):
    snapshot = ready_state().snapshot(now)
    assert not snapshot["valid"] and snapshot["current_season_id"] is None
    assert snapshot["unavailable_reason"] == "clock_unavailable"


def test_delta_upserts_by_system_open_id_and_preserves_unmentioned_future_rows():
    state = ready_state([activity(), activity(key=12, system=700020, begin=201, end=300)])
    state.observe(notification(8002, [activity(end=180)]))
    assert state.snapshot(180)["current_season_id"] == 1
    assert not state.snapshot(181)["valid"]
    assert state.snapshot(201)["current_season_id"] == 2
    state.observe(notification(8002, []))
    assert state.snapshot(250)["current_season_id"] == 2


def test_no_active_or_overlapping_activity_never_guesses_a_season():
    assert ready_state([]).snapshot(150)["unavailable_reason"] == "no_active_season"
    state = ready_state([activity(), activity(key=12, system=700020)])
    assert state.snapshot(150)["unavailable_reason"] == "ambiguous_season"


@pytest.mark.parametrize("row", [
    {"act_comm": {"system_id": 700010, "begin_time": 100, "end_time": 200}},
    {"act_comm": {"system_open_id": 11, "begin_time": 100, "end_time": 200}},
    {"act_comm": {"system_open_id": 11, "system_id": 700010, "begin_time": 100}},
    activity(begin=201, end=200), activity(key=0), activity(system=-1),
])
def test_incomplete_or_invalid_activity_invalidates_until_a_new_full_baseline(row):
    state = ready_state()
    state.observe(notification(8002, [row]))
    assert not state.snapshot(150)["valid"]
    state.observe(notification(8002, [activity()]))
    assert not state.snapshot(150)["valid"]
    state.observe(notification(rows=[activity()]))
    assert state.snapshot(150)["valid"]


@pytest.mark.parametrize("response", [
    notification(8002, payload=b"\xff"), notification(8002, payload=b""),
    notification(8002, payload=b"", code=1), notification(8002, payload=b"", remaining=1),
])
def test_bad_tracked_notification_drops_previous_valid_state(response):
    state = ready_state()
    state.observe(response)
    assert not state.snapshot(150)["valid"]


def test_ignores_unrelated_notifications_and_clear_requires_new_login():
    state = ready_state()
    state.observe(notification(3001, payload=b"\xff"))
    assert state.snapshot(150)["valid"]
    state.clear()
    assert not state.snapshot(150)["valid"]
    state.observe(notification(rows=[activity()]))
    assert state.snapshot(150)["unavailable_reason"] == "login_pending"


def test_activity_count_and_nonpositive_derived_season_are_unavailable():
    state = ready_state([activity(key=i+1) for i in range(257)])
    assert not state.snapshot(150)["valid"]
    assert ready_state([activity(system=700000)]).snapshot(150)["unavailable_reason"] == "season_id_unavailable"


def test_derived_season_uses_both_system_id_digits():
    snapshot = ready_state([activity(system=701230)]).snapshot(150)
    assert snapshot["current_season_id"] == 23
    assert snapshot["current_lumi_tag"] == 24


@pytest.mark.parametrize("kind", ["descriptor", "binding", "source", "roots"])
def test_tampered_public_resource_fails_closed(monkeypatch, tmp_path, kind):
    module = implementation("season_codec")
    root = files("rumi_market.game")
    manifest = json.loads(root.joinpath("season-source.json").read_bytes())
    raw = root.joinpath("season.descriptor.pb").read_bytes()
    if kind == "descriptor":
        raw += b"\0"
    elif kind == "binding":
        manifest["rpc_bindings"][0]["rpc_id"] = 8003
    elif kind == "source":
        manifest["source_game_sha256"] = "0" * 64
    else:
        manifest["selected_roots"] = []
    (tmp_path / "season-source.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "season.descriptor.pb").write_bytes(raw)
    monkeypatch.setattr(module, "files", lambda package: tmp_path)
    with pytest.raises(ValueError, match="^GAME_SEASON_PROTOCOL_INVALID$"):
        module.SeasonProtoCodec()
    state = implementation().SeasonState()
    state.mark_login_complete()
    assert state.snapshot(150)["unavailable_reason"] == "schema_unavailable"


def test_season_codec_bounds_payloads_and_hides_input_errors(monkeypatch):
    module = implementation("season_codec")
    codec = module.SeasonProtoCodec()
    with pytest.raises(ValueError, match="^GAME_SEASON_PROTOCOL_INVALID$"):
        codec.encode(8001, synthetic_private_field="synthetic-private-value")
    monkeypatch.setattr(module, "BUSINESS_MESSAGE_LIMIT", 1)
    with pytest.raises(ValueError, match="^GAME_SEASON_PROTOCOL_INVALID$"):
        codec.decode(8001, b"\x0a\x00")


def test_delta_capacity_bound_and_duplicate_keys_follow_upsert_semantics():
    rows = [activity(key=i+1, begin=300, end=400) for i in range(256)]
    state = ready_state(rows)
    state.observe(notification(8002, [activity(key=257)]))
    assert not state.snapshot(150)["valid"]
    state = ready_state()
    state.observe(notification(8002, [activity(system=700020), activity(system=700030)]))
    assert state.snapshot(150)["current_season_id"] == 3
