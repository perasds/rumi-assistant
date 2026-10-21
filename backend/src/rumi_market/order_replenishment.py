"""Choose one conservative refill step; never reassign workers or interrupt work.

New-recipe duration stays unknown until the server reports its actual speed.
This heuristic is deliberately distinct from current-stock exact selection.
"""
from collections import defaultdict


def _count(value, minimum=0):
    return type(value) is int and value >= minimum


def _pending_products(snapshot, catalog):
    """Known stores and finite unfinished queues, never spendable inventory."""
    buildings, conflicting = {}, set()
    for row in snapshot.get('buildings', []):
        uid = row.get('building_uid')
        if uid is None:
            continue
        if uid in buildings and buildings[uid] != row:
            conflicting.add(uid)
        buildings.setdefault(uid, row)
    supply = defaultdict(int)
    for uid, building in buildings.items():
        if uid in conflicting or building.get('editable') is not True:
            continue
        stored = building.get('store_items')
        if (type(stored) is list and all(type(row) is dict and type(row.get('item_id')) is str
                and _count(row.get('count')) for row in stored)):
            for row in stored:
                supply[row['item_id']] += row['count']
        intended, completed = building.get('intend_count'), building.get('completed_count')
        if (building.get('recipe_id_known') is not True
                or type(building.get('recipe_state')) is not int or building['recipe_state'] != 1
                or building.get('is_producing') is not True or building.get('worker_valid') is not True
                or not _count(building.get('work_energy'), 1)
                or building.get('quantity_mode') != 'batch'
                or not _count(intended, 1) or not _count(completed) or completed > intended):
            continue
        recipes = [recipe for recipe in catalog['recipes']
                   if recipe['building_id'] == building.get('building_id')
                   and recipe['recipe_id'] == building.get('recipe_id')
                   and building.get('level') in recipe['available_levels']]
        if len(recipes) != 1:
            continue
        products = recipes[0]['products']
        if not products or any(not _count(row.get('count'), 1) for row in products):
            continue
        for product in products:
            supply[product['item_id']] += (intended - completed) * product['count']
    return supply


def material_dependency_ids(catalog, items):
    result, pending = set(items), list(items)
    while pending:
        target = pending.pop()
        for recipe in catalog['recipes']:
            if any(row['item_id'] == target for row in recipe['products']):
                for row in recipe['materials']:
                    if row['item_id'] not in result:
                        result.add(row['item_id'])
                        pending.append(row['item_id'])
    return result


def choose_replenishment(snapshot, catalog, reserves, *, batch_limit=1000):
    stock = {item: max(0, count - reserves.get(item, 0)) for item, count in snapshot['inventory'].items()}
    needs = defaultdict(int)
    for order in snapshot.get('orders', []):
        if order.get('is_unlock') and order.get('order_type') is not None:
            for row in order['materials']:
                needs[row['item_id']] += row['count']
    pending = _pending_products(snapshot, catalog)
    # Future receipts must first restore a reserve that is currently underfilled.
    for item in pending:
        pending[item] = max(0, pending[item] - max(0, reserves.get(item, 0) - snapshot['inventory'].get(item, 0)))
    gaps = {}
    for item, required in needs.items():
        held = min(required, stock.get(item, 0))
        stock[item] = stock.get(item, 0) - held
        covered = min(required - held, pending.get(item, 0))
        pending[item] -= covered
        gaps[item] = required - held - covered
    producers = defaultdict(list)
    for recipe in catalog['recipes']:
        for row in recipe['products']:
            if row['count'] > 0:
                producers[row['item_id']].append(recipe)

    def propose(item, missing, path, available, incoming):
        if item in path or len(path) > 20:
            return None, False
        covered = min(missing, incoming.get(item, 0))
        incoming[item] -= covered
        missing -= covered
        if missing <= 0:
            return None, covered > 0
        for recipe in producers[item]:
            output = next(row['count'] for row in recipe['products'] if row['item_id'] == item)
            requested = (missing + output - 1) // output
            for building in snapshot.get('buildings', []):
                if (building['building_id'] != recipe['building_id'] or building.get('level') not in recipe['available_levels']
                        or building.get('auto_production_enabled') is False
                        or not building.get('editable') or not building.get('worker_valid')
                        or type(building.get('work_energy')) is not int or building['work_energy'] <= 0
                        or building.get('recipe_id') not in (None, '0')):
                    continue
                capacity, volume = building.get('capacity_remaining'), recipe.get('volume')
                if type(capacity) is not int or type(volume) is not int or volume <= 0 or capacity < volume:
                    continue
                batches = min(requested, capacity // volume, batch_limit)
                trial_stock, trial_incoming = available.copy(), incoming.copy()
                affordable, waiting = batches, False
                inputs = defaultdict(int)
                for source in recipe['materials']:
                    inputs[source['item_id']] += source['count']
                for source, count in inputs.items():
                    held = trial_stock.get(source, 0)
                    if held < count:
                        upstream, pending_input = propose(source, count * batches - held,
                            path | {item}, trial_stock, trial_incoming)
                        if upstream:
                            return upstream, True
                        waiting = waiting or pending_input
                    affordable = min(affordable, held // count)
                    trial_stock[source] = max(0, held - count * batches)
                if affordable <= 0:
                    if waiting:
                        # Reserve this route once; another order cannot reuse its
                        # on-hand ingredients or the same pending upstream batch.
                        available.clear()
                        available.update(trial_stock)
                        incoming.clear()
                        incoming.update(trial_incoming)
                        return None, True
                    continue
                return {'operation': {'action': 'produce', 'building_uid': building['building_uid'],
                        'building_id': building['building_id'], 'recipe_id': recipe['recipe_id'],
                        'recipe_times': 0 if building.get('quantity_mode', 'continuous' if not inputs else 'batch') == 'continuous' else affordable},
                        'costs': [{'item_id': source, 'count': count * affordable} for source, count in inputs.items()]}, True
        return None, covered > 0

    for item, missing in sorted(gaps.items(), key=lambda row: (row[1], row[0])):
        if missing > 0:
            proposal, _ = propose(item, missing, set(), stock, pending)
            if proposal:
                return proposal
    return None
