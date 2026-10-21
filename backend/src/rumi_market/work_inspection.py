"""Read-only workplace inspection; proposals never move workers or replace work."""

from collections import defaultdict


_WORKER_MESSAGES = {
    'roster_unknown': ('unknown', '工作噜咪名单尚未同步完成'),
    'dispatch_unknown': ('unknown', '工作噜咪外派状态尚未同步完成'),
    'worker_state_unknown': ('unknown', '工作噜咪状态尚未同步完成'),
    'work_types_unknown': ('unknown', '工作噜咪能力尚未核实'),
    'worker_dispatched': ('blocked', '工作噜咪正在外派'),
    'work_type_mismatch': ('blocked', '工作噜咪工种不匹配'),
    'worker_conflict': ('blocked', '工作噜咪正被其他任务占用'),
    'worker_state_blocked': ('blocked', '工作噜咪当前状态不能上工'),
}


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _identifier(value, bits=31):
    return (type(value) is str and value.isascii() and value.isdecimal()
            and len(value) <= 20 and 0 < int(value) < 1 << bits and str(int(value)) == value)


def _candidate(building, catalog, inventory, reserves):
    candidates = []
    for recipe in catalog.get('recipes', []):
        recipe_id = recipe.get('recipe_id')
        if (recipe.get('building_id') != building['building_id'] or not _identifier(recipe_id)
                or recipe_id not in building['allowed_recipe_ids']
                or type(recipe.get('available_levels')) is not list
                or building['level'] not in recipe['available_levels']
                or not _integer(recipe.get('volume'), 1)
                or building['capacity_remaining'] < recipe['volume']):
            continue
        products, materials = recipe.get('products'), recipe.get('materials')
        if (type(products) is not list or len(products) != 1 or type(materials) is not list
                or not _identifier(products[0].get('item_id'))
                or not _integer(products[0].get('count'), 1)
                or (building['quantity_mode'] == 'continuous' and materials)):
            continue
        costs = defaultdict(int)
        for material in materials:
            if not _identifier(material.get('item_id')) or not _integer(material.get('count'), 1):
                break
            costs[material['item_id']] += material['count']
        else:
            if any(max(0, inventory.get(item, 0) - reserves.get(item, 0)) < count
                   for item, count in costs.items()):
                continue
            product = products[0]
            item_id = product['item_id']
            stock = inventory.get(item_id, 0)
            name = catalog.get('names', {}).get(item_id) or f'物品 {item_id}'
            times = 0 if building['quantity_mode'] == 'continuous' else 1
            candidate = {'recipe_id': recipe_id, 'item_id': item_id, 'name': name,
                         'stock': str(stock), 'output_count': str(product['count']),
                         'recipe_times': str(times)}
            proposal = {'operation': {'action': 'produce', 'building_uid': building['building_uid'],
                'building_id': building['building_id'], 'recipe_id': recipe_id, 'recipe_times': times},
                'costs': [{'item_id': item, 'count': count} for item, count in sorted(costs.items())]}
            candidates.append((stock, int(recipe_id), candidate, proposal))
    return min(candidates, key=lambda row: row[:2])[2:] if candidates else (None, None)


def inspect_workplaces(snapshot, catalog, reserves=None) -> list[dict]:
    """Inspect one snapshot; callers execute at most one proposal before resyncing."""
    reserves = {} if reserves is None else reserves
    inventory = snapshot.get('inventory')
    inventory_known = (snapshot.get('inventory_complete') is True and type(inventory) is dict
                       and all(_integer(count) for count in inventory.values()))
    reserves_known = type(reserves) is dict and all(_integer(count) for count in reserves.values())
    result = []
    for building in snapshot.get('buildings', []):
        row = {name: building.get(name) for name in
               ('building_uid', 'building_id', 'name', 'level', 'worker_uid', 'current_recipe_id')}
        if building.get('recipe_id_known') is True and building.get('recipe_id') in (None, '0'):
            row['current_recipe_id'] = '0'
        row.update(status='unknown', message='生产状态尚未同步完成', candidate=None, proposal=None)
        result.append(row)

        def status(value, message):
            row.update(status=value, message=message)

        if building.get('editable') is False:
            status('blocked', '当前建筑不可安排生产')
        elif (building.get('editable') is not True or not _identifier(building.get('building_uid'))
                or not _identifier(building.get('building_id')) or not _integer(building.get('level'), 1)):
            status('unknown', '建筑状态尚未同步完成')
        elif building.get('worker_missing') is True:
            status('missing_worker', '缺工作噜咪')
        elif (building.get('recipe_id_known') is not True
                or type(building.get('recipe_state')) is not int
                or building['recipe_state'] not in (0, 1)):
            continue
        elif building.get('recipe_id') not in (None, '0'):
            if building['recipe_state'] == 1:
                status('working', '正在生产，保留当前配方')
            else:
                status('blocked', '已有配方尚未清空，请在游戏中检查')
        elif building['recipe_state'] != 0:
            status('unknown', '配方与生产状态不一致')
        elif building.get('worker_valid') is not True:
            reason = building.get('worker_reason')
            if type(reason) is str and reason in _WORKER_MESSAGES:
                status(*_WORKER_MESSAGES[reason])
            elif type(reason) is str and reason and not reason.isascii():
                unknown = any(text in reason for text in ('尚未', '未知', '同步', '待确认'))
                status('unknown' if unknown else 'blocked', reason)
            else:
                status('unknown', '工作噜咪状态尚未同步完成')
        elif (not _identifier(building.get('worker_uid'), bits=64)
                or building['worker_uid'] == '2147483647'):
            status('unknown', '工作噜咪身份尚未确认')
        elif not inventory_known:
            status('unknown', '仓库库存尚未同步完成')
        elif not reserves_known:
            status('unknown', '保留库存尚未确认')
        elif (building.get('quantity_mode') not in ('continuous', 'batch')
                or type(building.get('allowed_recipe_ids')) is not list
                or not _integer(building.get('capacity_remaining'))
                or not _integer(building.get('work_energy'))):
            status('unknown', '配方、精力或容量尚未同步完成')
        elif building['work_energy'] == 0:
            status('blocked', '工作噜咪精力不足')
        elif building.get('auto_production_enabled') is False:
            status('blocked', '默认不安排自动生产')
        else:
            candidate, proposal = _candidate(building, catalog, inventory, reserves)
            if candidate is None:
                status('blocked', '没有满足配方、原料和容量条件的产物')
            else:
                status('idle', f'空闲，可生产{candidate["name"]}')
                row.update(candidate=candidate, proposal=proposal)
    return result
