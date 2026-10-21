"""Read-only order selection and conservative production simulation.

Current-inventory selection has a bounded exact search. Production is a feasible
estimate for the supplied current orders, never a claim about future random orders.
All quantities remain Python integers; transport adapters own decimal serialization.
"""
from collections import defaultdict
from copy import deepcopy

from .production_timing import estimate_production_time


def _vector(rows):
    result = defaultdict(int)
    for row in rows:
        count = row.get('count')
        if type(count) is not int or count < 0:
            raise ValueError('invalid material quantity')
        result[str(row['item_id'])] += count
    return dict(result)


def _producers(catalog):
    result = defaultdict(list)
    for recipe in catalog.get('recipes', []):
        for product in recipe['products']:
            if product['count'] > 0:
                result[str(product['item_id'])].append(recipe)
    return result


def expand_material_requirements(required, inventory, catalog):
    """Expand shortages into base products, consuming intermediates only once."""
    stock = dict(inventory)
    base = defaultdict(int)
    producers = _producers(catalog)

    def consume(item, count, path):
        held = min(count, max(0, stock.get(item, 0)))
        stock[item] = stock.get(item, 0) - held
        count -= held
        if count <= 0:
            return
        if item in path:
            raise ValueError('material dependency cycle')
        recipes = producers.get(item, [])
        recipe = recipes[0] if recipes else None
        if recipe is None or not recipe.get('materials'):
            base[item] += count
            return
        output = next(row['count'] for row in recipe['products'] if str(row['item_id']) == item)
        batches = (count + output - 1) // output
        for source, quantity in _vector(recipe['materials']).items():
            consume(source, quantity * batches, path | {item})
        for product in recipe['products']:
            product_id = str(product['item_id'])
            stock[product_id] = stock.get(product_id, 0) + product['count'] * batches
        stock[item] -= count

    # Hold direct order requirements first: upstream expansion must not spend them.
    missing = {}
    for item, count in required.items():
        held = min(count, max(0, stock.get(item, 0)))
        stock[item] = stock.get(item, 0) - held
        missing[item] = count - held
    for item, count in missing.items():
        consume(item, count, set())
    return dict(sorted(base.items()))


def _cans(order):
    return sum(row['count'] for row in order.get('rewards', []) if str(row.get('item_id')) == '54001')


def _select_inventory(orders, inventory, limit, budget):
    vectors = [_vector(row['materials']) for row in orders]
    rewards = [_cans(row) for row in orders]
    best, best_score, nodes, exhausted = [], (0, 0), 0, False

    def visit(index, chosen, stock, cans):
        nonlocal best, best_score, nodes, exhausted
        if nodes >= budget:
            exhausted = True
            return
        nodes += 1
        score = (len(chosen), cans)
        if score > best_score:
            best, best_score = chosen[:], score
        if index == len(orders) or len(chosen) == limit:
            return
        if len(chosen) + min(limit - len(chosen), len(orders) - index) < best_score[0]:
            return
        needed = vectors[index]
        if all(quantity <= stock.get(item, 0) for item, quantity in needed.items()):
            changed = stock.copy()
            for item, quantity in needed.items():
                changed[item] = changed.get(item, 0) - quantity
            visit(index + 1, chosen + [index], changed, cans + rewards[index])
        visit(index + 1, chosen, stock, cans)

    visit(0, [], inventory, 0)
    return best, best_score, not exhausted


class _Unavailable(Exception):
    def __init__(self, item, recipe=None, building=None, reason='production_unavailable'):
        self.item, self.recipe, self.building, self.reason = item, recipe, building, reason


class _Schedule:
    def __init__(self, snapshot, catalog, stock, now, deadline):
        self.revision, self.now, self.deadline = snapshot['revision'], now, deadline
        self.catalog, self.producers = catalog, _producers(catalog)
        self.buildings = snapshot.get('buildings', [])
        self.lots = {item: [[count, now]] for item, count in stock.items() if count > 0}
        self.building_calendar, self.worker_calendar = {}, {}
        self.used_batches = defaultdict(int)
        self.rows, self.actions = [], []

    def _take(self, item, count):
        ready = self.now
        for lot in sorted(self.lots.get(item, []), key=lambda row: row[1]):
            taken = min(count, lot[0])
            if taken:
                lot[0] -= taken
                count -= taken
                ready = max(ready, lot[1])
        return count, ready

    def ensure(self, item, count, path=frozenset()):
        count, ready = self._take(item, count)
        if count <= 0:
            return ready
        if item in path:
            raise _Unavailable(item, reason='material_cycle')
        candidates = []
        failure = _Unavailable(item)
        for recipe in self.producers.get(item, []):
            for building in self.buildings:
                if str(building['building_id']) != str(recipe.get('building_id')):
                    continue
                intended, completed = building.get('intend_count'), building.get('completed_count')
                if building.get('auto_production_enabled') is False and not (
                        building.get('recipe_id_known') is True
                        and str(building.get('recipe_id')) == str(recipe.get('recipe_id'))
                        and type(building.get('recipe_state')) is int and building['recipe_state'] == 1
                        and building.get('is_producing') is True and building.get('quantity_mode') == 'batch'
                        and type(intended) is int and type(completed) is int and intended > completed >= 0):
                    failure = _Unavailable(item, recipe, building, 'automatic_production_disabled')
                    continue
                if building.get('level') not in recipe.get('available_levels', []):
                    failure = _Unavailable(item, recipe, building, 'recipe_locked')
                    continue
                timing = estimate_production_time(building, recipe, now=self.now, revision=self.revision)
                if timing['time_source'] == 'unknown':
                    failure = _Unavailable(item, recipe, building, timing['blocked_reason'])
                    continue
                candidates.append((recipe, building, timing))
        if not candidates:
            raise failure
        # Choose the feasible alternative with the earliest finish. Each candidate
        # gets its own ledger, so a failed BOM cannot consume another order's stock.
        best = None
        for recipe, building, timing in candidates:
            trial = deepcopy(self)
            try:
                finished = trial._produce(item, count, recipe, building, timing, path)
            except _Unavailable as exc:
                failure = exc
                continue
            if best is None or finished < best[0]:
                best = finished, trial
        if best is None:
            raise failure
        finished, trial = best
        self.__dict__.update(trial.__dict__)
        return max(ready, finished)

    def _produce(self, item, count, recipe, building, timing, path):
        output = next(row['count'] for row in recipe['products'] if str(row['item_id']) == item)
        batches = (count + output - 1) // output
        uid, worker = str(building['building_uid']), str(building['worker_uid'])
        used = self.used_batches[uid]
        if used + batches > timing['remaining_batches']:
            raise _Unavailable(item, recipe, building, 'queue_exhausted')
        input_ready = self.now
        if not building.get('materials_prepaid', False):
            for material, quantity in _vector(recipe['materials']).items():
                input_ready = max(input_ready, self.ensure(material, quantity * batches, path | {item}))
        start = max(input_ready, self.building_calendar.get(uid, self.now), self.worker_calendar.get(worker, self.now))
        speed, per_batch = building['work_speed'], recipe['work_amount'] * 10000
        credit = timing['aligned_progress'] if used == 0 else 0
        duration = max(0, (per_batch * batches - credit + speed - 1) // speed)
        finish = start + duration
        if timing['time_source'] == 'server':
            # A queue deadline cannot be extrapolated into individual batches.
            finish = max(start, timing['ready_at_server_seconds'])
            duration = finish - start
        if finish > self.deadline:
            raise _Unavailable(item, recipe, building, 'outside_horizon')
        self.used_batches[uid] += batches
        self.building_calendar[uid], self.worker_calendar[worker] = finish, finish
        row = dict(building_uid=uid, building_id=str(building['building_id']), recipe_id=str(recipe['recipe_id']),
                   item_id=item, count=output * batches, batches=batches, time_source=timing['time_source'],
                   duration_seconds=duration, starts_at_server_seconds=start,
                   ready_at_server_seconds=finish, blocked_reason=None, inputs_revision=self.revision)
        self.rows.append(row)
        self.actions.extend([dict(action='produce', **row),
                             dict(action='claim', building_uid=uid, ready_at_server_seconds=finish,
                                  inputs_revision=self.revision)])
        for product in recipe['products']:
            self.lots.setdefault(str(product['item_id']), []).append([product['count'] * batches, finish])
        missing, available = self._take(item, count)
        if missing:
            raise _Unavailable(item)
        return available


def _unknown_row(failure, revision):
    building, recipe = failure.building or {}, failure.recipe or {}
    return dict(building_uid=building.get('building_uid'), building_id=building.get('building_id'),
                recipe_id=recipe.get('recipe_id'), item_id=failure.item, count=0, batches=0,
                time_source='unknown', duration_seconds=None, starts_at_server_seconds=None,
                ready_at_server_seconds=None, blocked_reason=failure.reason, inputs_revision=revision)


def plan_orders(snapshot, catalog, settings, *, now):
    """Plan current orders without I/O, mutation, purchases, refreshes or RPCs."""
    revision = snapshot['revision']
    horizon = min(7 * 86400, max(3600, int(settings.get('horizon_seconds', 86400))))
    limit = max(0, min(100, int(settings.get('max_orders', 100))))
    budget = max(1, min(1000000, int(settings.get('search_budget', 100000))))
    warnings = []
    unlocked = [row for row in snapshot.get('orders', []) if row.get('is_unlock') is True]
    typed = [row for row in unlocked if row.get('order_type') is not None]
    if len(typed) != len(unlocked):
        warnings.append('order_type_unknown')
    orders = [row for row in typed if any(_vector(row.get('materials', [])).values())]
    if len(typed) != len(orders):
        warnings.append('empty_order_materials')
    if len(orders) > 100:
        # Keep search recursion bounded; this cannot be called an exact global result.
        orders = orders[:100]
        warnings.append('order_scope_truncated')
    stock = {str(item): max(0, count - settings.get('reserves', {}).get(str(item), 0))
             for item, count in snapshot.get('inventory', {}).items()}
    complete = snapshot.get('inventory_complete', True)
    if not complete:
        warnings.append('inventory_unknown')
        selected, score, optimal = [], (0, 0), False
    else:
        selected, score, optimal = _select_inventory(orders, stock, limit, budget)
    reason = None if optimal else ('inventory_unknown' if not complete else 'search_budget_exhausted')
    if 'order_scope_truncated' in warnings:
        optimal, reason = False, 'order_scope_truncated'
    selected_ids = [orders[index]['slot_id'] for index in selected]
    selection = dict(slot_ids=selected_ids, count=score[0], canned_count=score[1], optimal=optimal, reason=reason)
    schedule = _Schedule(snapshot, catalog, stock, now, now + horizon)
    for index in selected:
        for item, count in _vector(orders[index]['materials']).items():
            schedule._take(item, count)
    remaining = [row for index, row in enumerate(orders) if index not in selected]
    scheduled, predicted_cans, unknown = [], score[1], []
    if complete and settings.get('allow_production', True):
        # Evaluate a few deterministic orderings against the inventory-first baseline.
        # This is a heuristic over a fixed order set, with no global optimality claim.
        variants = [remaining, sorted(remaining, key=lambda row: sum(_vector(row['materials']).values())),
                    sorted(remaining, key=_cans, reverse=True)]
        best = None
        for variant in variants:
            trial = deepcopy(schedule)
            accepted, cans, blocked = [], score[1], []
            for order in variant:
                if len(selected) + len(accepted) >= limit:
                    break
                candidate = deepcopy(trial)
                try:
                    for item, count in _vector(order['materials']).items():
                        candidate.ensure(item, count)
                except _Unavailable as failure:
                    blocked.append(_unknown_row(failure, revision))
                    continue
                trial, cans = candidate, cans + _cans(order)
                accepted.append(order['slot_id'])
            candidate_score = (len(accepted), cans)
            if best is None or candidate_score > best[0]:
                best = candidate_score, trial, accepted, cans, blocked
        if best:
            _, schedule, scheduled, predicted_cans, unknown = best
    required = defaultdict(int)
    for order in remaining:
        for item, count in _vector(order['materials']).items():
            required[item] += count
    available = stock.copy()
    for index in selected:
        for item, count in _vector(orders[index]['materials']).items():
            available[item] = available.get(item, 0) - count
    materials = [dict(item_id=item, name=catalog.get('names', {}).get(item), required=count,
                      available=available.get(item, 0) if complete else None,
                      shortage=max(0, count - available.get(item, 0)) if complete else None)
                 for item, count in sorted(required.items())]
    base = None
    if complete:
        try:
            base = expand_material_requirements(dict(required), available, catalog)
        except ValueError:
            warnings.append('material_cycle')
    if settings.get('allow_refresh'):
        warnings.append('refresh_not_modeled')
    if schedule.rows:
        warnings.extend(['production_is_estimate', 'claim_requires_server_confirmation', 'energy_rate_unverified'])
        if any(row.get('materials') for row in catalog.get('recipes', [])):
            warnings.append('production_inputs_reserved_unless_prepaid')
    return dict(revision=revision, inventory_selection=selection, materials=materials,
                base_requirements=base,
                production=schedule.rows + unknown, production_actions=schedule.actions,
                scheduled_slot_ids=selected_ids + scheduled, predicted_count=score[0] + len(scheduled),
                predicted_canned_count=predicted_cans, warnings=warnings,
                horizon_seconds=horizon, search_budget=budget, production_optimal=False)
