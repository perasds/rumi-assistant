"""Pending production reduces duplicate orders without becoming spendable stock."""

from copy import deepcopy

import pytest

from rumi_market.order_replenishment import choose_replenishment
from test_order_replenishment import fixture


def running(*, uid='8', intended=8, completed=0, recipe_id='2', building_id='1', **changes):
    row = {'building_uid': uid, 'building_id': building_id, 'level': 1, 'editable': True,
        'worker_valid': True, 'worker_uid': '98', 'work_energy': 1000,
        'recipe_id_known': True, 'recipe_id': recipe_id, 'recipe_state': 1,
        'is_producing': True, 'quantity_mode': 'batch', 'intend_count': intended,
        'completed_count': completed, 'capacity_remaining': 100, 'store_items': []}
    row.update(changes)
    return row


def proposal(snapshot, catalog, reserves=None, batch_limit=1000):
    return choose_replenishment(snapshot, catalog, reserves or {}, batch_limit=batch_limit)


@pytest.mark.parametrize('remaining', [8, 9, 10000000000000000000])
def test_complete_or_excess_pending_output_never_starts_a_duplicate(remaining):
    snapshot, catalog, _ = fixture()
    snapshot['buildings'].append(running(intended=remaining))
    before = deepcopy((snapshot, catalog))
    assert proposal(snapshot, catalog) is None
    assert (snapshot, catalog) == before


def test_partial_pending_output_only_fills_the_uncovered_total_across_orders():
    snapshot, catalog, _ = fixture()
    snapshot['orders'] = [
        {'is_unlock': True, 'order_type': 0, 'materials': [{'item_id': '20', 'count': 3}]},
        {'is_unlock': True, 'order_type': 1, 'materials': [{'item_id': '20', 'count': 5}]},
        {'is_unlock': False, 'order_type': 1, 'materials': [{'item_id': '20', 'count': 100}]},
        {'is_unlock': True, 'order_type': None, 'materials': [{'item_id': '20', 'count': 100}]},
    ]
    snapshot['buildings'].append(running(intended=10, completed=4))
    result = proposal(snapshot, catalog)
    assert result['operation']['recipe_times'] == 2
    assert result['costs'] == [{'item_id': '10', 'count': 4}]


def test_inventory_after_reserves_and_all_producing_buildings_share_one_gap():
    snapshot, catalog, _ = fixture()
    snapshot['inventory']['20'] = 5
    snapshot['buildings'] += [running(uid='8', intended=3), running(uid='9', intended=3)]
    assert proposal(snapshot, catalog, {'20': 3}) is None
    snapshot['buildings'][-1]['intend_count'] = 2
    assert proposal(snapshot, catalog, {'20': 3})['operation']['recipe_times'] == 1


def test_single_batch_output_and_completed_batches_are_not_confused():
    snapshot, catalog, _ = fixture()
    catalog['recipes'][0]['products'][0]['count'] = 3
    snapshot['buildings'].append(running(intended=4, completed=2))
    assert proposal(snapshot, catalog)['operation']['recipe_times'] == 1  # 8 - 2*3 = 2.


def test_ready_store_plus_unfinished_batches_count_each_batch_once():
    snapshot, catalog, _ = fixture()
    snapshot['buildings'].append(running(intended=8, completed=2,
        store_items=[{'item_id': '20', 'count': 2}]))
    assert proposal(snapshot, catalog) is None
    snapshot['buildings'][-1]['store_items'] = []
    assert proposal(snapshot, catalog)['operation']['recipe_times'] == 2


def test_ready_store_can_cover_orders_even_after_worker_or_queue_stopped():
    snapshot, catalog, _ = fixture()
    snapshot['buildings'].append(running(intended=8, completed=8, recipe_state=0,
        is_producing=False, worker_valid=False, work_energy=0,
        store_items=[{'item_id': '20', 'count': 8}]))
    assert proposal(snapshot, catalog) is None


def test_repeated_building_rows_do_not_double_count_the_same_queue_or_store():
    snapshot, catalog, _ = fixture()
    current = running(intended=3, completed=1, store_items=[{'item_id': '20', 'count': 1}])
    snapshot['buildings'] += [current, deepcopy(current)]
    # One building contributes 3, leaving 5, bounded by the idle building's capacity 3.
    assert proposal(snapshot, catalog)['operation']['recipe_times'] == 3


@pytest.mark.parametrize('change', [
    {'recipe_state': None}, {'recipe_state': 0}, {'recipe_state': True},
    {'is_producing': False}, {'is_producing': None}, {'worker_valid': False},
    {'work_energy': 0}, {'work_energy': None}, {'recipe_id': '999'}, {'recipe_id_known': False},
    {'intend_count': None}, {'intend_count': True}, {'intend_count': -1},
    {'completed_count': None}, {'completed_count': -1}, {'completed_count': 9},
    {'editable': False},
])
def test_unknown_or_inactive_queue_is_not_guessed_to_cover_orders(change):
    snapshot, catalog, _ = fixture()
    snapshot['buildings'].append(running(**change))
    assert proposal(snapshot, catalog)['operation']['recipe_times'] == 3


def test_continuous_zero_intended_count_never_means_infinite_future_stock():
    snapshot, catalog, _ = fixture()
    snapshot['buildings'].append(running(intended=0, quantity_mode='continuous'))
    assert proposal(snapshot, catalog)['operation']['recipe_times'] == 3
    snapshot['buildings'][-1]['store_items'] = [{'item_id': '20', 'count': 8}]
    assert proposal(snapshot, catalog) is None


def upstream_fixture():
    snapshot, catalog, _ = fixture()
    snapshot['inventory'] = {}
    catalog['recipes'].append({'building_id': '3', 'recipe_id': '4', 'available_levels': [1],
        'volume': 1, 'materials': [], 'products': [{'item_id': '10', 'count': 1}]})
    snapshot['buildings'].append({'building_uid': '9', 'building_id': '3', 'level': 1,
        'editable': True, 'worker_valid': True, 'worker_uid': '97', 'work_energy': 1000,
        'recipe_id': None, 'capacity_remaining': 100, 'quantity_mode': 'batch'})
    return snapshot, catalog


def test_pending_upstream_is_waited_for_and_never_spent_as_real_input():
    snapshot, catalog = upstream_fixture()
    snapshot['buildings'].append(running(uid='10', building_id='3', recipe_id='4', intended=100))
    assert proposal(snapshot, catalog, batch_limit=1) is None
    snapshot['inventory']['10'] = 1
    assert proposal(snapshot, catalog, batch_limit=1) is None
    snapshot['inventory']['10'] = 2
    result = proposal(snapshot, catalog, batch_limit=1)
    assert result['operation']['building_id'] == '1'
    assert result['costs'] == [{'item_id': '10', 'count': 2}]


def test_partial_upstream_only_schedules_its_uncovered_shortfall():
    snapshot, catalog = upstream_fixture()
    snapshot['buildings'].append(running(uid='10', building_id='3', recipe_id='4', intended=1))
    result = proposal(snapshot, catalog, batch_limit=1)
    assert result['operation']['recipe_id'] == '4' and result['operation']['recipe_times'] == 1


def test_waiting_to_claim_upstream_is_not_used_to_start_its_consumer():
    snapshot, catalog = upstream_fixture()
    snapshot['buildings'].append(running(uid='10', building_id='3', recipe_id='4',
        intended=2, completed=2, recipe_state=0, is_producing=False,
        store_items=[{'item_id': '10', 'count': 2}]))
    assert proposal(snapshot, catalog, batch_limit=1) is None


def test_one_pending_upstream_batch_cannot_cover_two_distinct_consumer_orders():
    snapshot, catalog = upstream_fixture()
    snapshot['orders'] = [
        {'is_unlock': True, 'order_type': 1, 'materials': [{'item_id': '20', 'count': 1}]},
        {'is_unlock': True, 'order_type': 1, 'materials': [{'item_id': '30', 'count': 1}]},
    ]
    other = deepcopy(catalog['recipes'][0])
    other.update(building_id='5', recipe_id='6', products=[{'item_id': '30', 'count': 1}])
    catalog['recipes'].append(other)
    idle = deepcopy(snapshot['buildings'][0])
    idle.update(building_uid='11', building_id='5')
    snapshot['buildings'].append(idle)
    snapshot['buildings'].append(running(uid='10', building_id='3', recipe_id='4', intended=2))
    result = proposal(snapshot, catalog)
    assert result['operation']['recipe_id'] == '4'
    assert result['operation']['recipe_times'] == 2


@pytest.mark.parametrize('supply_kind', ['inventory', 'pending'])
def test_supply_reserved_for_a_direct_order_cannot_be_reused_as_another_orders_input(supply_kind):
    snapshot, catalog = upstream_fixture()
    snapshot['orders'] = [
        {'is_unlock': True, 'order_type': 1, 'materials': [{'item_id': '10', 'count': 2}]},
        {'is_unlock': True, 'order_type': 1, 'materials': [{'item_id': '20', 'count': 1}]},
    ]
    if supply_kind == 'inventory':
        snapshot['inventory']['10'] = 2
    else:
        snapshot['buildings'].append(running(uid='10', building_id='3', recipe_id='4', intended=2))
    result = proposal(snapshot, catalog)
    assert result['operation']['recipe_id'] == '4'
    assert result['operation']['recipe_times'] == 2


def test_pending_output_restores_missing_reserve_before_covering_order_demand():
    snapshot, catalog, _ = fixture()
    snapshot['inventory']['20'] = 2
    snapshot['orders'][0]['materials'] = [{'item_id': '20', 'count': 2}]
    snapshot['buildings'].append(running(intended=2))
    assert proposal(snapshot, catalog, {'20': 3})['operation']['recipe_times'] == 1
