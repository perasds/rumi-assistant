"""Ordinary work_old estimates, using server seconds and integer arithmetic.

The floor duration mirrors the client display. The separate ceil values are for
planning; neither value authorizes claiming products without server confirmation.
"""


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def estimate_production_time(building, recipe, *, now, revision):
    result = dict(time_source='unknown', duration_seconds=None, ready_at_server_seconds=None,
                  inputs_revision=revision, blocked_reason=None, next_batch_seconds=None,
                  safe_next_batch_seconds=None, safe_duration_seconds=None,
                  remaining_batches=None, completed_since_update=0, aligned_progress=None)

    def blocked(reason):
        return result | {'blocked_reason': reason}

    if building.get('blocked_reason'):
        return blocked(building['blocked_reason'])
    if str(building.get('worker_uid')) in {'None', '', '0', '2147483647'} or building.get('worker_valid') is False:
        return blocked('worker_invalid')
    if not recipe or str(building.get('recipe_id')) != str(recipe.get('recipe_id')):
        return blocked('recipe_unknown')
    speed, energy = building.get('work_speed'), building.get('work_energy')
    if not _integer(speed, 1):
        return blocked('speed_unknown')
    if not _integer(energy, 1):
        return blocked('energy_unavailable')
    state = building.get('recipe_state')
    if state == 0:
        return blocked('server_stopped')
    if state != 1 or type(state) is not int:
        return blocked('state_unknown')
    if building.get('is_producing') is False:
        return blocked('not_producing')
    capacity, volume = building.get('capacity_remaining'), recipe.get('volume')
    if not _integer(capacity) or not _integer(volume, 1):
        return blocked('capacity_unknown')
    if capacity < volume:
        return blocked('capacity_full')
    work = recipe.get('work_amount')
    progress, updated = building.get('work_progress'), building.get('recipe_update_time')
    intended, completed = building.get('intend_count'), building.get('completed_count')
    if not _integer(work, 1):
        return blocked('work_amount_unknown')
    if not all(_integer(value) for value in (progress, updated, intended, completed)):
        return blocked('progress_unknown')
    if not _integer(now) or now < updated:
        return blocked('clock_before_update')
    continuous = not recipe.get('materials') and intended == 0
    if not continuous and intended <= completed:
        return blocked('queue_finished')
    per_batch = work * 10000
    if progress >= per_batch:
        return blocked('progress_invalid')
    elapsed_completed, aligned = divmod(progress + (now - updated) * speed, per_batch)
    remaining = 1 if continuous else max(0, intended - completed - elapsed_completed)
    result.update(remaining_batches=remaining, completed_since_update=elapsed_completed, aligned_progress=aligned)
    if remaining > capacity // volume:
        return blocked('capacity_insufficient')
    numerator = max(0, per_batch * remaining - aligned) if remaining else 0
    safe_duration = (numerator + speed - 1) // speed
    # Ordinary work consumes at least one unit per working second. A configured
    # energy duration, when available, is stronger than this necessary bound.
    energy_seconds = building.get('energy_seconds_remaining')
    if energy < safe_duration or (_integer(energy_seconds) and energy_seconds < safe_duration):
        return blocked('energy_insufficient')
    next_numerator = max(0, per_batch - aligned) if remaining else 0
    result.update(time_source='calculated', duration_seconds=numerator // speed,
                  ready_at_server_seconds=now + numerator // speed,
                  safe_duration_seconds=safe_duration, next_batch_seconds=next_numerator // speed,
                  safe_next_batch_seconds=(next_numerator + speed - 1) // speed)
    server_ready = building.get('server_ready_at_seconds')
    if _integer(server_ready):
        duration = max(0, server_ready - now)
        result.update(time_source='server', duration_seconds=duration,
                      ready_at_server_seconds=max(now, server_ready), safe_duration_seconds=duration)
    return result
