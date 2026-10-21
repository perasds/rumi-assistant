"""Local cumulative evidence from verified absolute inventory notifications."""

import pytest

from test_inventory_state import combined, delta, full, implementation, item, response


def movements(state):
    snapshot = state.snapshot()
    return snapshot.get('received_totals'), snapshot.get('spent_totals')


def test_first_complete_inventory_only_establishes_movement_baseline():
    state = implementation().InventoryState(True)
    assert movements(state) == ({}, {})
    state.observe(delta((1, item(4))))
    state.observe(full(item(10), item(5, uid=99)))
    assert movements(state) == ({}, {})


def test_rebuilt_baseline_uses_new_epoch_even_when_movement_totals_match():
    state = implementation().InventoryState(True)
    assert state.snapshot().get('movement_epoch') == 0
    state.observe(full(item(10)))
    state.observe(delta((3, item(8))))
    before = state.snapshot()
    state.invalidate()
    assert state.snapshot()['movement_epoch'] == 1
    state.observe(full(item(10)))
    state.observe(delta((3, item(8))))
    after = state.snapshot()
    assert after['spent_totals'] == before['spent_totals'] == {'101': 2}
    assert after['movement_epoch'] != before['movement_epoch']
    state.close()
    assert state.snapshot()['movement_epoch'] == 2


def test_absolute_debit_then_credit_are_retained_when_balance_recovers():
    state = implementation().InventoryState(True)
    state.observe(full(item(10)))
    state.observe(delta((3, item(8))))
    state.observe(delta((3, item(12))))
    assert movements(state) == ({'101': 4}, {'101': 2})
    snapshot = state.snapshot()
    snapshot['received_totals']['101'] = 999
    snapshot['spent_totals'].clear()
    assert movements(state) == ({'101': 4}, {'101': 2})


@pytest.mark.parametrize('final_count', [13, 10])
def test_one_stackable_notice_retains_debit_and_credit_even_when_net_zero(final_count):
    state = implementation().InventoryState(True)
    state.observe(full(item(10), item(4, item_id=102)))
    sequence = state.sequence
    state.observe(delta((3, item(8)), (3, item(final_count)), (3, item(5, item_id=102))))
    assert state.snapshot()['items'] == {'101': final_count, '102': 5}
    assert movements(state) == ({'101': final_count - 8, '102': 1}, {'101': 2})
    assert state.sequence == sequence + 1


def test_net_zero_stackable_changes_publish_evidence_without_another_item_change():
    state = implementation().InventoryState(True)
    state.observe(full(item(10)))
    sequence = state.sequence
    state.observe(delta((3, item(8)), (3, item(10))))
    assert state.snapshot()['items'] == {'101': 10}
    assert movements(state) == ({'101': 2}, {'101': 2})
    assert state.sequence == sequence + 1


def test_stackable_insert_then_remove_retains_both_movements_when_inventory_stays_empty():
    state = implementation().InventoryState(True)
    state.observe(full())
    sequence = state.sequence
    state.observe(delta((1, item(5)), (2, {'item_id': 101})))
    assert state.snapshot()['items'] == {}
    assert movements(state) == ({'101': 5}, {'101': 5})
    assert state.sequence == sequence + 1


@pytest.mark.parametrize('reverse', [False, True])
def test_stackable_instance_migration_does_not_invent_debits_or_credits(reverse):
    state = implementation().InventoryState(True)
    old_uid, new_uid = (99, 0) if reverse else (0, 99)
    state.observe(full(item(10, uid=old_uid)))
    state.observe(delta((2, {'item_id': 101, 'item_uid': old_uid}), (1, item(10, uid=new_uid))))
    assert state.snapshot()['items'] == {'101': 10}
    assert movements(state) == ({}, {})


def test_transient_other_instance_uses_only_aggregate_item_difference():
    state = implementation().InventoryState(True)
    state.observe(full(item(10)))
    state.observe(delta((3, item(8)), (1, item(2, uid=99)), (3, item(13)),
                        (2, {'item_id': 101, 'item_uid': 99})))
    assert state.snapshot()['items'] == {'101': 13}
    assert movements(state) == ({'101': 3}, {})


def test_invalid_tail_of_stackable_notice_discards_previous_and_pending_evidence():
    state = implementation().InventoryState(True)
    state.observe(full(item(10)))
    state.observe(delta((3, item(12))))
    state.observe(delta((3, item(10)), (3, item(15)), (3, item(-1, item_id=102))))
    assert not state.snapshot()['valid']
    assert movements(state) == ({}, {})
    assert state.snapshot()['movement_epoch'] == 1


def test_instance_changes_in_one_publish_only_count_aggregate_item_difference():
    state = implementation().InventoryState(True)
    state.observe(full(item(3), item(4, uid=99)))
    state.observe(delta((3, item(2)), (3, item(5, uid=99))))
    assert state.snapshot()['items'] == {'101': 7}
    assert movements(state) == ({}, {})


def test_repeated_absolute_delta_and_full_notice_do_not_duplicate_movements():
    state = implementation().InventoryState(True)
    state.observe(full(item(10)))
    state.observe(delta((3, item(12))))
    sequence = state.sequence
    state.observe(delta((3, item(12))))
    state.observe(full(item(12)))
    assert state.sequence == sequence
    assert movements(state) == ({'101': 2}, {})


def test_full_replacement_records_added_and_removed_item_totals():
    state = implementation().InventoryState(True)
    state.observe(full(item(5), item(7, item_id=102)))
    state.observe(full(item(7), item(2, item_id=103)))
    assert movements(state) == ({'101': 2, '103': 2}, {'102': 7})


def test_movement_totals_retain_exact_large_aggregated_counts():
    state = implementation().InventoryState(True)
    large = 2**63 - 1
    state.observe(full())
    state.observe(full(item(large), item(large, uid=99)))
    state.observe(full())
    assert movements(state) == ({'101': large * 2}, {'101': large * 2})


@pytest.mark.parametrize('ending', ['negative', 'malformed', 'partial-combine', 'invalidate', 'close'])
def test_invalid_or_closed_inventory_discards_all_movement_evidence(ending):
    state = implementation().InventoryState(True)
    state.observe(full(item(10)))
    state.observe(delta((3, item(8))))
    state.observe(delta((3, item(12))))
    if ending == 'invalidate':
        state.invalidate()
    elif ending == 'close':
        state.close()
    elif ending == 'negative':
        state.observe(delta((3, item(-1))))
    elif ending == 'partial-combine':
        state.observe(combined(delta((3, item(20))), response(2942, b'\xff')))
    else:
        state.observe(response(2942, b'\xff'))
    assert not state.snapshot()['valid']
    assert movements(state) == ({}, {})
    assert state.snapshot().get('movement_epoch') == 1
    if ending != 'close':
        state.observe(full(item(30)))
        assert state.snapshot()['valid']
        assert movements(state) == ({}, {})
        assert state.snapshot()['movement_epoch'] == 1
