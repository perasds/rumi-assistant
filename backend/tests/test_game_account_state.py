"""Synthetic, network-free currency snapshot and event-driven update contracts."""

import asyncio
import hashlib
import importlib
from importlib.resources import files
import json
from pathlib import Path
import re

import pytest

from rumi_market.game.envelope import ResponseEnvelope
from test_game_profile_sync import message, response


def implementation():
    try:
        return importlib.import_module("rumi_market.game.account_state")
    except ModuleNotFoundError:
        pytest.fail("The live gold item state is not implemented")


def state():
    return implementation().GoldItemState(verified_version=True)


def full(*rows):
    return response(2941, message("Cmd_Notify_ItemAll", item_list=list(rows)).SerializeToString())


def item(count=20, *, item_id=10001, uid=0):
    return {"item_id": item_id, "item_uid": uid, "count": count}


def changes(*rows):
    return response(2942, message("Cmd_Notify_ItemChange", change_list=[
        {"change_type": kind, "data": message("CmdItemReal", **fields).SerializeToString()}
        for kind, fields in rows]).SerializeToString())


def combined(*notices):
    return response(2016, message("Cmd_Notify_Combine_Info", unit_lst=[
        {"id": notice.command, "msg": notice.data} for notice in notices]).SerializeToString())


def test_gold_source_manifest_binds_version_and_complete_no_alias_configuration_evidence():
    module = implementation()
    source = json.loads(files("rumi_market.game").joinpath("gold-source.json").read_bytes())
    assert source["verified_client_version"] == module.GOLD_CLIENT_VERSION == "1.5.3"
    assert source["verified_hot_version"] == module.GOLD_HOT_VERSION == 62
    assert source["verified_hot_versions"] == sorted(module.GOLD_HOT_VERSIONS) == [62, 64, 65, 69, 71, 72]
    current = source["additional_verified_versions"][0]
    assert current["hot_version"] == 64 and current["gold_item_id"] == 10001
    assert current["gold_methods_semantically_unchanged"] is True
    assert current["gold_item_has_no_alias"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", current["source_game_sha256"])
    assert current["pachinko_table"]["strict_eof"] is True
    hot65 = source["additional_verified_versions"][1]
    assert hot65["hot_version"] == 65 and hot65["gold_item_id"] == 10001
    assert hot65["gold_methods_semantically_unchanged"] is True
    assert hot65["gold_item_has_no_alias"] is True
    assert hot65["gold_wire_definitions_unchanged"] is True
    assert hot65["gold_normalization_tables_unchanged"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", hot65["verification_report_sha256"])
    hot69 = next(row for row in source["additional_verified_versions"] if row["hot_version"] == 69)
    assert hot69["gold_item_id"] == 10001 and hot69["gold_item_has_no_alias"] is True
    assert hot69["gold_consumed_contract_compatible"] is True
    assert hot69["gold_wire_consumed_fields_unchanged"] is True
    assert hot69["gold_normalization_for_item_10001_unchanged"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", hot69["verification_report_sha256"])
    assert source["gold_item_id"] == module.GOLD_ITEM_ID == 10001
    assert source["normalization"]["item_10001_has_no_entry"] is True
    assert source["normalization"]["other_item_ids_can_alias_to_10001_in_verified_tables"] is False
    assert len(source["tables"]) == 5
    assert all(row["strict_eof"] and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) for row in source["tables"])
    assert all(10001 not in row.get("all_target_item_ids", []) for row in source["tables"])
    assert not source["network_requests_sent"] and not source["private_values_exported"]


@pytest.mark.external_materials
@pytest.mark.parametrize("hot_version", [65, 69])
def test_gold_source_reports_match_manifest_identity(external_materials_root, hot_version):
    source = json.loads(files("rumi_market.game").joinpath("gold-source.json").read_bytes())
    candidate = next(row for row in source["additional_verified_versions"] if row["hot_version"] == hot_version)
    raw = (external_materials_root / candidate["verification_report"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == candidate["verification_report_sha256"]
    report = json.loads(raw)
    assert report["candidate_hot_version"] == hot_version
    if hot_version == 65:
        assert report["static_gold_compatibility_confirmed"]
        assert report["source_game_sha256"] == candidate["source_game_sha256"]
    else:
        assert report["new_game_sha256"] == candidate["source_game_sha256"]
        assert report["reviewed_version_gate_support"]["GOLD_HOT_VERSIONS"]["may_add_69"] is True


@pytest.mark.external_materials
def test_hot71_gold_evidence_binds_identical_game_and_every_normalization_table(external_materials_root):
    source = json.loads(files("rumi_market.game").joinpath("gold-source.json").read_bytes())
    candidate = next(row for row in source["additional_verified_versions"] if row["hot_version"] == 71)
    report_bytes = (external_materials_root / candidate["verification_report"]).read_bytes()
    assert hashlib.sha256(report_bytes).hexdigest() == candidate["verification_report_sha256"]
    report = json.loads(report_bytes)
    assert report["baseline_hot_version"] == 69 and report["candidate_hot_version"] == 71
    assert report["static_gold_compatibility_confirmed"] is True
    assert report["same_game_bytes"] is True
    assert report["old_game_sha256"] == report["new_game_sha256"] == candidate["source_game_sha256"]
    assert {row["table"] for row in report["gold_tables"]} == {
        "ItemUse", "ActCakeOpen", "ActPachinkoOpen", "ActFightDragonOpen", "ActFarm",
    }
    assert all(row["same_bytes"] and row["old_sha256"] == row["new_sha256"] for row in report["gold_tables"])
    assert report["gold_normalization"]["item_use_10001_rows"] == []
    assert report["gold_normalization"]["dynamic_branches_target_10001"] is False
    assert {row["rpc_id"] for row in report["gold_wire_contract"]["bindings"]} == {2016, 2941, 2942}
    assert report["acquisition"]["manifest"]["catalog_sha256"] == candidate["source_catalog_sha256"]
    assert report["acquisition"]["manifest"]["files"]["vib"]["encrypted_sha256"] == candidate["source_vib_bundle_sha256"]
    assert report["acquisition"]["account_login_requests_sent"] is False


def test_gold_starts_unknown_and_verified_full_snapshot_sums_all_currency_stacks():
    tracker = state()
    assert tracker.gold_balance is None and tracker.gold_revision == 0
    tracker.observe(full(item(12), item(8, uid=9), item(900, item_id=20002)))
    assert tracker.gold_balance == 20 and tracker.gold_revision == 1


def test_empty_full_snapshot_means_zero_and_replaces_previous_gold_items():
    tracker = state()
    tracker.observe(full(item(20)))
    tracker.observe(full(item(8, item_id=20002)))
    assert tracker.gold_balance == 0 and tracker.gold_revision == 2
    tracker.observe(full())
    assert tracker.gold_balance == 0 and tracker.gold_revision == 3


def test_new_delete_and_modify_use_absolute_counts_and_uid_zero_is_valid():
    tracker = state()
    tracker.observe(full({"item_id": 10001, "count": 20}))
    tracker.observe(changes((1, item(6, uid=9))))
    assert tracker.gold_balance == 26
    tracker.observe(changes((3, item(11))))
    assert tracker.gold_balance == 17
    tracker.observe(changes((2, {"item_id": 10001, "item_uid": 9})))
    assert tracker.gold_balance == 11
    tracker.observe(changes((2, {"item_id": 10001})))
    assert tracker.gold_balance == 0


def test_duplicate_new_and_unrelated_items_do_not_change_gold_revision():
    tracker = state()
    tracker.observe(full(item(20)))
    revision = tracker.gold_revision
    tracker.observe(changes((1, item(999)), (1, item(50, item_id=20002))))
    assert tracker.gold_balance == 20 and tracker.gold_revision == revision
    tracker.observe(response(9999, b"\xff"))
    assert tracker.gold_balance == 20 and tracker.gold_revision == revision


def test_combined_messages_apply_full_then_delta_in_source_order():
    tracker = state()
    tracker.observe(combined(full(item(30)), combined(changes((3, item(8))))))
    assert tracker.gold_balance == 8 and tracker.gold_revision == 2


def test_delta_before_full_snapshot_does_not_invent_a_balance():
    tracker = state()
    tracker.observe(changes((1, item(30))))
    assert tracker.gold_balance is None
    tracker.observe(full(item(4)))
    assert tracker.gold_balance == 4


@pytest.mark.parametrize("bad", [
    full(item(-1)), full({"item_id": 10001}), full({"count": 4}),
    full(item(1), item(2)), full(item(2**63 - 1), item(1, uid=2)),
    changes((4, item(1))), changes((0, item(1))), changes((3, {"item_id": 10001})),
    response(2941, b"\xff"), response(2942, b"\xff"), response(2016, b"\xff"),
    response(2941, b"", code=-1),
    response(2942, message("Cmd_Notify_ItemChange", change_list=[{"change_type": 3, "data": b"\xff"}]).SerializeToString()),
])
def test_invalid_account_updates_invalidate_until_a_new_full_snapshot(bad):
    tracker = state()
    tracker.observe(full(item(30)))
    tracker.observe(bad)
    assert tracker.gold_balance is None
    tracker.observe(changes((1, item(30))))
    assert tracker.gold_balance is None
    tracker.observe(full(item(9)))
    assert tracker.gold_balance == 9


def test_filtered_non_gold_malformed_update_also_invalidates_account_state():
    tracker = state()
    tracker.observe(full(item(30)))
    tracker.observe(changes((3, {"item_id": 20002})))
    assert tracker.gold_balance is None


@pytest.mark.parametrize("verified", [False])
def test_unverified_configuration_version_never_exposes_a_balance(verified):
    tracker = implementation().GoldItemState(verified_version=verified)
    tracker.observe(full(item(20)))
    assert tracker.gold_balance is None and tracker.gold_revision == 0


@pytest.mark.parametrize("verified", [None, 0, 1, "true"])
def test_version_verification_flag_is_strictly_boolean(verified):
    with pytest.raises(ValueError, match="^GAME_ACCOUNT_STATE_INVALID$"):
        implementation().GoldItemState(verified_version=verified)


@pytest.mark.parametrize("limit", ["MAX_COMBINE_DEPTH", "MAX_COMBINE_UNITS", "MAX_ITEM_ROWS"])
def test_collection_and_recursion_limits_fail_closed(monkeypatch, limit):
    module = implementation()
    tracker = module.GoldItemState(verified_version=True)
    tracker.observe(full(item(30)))
    monkeypatch.setattr(module, limit, 1)
    if limit == "MAX_COMBINE_DEPTH":
        notice = combined(combined(combined(full(item(1)))))
    elif limit == "MAX_COMBINE_UNITS":
        notice = combined(full(item(1)), full(item(2)))
    else:
        notice = full(item(1), item(2, uid=3))
    tracker.observe(notice)
    assert tracker.gold_balance is None


def test_incomplete_envelope_is_not_treated_as_a_snapshot():
    tracker = state()
    tracker.observe(full(item(30)))
    tracker.observe(ResponseEnvelope(2941, 0, 4, 0, 1, full(item(5)).data))
    assert tracker.gold_balance is None


def test_gold_item_map_cannot_grow_past_inventory_limit_across_delta_messages(monkeypatch):
    module = implementation()
    tracker = state()
    monkeypatch.setattr(module, "MAX_ITEM_ROWS", 2)
    tracker.observe(full(item(20)))
    tracker.observe(changes((1, item(3, uid=1))))
    assert tracker.gold_balance == 23
    tracker.observe(changes((1, item(4, uid=2))))
    assert tracker.gold_balance is None


def test_missing_combined_unit_command_invalidates_instead_of_skipping_unknown_state():
    tracker = state()
    tracker.observe(full(item(30)))
    tracker.observe(response(2016, message("Cmd_Notify_Combine_Info", unit_lst=[
        {"msg": full(item(2)).data}]).SerializeToString()))
    assert tracker.gold_balance is None


def test_unavailable_verified_schema_is_fail_closed(monkeypatch):
    module = implementation()
    def failed_schema():
        raise ValueError("synthetic-private-schema-error")
    monkeypatch.setattr(module, "_account_schema", failed_schema)
    tracker = module.GoldItemState(verified_version=True)
    tracker.observe(full(item(20)))
    assert tracker.gold_balance is None and tracker.gold_revision == 0


def test_delta_collection_limit_is_checked_before_applying_changes(monkeypatch):
    module = implementation()
    tracker = state()
    tracker.observe(full(item(20)))
    monkeypatch.setattr(module, "MAX_ITEM_CHANGES", 1)
    tracker.observe(changes((3, item(10)), (1, item(3, uid=9))))
    assert tracker.gold_balance is None


def test_payload_limit_is_checked_before_decoding(monkeypatch):
    module = implementation()
    tracker = state()
    tracker.observe(full(item(20)))
    monkeypatch.setattr(module, "BUSINESS_MESSAGE_LIMIT", 1)
    tracker.observe(full(item(4)))
    assert tracker.gold_balance is None


async def test_wait_is_event_driven_and_requires_exact_balance_and_new_gold_revision():
    tracker = state()
    tracker.observe(full(item(30)))
    revision = tracker.gold_revision
    waiting = asyncio.create_task(tracker.wait_for_gold_balance(20, revision, timeout=1))
    await asyncio.sleep(0)
    tracker.observe(changes((1, item(500, item_id=20002))))
    await asyncio.sleep(0)
    assert not waiting.done()
    tracker.observe(changes((3, item(25))))
    await asyncio.sleep(0)
    assert not waiting.done()
    tracker.observe(changes((3, item(20))))
    assert await asyncio.wait_for(waiting, 0.1) is True


async def test_wait_accepts_result_already_received_and_does_not_miss_notifications():
    tracker = state()
    tracker.observe(full(item(30)))
    revision = tracker.gold_revision
    tracker.observe(changes((3, item(20))))
    assert await tracker.wait_for_gold_balance(20, revision, timeout=0.01) is True


async def test_wait_accepts_observed_debit_followed_by_income_without_replacing_current_balance():
    tracker = state()
    tracker.observe(full(item(1000)))
    revision = tracker.gold_revision
    tracker.observe(changes((3, item(900))))
    tracker.observe(changes((3, item(950))))
    assert await tracker.wait_for_gold_balance(900, revision, timeout=0.01) is True
    assert tracker.gold_balance == 950 and tracker.gold_revision == revision + 2


async def test_waiter_already_waiting_can_see_debit_and_income_delivered_before_it_resumes():
    tracker = state()
    tracker.observe(full(item(1000)))
    waiting = asyncio.create_task(tracker.wait_for_gold_balance(900, tracker.gold_revision, timeout=0.1))
    await asyncio.sleep(0)
    tracker.observe(changes((3, item(900))))
    tracker.observe(changes((3, item(950))))
    assert await waiting is True
    assert tracker.gold_balance == 950


async def test_invalidation_removes_old_matching_observations_even_after_new_baseline():
    tracker = state()
    tracker.observe(full(item(1000)))
    revision = tracker.gold_revision
    tracker.observe(changes((3, item(900))))
    tracker.observe(changes((3, item(950))))
    tracker.invalidate()
    assert await tracker.wait_for_gold_balance(900, revision, timeout=0.01) is False
    tracker.observe(full(item(1000)))
    assert await tracker.wait_for_gold_balance(900, revision, timeout=0.01) is False
    assert tracker.gold_balance == 1000


async def test_bounded_history_drops_old_evidence_instead_of_confirming_evicted_balance(monkeypatch):
    module = implementation()
    monkeypatch.setattr(module, "MAX_GOLD_HISTORY", 2, raising=False)
    tracker = state()
    tracker.observe(full(item(1000)))
    revision = tracker.gold_revision
    tracker.observe(changes((3, item(900))))
    tracker.observe(changes((3, item(950))))
    assert await tracker.wait_for_gold_balance(900, revision, timeout=0.01) is True
    tracker.observe(changes((3, item(975))))
    assert await tracker.wait_for_gold_balance(900, revision, timeout=0.01) is False
    assert tracker.gold_balance == 975


async def test_history_matching_still_requires_a_strictly_newer_gold_revision():
    tracker = state()
    tracker.observe(full(item(1000)))
    tracker.observe(changes((3, item(900))))
    revision = tracker.gold_revision
    tracker.observe(changes((3, item(950))))
    assert await tracker.wait_for_gold_balance(900, revision, timeout=0.01) is False
    tracker.close()
    assert await tracker.wait_for_gold_balance(950, revision, timeout=0.01) is False


async def test_old_equal_balance_does_not_complete_wait_without_a_fresh_revision():
    tracker = state()
    tracker.observe(full(item(20)))
    assert await tracker.wait_for_gold_balance(20, tracker.gold_revision, timeout=0.01) is False


@pytest.mark.parametrize("action", ["invalidate", "close"])
async def test_invalidation_and_close_wake_waiters_without_a_timeout(action):
    tracker = state()
    tracker.observe(full(item(30)))
    waiting = asyncio.create_task(tracker.wait_for_gold_balance(20, tracker.gold_revision, timeout=5))
    await asyncio.sleep(0)
    getattr(tracker, action)()
    assert await asyncio.wait_for(waiting, 0.1) is False
    assert tracker.gold_balance is None
    if action == "close":
        tracker.observe(full(item(50)))
        assert tracker.gold_balance is None


async def test_multiple_waiters_observe_same_update_independently():
    tracker = state()
    tracker.observe(full(item(30)))
    tasks = [asyncio.create_task(tracker.wait_for_gold_balance(20, tracker.gold_revision, timeout=1)) for _ in range(2)]
    await asyncio.sleep(0)
    tracker.observe(changes((3, item(20))))
    assert await asyncio.gather(*tasks) == [True, True]


async def test_cancelled_waiter_does_not_cancel_shared_notification_or_other_waiters():
    tracker = state()
    tracker.observe(full(item(30)))
    first = asyncio.create_task(tracker.wait_for_gold_balance(20, tracker.gold_revision, timeout=1))
    second = asyncio.create_task(tracker.wait_for_gold_balance(20, tracker.gold_revision, timeout=1))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    tracker.observe(changes((3, item(20))))
    assert await second is True


@pytest.mark.parametrize("expected,revision,timeout", [
    (True, 0, 1), (-1, 0, 1), (2**63, 0, 1), (1, True, 1), (1, -1, 1),
    (1, 0, 0), (1, 0, float("nan")), (1, 0, True),
])
async def test_wait_rejects_invalid_parameters_with_safe_code(expected, revision, timeout):
    with pytest.raises(ValueError, match="^GAME_ACCOUNT_STATE_INVALID$"):
        await state().wait_for_gold_balance(expected, revision, timeout=timeout)
