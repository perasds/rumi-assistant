"""Pure bounded production for a feasible subset of the current orders.

Selection compares a few deterministic orderings, preferring order count and then
cans; it does not claim a global optimum or extrapolate new-recipe work speed.
Known pending output covers demand but never becomes spendable input.
"""
from collections import defaultdict

from .order_replenishment import _pending_products


def _count(value, minimum=0):
    return type(value) is int and value >= minimum


def _vector(rows):
    result = defaultdict(int)
    for row in rows:
        if type(row.get('item_id')) is not str or not _count(row.get('count')):
            raise ValueError('invalid material quantity')
        result[row['item_id']] += row['count']
    return {item: count for item, count in result.items() if count}


def _unique(rows, key):
    known, conflicting = {}, set()
    for row in rows:
        identity = key(row)
        if identity is None:
            continue
        if identity in known and known[identity] != row:
            conflicting.add(identity)
        known.setdefault(identity, row)
    return [row for identity, row in known.items() if identity not in conflicting]


def _idle(building):
    return (building.get('auto_production_enabled') is not False
        and building.get('editable') is True and not building.get('blocked_reason')
        and building.get('recipe_id_known') is True and building.get('recipe_id') in (None, '0')
        and type(building.get('recipe_state')) is int and building['recipe_state'] == 0
        and building.get('is_producing') is False and building.get('worker_valid') is True
        and str(building.get('worker_uid')) not in {'None', '', '0', '2147483647'}
        and _count(building.get('work_energy'), 1)
        and _count(building.get('capacity_remaining')))


def _fits(building, recipe):
    allowed = building.get('allowed_recipe_ids')
    return (building['building_id'] == recipe['building_id']
        and building.get('recipe_id') in (None, '0', recipe['recipe_id'])
        and building.get('level') in recipe['available_levels']
        and building['capacity_remaining'] >= recipe['volume']
        and (allowed is None or recipe['recipe_id'] in {str(item) for item in allowed})
        and (not recipe['materials'] or building.get('quantity_mode') == 'batch'))


def _owned_future(building, recipes):
    """An owned finite queue may repeat its same recipe after confirmed claims."""
    intended, completed = building.get('intend_count'), building.get('completed_count')
    return (building.get('queue_owned') is True
        and type(building.get('recipe_state')) is int and building['recipe_state'] == 1
        and building.get('is_producing') is True and building.get('quantity_mode') == 'batch'
        and _count(intended, 1) and _count(completed) and completed < intended
        and _idle(building | {'recipe_id': None, 'recipe_state': 0, 'is_producing': False})
        and any(recipe['recipe_id'] == building.get('recipe_id') and _fits(building, recipe)
                for recipe in recipes))


def _requirements(orders):
    result = defaultdict(int)
    for order in orders:
        for item, count in _vector(order['materials']).items():
            result[item] += count
    return result


def _hold(required, stock, incoming):
    gaps = {}
    for item, count in required.items():
        held = min(count, stock.get(item, 0))
        stock[item] = stock.get(item, 0) - held
        covered = min(count - held, incoming.get(item, 0))
        incoming[item] = incoming.get(item, 0) - covered
        gaps[item] = count - held - covered
    return gaps


def _feasible(orders, stock, incoming, producers, buildings):
    """Check the whole BOM transactionally; capacity may be reused after claims."""
    supply = stock.copy()
    for item, count in incoming.items():
        supply[item] = supply.get(item, 0) + count
    gaps = _hold(_requirements(orders), supply, {})

    def consume(item, count, path, available):
        held = min(count, available.get(item, 0))
        available[item] = available.get(item, 0) - held
        count -= held
        if count <= 0:
            return True
        if item in path or len(path) > 20:
            return False
        for recipe in producers.get(item, []):
            if not any(_fits(building, recipe) for building in buildings):
                continue
            output = _vector(recipe['products'])[item]
            batches = (count + output - 1) // output
            trial = available.copy()
            if not all(consume(source, quantity * batches, path | {item}, trial)
                       for source, quantity in _vector(recipe['materials']).items()):
                continue
            for product, quantity in _vector(recipe['products']).items():
                trial[product] = trial.get(product, 0) + quantity * batches
            trial[item] -= count
            available.clear()
            available.update(trial)
            return True
        return False

    return all(consume(item, count, set(), supply) for item, count in gaps.items())


def choose_bounded_production(snapshot, catalog, reserves, *, remaining_orders=100,
        queue_reserves=None, blocked_items=None, max_batches=10, buffer_batches=1):
    """Return one existing-shape produce proposal, with no I/O or input mutation.

    ``queue_reserves`` is additional unobserved consumption, in integer item units.
    ``blocked_items`` excludes any candidate recipe touching those products/inputs.
    Only verified idle buildings are candidates; occupied queues remain untouched.
    A verified ``queue_owned`` finite queue can supply its same recipe in future
    BOM feasibility only, allowing an independent current action to proceed.
    """
    if (not snapshot.get('inventory_complete', True) or not _count(remaining_orders, 1)
            or not _count(max_batches, 1)):
        return None
    blocked = set(blocked_items or ())
    recipes = []
    for row in catalog.get('recipes', []):
        try:
            materials, products = _vector(row['materials']), _vector(row['products'])
        except (KeyError, TypeError, ValueError):
            continue
        if not products or not _count(row.get('volume'), 1):
            continue
        recipes.append(row | {'materials': [{'item_id': item, 'count': count}
            for item, count in materials.items()], 'products': [{'item_id': item, 'count': count}
            for item, count in products.items()]})
    recipes = _unique(recipes, lambda row: (row['building_id'], row['recipe_id']))
    buildings = _unique(snapshot.get('buildings', []), lambda row: row.get('building_uid'))
    idle = [building for building in buildings if _idle(building)]
    owned = [building for building in buildings if _owned_future(building, recipes)]
    future = idle + owned
    producers, future_producers = defaultdict(list), defaultdict(list)
    for recipe in recipes:
        unblocked = not blocked.intersection(_vector(recipe['materials']) | _vector(recipe['products']))
        future_route = unblocked or any(_fits(building, recipe) for building in owned)
        for item in _vector(recipe['products']):
            if future_route:
                future_producers[item].append(recipe)
            if unblocked:
                producers[item].append(recipe)
    protected = dict(reserves)
    for item, count in (queue_reserves or {}).items():
        protected[item] = protected.get(item, 0) + count
    stock = {item: max(0, count - protected.get(item, 0))
             for item, count in snapshot['inventory'].items()}
    pending = _pending_products({'buildings': buildings}, {'recipes': recipes})
    for item in pending:
        pending[item] = max(0, pending[item] - max(0,
            protected.get(item, 0) - snapshot['inventory'].get(item, 0)))
    orders = []
    for index, row in enumerate(snapshot.get('orders', [])):
        try:
            valid = row.get('is_unlock') is True and row.get('order_type') is not None and _vector(row['materials'])
        except (KeyError, TypeError, ValueError):
            continue
        if valid:
            orders.append(row | {'_identity': ('slot', row['slot_id']) if row.get('slot_id') is not None else ('row', index)})
    orders = _unique(orders, lambda row: row['_identity'])[:100]

    def cans(order):
        return sum(row['count'] for row in order.get('rewards', [])
            if row.get('item_id') == '54001' and _count(row.get('count')))

    selected, best_score = [], (0, 0)
    variants = [orders, sorted(orders, key=lambda row: sum(_vector(row['materials']).values())),
                sorted(orders, key=cans, reverse=True)]
    for variant in variants:
        accepted = []
        for order in variant:
            if len(accepted) >= min(100, remaining_orders):
                break
            if _feasible(accepted + [order], stock, pending, future_producers, future):
                accepted.append(order)
        score = len(accepted), sum(cans(order) for order in accepted)
        if score > best_score:
            selected, best_score = accepted, score
    target_stock, target_pending = stock.copy(), pending.copy()
    gaps = _hold(_requirements(selected), stock, pending)

    def preserves_targets(recipe, batches):
        remaining, incoming = target_stock.copy(), target_pending.copy()
        for item, count in _vector(recipe['materials']).items():
            remaining[item] = remaining.get(item, 0) - count * batches
            if remaining[item] < 0:
                return False
        for item, count in _vector(recipe['products']).items():
            incoming[item] = incoming.get(item, 0) + count * batches
        return _feasible(selected, remaining, incoming, future_producers, future)

    def propose(item, missing, path, available, incoming):
        if missing <= 0 or item in path or len(path) > 20:
            return None, False
        covered = min(missing, incoming.get(item, 0))
        incoming[item] = incoming.get(item, 0) - covered
        missing -= covered
        if not missing:
            return None, bool(covered)
        for recipe in producers.get(item, []):
            output = _vector(recipe['products'])[item]
            for building in idle:
                if not _fits(building, recipe):
                    continue
                batches = min((missing + output - 1) // output,
                    building['capacity_remaining'] // recipe['volume'], max_batches)
                inputs = _vector(recipe['materials'])
                required_inputs = [{'item_id': source, 'count': count * batches}
                    for source, count in inputs.items()]
                if not _feasible([{'materials': required_inputs}], available, incoming, future_producers, future):
                    continue
                trial, waiting = available.copy(), False
                trial_incoming = incoming.copy()
                affordable = batches
                for source, count in inputs.items():
                    held = trial.get(source, 0)
                    if held < count:
                        upstream, pending_input = propose(source, count * batches - held,
                            path | {item}, trial, trial_incoming)
                        if upstream:
                            return upstream, True
                        waiting = waiting or pending_input
                    affordable = min(affordable, held // count)
                    trial[source] = max(0, held - count * batches)
                if affordable and preserves_targets(recipe, affordable):
                    continuous = not inputs and building.get('quantity_mode') == 'continuous'
                    return (recipe, building, 0 if continuous else affordable), True
                if waiting:
                    available.clear()
                    available.update(trial)
                    incoming.clear()
                    incoming.update(trial_incoming)
                    return None, True
        return None, bool(covered)

    for item, missing in sorted(gaps.items(), key=lambda row: (row[1], row[0])):
        proposal, _ = propose(item, missing, set(), stock, pending)
        if proposal is None:
            continue
        recipe, building, batches = proposal
        inputs = _vector(recipe['materials'])
        if batches and _count(buffer_batches, 1):
            spare = {item: max(0, count - protected.get(item, 0))
                for item, count in snapshot['inventory'].items()}
            _hold(_requirements(orders), spare, {})
            extra = min(1, max_batches - batches,
                building['capacity_remaining'] // recipe['volume'] - batches)
            for source, count in inputs.items():
                extra = min(extra, max(0, spare.get(source, 0) - count * batches) // count)
            if extra and preserves_targets(recipe, batches + extra):
                batches += extra
        return {'operation': {'action': 'produce', 'building_uid': building['building_uid'],
            'building_id': building['building_id'], 'recipe_id': recipe['recipe_id'],
            'recipe_times': batches}, 'costs': [{'item_id': item, 'count': count * batches}
            for item, count in inputs.items()]}
    return None
