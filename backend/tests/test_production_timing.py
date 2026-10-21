import pytest


def building(**changes):
    value = dict(building_uid='b1', building_id='wood', level=1, worker_uid='w1',
                 recipe_id='wood', work_speed=3000, work_progress=1000,
                 recipe_update_time=100, intend_count=4, completed_count=1,
                 work_energy=1000, recipe_state=1, capacity_remaining=100,
                 editable=True, blocked_reason=None)
    return value | changes


def recipe(**changes):
    return dict(recipe_id='wood', work_amount=2, volume=1,
                materials=[{'item_id': 'seed', 'count': 1}],
                products=[{'item_id': 'wood', 'count': 1}]) | changes


def estimate(row=None, config=None, now=102):
    from rumi_market.production_timing import estimate_production_time
    return estimate_production_time(row or building(), config or recipe(), now=now, revision='r1')


def test_remaining_time_aligns_progress_with_server_seconds_and_uses_work_amount():
    result = estimate(config=recipe(first_work_amount=99))
    assert result['time_source'] == 'calculated'
    assert result['duration_seconds'] == 17  # floor((3*20000 - (1000+2*3000))/3000)
    assert result['ready_at_server_seconds'] == 119
    assert result['next_batch_seconds'] == 4
    assert result['safe_next_batch_seconds'] == 5
    assert result['remaining_batches'] == 3
    assert result['inputs_revision'] == 'r1'


def test_elapsed_complete_batches_reduce_remaining_without_double_counting():
    result = estimate(now=108)
    assert result['remaining_batches'] == 2
    assert result['duration_seconds'] == 11
    assert result['completed_since_update'] == 1


def test_continuous_production_has_only_one_finite_next_batch():
    result = estimate(row=building(intend_count=0), config=recipe(materials=[]))
    assert result['remaining_batches'] == 1
    assert result['duration_seconds'] == 4


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'worker_uid': None}, 'worker_invalid'),
    ({'worker_uid': '2147483647'}, 'worker_invalid'),
    ({'work_speed': 0}, 'speed_unknown'),
    ({'work_energy': 0}, 'energy_unavailable'),
    ({'recipe_state': 0}, 'server_stopped'),
    ({'recipe_state': None}, 'state_unknown'),
    ({'capacity_remaining': None}, 'capacity_unknown'),
    ({'capacity_remaining': 0}, 'capacity_full'),
    ({'recipe_update_time': None}, 'progress_unknown'),
    ({'recipe_update_time': 103}, 'clock_before_update'),
    ({'blocked_reason': 'upgrading'}, 'upgrading'),
    ({'is_producing': False}, 'not_producing'),
    ({'worker_valid': False}, 'worker_invalid'),
    ({'intend_count': 1}, 'queue_finished'),
])
def test_incomplete_or_blocked_work_never_becomes_zero_seconds(changes, reason):
    result = estimate(row=building(**changes))
    assert result['time_source'] == 'unknown'
    assert result['duration_seconds'] is None
    assert result['ready_at_server_seconds'] is None
    assert result['blocked_reason'] == reason


def test_unknown_capacity_is_not_replaced_by_catalog_base_capacity():
    result = estimate(row=building(capacity_remaining=None), config=recipe(base_capacity=999999))
    assert result['blocked_reason'] == 'capacity_unknown'


def test_semantically_verified_server_deadline_has_explicit_source():
    result = estimate(row=building(server_ready_at_seconds=120))
    assert result['time_source'] == 'server'
    assert result['duration_seconds'] == 18


def test_energy_that_cannot_cover_the_queue_does_not_promise_wall_clock_eta():
    result = estimate(row=building(work_energy=3))
    assert result['blocked_reason'] == 'energy_insufficient'
