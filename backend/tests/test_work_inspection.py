from copy import deepcopy

import pytest


def inspect(snapshot, catalog, reserves=None):
    from rumi_market.work_inspection import inspect_workplaces

    return inspect_workplaces(snapshot, catalog, reserves)


def fixture():
    building = {'building_uid': '7', 'building_id': '1', 'name': '加工台', 'level': 2,
        'editable': True, 'recipe_id_known': True, 'recipe_id': None,
        'current_recipe_id': None, 'recipe_state': 0, 'quantity_mode': 'batch',
        'worker_uid': '99', 'worker_valid': True, 'worker_missing': False, 'worker_reason': None,
        'work_energy': 100, 'capacity_remaining': 5, 'allowed_recipe_ids': ['2', '3']}
    recipes = [{'building_id': '1', 'recipe_id': recipe_id, 'available_levels': [2],
        'volume': 1, 'materials': [{'item_id': '10', 'count': 2}],
        'products': [{'item_id': item_id, 'count': 1}]}
        for recipe_id, item_id in [('2', '20'), ('3', '21')]]
    snapshot = {'inventory_complete': True, 'inventory': {'10': 10, '20': 8, '21': 2},
                'server_now': 100, 'buildings': [building]}
    return snapshot, {'recipes': recipes, 'names': {'20': '甲产物', '21': '乙产物'}}


def test_idle_chooses_lowest_real_warehouse_stock_and_one_batch_without_mutation():
    snapshot, catalog = fixture()
    before = deepcopy((snapshot, catalog))
    row, = inspect(snapshot, catalog)
    assert row == {'building_uid': '7', 'building_id': '1', 'name': '加工台', 'level': 2,
        'status': 'idle', 'message': '空闲，可生产乙产物', 'worker_uid': '99',
        'current_recipe_id': '0',
        'candidate': {'recipe_id': '3', 'item_id': '21', 'name': '乙产物', 'stock': '2',
                      'output_count': '1', 'recipe_times': '1'},
        'proposal': {'operation': {'action': 'produce', 'building_uid': '7', 'building_id': '1',
                                  'recipe_id': '3', 'recipe_times': 1},
                     'costs': [{'item_id': '10', 'count': 2}]}}
    assert (snapshot, catalog) == before


def test_stock_ranking_uses_python_integers_and_numeric_recipe_id_ties():
    snapshot, catalog = fixture()
    snapshot['inventory'].update({'20': 2**63 + 1, '21': 2**63})
    assert inspect(snapshot, catalog)[0]['candidate']['stock'] == str(2**63)
    snapshot['inventory'].update({'20': 5, '21': 5})
    catalog['recipes'][0]['recipe_id'] = '10'
    snapshot['buildings'][0]['allowed_recipe_ids'] = ['3', '10']
    assert inspect(snapshot, catalog)[0]['candidate']['recipe_id'] == '3'


def test_missing_item_is_zero_only_with_confirmed_inventory():
    snapshot, catalog = fixture()
    del snapshot['inventory']['20']
    assert inspect(snapshot, catalog)[0]['candidate']['item_id'] == '20'
    snapshot['inventory_complete'] = False
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'unknown' and row['proposal'] is None


def test_filters_materials_after_reserves_before_stock_ranking():
    snapshot, catalog = fixture()
    catalog['recipes'][1]['materials'] = [{'item_id': '11', 'count': 1}]
    snapshot['inventory']['11'] = 1
    row, = inspect(snapshot, catalog, {'11': 1})
    assert row['candidate']['item_id'] == '20'
    assert row['candidate']['stock'] == '8'
    assert inspect(snapshot, catalog, {'10': 10, '11': 1})[0]['proposal'] is None


def test_all_inputs_are_aggregated_and_upstream_production_is_not_scheduled():
    snapshot, catalog = fixture()
    catalog['recipes'][1]['materials'] = [{'item_id': '10', 'count': 6}, {'item_id': '10', 'count': 6}]
    catalog['recipes'].append({'building_id': '9', 'recipe_id': '4', 'available_levels': [2],
        'volume': 1, 'materials': [], 'products': [{'item_id': '10', 'count': 100}]})
    assert inspect(snapshot, catalog)[0]['candidate']['recipe_id'] == '2'
    snapshot['inventory']['10'] = 1
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'blocked' and row['proposal'] is None


@pytest.mark.parametrize('change', [
    {'allowed_recipe_ids': ['2']}, {'level': 3},
])
def test_exact_level_and_explicit_allowed_recipe_membership_are_both_required(change):
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(change)
    row, = inspect(snapshot, catalog)
    if 'allowed_recipe_ids' in change:
        assert row['candidate']['recipe_id'] == '2'
    else:
        assert row['proposal'] is None


@pytest.mark.parametrize('change', [
    {'recipe_state': None}, {'recipe_state': True}, {'recipe_id_known': False},
    {'quantity_mode': 'unverified'}, {'allowed_recipe_ids': None},
    {'capacity_remaining': None}, {'work_energy': None}, {'editable': None},
])
def test_unknown_required_state_never_produces(change):
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(change)
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'unknown' and row['proposal'] is None


@pytest.mark.parametrize('change', [
    {'work_energy': 0}, {'capacity_remaining': 0}, {'editable': False},
])
def test_known_unavailable_workplace_is_blocked(change):
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(change)
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'blocked' and row['proposal'] is None


@pytest.mark.parametrize('mode,intended,completed', [('continuous', 0, 0), ('batch', 2, 1)])
def test_active_recipe_is_working_and_never_replaced(mode, intended, completed):
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(recipe_id='2', current_recipe_id='2', recipe_state=1,
        quantity_mode=mode, intend_count=intended, completed_count=completed)
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'working' and row['candidate'] is None and row['proposal'] is None


@pytest.mark.parametrize('state,intended,completed', [(0, 2, 1), (0, 2, 2), (1, 2, 2)])
def test_stopped_or_completed_nonzero_recipe_never_becomes_idle(state, intended, completed):
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(recipe_id='2', current_recipe_id='2', recipe_state=state,
        intend_count=intended, completed_count=completed)
    assert inspect(snapshot, catalog)[0]['proposal'] is None


def test_recipe_zero_with_working_state_is_unknown_instead_of_overwritten():
    snapshot, catalog = fixture()
    snapshot['buildings'][0]['recipe_state'] = 1
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'unknown' and row['proposal'] is None


def test_only_explicit_missing_worker_is_marked_missing_even_without_inventory():
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(worker_uid='0', worker_missing=True, worker_valid=False,
        worker_reason='worker_missing')
    snapshot['inventory_complete'] = False
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'missing_worker' and row['message'] == '缺工作噜咪'
    assert row['proposal'] is None


@pytest.mark.parametrize('reason,expected', [
    ('roster_unknown', 'unknown'), ('dispatch_unknown', 'unknown'),
    ('worker_state_unknown', 'unknown'), ('work_types_unknown', 'unknown'),
    ('worker_dispatched', 'blocked'), ('work_type_mismatch', 'blocked'),
    ('worker_conflict', 'blocked'), (None, 'unknown'),
])
def test_worker_unknown_and_known_ineligibility_are_not_called_missing(reason, expected):
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(worker_valid=False, worker_reason=reason)
    row, = inspect(snapshot, catalog)
    assert row['status'] == expected and row['proposal'] is None
    assert row['message'] != '缺工作噜咪'


def test_continuous_has_zero_times_only_for_recipes_without_material_costs():
    snapshot, catalog = fixture()
    snapshot['buildings'][0]['quantity_mode'] = 'continuous'
    catalog['recipes'][0]['materials'] = []
    row, = inspect(snapshot, catalog)
    assert row['candidate']['recipe_id'] == '2'
    assert row['candidate']['recipe_times'] == '0'
    assert row['proposal']['operation']['recipe_times'] == 0
    assert row['proposal']['costs'] == []
    catalog['recipes'][0]['materials'] = [{'item_id': '10', 'count': 1}]
    assert inspect(snapshot, catalog)[0]['proposal'] is None


def test_every_workplace_is_reported_in_input_order_and_input_fields_are_preserved():
    snapshot, catalog = fixture()
    second = deepcopy(snapshot['buildings'][0])
    second.update(building_uid='8', name='第二工位', worker_uid='100', editable=False)
    snapshot['buildings'].append(second)
    rows = inspect(snapshot, catalog)
    assert [row['building_uid'] for row in rows] == ['7', '8']
    assert rows[1]['name'] == '第二工位' and rows[1]['worker_uid'] == '100'
    assert rows[1]['candidate'] is None and rows[1]['proposal'] is None


@pytest.mark.parametrize('worker_uid', [None, '', '0', '2147483647', '099', True])
def test_valid_flag_cannot_replace_an_exact_assigned_worker_uid(worker_uid):
    snapshot, catalog = fixture()
    snapshot['buildings'][0]['worker_uid'] = worker_uid
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'unknown' and row['proposal'] is None


@pytest.mark.parametrize('field,value', [
    ('available_levels', None), ('products', None), ('products', []),
    ('materials', None), ('volume', None),
])
def test_incomplete_recipe_never_produces_or_hides_other_feasible_recipes(field, value):
    snapshot, catalog = fixture()
    catalog['recipes'][1][field] = value
    row, = inspect(snapshot, catalog)
    assert row['candidate']['recipe_id'] == '2'


def test_worker_status_tokens_have_public_chinese_messages():
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(worker_valid=False, worker_reason='worker_dispatched')
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'blocked' and row['message'] == '工作噜咪正在外派'


def test_empty_recipe_is_distinct_from_unknown_in_the_public_workplace():
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(recipe_id_known=True, recipe_id=None, current_recipe_id=None)
    row, = inspect(snapshot, catalog)
    assert row['current_recipe_id'] == '0'
    snapshot['buildings'][0]['recipe_id_known'] = False
    row, = inspect(snapshot, catalog)
    assert row['current_recipe_id'] is None


def test_product_reserve_does_not_change_real_stock_ranking_and_duplicate_costs_are_summed():
    snapshot, catalog = fixture()
    catalog['recipes'][1]['materials'] = [{'item_id': '10', 'count': 2}, {'item_id': '10', 'count': 3}]
    row, = inspect(snapshot, catalog, {'21': 2})
    assert row['candidate']['stock'] == '2'
    assert row['proposal']['costs'] == [{'item_id': '10', 'count': 5}]


def test_disabled_idle_workplace_reports_the_default_without_a_proposal():
    snapshot, catalog = fixture()
    snapshot['buildings'][0]['auto_production_enabled'] = False
    before = deepcopy((snapshot, catalog))
    row, = inspect(snapshot, catalog)
    assert row['status'] == 'blocked'
    assert row['message'] == '默认不安排自动生产'
    assert row['candidate'] is None and row['proposal'] is None
    assert (snapshot, catalog) == before


@pytest.mark.parametrize('change,expected', [
    ({'recipe_id': '2', 'current_recipe_id': '2', 'recipe_state': 1}, 'working'),
    ({'worker_missing': True, 'worker_uid': '0'}, 'missing_worker'),
    ({'recipe_id_known': False}, 'unknown'),
    ({'worker_valid': False, 'worker_reason': 'worker_dispatched'}, 'blocked'),
])
def test_disabled_workplace_preserves_the_actual_nonidle_status(change, expected):
    snapshot, catalog = fixture()
    snapshot['buildings'][0].update(auto_production_enabled=False, **change)
    row, = inspect(snapshot, catalog)
    assert row['status'] == expected
    assert row['message'] != '默认不安排自动生产'
    assert row['candidate'] is None and row['proposal'] is None
