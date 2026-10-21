"""Synthetic Home snapshots, client-specific merge rules and fail-closed limits."""

import asyncio
import importlib
import json

import pytest

from rumi_market.game.envelope import ResponseEnvelope


def implementation():
    try:
        return importlib.import_module("rumi_market.game.home_state")
    except ModuleNotFoundError:
        pytest.fail("The session-owned live Home state is not implemented")


def state():
    return implementation().HomeState(verified_version=True)


def notice(command, **fields):
    from rumi_market.game.home_codec import HomeProtoCodec
    return ResponseEnvelope(command, 0, 1, 0, 0, HomeProtoCodec().encode(command, **fields))


def arch(uid=9, level=1, **fields):
    return {"architecture_uid": uid, "architecture_id": 10101, "architecture_level": level, **fields}


def full(*rows, **fields):
    return notice(3001, architecture_list=list(rows), **fields)


def changes(kind, row):
    return notice(3003, change_list=[{"change_type": kind, "architecture_info": row}])


def combined(*messages):
    return notice(2016, unit_lst=[{"id": row.command, "msg": row.data} for row in messages])


def test_home_initially_unknown_and_empty_full_snapshot_is_valid():
    tracker = state()
    assert tracker.snapshot() == {"valid": False, "sequence": 0, "observed_at": None,
                                  "architectures": [], "home_lumis": [], "workers": [], "roster_ready": False,
                                  "dispatch_ready": False, "dispatch_lumi_uids": [], "unavailable_reason": None}
    tracker.observe(full())
    assert tracker.snapshot()["valid"] and tracker.snapshot()["architectures"] == []


def test_split_3998_architectures_override_homeall_list_and_pending_is_consumed():
    tracker = state()
    tracker.observe(notice(3998, architecture_list=[arch(9)]))
    tracker.observe(notice(3998, architecture_list=[arch(10)]))
    assert not tracker.snapshot()["valid"]
    tracker.observe(full(arch(99)))
    assert [row["uid"] for row in tracker.snapshot()["architectures"]] == [9, 10]
    tracker.observe(full(arch(99)))
    assert [row["uid"] for row in tracker.snapshot()["architectures"]] == [99]


def test_empty_pending_batch_still_takes_precedence_and_3004_is_source_noop():
    tracker = state()
    tracker.observe(notice(3998))
    tracker.observe(full(arch(), home_lumi_list=[{"lumi_uid": 11, "lumi_id": 701}]))
    before = tracker.snapshot()
    assert before["architectures"] == []
    tracker.observe(notice(3004, home_lumi_list=[{"lumi_uid": 12, "lumi_id": 702}]))
    assert tracker.snapshot() == before


def test_architecture_cases_use_1_2_5_not_generic_repeat_change_enum():
    tracker = state()
    tracker.observe(full(arch()))
    tracker.observe(changes(1, arch(10)))
    tracker.observe(changes(5, arch(9, 4)))
    assert [(row["uid"], row["level"]) for row in tracker.snapshot()["architectures"]] == [(9, 4), (10, 1)]
    tracker.observe(changes(2, {"architecture_uid": 9}))
    assert [row["uid"] for row in tracker.snapshot()["architectures"]] == [10]
    tracker.observe(changes(3, arch(10, 9)))
    assert not tracker.snapshot()["valid"]
    tracker.observe(changes(1, arch()))
    assert not tracker.snapshot()["valid"]
    tracker.observe(full(arch()))
    assert tracker.snapshot()["valid"]


def test_energy_is_uid_replace_or_append_and_change_type_does_not_delete():
    tracker = state()
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": 11, "lumi_id": 701}, {"lumi_uid": 12, "lumi_id": 702}]))
    tracker.observe(notice(2913))
    tracker.observe(full(arch(), lumi_list=[{"lumi_uid": 11, "work_energy": 90}]))
    tracker.observe(notice(3005, change_list=[{"change_type": 2, "lumi_energy": {"lumi_uid": 11, "work_energy": 0}},
                                           {"lumi_energy": {"lumi_uid": 12, "work_energy": 17}}]))
    assert [row["work_energy"] for row in tracker.snapshot()["workers"]] == [0, 17]


def test_projection_joins_owned_roster_and_occupancy_without_private_names_or_values():
    tracker = state()
    uid = 2**64 - 1
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": uid, "lumi_id": 701, "psName": "synthetic-private-name",
        "init_aptitude": {"aptitude_work": 32}, "lumi_state": 2**63}]))
    tracker.observe(notice(2913))
    tracker.observe(full(arch(work_old={"lumi_uid": uid, "lumi_id": 701, "recipe_state": 0},
        dorm={"lumi_list": [{"lumi_uid": uid}]}, home_travel={"slot_list": [{"slot_id": 1, "lumi_uid": uid}]})))
    snapshot = tracker.snapshot()
    worker = snapshot["workers"][0]
    assert (worker["uid"], worker["id"], worker["work_aptitude"], worker["raw_state"]) == (str(uid), 701, 32, str(2**63))
    assert {row["kind"] for row in worker["occupancy"]} == {"work_old", "dorm", "travel"}
    assert worker["work_energy"] is None and snapshot["roster_ready"]
    assert snapshot["architectures"][0]["work_old"]["recipe_state"] == 0
    assert "synthetic-private-name" not in json.dumps(snapshot)
    snapshot["architectures"].clear()
    assert tracker.snapshot()["architectures"]


def test_missing_optional_fields_remain_unknown_instead_of_becoming_zero():
    tracker = state()
    tracker.observe(full({"architecture_uid": 9}, home_lumi_list=[{"lumi_uid": 11}]))
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": 11}]))
    tracker.observe(notice(2913))
    snapshot = tracker.snapshot()
    assert snapshot["architectures"][0]["id"] is None
    assert snapshot["architectures"][0]["level"] is None
    assert snapshot["workers"][0]["work_aptitude"] is None
    assert snapshot["workers"][0]["raw_state"] is None


@pytest.mark.parametrize("bad", [
    lambda: full({"architecture_id": 10101}), lambda: full(arch(), arch()),
    lambda: changes(99, arch()), lambda: notice(3005, change_list=[{}]),
    lambda: ResponseEnvelope(3001, 0, 0, 0, 0, b"\xff"),
    lambda: ResponseEnvelope(3001, 0, 0, -1, 0, b""),
    lambda: ResponseEnvelope(3001, 0, 0, 0, 1, b""),
])
def test_invalid_home_payloads_clear_state_until_independent_full_baseline(bad):
    tracker = state()
    tracker.observe(full(arch()))
    tracker.observe(bad())
    snapshot = tracker.snapshot()
    assert not snapshot["valid"] and snapshot["architectures"] == []
    tracker.observe(full(arch(10)))
    assert tracker.snapshot()["valid"]


def test_combine_applies_in_wire_order_and_unknown_unrelated_commands_are_ignored():
    tracker = state()
    tracker.observe(combined(full(arch()), combined(changes(5, arch(9, 3)))))
    assert tracker.snapshot()["architectures"][0]["level"] == 3
    before = tracker.snapshot()
    tracker.observe(ResponseEnvelope(9999, 0, 0, 0, 0, b"\xff"))
    assert tracker.snapshot() == before


@pytest.mark.parametrize("limit", ["MAX_COMBINE_DEPTH", "MAX_COMBINE_UNITS", "MAX_ARCHITECTURES", "MAX_CHANGES"])
def test_home_limits_fail_closed_without_publishing_truncated_state(monkeypatch, limit):
    module = implementation()
    tracker = state()
    tracker.observe(full(arch()))
    monkeypatch.setattr(module, limit, 1)
    if limit == "MAX_COMBINE_DEPTH": message = combined(combined(combined(full())))
    elif limit == "MAX_COMBINE_UNITS": message = combined(full(), full())
    elif limit == "MAX_ARCHITECTURES": message = full(arch(), arch(10))
    else: message = notice(3003, change_list=[{"change_type": 2, "architecture_info": {"architecture_uid": 9}}] * 2)
    tracker.observe(message)
    assert not tracker.snapshot()["valid"]


def test_unverified_version_and_close_cannot_expose_home_or_owned_roster():
    tracker = implementation().HomeState(verified_version=False)
    assert tracker.snapshot()['unavailable_reason'] == 'HOME_VERSION_UNVERIFIED'
    tracker.observe(full(arch()))
    assert not tracker.snapshot()["valid"]
    tracker = state()
    tracker.observe(full(arch()))
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": 11}]))
    tracker.close()
    tracker.observe(full(arch()))
    snapshot = tracker.snapshot()
    assert not snapshot["valid"] and not snapshot["roster_ready"]
    assert snapshot["architectures"] == snapshot["workers"] == []


async def test_wait_for_update_uses_new_sequence_and_wakes_on_invalidation():
    tracker = state()
    tracker.observe(full(arch()))
    sequence = tracker.snapshot()["sequence"]
    pending = asyncio.create_task(tracker.wait_for_update(sequence, timeout=1))
    await asyncio.sleep(0)
    assert not pending.done()
    tracker.observe(changes(5, arch(9, 2)))
    assert await asyncio.wait_for(pending, 0.1)
    assert await tracker.wait_for_update(sequence, timeout=0.01)
    pending = asyncio.create_task(tracker.wait_for_update(tracker.snapshot()["sequence"], timeout=1))
    await asyncio.sleep(0)
    tracker.invalidate()
    assert await asyncio.wait_for(pending, 0.1) is False


def pet_changes(*rows):
    from rumi_market.game.home_codec import HomeProtoCodec
    codec = HomeProtoCodec()
    return notice(2972, change_list=[{"change_type": kind,
        "data": codec.message_class(2971).DESCRIPTOR.fields_by_name["lumi_list"].message_type._concrete_class(**fields).SerializeToString()}
        for kind, fields in rows])


def test_owned_lumi_batches_become_complete_only_at_login_end():
    tracker = state()
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": 11, "lumi_id": 701}]))
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": 12, "lumi_id": 702}]))
    assert tracker.snapshot()["roster_ready"] is False
    tracker.observe(notice(2913))
    assert tracker.snapshot()["roster_ready"]
    assert [row["uid"] for row in tracker.snapshot()["workers"]] == ["11", "12"]
    tracker.observe(notice(2971))
    tracker.observe(notice(2913))
    assert tracker.snapshot()["roster_ready"] and tracker.snapshot()["workers"] == []


def test_pet_deltas_use_proven_add_delete_and_whole_record_replace():
    tracker = state()
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": 11, "lumi_id": 701, "lumi_state": 128}]))
    tracker.observe(notice(2913))
    tracker.observe(pet_changes((1, {"lumi_uid": 12, "lumi_id": 702})))
    tracker.observe(pet_changes((1, {"lumi_uid": 11, "lumi_id": 999})))
    assert [row["id"] for row in tracker.snapshot()["workers"]] == [701, 702]
    tracker.observe(pet_changes((3, {"lumi_uid": 11, "lumi_id": 703})))
    assert tracker.snapshot()["workers"][0]["raw_state"] is None
    tracker.observe(pet_changes((2, {"lumi_uid": 12})))
    assert [row["uid"] for row in tracker.snapshot()["workers"]] == ["11"]
    tracker.observe(pet_changes((4, {"lumi_uid": 11})))
    assert not tracker.snapshot()["roster_ready"] and tracker.snapshot()["workers"] == []


def test_state_only_notification_does_not_erase_owned_fields_or_create_unknown_pet():
    tracker = state()
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": 11, "lumi_id": 701, "init_aptitude": {"aptitude_work": 32}}]))
    tracker.observe(notice(2913))
    tracker.observe(notice(3235, change_list=[{"lumi_uid": 11, "lumi_state": 128}, {"lumi_uid": 12, "lumi_state": 0}]))
    row = tracker.snapshot()["workers"][0]
    assert (row["id"], row["work_aptitude"], row["raw_state"]) == (701, 32, "128")
    assert len(tracker.snapshot()["workers"]) == 1


def test_dispatch_occupancy_uses_map_value_lumi_uid_and_updates_with_architecture():
    tracker = state()
    tracker.observe(notice(2971, lumi_list=[{"lumi_uid": 11, "lumi_id": 701}]))
    tracker.observe(notice(2913))
    tracker.observe(full(arch(actFarm={"id": 1, "dispatchStores": {900: {"id": 900, "lumiUid": 11,
        "base": {"name": "synthetic-private-role"}}}})))
    snapshot = tracker.snapshot()
    assert snapshot["architectures"][0]["dispatch_uids"] == ["11"]
    assert snapshot["architectures"][0]["has_farm_data"] is True
    assert any(row["kind"] == "dispatch" for row in snapshot["workers"][0]["occupancy"])
    assert "synthetic-private-role" not in json.dumps(snapshot)
    tracker.observe(changes(5, arch(actFarm={"id": 1})))
    assert tracker.snapshot()["architectures"][0]["dispatch_uids"] == []


def test_farm_presence_marker_does_not_infer_missing_or_empty_optional_data():
    tracker = state()
    tracker.observe(full(arch()))
    assert tracker.snapshot()["architectures"][0]["has_farm_data"] is False
    tracker.observe(changes(5, arch(actFarm={})))
    assert tracker.snapshot()["architectures"][0]["has_farm_data"] is True
    assert tracker.snapshot()["dispatch_ready"] is False


def test_nested_projection_budget_fails_closed_before_exposing_oversized_lists(monkeypatch):
    module = implementation()
    tracker = state()
    monkeypatch.setattr(module, "MAX_PROJECTION_ROWS", 3, raising=False)
    tracker.observe(full(arch(dorm={"lumi_list": [{"lumi_uid": 11}, {"lumi_uid": 12}, {"lumi_uid": 13}]})))
    assert not tracker.snapshot()["valid"] and tracker.snapshot()["architectures"] == []


def test_dispatch_full_baselines_and_dynamic_add_replace_remove_are_live():
    tracker = state()
    tracker.observe(full(arch(actFarm={"id": 1})))
    assert tracker.snapshot()["dispatch_ready"] and tracker.snapshot()["dispatch_lumi_uids"] == []
    tracker.observe(notice(10093, info={"id": 1, "dispatchStores": {900: {"id": 900, "lumiUid": 11}}}))
    assert tracker.snapshot()["dispatch_lumi_uids"] == ["11"]
    assert tracker.snapshot()["architectures"][0]["dispatch_uids"] == ["11"]
    tracker.observe(notice(10083, stealInfo={"id": 901, "lumiUid": 12}))
    assert tracker.snapshot()["dispatch_lumi_uids"] == ["11", "12"]
    tracker.observe(notice(10091, infos=[{"id": 900, "lumiUid": 13}]))
    assert tracker.snapshot()["dispatch_lumi_uids"] == ["12", "13"]
    tracker.observe(notice(10089, id=900))
    assert tracker.snapshot()["dispatch_lumi_uids"] == ["12"]
    tracker.observe(notice(10093, info={"id": 1}))
    assert tracker.snapshot()["dispatch_ready"] and tracker.snapshot()["dispatch_lumi_uids"] == []


def test_dispatch_deltas_never_establish_or_repair_missing_complete_baseline():
    tracker = state()
    tracker.observe(full(arch()))
    assert not tracker.snapshot()["dispatch_ready"]
    tracker.observe(notice(10091, infos=[{"id": 900, "lumiUid": 11}]))
    tracker.observe(notice(10083, stealInfo={"id": 901, "lumiUid": 12}))
    assert not tracker.snapshot()["dispatch_ready"]
    tracker.observe(notice(10093, info={"id": 1}))
    assert tracker.snapshot()["dispatch_ready"]
    tracker.observe(notice(10083))
    assert not tracker.snapshot()["dispatch_ready"] and tracker.snapshot()["valid"]


async def test_handshake_observes_home_independently_of_archive_and_gold_failures():
    from rumi_market.game.handshake import GameHandshake
    from test_game_client import MemoryTransport
    from test_profile_handshake_integration import AccountStateObserver, Collector

    class BrokenArchive(Collector):
        def observe(self, response):
            raise ValueError("synthetic-private-archive-error")

    tracker = state()
    gold = AccountStateObserver(fail=True)
    handshake = GameHandshake(MemoryTransport(), profile_collector=BrokenArchive(),
                              account_state=gold, home_state=tracker)
    handshake._observe_response(full(arch()))
    assert tracker.snapshot()["valid"] and gold.invalidated
    assert handshake.profile_archive_status == "failed"
    await handshake.close()
    assert not tracker.snapshot()["valid"] and tracker._closed


async def test_home_observer_failure_does_not_stop_other_handshake_consumers():
    from rumi_market.game.handshake import GameHandshake
    from test_game_client import MemoryTransport
    from test_profile_handshake_integration import AccountStateObserver, Collector

    gold, home, collector = AccountStateObserver(), AccountStateObserver(fail=True), Collector()
    handshake = GameHandshake(MemoryTransport(), profile_collector=collector,
                              account_state=gold, home_state=home)
    response = full(arch())
    handshake._observe_response(response)
    assert home.invalidated and gold.commands == collector.commands == [3001]
    await handshake.close()
    assert home.closed and gold.closed and collector.closed
