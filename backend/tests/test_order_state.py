"""Synthetic order baselines and full replacement notifications."""

import importlib

import pytest

from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.order_codec import OrderProtoCodec


def implementation():
    try:
        return importlib.import_module("rumi_market.game.order_state")
    except ModuleNotFoundError:
        pytest.fail("The order state projection is not implemented")


def response(command, data=b"", code=0, remaining=0):
    return ResponseEnvelope(command, 0, 0, code, remaining, data)


def notice(command=3001, slots=None, **fields):
    info = {"slot_list": slots if slots is not None else [slot()], **fields}
    return response(command, OrderProtoCodec().encode(command, order_archi=info))


def slot(slot_id=1, **fields):
    return {"slot_id": slot_id, "is_unlock": True, "order_info": {
        "order_type": 2, "item_need": [{"item_id": 101, "count": 3}],
        "item_reward": [{"item_id": 102, "count": 4}],
        "refresh_time": 1234, "refresh_seconds": 30}, **fields}


def combined(*notices):
    return response(2016, OrderProtoCodec().encode(2016, unit_lst=[
        {"id": notice.command, "msg": notice.data} for notice in notices]))


def test_order_requires_baseline_and_projects_only_whitelisted_fields():
    state = implementation().OrderState(True)
    assert not state.snapshot()["valid"] and state.snapshot()["orders"] == []
    state.observe(notice(today_complete_times=0, currentEnergy=20, limitEnergy=300))
    snapshot = state.snapshot()
    assert snapshot["valid"] and snapshot["sequence"] == 1 and snapshot["observed_at"]
    assert snapshot["unavailable_reason"] is None
    assert snapshot["today_complete_times"] == 0
    assert snapshot["current_energy"] == 20 and snapshot["limit_energy"] == 300
    assert snapshot["orders"] == [{"slot_id": "1", "is_unlock": True, "order_type": 2,
                                    "materials": [{"item_id": "101", "count": 3}],
                                    "rewards": [{"item_id": "102", "count": 4}],
                                    "refresh_time": 1234, "refresh_seconds": 30}]
    assert set(snapshot) == {"valid", "sequence", "observed_at", "unavailable_reason", "orders",
                             "today_complete_times", "current_energy", "limit_energy"}


def test_order_3301_is_complete_replacement_and_can_establish_baseline():
    state = implementation().OrderState(True)
    state.observe(notice(3301, slots=[slot(2)], today_complete_times=4))
    assert state.snapshot()["valid"] and state.snapshot()["orders"][0]["slot_id"] == "2"
    state.observe(notice(3301, slots=[slot(3)], currentEnergy=0))
    assert [row["slot_id"] for row in state.snapshot()["orders"]] == ["3"]
    assert state.snapshot()["today_complete_times"] is None
    assert state.snapshot()["current_energy"] == 0
    state.observe(notice(slots=[]))
    assert state.snapshot()["valid"] and state.snapshot()["orders"] == []


def test_order_preserves_absent_values_locked_slots_and_large_quantities():
    state = implementation().OrderState(True)
    state.observe(notice(slots=[{"slot_id": 0, "is_unlock": False},
                                slot(2, order_info={"item_need": [{"item_id": 101, "count": 2**63 - 1}]})]))
    snapshot = state.snapshot()
    assert snapshot["valid"] and snapshot["today_complete_times"] is None
    assert snapshot["current_energy"] is None and snapshot["limit_energy"] is None
    locked, partial = snapshot["orders"]
    assert locked == {"slot_id": "0", "is_unlock": False, "order_type": None,
                      "materials": [], "rewards": [], "refresh_time": None, "refresh_seconds": None}
    assert partial["order_type"] is None and partial["refresh_time"] is None
    assert partial["materials"][0]["count"] == 2**63 - 1


def test_order_duplicate_and_unrelated_ack_do_not_advance_sequence():
    state = implementation().OrderState(True)
    state.observe(notice())
    sequence = state.sequence
    state.observe(notice())
    state.observe(response(3303, OrderProtoCodec().encode(3303, reward=[{"item_id": 1, "count": 9}])))
    state.observe(response(9999, b"\xff"))
    assert state.sequence == sequence
    snapshot = state.snapshot()
    snapshot["orders"][0]["materials"][0]["count"] = 999
    assert state.snapshot()["orders"][0]["materials"][0]["count"] == 3


def test_order_combined_updates_and_refresh_times_use_server_fields_without_local_expiry():
    state = implementation().OrderState(True)
    state.observe(combined(notice(), combined(notice(3301, slots=[slot(2, order_info={
        "order_type": 0, "refresh_time": 1, "refresh_seconds": 0})]))))
    row = state.snapshot()["orders"][0]
    assert row["slot_id"] == "2" and row["refresh_time"] == 1 and row["refresh_seconds"] == 0
    assert row["order_type"] == 0


@pytest.mark.parametrize("make_bad", [
    lambda: response(3001), lambda: response(3301),
    lambda: notice(slots=[slot(), slot()]),
    lambda: notice(slots=[{"is_unlock": True}]),
    lambda: notice(slots=[{"slot_id": 1}]),
    lambda: notice(slots=[slot(order_info={"item_need": [{"item_id": 1}]})]),
    lambda: notice(slots=[slot(order_info={"item_need": [{"count": 1}]})]),
    lambda: notice(slots=[slot(order_info={"item_reward": [{"item_id": 1, "count": -1}]})]),
    lambda: notice(slots=[slot(order_info={"refresh_seconds": -1})]),
    lambda: notice(today_complete_times=-1),
    lambda: response(3301, b"\xff"), lambda: response(3301, code=1),
    lambda: response(3001, remaining=1), lambda: response(2016, b"\xff"),
])
def test_order_invalid_updates_clear_orders_and_allow_new_baseline(make_bad):
    state = implementation().OrderState(True)
    state.observe(notice())
    state.observe(make_bad())
    snapshot = state.snapshot()
    assert not snapshot["valid"] and snapshot["orders"] == [] and snapshot["observed_at"] is None
    assert snapshot["today_complete_times"] is None
    state.observe(notice(3301))
    assert state.snapshot()["valid"]


@pytest.mark.parametrize("limit", ["MAX_ORDER_SLOTS", "MAX_ORDER_ITEMS", "MAX_PROJECTION_ROWS", "MAX_COMBINE_UNITS", "MAX_COMBINE_DEPTH"])
def test_order_capacity_limits_invalidate(monkeypatch, limit):
    module = implementation()
    state = module.OrderState(True)
    state.observe(notice())
    monkeypatch.setattr(module, limit, 1)
    notices = {"MAX_ORDER_SLOTS": notice(slots=[slot(1), slot(2)]),
               "MAX_ORDER_ITEMS": notice(slots=[slot(order_info={"item_need": [{"item_id": 1, "count": 1}, {"item_id": 2, "count": 1}]})]),
               "MAX_PROJECTION_ROWS": notice(), "MAX_COMBINE_UNITS": combined(notice(), notice()),
               "MAX_COMBINE_DEPTH": combined(combined(notice()))}
    state.observe(notices[limit])
    assert not state.snapshot()["valid"]


def test_order_unverified_version_and_close_clear_all_order_state():
    state = implementation().OrderState(False)
    state.observe(notice())
    assert not state.snapshot()["valid"] and state.sequence == 0
    assert state.snapshot()["unavailable_reason"] == "ORDER_VERSION_UNVERIFIED"
    state = implementation().OrderState(True)
    state.observe(notice())
    state.close()
    state.observe(notice())
    assert not state.snapshot()["valid"] and state.snapshot()["orders"] == []
    assert state.snapshot()["current_energy"] is None


def test_order_missing_nested_combined_payload_invalidates_existing_baseline():
    state = implementation().OrderState(True)
    state.observe(notice())
    state.observe(response(2016, OrderProtoCodec().encode(2016, unit_lst=[{"id": 2016}])))
    assert not state.snapshot()["valid"]


@pytest.mark.parametrize("verified", [0, 1, None, "true"])
def test_order_version_flag_is_strict(verified):
    with pytest.raises(ValueError, match="^GAME_ORDER_STATE_INVALID$"):
        implementation().OrderState(verified)
