"""Synthetic queue evidence; no provider, account, clock or network is used."""
from copy import deepcopy

import pytest

from rumi_market.order_production_queue import QueueBook


def recipe(**changes):
    return {'building_id': '10101', 'recipe_id': '1', 'available_levels': [1],
            'materials': [{'item_id': '10', 'count': 2}],
            'products': [{'item_id': '20', 'count': 1}]} | changes


def proposal(times=5, **changes):
    return {'operation': {'action': 'produce', 'building_uid': '7',
            'building_id': '10101', 'recipe_id': '1', 'recipe_times': times} | changes,
            'costs': [{'item_id': '10', 'count': 2 * times}]}


def raw(*, active=False, spent=0, received=0, completed=0, sequence=1):
    return {'role_key': ('7', '123'),
        'inventory': {'valid': True, 'sequence': sequence, 'movement_epoch': 0,
            'items': {'10': 100 - spent, '20': received},
            'spent_totals': {'10': spent}, 'received_totals': {'20': received}},
        'home': {'valid': True, 'sequence': sequence, 'architectures': [
            {'uid': 7, 'id': 10101, 'level': 1, 'work_old': {'recipe_id': int(active),
                'recipe_intend_times': 5 if active else 0,
                'recipe_complete_times': completed, 'recipe_state': int(active),
                'lumi_uid': '99'}}]}}


def work(snapshot):
    return snapshot['home']['architectures'][0]['work_old']


def registered(*, prepaid=False):
    book, before = QueueBook(), raw()
    after = raw(active=True, spent=10 if prepaid else 0, sequence=2)
    assert book.add(before, after, proposal(), recipe(), owner_id='run-one') is True
    return book, after


def test_add_keeps_full_future_cost_even_when_all_costs_are_prepaid():
    book, after = registered(prepaid=True)
    assert book.reserves(after) == {'10': 10}
    queue = book.active['7']
    assert (queue.times, queue.settled_batches, queue.owner_id) == (5, 0, 'run-one')
    assert queue.costs == {'10': 2} and queue.products == {'20': 1}
    assert book.blocked_items() == {'10', '20'}


def test_manual_debit_cannot_release_any_incomplete_batch():
    book, _ = registered()
    current = raw(active=True, spent=10, received=5, sequence=3)
    assert book.reserves(current) == {'10': 10}
    assert book.active['7'].settled_batches == 0


@pytest.mark.parametrize('prepaid', [False, True])
def test_settlement_uses_common_lower_bound_of_completed_debits_and_receipts(prepaid):
    book, _ = registered(prepaid=prepaid)
    first = raw(active=True, spent=10 if prepaid else 2, received=1, completed=1, sequence=3)
    assert book.reserves(first) == {'10': 8}
    second = raw(active=True, spent=10 if prepaid else 4, received=1, completed=2, sequence=4)
    assert book.reserves(second) == {'10': 8}
    second['inventory']['received_totals']['20'] = 2
    second['inventory']['items']['20'] = 2
    second['inventory']['sequence'] += 1
    assert book.reserves(second) == {'10': 6}


def test_native_completed_count_alone_and_empty_ack_cannot_release_costs():
    book, _ = registered()
    current = raw(active=True, completed=5, sequence=3)
    work(current)['recipe_id'] = 0
    assert book.refresh(current) is True
    assert book.reserves(current) == {'10': 10}
    assert book.active['7'].recipe_alive is False
    current['inventory'].update(sequence=4, spent_totals={'10': 10}, received_totals={'20': 5})
    assert book.reserves(current) == {}
    assert book.active['7'].finished and book.blocked_items() == set()


def test_known_order_debits_and_rewards_are_excluded_from_queue_evidence():
    book, _ = registered()
    book.note_order([{'item_id': '10', 'count': 8}], [{'item_id': '20', 'count': 4}])
    current = raw(active=True, spent=10, received=5, completed=5, sequence=3)
    assert book.reserves(current) == {'10': 8}
    assert book.active['7'].settled_batches == 1


def test_order_adjustment_can_arrive_before_its_inventory_notification():
    book, after = registered()
    book.note_order({'10': 8}, {'20': 4})
    assert book.refresh(after) is True
    assert book.reserves(after) == {'10': 10}


def test_same_book_preserves_reservation_and_original_owner_across_run_stop():
    book, after = registered()
    assert book.refresh(deepcopy(after)) is True
    assert book.active['7'].owner_id == 'run-one'
    assert not book.add(raw(), after, proposal(), recipe(), owner_id='run-two')
    assert book.reserves(after) == {'10': 10}


def test_cancel_freezes_remaining_cost_without_using_future_debits_or_refunds():
    book, _ = registered(prepaid=True)
    current = raw(active=True, spent=10, received=1, completed=1, sequence=3)
    assert book.reserves(current) == {'10': 8}
    work(current)['recipe_id'] = 0
    current['home']['sequence'] += 1
    assert book.retire('7', current) is True
    assert book.active == {} and book.blocked_items() == set()
    assert book.reserves(current) == {'10': 8}
    later = deepcopy(current)
    later['inventory'].update(sequence=5, spent_totals={'10': 20}, received_totals={'20': 50})
    assert book.reserves(later) == {'10': 8}


def test_cancel_tombstone_reserve_survives_a_later_queue_at_same_building():
    book, _ = registered()
    stopped = raw(active=True, sequence=3)
    work(stopped)['recipe_id'] = 0
    assert book.retire('7', stopped)
    idle = deepcopy(stopped)
    work(idle).update(recipe_state=0, recipe_intend_times=0, recipe_complete_times=0)
    idle['home']['sequence'] += 1
    started = deepcopy(idle)
    work(started).update(recipe_id=1, recipe_state=1, recipe_intend_times=5)
    started['home']['sequence'] += 1
    assert book.add(idle, started, proposal(), recipe(), owner_id='run-two')
    assert book.reserves(started) == {'10': 20}


@pytest.mark.parametrize('change', [
    lambda s: s['inventory'].update(movement_epoch=1),
    lambda s: s['inventory'].update(sequence=0),
    lambda s: s['inventory'].update(spent_totals={'10': -1}),
    lambda s: s['inventory'].update(spent_totals={'10': True}),
    lambda s: work(s).update(recipe_id=2),
    lambda s: work(s).update(recipe_intend_times=6),
    lambda s: work(s).update(recipe_complete_times=6),
    lambda s: work(s).update(lumi_uid='100'),
    lambda s: work(s).update(recipe_id=0),
    lambda s: s['home']['architectures'][0].update(has_farm_data=True),
])
def test_epoch_counter_or_unknown_queue_edit_invalidates_without_releasing(change):
    book, after = registered()
    current = deepcopy(after)
    current['home']['sequence'] += 1
    current['inventory']['sequence'] += 1
    change(current)
    assert book.refresh(current) is False
    assert book.reserves(current) == {'10': 10}


def test_completed_and_movement_counters_cannot_regress_after_settlement():
    book, _ = registered()
    current = raw(active=True, spent=4, received=2, completed=2, sequence=3)
    assert book.reserves(current) == {'10': 6}
    current['inventory'].update(sequence=4, spent_totals={'10': 3})
    assert book.refresh(current) is False
    assert book.reserves(current) == {'10': 6}


@pytest.mark.parametrize('section,key', [('inventory', 'movement_epoch'),
    ('inventory', 'sequence'), ('inventory', 'spent_totals'),
    ('inventory', 'received_totals'), ('home', 'sequence')])
def test_add_requires_explicit_complete_ledger_state(section, key):
    before, after = raw(), raw(active=True, sequence=2)
    before[section].pop(key)
    assert not QueueBook().add(before, after, proposal(), recipe(), owner_id='run')


@pytest.mark.parametrize('times', [0, -1, True, '5', 1.5])
def test_non_integer_or_unbounded_queue_size_is_denied(times):
    value = proposal()
    value['operation']['recipe_times'] = times
    assert not QueueBook().add(raw(), raw(active=True, sequence=2), value, recipe(), owner_id='run')


@pytest.mark.parametrize('change', [
    {'materials': [{'item_id': '10', 'count': True}]},
    {'materials': [{'item_id': '10', 'count': -2}]},
    {'products': []}, {'building_id': '10102'}, {'available_levels': [2]},
])
def test_invalid_or_mismatched_recipe_configuration_is_denied(change):
    assert not QueueBook().add(raw(), raw(active=True, sequence=2), proposal(),
        recipe(**change), owner_id='run')


@pytest.mark.parametrize('change', [{'materials': []}, {'quantity_mode': 'continuous'}])
def test_queue_book_only_tracks_material_recipes_with_finite_quantity_mode(change):
    value = proposal()
    if change.get('materials') == []:
        value['costs'] = []
    assert not QueueBook().add(raw(), raw(active=True, sequence=2), value,
        recipe(**change), owner_id='run')


@pytest.mark.parametrize('field,value', [('has_farm_data', True),
    ('cur_recipe', {}), ('quantity_mode', 'continuous')])
def test_explicit_native_branch_conflict_prevents_registration(field, value):
    before, after = raw(), raw(active=True, sequence=2)
    after['home']['architectures'][0][field] = value
    assert not QueueBook().add(before, after, proposal(), recipe(), owner_id='run')


def test_building_authorization_stays_with_service_and_native_operations():
    before, after, plan, config = raw(), raw(active=True, sequence=2), proposal(), recipe()
    for snapshot in (before, after):
        snapshot['home']['architectures'][0]['id'] = 12345
    plan['operation']['building_id'] = config['building_id'] = '12345'
    assert QueueBook().add(before, after, plan, config, owner_id='run')


def test_costs_must_match_exact_total_recipe_materials():
    value = proposal()
    value['costs'][0]['count'] = 2
    assert not QueueBook().add(raw(), raw(active=True, sequence=2), value, recipe(), owner_id='run')


def test_other_active_queue_cannot_share_cost_or_product_attribution():
    book, first = registered()
    before = deepcopy(first)
    extra = deepcopy(before['home']['architectures'][0])
    extra['uid'] = 8
    extra['work_old'].update(recipe_id=0, recipe_intend_times=0, recipe_state=0)
    before['home']['architectures'].append(extra)
    before['home']['sequence'] += 1
    after = deepcopy(before)
    after['home']['architectures'][1]['work_old'].update(recipe_id=1, recipe_intend_times=5, recipe_state=1)
    after['home']['sequence'] += 1
    assert not book.add(before, after, proposal(building_uid='8'), recipe(), owner_id='second')


def test_add_and_refresh_do_not_mutate_caller_snapshots_or_catalog():
    book, before, after, plan, config = QueueBook(), raw(), raw(active=True, sequence=2), proposal(), recipe()
    original = deepcopy((before, after, plan, config))
    assert book.add(before, after, plan, config, owner_id='run')
    book.refresh(after)
    assert (before, after, plan, config) == original


def test_every_material_and_product_must_confirm_the_same_number_of_batches():
    book, before, after = QueueBook(), raw(), raw(active=True, sequence=2)
    config = recipe(materials=[{'item_id': '10', 'count': 2}, {'item_id': '11', 'count': 3}],
        products=[{'item_id': '20', 'count': 1}, {'item_id': '21', 'count': 2}])
    plan = proposal()
    plan['costs'].append({'item_id': '11', 'count': 15})
    assert book.add(before, after, plan, config, owner_id='run')
    current = raw(active=True, spent=10, received=5, completed=5, sequence=3)
    current['inventory']['spent_totals']['11'] = 6
    current['inventory']['received_totals']['21'] = 2
    assert book.reserves(current) == {'10': 8, '11': 12}
    assert book.active['7'].settled_batches == 1


def test_fully_settled_but_native_active_queue_remains_available_for_owner_cancel():
    book, _ = registered()
    current = raw(active=True, spent=10, received=5, completed=5, sequence=3)
    assert book.reserves(current) == {}
    queue = book.active['7']
    assert queue.finished and queue.recipe_alive and queue.owner_id == 'run-one'
    assert not book.retire('7', current)
    assert book.active['7'] is queue
    work(current)['recipe_id'] = 0
    current['home']['sequence'] += 1
    assert book.retire('7', current)
    assert book.reserves(current) == {}


def test_known_order_offsets_before_new_queue_do_not_reduce_its_later_evidence():
    book = QueueBook()
    book.note_order({'10': 8}, {'20': 4})
    before = raw(spent=8, received=4)
    after = raw(active=True, spent=8, received=4, sequence=2)
    assert book.add(before, after, proposal(), recipe(), owner_id='run')
    current = raw(active=True, spent=10, received=5, completed=1, sequence=3)
    assert book.reserves(current) == {'10': 8}


@pytest.mark.parametrize('change', [
    lambda s: work(s).update(recipe_state=99),
    lambda s: work(s).update(recipe_complete_times=True),
    lambda s: s['home'].update(sequence=True),
    lambda s: s['inventory'].update(movement_epoch=False),
])
def test_unknown_or_boolean_native_fields_cannot_be_used_as_queue_evidence(change):
    book, after = registered()
    current = deepcopy(after)
    current['home']['sequence'] += 1
    change(current)
    assert book.refresh(current) is False


def test_notification_without_a_home_sequence_change_cannot_advance_completed_count():
    book, after = registered()
    current = deepcopy(after)
    work(current)['recipe_complete_times'] = 1
    current['inventory'].update(sequence=3, spent_totals={'10': 2}, received_totals={'20': 1})
    assert not book.refresh(current)
    assert book.reserves(current) == {'10': 10}


def test_new_generation_can_start_after_idle_queue_retains_its_complete_count():
    book, before = QueueBook(), raw()
    work(before).update(recipe_intend_times=3, recipe_complete_times=3)
    after = raw(active=True, sequence=2)
    assert book.add(before, after, proposal(), recipe(), owner_id='new-generation')
    assert book.active['7'].current_completed == 0
    assert book.reserves(after) == {'10': 10}
    current = raw(active=True, spent=2, received=1, completed=1, sequence=3)
    assert book.reserves(current) == {'10': 8}


def test_same_book_replaces_a_fully_settled_idle_queue_with_a_new_generation():
    book, _ = registered()
    previous = raw(active=True, spent=10, received=5, completed=5, sequence=3)
    work(previous).update(recipe_id=0, recipe_state=0)
    assert book.reserves(previous) == {}
    after = deepcopy(previous)
    work(after).update(recipe_id=1, recipe_state=1, recipe_complete_times=0)
    after['home']['sequence'] += 1
    assert book.add(previous, after, proposal(), recipe(), owner_id='run-two')
    assert book.active['7'].owner_id == 'run-two'
    assert book.active['7'].settled_batches == 0
    assert book.reserves(after) == {'10': 10}


@pytest.mark.parametrize('settled', [0, 1, 5])
def test_cancel_with_both_native_counts_cleared_freezes_previously_settled_remainder(settled):
    book, _ = registered(prepaid=True)
    current = raw(active=True, spent=10, received=settled, completed=settled, sequence=3)
    assert book.reserves(current) == ({'10': 2 * (5 - settled)} if settled < 5 else {})
    cancelled = deepcopy(current)
    work(cancelled).update(recipe_id=0, recipe_intend_times=0, recipe_complete_times=0)
    cancelled['home']['sequence'] += 1
    # Later receipts in the cancellation notification cannot reconstruct batches.
    cancelled['inventory'].update(sequence=4, received_totals={'20': 5, '10': 10})
    assert book.retire('7', cancelled)
    assert book.active == {}
    assert book.blocked_items() == set()
    assert book.reserves(cancelled) == ({'10': 2 * (5 - settled)} if settled < 5 else {})


@pytest.mark.parametrize('change', [
    lambda s: work(s).update(recipe_intend_times=0, recipe_complete_times=1),
    lambda s: work(s).update(recipe_intend_times=5, recipe_complete_times=0),
    lambda s: work(s).update(recipe_intend_times=4, recipe_complete_times=1),
    lambda s: work(s).update(lumi_uid='100'),
    lambda s: s['home']['architectures'][0].update(id=10105),
    lambda s: s['home']['architectures'][0].update(level=2),
    lambda s: s['home'].update(sequence=3),
])
def test_cancel_does_not_accept_partial_reset_identity_change_or_stale_notification(change):
    book, _ = registered()
    current = raw(active=True, spent=2, received=1, completed=1, sequence=3)
    assert book.reserves(current) == {'10': 8}
    cancelled = deepcopy(current)
    work(cancelled)['recipe_id'] = 0
    cancelled['home']['sequence'] += 1
    change(cancelled)
    assert not book.retire('7', cancelled)
    assert book.active['7'].settled_batches == 1


def external(snapshot, *, active=True):
    row = deepcopy(snapshot['home']['architectures'][0])
    row.update(uid=8, id=10105)
    row['work_old'].update(recipe_id=2 if active else 0, recipe_intend_times=3 if active else 0,
        recipe_complete_times=0, recipe_state=int(active), lumi_uid='100')
    snapshot['home']['architectures'].append(row)
    return snapshot


def external_book(*, spent=None, received=None, active=True):
    book = QueueBook()
    before, after = external(raw(), active=active), external(raw(active=True, sequence=2), active=active)
    assert book.add(before, after, proposal(), recipe(), owner_id='run',
        external_spent=spent, external_received=received)
    return book, after


def test_external_material_debits_cannot_settle_owned_batches():
    book, _ = external_book(spent={'10': 6})
    current = external(raw(active=True, spent=6, received=3, completed=3, sequence=3))
    assert book.reserves(current) == {'10': 10}
    current['inventory'].update(sequence=4, spent_totals={'10': 8})
    assert book.reserves(current) == {'10': 8}


def test_external_same_product_receipts_cannot_settle_owned_batches():
    book, _ = external_book(received={'20': 4})
    current = external(raw(active=True, spent=10, received=4, completed=5, sequence=3))
    assert book.reserves(current) == {'10': 10}
    current['inventory'].update(sequence=4, received_totals={'20': 5})
    assert book.reserves(current) == {'10': 8}


def test_owned_evidence_must_exceed_external_budgets_and_known_order_movements():
    book, _ = external_book(spent={'10': 6}, received={'20': 4})
    book.note_order({'10': 8}, {'20': 2})
    current = external(raw(active=True, spent=16, received=7, completed=5, sequence=3))
    assert book.reserves(current) == {'10': 8}
    assert book.active['7'].external_spent == {'10': 6}
    assert book.active['7'].external_received == {'20': 4}


@pytest.mark.parametrize('budget', [{'10': -1}, {'10': True}, {'10': '2'}, []])
def test_external_budget_requires_nonnegative_integer_dictionary(budget):
    assert not QueueBook().add(raw(), raw(active=True, sequence=2), proposal(), recipe(),
        owner_id='run', external_spent=budget)


def test_external_budget_is_copied_and_accepts_zero():
    budget = {'10': 6, '11': 0}
    book, after = external_book(spent=budget)
    budget['10'] = 0
    assert book.active['7'].external_spent['10'] == 6
    assert book.refresh(after)


@pytest.mark.parametrize('change', [
    lambda s: s['home']['architectures'][1]['work_old'].update(recipe_id=3),
    lambda s: s['home']['architectures'][1]['work_old'].update(recipe_intend_times=4),
    lambda s: s['home']['architectures'][1]['work_old'].update(lumi_uid='101'),
    lambda s: s['home']['architectures'][1].update(id=10109),
    lambda s: s['home']['architectures'][1].update(level=2),
    lambda s: s['home']['architectures'].append(dict(s['home']['architectures'][1], uid=9)),
])
def test_external_queue_identity_changes_invalidate_before_releasing_owned_reserves(change):
    book, _ = external_book(spent={'10': 6}, received={'20': 3})
    current = external(raw(active=True, spent=16, received=8, completed=5, sequence=3))
    change(current)
    assert not book.refresh(current)
    assert book.reserves(current) == {'10': 10}


def test_external_idle_building_cannot_start_a_new_unbudgeted_queue():
    book, _ = external_book(active=False)
    current = external(raw(active=True, spent=10, received=5, completed=5, sequence=3))
    assert not book.refresh(current)
    assert book.reserves(current) == {'10': 10}


def test_external_idle_counter_increase_is_not_continuous_production_progress():
    book, after = external_book(active=False)
    current = deepcopy(after)
    current['home']['sequence'] += 1
    current['home']['architectures'][1]['work_old']['recipe_complete_times'] = 2
    assert not book.refresh(current)


def test_external_completed_count_cannot_regress_while_recipe_is_still_active():
    book, after = external_book()
    current = deepcopy(after)
    current['home']['sequence'] += 1
    current['home']['architectures'][1]['work_old']['recipe_complete_times'] = 2
    assert book.refresh(current)
    current['home']['sequence'] += 1
    current['home']['architectures'][1]['work_old']['recipe_complete_times'] = 1
    assert not book.refresh(current)


def test_external_completed_count_progresses_monotonically_and_may_stop_normally():
    book, _ = external_book(spent={'10': 6}, received={'20': 3})
    current = external(raw(active=True, sequence=3))
    other = current['home']['architectures'][1]['work_old']
    other['recipe_complete_times'] = 2
    assert book.refresh(current)
    current['home']['sequence'] += 1
    other.update(recipe_id=0, recipe_intend_times=0, recipe_complete_times=0)
    assert book.refresh(current)
    current['home']['sequence'] += 1
    other.update(recipe_id=2, recipe_intend_times=3)
    assert not book.refresh(current)


def test_external_continuous_and_cur_recipe_progress_alone_do_not_invalidate():
    book, before, after = QueueBook(), external(raw()), external(raw(active=True, sequence=2))
    for snapshot in (before, after):
        row = snapshot['home']['architectures'][1]
        row['work_old']['recipe_intend_times'] = 0
        row['cur_recipe'] = {'recipe_id': 12, 'recipe_lumi_uid': '100', 'recipe_real_run_seconds': 0}
    assert book.add(before, after, proposal(), recipe(), owner_id='run')
    current = deepcopy(after)
    current['home']['sequence'] += 1
    current['home']['architectures'][1]['work_old']['recipe_complete_times'] = 9
    current['home']['architectures'][1]['cur_recipe']['recipe_real_run_seconds'] = 30
    assert book.refresh(current)
    current['home']['sequence'] += 1
    current['home']['architectures'][1]['cur_recipe']['recipe_id'] = 13
    assert not book.refresh(current)


def test_later_book_owned_queue_and_its_confirmed_retirement_are_not_external_edits():
    book, after = external_book(active=False)
    started = deepcopy(after)
    started['home']['sequence'] += 1
    started['home']['architectures'][1]['work_old'].update(recipe_id=2,
        recipe_intend_times=5, recipe_state=1)
    plan = {'operation': {'action': 'produce', 'building_uid': '8', 'building_id': '10105',
        'recipe_id': '2', 'recipe_times': 5}, 'costs': [{'item_id': '30', 'count': 5}]}
    config = recipe(building_id='10105', recipe_id='2', materials=[{'item_id': '30', 'count': 1}],
        products=[{'item_id': '40', 'count': 1}])
    assert book.add(after, started, plan, config, owner_id='run')
    assert book.refresh(started)
    cancelled = deepcopy(started)
    cancelled['home']['sequence'] += 1
    cancelled['home']['architectures'][1]['work_old'].update(recipe_id=0,
        recipe_intend_times=0, recipe_complete_times=0)
    assert book.retire('8', cancelled)
    assert book.refresh(cancelled)
    assert book.reserves(cancelled) == {'10': 10, '30': 5}


def unreserved_start(before, *, times=3, item='40'):
    after = deepcopy(before)
    after['home']['sequence'] += 1
    after['home']['architectures'][1]['work_old'].update(recipe_id=2,
        recipe_intend_times=times, recipe_complete_times=0, recipe_state=1)
    operation = {'action': 'produce', 'building_uid': '8', 'building_id': '10105',
        'recipe_id': '2', 'recipe_times': times}
    config = recipe(building_id='10105', recipe_id='2', materials=[],
        products=[{'item_id': item, 'count': 1}])
    return after, operation, config


@pytest.mark.parametrize('times', [0, 3])
def test_confirmed_nocost_start_updates_external_baseline_without_adding_ownership(times):
    book, before = external_book(active=False)
    after, operation, config = unreserved_start(before, times=times)
    assert book.note_unreserved_start(before, after, operation, config)
    assert set(book.active) == {'7'}
    assert book.active['7'].owner_id == 'run'
    assert book.refresh(after)
    assert book.reserves(after) == {'10': 10}


@pytest.mark.parametrize('item', ['10', '20'])
def test_nocost_start_cannot_touch_unfinished_owned_inputs_or_outputs(item):
    book, before = external_book(active=False)
    after, operation, config = unreserved_start(before, item=item)
    assert not book.note_unreserved_start(before, after, operation, config)
    assert book.refresh(before)


@pytest.mark.parametrize('change', [
    lambda a, o, c: c.update(materials=[{'item_id': '30', 'count': 1}]),
    lambda a, o, c: o.update(action='complete'),
    lambda a, o, c: o.update(recipe_times=True),
    lambda a, o, c: o.update(recipe_times=-1),
    lambda a, o, c: a['home'].update(sequence=2),
    lambda a, o, c: a['home']['architectures'][1]['work_old'].update(recipe_id=3),
    lambda a, o, c: a['home']['architectures'][1]['work_old'].update(recipe_intend_times=4),
    lambda a, o, c: a['home']['architectures'][1]['work_old'].update(lumi_uid='101'),
    lambda a, o, c: a['home']['architectures'][1].update(level=2),
])
def test_nocost_start_requires_confirmed_exact_recipe_configuration(change):
    book, before = external_book(active=False)
    after, operation, config = unreserved_start(before)
    change(after, operation, config)
    assert not book.note_unreserved_start(before, after, operation, config)


def test_nocost_start_cannot_hide_another_external_generation_change_or_partially_settle():
    book, before, baseline = QueueBook(), external(raw(), active=False), external(raw(active=True, sequence=2), active=False)
    for snapshot in (before, baseline):
        snapshot['home']['architectures'].append(dict(deepcopy(snapshot['home']['architectures'][1]), uid=9))
    assert book.add(before, baseline, proposal(), recipe(), owner_id='run')
    after, operation, config = unreserved_start(baseline)
    work(after)['recipe_complete_times'] = 1
    after['inventory'].update(sequence=3, spent_totals={'10': 2}, received_totals={'20': 1})
    after['home']['architectures'][2]['work_old'].update(recipe_id=3,
        recipe_intend_times=2, recipe_state=1)
    assert not book.note_unreserved_start(baseline, after, operation, config)
    assert book.active['7'].settled_batches == 0
    assert book.refresh(baseline)


def test_nocost_start_cannot_hide_an_earlier_target_generation_change():
    book, before = external_book(active=False)
    work_on_target = before['home']['architectures'][1]['work_old']
    work_on_target.update(recipe_intend_times=2, recipe_complete_times=2)
    before['home']['sequence'] += 1
    after, operation, config = unreserved_start(before)
    assert not book.note_unreserved_start(before, after, operation, config)


def test_confirmed_cancel_idle_fingerprint_ignores_clock_and_store_but_rejects_new_counts():
    book, _ = registered()
    cancelled = raw(active=True, sequence=3)
    work(cancelled)['recipe_id'] = 0
    assert book.retire('7', cancelled)
    assert book.is_confirmed_idle(cancelled, '7')
    current = deepcopy(cancelled)
    work(current)['recipe_update_time'] = 1000
    current['home']['architectures'][0]['store_items'] = [{'item_id': '20', 'count': 1}]
    assert book.is_confirmed_idle(current, '7')
    work(current)['recipe_complete_times'] = 1
    assert not book.is_confirmed_idle(current, '7')


def test_observed_new_generation_permanently_expires_confirmed_idle_fingerprint():
    book, _ = registered()
    cancelled = raw(active=True, sequence=3)
    work(cancelled).update(recipe_id=0, recipe_intend_times=0, recipe_complete_times=0)
    assert book.retire('7', cancelled)
    assert book.is_confirmed_idle(cancelled, '7')
    restarted = deepcopy(cancelled)
    restarted['home']['sequence'] += 1
    work(restarted).update(recipe_id=2, recipe_intend_times=3)
    assert book.refresh(restarted)
    assert not book.is_confirmed_idle(cancelled, '7')


def test_confirmed_partial_cancel_retained_counts_allow_replacement_with_frozen_debt():
    book, _ = registered()
    current = raw(active=True, spent=2, received=1, completed=1, sequence=3)
    assert book.reserves(current) == {'10': 8}
    work(current).update(recipe_id=0, recipe_state=0)
    current['home']['sequence'] += 1
    assert book.retire('7', current)
    assert book.is_confirmed_idle(current, '7')
    after = deepcopy(current)
    work(after).update(recipe_id=1, recipe_state=1, recipe_complete_times=0)
    after['home']['sequence'] += 1
    assert book.add(current, after, proposal(), recipe(), owner_id='new-run')
    assert book.reserves(after) == {'10': 18}
    assert not book.is_confirmed_idle(after, '7')


def test_unconfirmed_idle_partial_counters_still_do_not_allow_replacement():
    before = raw()
    work(before).update(recipe_intend_times=5, recipe_complete_times=1)
    assert not QueueBook().add(before, raw(active=True, sequence=2), proposal(), recipe(), owner_id='run')


def test_nocost_start_replaces_only_finished_old_ownership_at_its_target():
    book, _ = registered()
    before = raw(active=True, spent=10, received=5, completed=5, sequence=3)
    work(before).update(recipe_id=0, recipe_state=0)
    assert book.reserves(before) == {}
    after = deepcopy(before)
    work(after).update(recipe_id=2, recipe_state=1, recipe_intend_times=3, recipe_complete_times=0)
    after['home']['sequence'] += 1
    operation = {'action': 'produce', 'building_uid': '7', 'building_id': '10101',
        'recipe_id': '2', 'recipe_times': 3}
    config = recipe(recipe_id='2', materials=[], products=[{'item_id': '40', 'count': 1}])
    assert book.note_unreserved_start(before, after, operation, config)
    assert book.active == {}
    assert book.refresh(after)


def test_nocost_start_cannot_hide_owned_progress_regression_between_its_two_snapshots():
    book, baseline = external_book(active=False)
    before = deepcopy(baseline)
    work(before)['recipe_complete_times'] = 1
    before['home']['sequence'] += 1
    before['inventory'].update(sequence=3, spent_totals={'10': 2}, received_totals={'20': 1})
    after, operation, config = unreserved_start(before)
    work(after)['recipe_complete_times'] = 0
    assert not book.note_unreserved_start(before, after, operation, config)
    assert book.active['7'].settled_batches == 0
