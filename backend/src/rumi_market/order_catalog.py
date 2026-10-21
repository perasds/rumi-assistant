"""Small, session-independent catalog for order material planning."""
import json
from pathlib import Path


_DATA = Path(__file__).resolve().parents[3] / 'data/home'


def load_order_catalog(production_path=None, management_path=None):
    """Merge shipped recipes with exact level membership and ordinary work rules.

    Do not infer level availability from a minimum level: configured higher levels
    can stop referencing a recipe. No private analysis artifact is needed at runtime.
    """
    production = json.loads(Path(production_path or _DATA / 'production.json').read_text(encoding='utf-8'))
    management = json.loads(Path(management_path or _DATA / 'management.json').read_text(encoding='utf-8'))
    buildings = {row['building_id']: row for row in management['buildings']}
    rules = {row['recipe_id']: row for row in management['recipes']}
    names, recipes = {}, []
    seen = set()
    for building in production['buildings']:
        for recipe in building['recipes']:
            key = (building['building_id'], recipe['recipe_id'])
            if key in seen:
                raise ValueError('duplicate production recipe')
            seen.add(key)
            rule = rules[recipe['recipe_id']]
            levels = [row['level'] for row in buildings[building['building_id']]['levels']
                      if recipe['recipe_id'] in row['recipe_ids']]
            if not levels or type(rule['work_amount']) is not int or rule['work_amount'] < 1:
                raise ValueError('invalid production work rules')
            for item in recipe['materials'] + recipe['products']:
                if type(item['count']) is not int or item['count'] < 1:
                    raise ValueError('invalid production quantity')
                names[item['item_id']] = item.get('name')
            recipes.append(dict(building_id=building['building_id'], recipe_id=recipe['recipe_id'],
                                available_levels=levels, work_amount=rule['work_amount'], volume=rule['volume'],
                                materials=[{'item_id': row['item_id'], 'count': row['count']} for row in recipe['materials']],
                                products=[{'item_id': row['item_id'], 'count': row['count']} for row in recipe['products']]))
    return {'recipes': recipes, 'names': names,
            'building_names': {row['building_id']: row['name'] for row in production['buildings']}}
