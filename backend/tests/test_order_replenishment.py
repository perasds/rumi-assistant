from rumi_market.order_replenishment import choose_replenishment


def fixture():
    recipe = {'building_id': '1', 'recipe_id': '2', 'available_levels': [1], 'volume': 1,
              'materials': [{'item_id': '10', 'count': 2}], 'products': [{'item_id': '20', 'count': 1}]}
    snapshot = {'inventory': {'10': 10}, 'orders': [{'is_unlock': True, 'order_type': 1,
        'materials': [{'item_id': '20', 'count': 8}]}], 'buildings': [{
        'building_uid': '7', 'building_id': '1', 'level': 1, 'editable': True,
        'worker_valid': True, 'worker_uid': '99', 'work_energy': 1000, 'recipe_id': None, 'capacity_remaining': 3}]}
    return snapshot, {'recipes': [recipe]}, {}


def test_start_only_idle_building_with_assigned_worker_and_confirmed_capacity():
    snapshot, catalog, reserves = fixture()
    proposal = choose_replenishment(snapshot, catalog, reserves)
    assert proposal['operation'] == {'action': 'produce', 'building_uid': '7', 'building_id': '1', 'recipe_id': '2', 'recipe_times': 3}
    assert proposal['costs'] == [{'item_id': '10', 'count': 6}]
    for change in ({'capacity_remaining': None}, {'recipe_id': '9'}, {'worker_valid': False}, {'editable': False}, {'work_energy': 0}, {'work_energy': None}):
        snapshot, catalog, _ = fixture()
        snapshot['buildings'][0].update(change)
        assert choose_replenishment(snapshot, catalog, reserves) is None


def test_reserves_and_shared_inputs_bound_batches_without_changing_current_jobs():
    snapshot, catalog, _ = fixture()
    assert choose_replenishment(snapshot, catalog, {'10': 8})['operation']['recipe_times'] == 1
    assert choose_replenishment(snapshot, catalog, {'10': 10}) is None


def test_recursive_shortage_starts_upstream_on_its_own_idle_assigned_building():
    snapshot, catalog, _ = fixture()
    snapshot['inventory'] = {}
    catalog['recipes'].append({'building_id': '3', 'recipe_id': '4', 'available_levels': [1], 'volume': 1,
        'materials': [], 'products': [{'item_id': '10', 'count': 1}]})
    snapshot['buildings'].append({'building_uid': '8', 'building_id': '3', 'level': 1, 'editable': True,
        'worker_valid': True, 'worker_uid': '98', 'work_energy': 1000, 'recipe_id': None, 'capacity_remaining': 5})
    proposal = choose_replenishment(snapshot, catalog, {})
    assert proposal['operation']['recipe_id'] == '4'
    assert proposal['operation']['recipe_times'] == 0 and proposal['costs'] == []


def test_disabled_idle_building_never_starts_automatic_replenishment():
    snapshot, catalog, reserves = fixture()
    snapshot['buildings'][0]['auto_production_enabled'] = False
    assert choose_replenishment(snapshot, catalog, reserves) is None


def test_disabled_existing_production_and_store_still_cover_shortage():
    from copy import deepcopy

    snapshot, catalog, reserves = fixture()
    current = deepcopy(snapshot['buildings'][0])
    current.update(building_uid='8', auto_production_enabled=False,
        recipe_id='2', recipe_id_known=True, recipe_state=1, is_producing=True,
        quantity_mode='batch', intend_count=8, completed_count=2,
        store_items=[{'item_id': '20', 'count': 2}])
    snapshot['buildings'].append(current)
    assert choose_replenishment(snapshot, catalog, reserves) is None
