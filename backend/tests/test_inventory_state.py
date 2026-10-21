"""Synthetic full inventory projection; no accounts or game operations."""

import importlib

import pytest

from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.profile_sync import _load_schema


def implementation():
    try:
        return importlib.import_module("rumi_market.game.inventory_state")
    except ModuleNotFoundError:
        pytest.fail("The full inventory projection is not implemented")


def response(command, data=b"", code=0, remaining=0):
    return ResponseEnvelope(command, 0, 0, code, remaining, data)


def full(*rows):
    classes, _, _ = _load_schema()
    return response(2941, classes[2941](item_list=rows).SerializeToString())


def item(count=10, item_id=101, uid=0):
    return {"item_id": item_id, "item_uid": uid, "count": count}


def delta(*rows):
    classes, item_class, _ = _load_schema()
    return response(2942, classes[2942](change_list=[
        {"change_type": kind, "data": item_class(**row).SerializeToString()}
        for kind, row in rows]).SerializeToString())


def combined(*notices):
    classes, _, _ = _load_schema()
    return response(2016, classes[2016](unit_lst=[
        {"id": notice.command, "msg": notice.data} for notice in notices]).SerializeToString())


def test_inventory_requires_a_full_baseline_and_sums_distinct_instances():
    state = implementation().InventoryState(True)
    assert state.snapshot()["valid"] is False and state.snapshot()["items"] == {}
    state.observe(delta((1, item(8))))
    assert not state.snapshot()["valid"]
    state.observe(full(item(3), item(4, uid=99), item(9, item_id=102)))
    snapshot = state.snapshot()
    assert snapshot["valid"] and snapshot["items"] == {"101": 7, "102": 9}
    assert snapshot["sequence"] == 1 and snapshot["observed_at"]
    assert snapshot["unavailable_reason"] is None
    assert set(snapshot) == {"valid", "items", "sequence", "observed_at", "unavailable_reason",
                             "received_totals", "spent_totals", "movement_epoch"}


def test_inventory_changes_are_absolute_and_duplicate_new_is_ignored():
    state = implementation().InventoryState(True)
    state.observe(full(item(8)))
    state.observe(delta((1, item(5, uid=9)), (1, item(100)), (3, item(2))))
    assert state.snapshot()["items"] == {"101": 7}
    sequence = state.snapshot()["sequence"]
    state.observe(delta((1, item(999)), (3, item(2))))
    assert state.snapshot()["sequence"] == sequence
    state.observe(delta((2, {"item_id": 101, "item_uid": 9})))
    assert state.snapshot()["items"] == {"101": 2}
    state.observe(delta((2, {"item_id": 101})))
    assert state.snapshot()["valid"] and state.snapshot()["items"] == {}


def test_inventory_full_replaces_state_and_retains_exact_large_totals():
    state = implementation().InventoryState(True)
    large = 2**63 - 1
    state.observe(full(item(large), item(large, uid=2)))
    assert state.snapshot()["items"] == {"101": large * 2}
    snapshot = state.snapshot()
    snapshot["items"]["101"] = 0
    assert state.snapshot()["items"]["101"] == large * 2
    state.observe(full())
    assert state.snapshot()["valid"] and state.snapshot()["items"] == {}


def test_inventory_combines_full_and_delta_in_order_and_ignores_unknown_commands():
    state = implementation().InventoryState(True)
    state.observe(combined(full(item(8)), combined(delta((3, item(5)))), response(9999, b"\xff")))
    assert state.snapshot()["items"] == {"101": 5}


@pytest.mark.parametrize("make_bad", [
    lambda: full(item(-1)), lambda: full({"item_id": 101}),
    lambda: full({"count": 1}), lambda: full(item(), item()),
    lambda: delta((0, item())), lambda: delta((4, item())),
    lambda: delta((3, {"item_id": 101})),
    lambda: response(2941, b"\xff"), lambda: response(2942, b"\xff"),
    lambda: response(2941, code=2), lambda: response(2942, remaining=1),
    lambda: response(2016, b"\xff"),
])
def test_inventory_invalid_updates_clear_state_until_full_recovery(make_bad):
    state = implementation().InventoryState(True)
    state.observe(full(item()))
    state.observe(make_bad())
    assert not state.snapshot()["valid"] and state.snapshot()["items"] == {}
    assert state.snapshot()["observed_at"] is None
    state.observe(delta((1, item())))
    assert not state.snapshot()["valid"]
    state.observe(full(item(7)))
    assert state.snapshot()["valid"] and state.snapshot()["items"] == {"101": 7}


@pytest.mark.parametrize("limit", ["MAX_ITEM_ROWS", "MAX_ITEM_CHANGES", "MAX_COMBINE_UNITS", "MAX_COMBINE_DEPTH"])
def test_inventory_capacity_limits_invalidate(monkeypatch, limit):
    module = implementation()
    state = module.InventoryState(True)
    state.observe(full(item()))
    monkeypatch.setattr(module, limit, 1)
    notice = {"MAX_ITEM_ROWS": full(item(), item(uid=2)),
              "MAX_ITEM_CHANGES": delta((3, item(1)), (3, item(2))),
              "MAX_COMBINE_UNITS": combined(full(), full()),
              "MAX_COMBINE_DEPTH": combined(combined(full()))}[limit]
    state.observe(notice)
    assert not state.snapshot()["valid"]


def test_inventory_unverified_version_and_close_never_expose_items():
    state = implementation().InventoryState(False)
    state.observe(full(item()))
    assert not state.snapshot()["valid"] and state.snapshot()["sequence"] == 0
    assert state.snapshot()["unavailable_reason"] == "INVENTORY_VERSION_UNVERIFIED"
    state = implementation().InventoryState(True)
    state.observe(full(item()))
    state.close()
    state.observe(full(item()))
    assert not state.snapshot()["valid"] and state.snapshot()["items"] == {}


def test_inventory_missing_combined_payload_cannot_invent_an_empty_baseline():
    classes, _, _ = _load_schema()
    state = implementation().InventoryState(True)
    state.observe(response(2016, classes[2016](unit_lst=[{"id": 2941}]).SerializeToString()))
    assert not state.snapshot()["valid"]


@pytest.mark.parametrize("verified", [0, 1, None, "true"])
def test_inventory_version_flag_is_strict(verified):
    with pytest.raises(ValueError, match="^GAME_INVENTORY_STATE_INVALID$"):
        implementation().InventoryState(verified)
