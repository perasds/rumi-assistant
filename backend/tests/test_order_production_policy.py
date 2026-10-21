"""Bounded proposals use current targets without treating future output as stock."""
from copy import deepcopy

import pytest

from rumi_market.order_production_policy import choose_bounded_production


def recipe(item='20', *, uid='2', building='1', output=1, materials=None, volume=1):
    return {'building_id': building, 'recipe_id': uid, 'available_levels': [1],
            'volume': volume, 'materials': ([{'item_id': '10', 'count': 2}]
                if materials is None else materials), 'products': [{'item_id': item, 'count': output}]}


def building(uid='7', *, kind='1', mode='batch', **changes):
    row = {'building_uid': uid, 'building_id': kind, 'level': 1, 'editable': True,
           'recipe_id_known': True, 'recipe_id': None, 'recipe_state': 0,
           'is_producing': False, 'worker_valid': True, 'worker_uid': f'w{uid}',
           'work_energy': 1000, 'capacity_remaining': 1000, 'quantity_mode': mode,
           'store_items': []}
    return row | changes


def order(uid='a', count=3, *, item='20', cans=1, materials=None, **changes):
    return {'slot_id': uid, 'is_unlock': True, 'order_type': 1,
            'materials': materials or [{'item_id': item, 'count': count}],
            'rewards': [{'item_id': '54001', 'count': cans}]} | changes


def state(*orders, inventory=None, buildings=None):
    return {'inventory': {'10': 1000} if inventory is None else inventory,
            'orders': list(orders) or [order()],
            'buildings': [building()] if buildings is None else buildings}


def choose(snapshot=None, recipes=None, reserves=None, **options):
    return choose_bounded_production(snapshot or state(),
        {'recipes': recipes or [recipe()]}, reserves or {}, **options)


def test_target_shortage_gets_at_most_one_extra_batch_and_counts_total_cost():
    result = choose(state(order(count=8), inventory={'10': 1000, '20': 3}), [recipe(output=2)])
    assert result['operation'] == {'action': 'produce', 'building_uid': '7',
        'building_id': '1', 'recipe_id': '2', 'recipe_times': 4}
    assert result['costs'] == [{'item_id': '10', 'count': 8}]


@pytest.mark.parametrize('options, expected', [({}, 10), ({'max_batches': 3}, 3),
    ({'buffer_batches': 0}, 3), ({'buffer_batches': 100}, 4)])
def test_batch_ceiling_and_buffer_ceiling_are_finite(options, expected):
    count = 100 if 'buffer_batches' not in options else 3
    assert choose(state(order(count=count)), **options)['operation']['recipe_times'] == expected


def test_reserves_queue_consumption_and_capacity_bound_actual_inputs():
    value = state(order(count=3), inventory={'10': 15},
        buildings=[building(capacity_remaining=5)])
    result = choose(value, reserves={'10': 3}, queue_reserves={'10': 4})
    assert result['operation']['recipe_times'] == 4
    assert result['costs'] == [{'item_id': '10', 'count': 8}]
    value['buildings'][0]['capacity_remaining'] = 3
    assert choose(value, reserves={'10': 3}, queue_reserves={'10': 4})['operation']['recipe_times'] == 3


def test_buffer_holds_direct_materials_of_other_current_orders():
    value = state(order('make', count=2), order('direct', count=5, item='10'),
        inventory={'10': 10})
    result = choose(value)
    assert result['operation']['recipe_times'] == 2
    assert result['costs'] == [{'item_id': '10', 'count': 4}]


def test_remaining_order_budget_prefers_more_orders_then_cans():
    value = state(order('large', count=6, cans=100), order('b', count=2, cans=2),
        order('c', count=2, cans=3), inventory={'10': 12})
    assert choose(value, remaining_orders=2, buffer_batches=0)['operation']['recipe_times'] == 4
    assert choose(value, remaining_orders=1, buffer_batches=0)['operation']['recipe_times'] == 6
    assert choose(value, remaining_orders=0) is None


def test_impossible_order_never_spends_inputs_of_a_feasible_order():
    bad = recipe('bad', uid='bad', materials=[{'item_id': '10', 'count': 2},
        {'item_id': 'unobtainable', 'count': 1}])
    value = state(order('bad', item='bad', cans=100), order('good', count=2), inventory={'10': 4})
    result = choose(value, [bad, recipe()], buffer_batches=0)
    assert result['operation']['recipe_id'] == '2'
    assert result['costs'] == [{'item_id': '10', 'count': 4}]


def test_unavailable_first_recipe_does_not_trigger_irrelevant_upstream_work():
    configs = [recipe(materials=[{'item_id': 'wood', 'count': 1},
        {'item_id': 'unobtainable', 'count': 1}]),
        recipe(uid='alternative', materials=[{'item_id': 'stone', 'count': 1}]),
        recipe('wood', uid='upstream', building='3', materials=[{'item_id': 'seed', 'count': 1}])]
    value = state(order(count=1), inventory={'seed': 10, 'stone': 10},
        buildings=[building(), building('8', kind='3')])
    assert choose(value, configs, buffer_batches=0)['operation']['recipe_id'] == 'alternative'


def test_recipe_choice_preserves_shared_inputs_for_the_other_selected_order():
    configs = [recipe(), recipe(uid='alternative', materials=[{'item_id': '30', 'count': 2}]),
        recipe('40', uid='other', building='3')]
    value = state(order('other', count=1, item='40'), order('a', count=1),
        inventory={'10': 2, '30': 2}, buildings=[building(), building('8', kind='3')])
    assert choose(value, configs, buffer_batches=0)['operation']['recipe_id'] == 'alternative'


def test_buffer_cannot_spend_the_other_selected_orders_production_input():
    configs = [recipe(), recipe('40', uid='other', building='3')]
    value = state(order('a', count=1), order('other', count=1, item='40'),
        inventory={'10': 4}, buildings=[building(), building('8', kind='3')])
    assert choose(value, configs)['operation']['recipe_times'] == 1


def test_dependency_chain_starts_only_an_action_with_actual_inputs():
    value = state(order(count=2), inventory={'seed': 20},
        buildings=[building(), building('8', kind='3')])
    upstream = recipe('10', uid='4', building='3', output=2,
        materials=[{'item_id': 'seed', 'count': 1}])
    result = choose(value, [recipe(), upstream])
    assert result['operation']['recipe_id'] == '4'
    assert result['operation']['recipe_times'] == 3
    assert result['costs'] == [{'item_id': 'seed', 'count': 3}]


def pending(uid='8', *, item='20', intended=3, completed=0, **changes):
    return building(uid, **({'recipe_id': '2', 'recipe_state': 1, 'is_producing': True,
        'intend_count': intended, 'completed_count': completed} | changes))


def test_confirmed_pending_output_and_stores_reduce_the_exact_target_gap():
    value = state(order(count=8), inventory={'10': 100}, buildings=[building(),
        pending(intended=5, completed=2, store_items=[{'item_id': '20', 'count': 2}])])
    assert choose(value, buffer_batches=0)['operation']['recipe_times'] == 3
    value['buildings'][1]['intend_count'] = 8
    assert choose(value) is None


def test_pending_restores_missing_reserve_before_covering_orders():
    value = state(order(count=2), inventory={'10': 100, '20': 2},
        buildings=[building(), pending(intended=2)])
    assert choose(value, reserves={'20': 3}, buffer_batches=0)['operation']['recipe_times'] == 1


def test_pending_upstream_is_never_used_as_spendable_input():
    upstream = recipe('10', uid='4', building='3', materials=[])
    value = state(order(count=2), inventory={}, buildings=[building(),
        building('8', kind='3'), pending('9', building_id='3', recipe_id='4', intended=4)])
    assert choose(value, [recipe(), upstream]) is None


@pytest.mark.parametrize('blocked', [{'10'}, {'20'}])
def test_blocked_material_or_product_excludes_a_candidate(blocked):
    assert choose(blocked_items=blocked) is None


def test_a_blocked_queue_does_not_prevent_independent_production():
    other = recipe('40', uid='6', building='5', materials=[{'item_id': '30', 'count': 1}])
    value = state(order('a'), order('b', count=2, item='40'), inventory={'10': 100, '30': 10},
        buildings=[building(), building('8', kind='5')])
    result = choose(value, [recipe(), other], blocked_items={'10'})
    assert result['operation']['recipe_id'] == '6'


def test_blocked_idle_route_cannot_consume_the_remaining_order_budget():
    other = recipe('40', uid='6', building='5', materials=[{'item_id': '30', 'count': 1}])
    value = state(order('a', cans=100), order('b', count=2, item='40'),
        inventory={'10': 100, '30': 10}, buildings=[building(), building('8', kind='5')])
    result = choose(value, [recipe(), other], blocked_items={'20'}, remaining_orders=1)
    assert result['operation']['recipe_id'] == '6'


def owned_large_order_fixture(*, shared=False, **queue_changes):
    other = recipe('40', uid='6', building='5', materials=[
        {'item_id': '10' if shared else '30', 'count': 2}])
    current = pending('8', **({'intended': 10, 'queue_owned': True} | queue_changes))
    value = state(order(materials=[{'item_id': '20', 'count': 100},
        {'item_id': '40', 'count': 2}]), inventory={'10': 1000, '30': 100},
        buildings=[current, building('9', kind='5')])
    return value, [recipe(), other]


def test_verified_owned_partial_queue_allows_independent_material_of_same_large_order():
    value, configs = owned_large_order_fixture()
    result = choose(value, configs, blocked_items={'10', '20'})
    assert result['operation']['building_uid'] == '9'
    assert result['operation']['recipe_id'] == '6'
    assert result['operation']['recipe_times'] == 3
    assert result['costs'] == [{'item_id': '30', 'count': 6}]


def test_external_partial_queue_does_not_authorize_a_future_recipe_route():
    value, configs = owned_large_order_fixture(queue_owned=False)
    assert choose(value, configs, blocked_items={'10', '20'}) is None


def test_owned_future_route_preserves_shared_material_and_unobserved_queue_consumption():
    value, configs = owned_large_order_fixture(shared=True)
    value['inventory'] = {'10': 204}
    result = choose(value, configs, queue_reserves={'10': 20}, blocked_items={'20'})
    # The active ten batches cover 10; the other 90 need 180 units later.
    assert result['operation']['recipe_times'] == 2
    assert result['costs'] == [{'item_id': '10', 'count': 4}]
    value['inventory']['10'] = 203
    assert choose(value, configs, queue_reserves={'10': 20}, blocked_items={'20'}) is None


@pytest.mark.parametrize('change', [
    {'intend_count': None}, {'intend_count': True}, {'intend_count': 0},
    {'completed_count': None}, {'completed_count': -1}, {'completed_count': 11},
    {'recipe_id_known': False}, {'recipe_state': None}, {'is_producing': False},
    {'quantity_mode': 'continuous'},
])
def test_owned_queue_with_unknown_or_invalid_finite_state_does_not_authorize_future_work(change):
    value, configs = owned_large_order_fixture()
    value['buildings'][0].update(change)
    assert choose(value, configs, blocked_items={'10', '20'}) is None


def test_owned_future_route_only_uses_that_queues_current_recipe():
    value, configs = owned_large_order_fixture(recipe_id='different')
    configs.append(recipe('50', uid='different'))
    assert choose(value, configs, blocked_items={'10', '20'}) is None


@pytest.mark.parametrize('change', [
    {'recipe_id_known': False}, {'recipe_id': '9'}, {'recipe_state': None},
    {'is_producing': True}, {'worker_valid': False}, {'worker_uid': '0'},
    {'work_energy': 0}, {'work_energy': True}, {'editable': False},
    {'capacity_remaining': None}, {'blocked_reason': 'unknown'},
])
def test_only_verified_idle_assigned_buildings_can_start(change):
    assert choose(state(buildings=[building(**change)])) is None


def test_duplicate_material_rows_are_added_and_duplicate_queues_count_once():
    config = recipe(materials=[{'item_id': '10', 'count': 1}, {'item_id': '10', 'count': 1}])
    active = pending(intended=2)
    value = state(order(count=5), inventory={'10': 100},
        buildings=[building(), active, deepcopy(active)])
    result = choose(value, [config], buffer_batches=0)
    assert result['operation']['recipe_times'] == 3
    assert result['costs'] == [{'item_id': '10', 'count': 6}]


def test_conflicting_duplicate_idle_building_is_not_actionable():
    assert choose(state(buildings=[building(), building(capacity_remaining=1)])) is None


def test_conflicting_duplicate_recipe_is_not_guessed():
    assert choose(recipes=[recipe(), recipe(output=2)]) is None


def test_material_cycle_is_skipped_without_recursing_or_consuming_stock():
    configs = [recipe('20', materials=[{'item_id': '10', 'count': 2}]),
        recipe('10', uid='4', building='3', materials=[{'item_id': '20', 'count': 1}])]
    value = state(inventory={}, buildings=[building(), building('8', kind='3')])
    assert choose(value, configs) is None


def test_continuous_raw_production_retains_zero_and_has_no_buffer_costs():
    value = state(order(count=100), inventory={}, buildings=[building(mode='continuous')])
    result = choose(value, [recipe(materials=[])])
    assert result['operation']['recipe_times'] == 0
    assert result['costs'] == []


def test_selection_and_proposal_never_mutate_input_snapshots_or_catalog():
    value, configs, reserves = state(), {'recipes': [recipe()]}, {'10': 4}
    before = deepcopy((value, configs, reserves))
    choose_bounded_production(value, configs, reserves, queue_reserves={'10': 2})
    assert (value, configs, reserves) == before


def test_disabled_idle_building_never_starts_automatic_production():
    value = state(buildings=[building(auto_production_enabled=False)])
    before = deepcopy(value)
    assert choose(value) is None
    assert value == before


def test_disabled_owned_queue_cannot_promise_future_repeated_batches():
    value, configs = owned_large_order_fixture(auto_production_enabled=False)
    assert choose(value, configs, blocked_items={'10', '20'}) is None


def test_disabled_existing_queue_still_covers_demand_for_independent_work():
    value, configs = owned_large_order_fixture(auto_production_enabled=False)
    value['orders'][0]['materials'][0]['count'] = 5
    result = choose(value, configs, blocked_items={'10', '20'})
    assert result['operation']['building_uid'] == '9'
    assert result['operation']['recipe_id'] == '6'


def test_disabled_existing_queue_and_store_reduce_another_idle_buildings_gap():
    value = state(order(count=8), inventory={'10': 100}, buildings=[building(),
        pending(intended=5, completed=2, auto_production_enabled=False,
                store_items=[{'item_id': '20', 'count': 2}])])
    assert choose(value, buffer_batches=0)['operation']['recipe_times'] == 3
