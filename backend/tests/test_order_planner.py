import copy
import itertools
import random

import pytest


def order(slot, count=1, cans=1, item='wood', **changes):
    return dict(slot_id=str(slot), is_unlock=True, order_type=1,
                materials=[{'item_id': item, 'count': count}],
                rewards=[{'item_id': '54001', 'count': cans}],
                refresh_time=None, refresh_seconds=None) | changes


def snapshot(orders, inventory, buildings=None):
    return dict(revision='r1', orders=orders, inventory=inventory, buildings=buildings or [])


def plan(value, catalog=None, **settings):
    from rumi_market.order_planner import plan_orders
    return plan_orders(value, catalog or {'recipes': [], 'names': {}}, settings, now=100)


def oracle(orders, inventory, limit=100):
    # Deliberately independent brute force, no production helpers.
    best = (0, 0)
    for bits in itertools.product((False, True), repeat=len(orders)):
        selected = [row for row, take in zip(orders, bits) if take]
        if len(selected) > limit:
            continue
        spent = {}
        for row in selected:
            for item in row['materials']:
                spent[item['item_id']] = spent.get(item['item_id'], 0) + item['count']
        if all(count <= inventory.get(item, 0) for item, count in spent.items()):
            best = max(best, (len(selected), sum(item['count'] for row in selected
                                                for item in row['rewards'] if item['item_id'] == '54001')))
    return best


def test_order_count_precedes_canned_food_greedy_choice():
    result = plan(snapshot([order('a', 10, 100), order('b', 5, 40), order('c', 5, 40)], {'wood': 10}))
    assert result['inventory_selection'] == dict(slot_ids=['b', 'c'], count=2, canned_count=80, optimal=True, reason=None)
    assert result['predicted_count'] == 2


def test_shared_inventory_reserves_and_large_python_integers_are_preserved():
    huge = 2**64
    result = plan(snapshot([order('a', huge, 3), order('b', huge, 4)], {'wood': huge + 1}), reserves={'wood': 1})
    assert result['inventory_selection']['slot_ids'] == ['b']
    assert result['inventory_selection']['canned_count'] == 4


def test_small_random_inventory_selections_match_independent_brute_force():
    randomizer = random.Random(312)
    for _ in range(100):
        orders = [order(i, randomizer.randint(1, 5), randomizer.randint(0, 30), randomizer.choice(['wood', 'stone']))
                  for i in range(randomizer.randint(1, 8))]
        stock = {'wood': randomizer.randint(0, 15), 'stone': randomizer.randint(0, 15)}
        result = plan(snapshot(orders, stock))['inventory_selection']
        assert (result['count'], result['canned_count']) == oracle(orders, stock)
        assert result['optimal'] is True


def test_budget_exhaustion_is_explicit_and_returns_a_feasible_selection():
    result = plan(snapshot([order(i, 1, i) for i in range(20)], {'wood': 10}), search_budget=2)
    assert result['inventory_selection']['optimal'] is False
    assert result['inventory_selection']['reason'] == 'search_budget_exhausted'
    assert result['inventory_selection']['count'] <= 10


def test_locked_orders_are_excluded_and_max_orders_is_a_constraint():
    result = plan(snapshot([order('locked', is_unlock=False), order('a'), order('b', cans=9)], {'wood': 5}), max_orders=1)
    assert result['inventory_selection']['slot_ids'] == ['b']


def test_unknown_inventory_is_not_zero_and_does_not_claim_exact_selection():
    result = plan(snapshot([order('a')], {}) | {'inventory_complete': False})
    assert result['inventory_selection']['optimal'] is False
    assert 'inventory_unknown' in result['warnings']


def production_building(uid='b1', worker='w1', recipe_id='wood', **changes):
    return dict(building_uid=uid, building_id=recipe_id, level=1, worker_uid=worker,
                recipe_id=recipe_id, work_speed=10000, work_progress=0,
                recipe_update_time=100, intend_count=10, completed_count=0,
                work_energy=100000, recipe_state=1, capacity_remaining=100,
                editable=True, blocked_reason=None) | changes


def production_recipe(item='wood', count=1, materials=None, work=10):
    return dict(building_id=item, recipe_id=item, available_levels=[1],
                products=[{'item_id': item, 'count': count}], materials=materials or [],
                work_amount=work, volume=1)


def test_known_current_production_predicts_one_continuous_batch_and_claim_action():
    current = production_building(intend_count=0)
    result = plan(snapshot([order('a')], {}, [current]), {'recipes': [production_recipe()], 'names': {}}, allow_production=True)
    assert result['inventory_selection']['count'] == 0
    assert result['predicted_count'] == 1
    assert result['scheduled_slot_ids'] == ['a']
    assert result['production'][0]['time_source'] == 'calculated'
    assert result['production'][0]['ready_at_server_seconds'] == 110
    assert any(row['action'] == 'claim' for row in result['production_actions'])


def test_unknown_production_time_is_visible_but_never_counts_as_immediate_supply():
    result = plan(snapshot([order('a')], {}, [production_building(capacity_remaining=None)]),
                  {'recipes': [production_recipe()], 'names': {}}, allow_production=True)
    assert result['predicted_count'] == 0
    assert result['production'][0]['duration_seconds'] is None
    assert result['production'][0]['blocked_reason'] == 'capacity_unknown'


def test_shared_inputs_and_worker_calendar_prevent_impossible_parallel_supply():
    catalog = {'recipes': [production_recipe('a', materials=[{'item_id': 'wood', 'count': 3}], work=2000),
                           production_recipe('b', materials=[{'item_id': 'wood', 'count': 3}], work=2000)], 'names': {}}
    buildings = [production_building('ba', 'same-worker', 'a'), production_building('bb', 'same-worker', 'b')]
    orders = [order('a', item='a'), order('b', item='b')]
    result = plan(snapshot(orders, {'wood': 6}, buildings), catalog, allow_production=True, horizon_seconds=3600)
    assert result['predicted_count'] == 1
    assert len(result['scheduled_slot_ids']) == 1
    # With separate workers both can finish before 1h; with only 3 shared inputs only one can.
    buildings[1]['worker_uid'] = 'other-worker'
    assert plan(snapshot(orders, {'wood': 6}, buildings), catalog, allow_production=True, horizon_seconds=3600)['predicted_count'] == 2
    assert plan(snapshot(orders, {'wood': 3}, buildings), catalog, allow_production=True, horizon_seconds=3600)['predicted_count'] == 1


def test_multilevel_batches_wait_for_inputs_and_respect_yield_rounding():
    catalog = {'recipes': [production_recipe('wood', count=3, work=5),
                           production_recipe('plank', materials=[{'item_id': 'wood', 'count': 5}], work=10)], 'names': {}}
    buildings = [production_building(recipe_id='wood', intend_count=0), production_building('bp', 'wp', 'plank')]
    # One ongoing continuous batch yields 3, plus 2 inventory, then the plank is feasible at 115.
    value = snapshot([order('a', item='plank')], {'wood': 2}, buildings)
    saved = copy.deepcopy(value)
    result = plan(value, catalog, allow_production=True)
    assert result['predicted_count'] == 1
    assert [(row['item_id'], row['count'], row['ready_at_server_seconds']) for row in result['production']] == [('wood', 3, 105), ('plank', 1, 115)]
    assert value == saved


def test_future_random_orders_and_refresh_are_not_invented():
    result = plan(snapshot([order('a')], {}), allow_refresh=True)
    assert result['predicted_count'] == 0
    assert 'refresh_not_modeled' in result['warnings']


def test_duplicate_item_rows_are_added_before_testing_inventory():
    duplicate = order('a', materials=[{'item_id': 'wood', 'count': 3}, {'item_id': 'wood', 'count': 4}])
    assert plan(snapshot([duplicate], {'wood': 6}))['inventory_selection']['count'] == 0
    assert plan(snapshot([duplicate], {'wood': 7}))['inventory_selection']['count'] == 1


def test_empty_slots_do_not_become_zero_cost_completed_orders():
    result = plan(snapshot([order('empty', materials=[]), order('a')], {'wood': 1}))
    assert result['inventory_selection']['slot_ids'] == ['a']
    assert 'empty_order_materials' in result['warnings']


def test_order_type_missing_is_unknown_and_excluded_from_selection():
    result = plan(snapshot([order('unknown', order_type=None), order('a')], {'wood': 5}))
    assert result['inventory_selection']['slot_ids'] == ['a']
    assert 'order_type_unknown' in result['warnings']


def test_base_requirements_are_one_shared_inventory_total_at_top_level():
    catalog = {'recipes': [production_recipe('plank', materials=[{'item_id': 'wood', 'count': 5}])], 'names': {}}
    result = plan(snapshot([order('a', item='plank'), order('b', 2)], {'wood': 1}), catalog, allow_production=False)
    assert result['base_requirements'] == {'wood': 6}
    assert all('base_requirements' not in row for row in result['materials'])


def test_server_deadline_after_horizon_is_not_counted_even_if_work_formula_is_short():
    row = production_building(intend_count=0, server_ready_at_seconds=5000)
    result = plan(snapshot([order('a')], {}, [row]), {'recipes': [production_recipe()], 'names': {}},
                  allow_production=True, horizon_seconds=3600)
    assert result['predicted_count'] == 0
    assert result['production'][0]['blocked_reason'] == 'outside_horizon'


def test_failed_multilevel_order_does_not_spend_shared_inputs_of_later_order():
    catalog = {'recipes': [production_recipe('plank', materials=[{'item_id': 'wood', 'count': 5},
                                                               {'item_id': 'missing', 'count': 1}])], 'names': {}}
    result = plan(snapshot([order('bad', item='plank'), order('good', 5)], {'wood': 5},
                           [production_building(recipe_id='plank')]), catalog, allow_production=True)
    assert result['inventory_selection']['slot_ids'] == ['good']
    assert result['predicted_count'] == 1


def test_setting_can_turn_off_production_without_losing_inventory_selection():
    result = plan(snapshot([order('a'), order('b', item='stone')], {'stone': 1},
                           [production_building(intend_count=0)]),
                  {'recipes': [production_recipe()], 'names': {}}, allow_production=False)
    assert result['inventory_selection']['slot_ids'] == ['b']
    assert result['production'] == []
    assert result['predicted_count'] == 1


def assert_feasible_replay(value, catalog, result):
    """Independent event validator: no planner ledger or timing helpers used."""
    stock = value['inventory'].copy()
    orders = {row['slot_id']: row for row in value['orders']}
    buildings = {row['building_uid']: row for row in value['buildings']}
    recipes = {row['recipe_id']: row for row in catalog['recipes']}
    current_ids = result['inventory_selection']['slot_ids']
    for slot in current_ids:
        for item in orders[slot]['materials']:
            stock[item['item_id']] = stock.get(item['item_id'], 0) - item['count']
    events, calendars = [], {}
    for row in result['production']:
        if row['time_source'] == 'unknown':
            continue
        config = recipes[row['recipe_id']]
        start, end = row['starts_at_server_seconds'], row['ready_at_server_seconds']
        assert 100 <= start <= end <= 100 + result['horizon_seconds']
        for resource in [('building', row['building_uid']), ('worker', buildings[row['building_uid']]['worker_uid'])]:
            for previous_start, previous_end in calendars.get(resource, []):
                assert end <= previous_start or start >= previous_end
            calendars.setdefault(resource, []).append((start, end))
        events.append((start, 1, config['materials'], -row['batches']))
        events.append((end, 0, config['products'], row['batches']))
    for _, _, items, multiplier in sorted(events, key=lambda event: event[:2]):
        for item in items:
            stock[item['item_id']] = stock.get(item['item_id'], 0) + item['count'] * multiplier
        assert all(count >= 0 for count in stock.values())
    for slot in result['scheduled_slot_ids']:
        if slot not in current_ids:
            for item in orders[slot]['materials']:
                stock[item['item_id']] = stock.get(item['item_id'], 0) - item['count']
    assert all(count >= 0 for count in stock.values())
    assert result['predicted_count'] == len(result['scheduled_slot_ids'])


def test_random_production_plans_pass_independent_resource_and_calendar_replay():
    randomizer = random.Random(20261018)
    for _ in range(50):
        catalog = {'recipes': [production_recipe('wood', count=randomizer.randint(1, 4), work=randomizer.randint(2, 10)),
                               production_recipe('plank', count=2, materials=[{'item_id': 'wood', 'count': 5}], work=7)], 'names': {}}
        buildings = [production_building('bw', 'worker', 'wood', intend_count=0),
                     production_building('bp', randomizer.choice(['worker', 'other']), 'plank')]
        value = snapshot([order(i, randomizer.randint(1, 4), item=randomizer.choice(['wood', 'plank'])) for i in range(5)],
                         {'wood': randomizer.randint(0, 12), 'plank': randomizer.randint(0, 3)}, buildings)
        result = plan(value, catalog, allow_production=True)
        assert_feasible_replay(value, catalog, result)


def test_disabled_idle_production_has_no_planned_action_and_an_explicit_reason():
    current = production_building(auto_production_enabled=False, building_id='wood', recipe_id=None,
        recipe_id_known=True, recipe_state=0, is_producing=False, quantity_mode='batch')
    result = plan(snapshot([order('a')], {}, [current]),
        {'recipes': [production_recipe()], 'names': {}}, allow_production=True)
    assert result['predicted_count'] == 0
    assert result['production_actions'] == []
    assert result['production'][0]['blocked_reason'] == 'automatic_production_disabled'


def disabled_current_queue(**changes):
    return production_building(auto_production_enabled=False, recipe_id_known=True,
        is_producing=True, quantity_mode='batch') | changes


def test_disabled_building_still_predicts_only_its_confirmed_current_queue():
    current = disabled_current_queue(intend_count=2)
    result = plan(snapshot([order('a', 2)], {}, [current]),
        {'recipes': [production_recipe()], 'names': {}}, allow_production=True)
    assert result['predicted_count'] == 1
    assert result['production'][0]['batches'] == 2


def test_disabled_building_does_not_repeat_an_exhausted_current_queue():
    current = disabled_current_queue(intend_count=2)
    result = plan(snapshot([order('a', 2), order('b')], {}, [current]),
        {'recipes': [production_recipe()], 'names': {}}, allow_production=True)
    assert result['predicted_count'] == 1
    assert sum(row['batches'] for row in result['production']) == 2


@pytest.mark.parametrize('change', [
    {'intend_count': 0}, {'intend_count': None}, {'intend_count': True},
    {'completed_count': None}, {'completed_count': -1}, {'completed_count': True},
    {'completed_count': 10}, {'recipe_id_known': False}, {'is_producing': False},
    {'quantity_mode': 'continuous'}, {'recipe_id': 'other'},
])
def test_disabled_building_never_promises_unverified_or_unbounded_future_work(change):
    result = plan(snapshot([order('a')], {}, [disabled_current_queue(**change)]),
        {'recipes': [production_recipe()], 'names': {}}, allow_production=True)
    assert result['predicted_count'] == 0
    assert result['production_actions'] == []
    assert result['production'][0]['blocked_reason'] == 'automatic_production_disabled'


def test_disabled_building_does_not_prevent_using_real_inventory_for_orders():
    result = plan(snapshot([order('a')], {'wood': 1}, [disabled_current_queue()]),
        {'recipes': [production_recipe()], 'names': {}}, allow_production=True)
    assert result['inventory_selection']['slot_ids'] == ['a']
    assert result['production'] == [] and result['production_actions'] == []
