"""Project fixed public home tables into a deployable catalog, offline only."""
import argparse
import hashlib
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT.parent / 'artifacts/rumi/home-analysis/home-config.public.json'
DEFAULT_OUTPUT = PROJECT / 'data/home/production.json'
SOURCE_SHA256 = '7ccc1a15a461f1233eb9626e64d3133554056b588be0c42ed8eead0a8078539d'
SOURCE_TABLES = ('Architecture', 'Recipe', 'Item', 'MultilingualCN', 'HomeIsland', 'LumiWorkingLevelEfficiency')


def _positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Invalid public configuration number')
    return value


def _name(value):
    if type(value) is not str or not value.strip():
        raise ValueError('Missing verified public name')
    return value


def _index(rows, key):
    result = {}
    for row in rows:
        value = _positive(row[key])
        if value in result:
            raise ValueError('Duplicate public configuration key')
        result[value] = row
    return result


def project_production(document, *, source_sha256):
    tables = document['tables']
    buildings = _index(tables['Architecture'], 'ArchID')
    recipes = _index(tables['Recipe'], 'RecipeID')
    items = _index(tables['referenced_items'], 'Key1')
    islands = _index(tables['HomeIsland'], 'IslandId')
    work_types = _index(tables['LumiWorkingLevelEfficiency'], 'WorkingEnum')

    def item_list(values):
        result = []
        for value in values:
            item_id = _positive(value['Id'])
            if type(value['Type']) is not int or value['Type'] != 2 or item_id not in items:
                raise ValueError('Unsupported or missing public item reference')
            result.append({'item_id': str(item_id), 'name': _name(items[item_id].get('Name_zh')),
                           'count': _positive(value['Count']), 'image_url': None})
        return result

    result = []
    for building_id, row in sorted(buildings.items()):
        levels = _index(row['LevelData'], 'Level')
        minimum_levels = {}
        for level, data in sorted(levels.items()):
            for value in data['FactoryRecipe']:
                recipe_id = _positive(value)
                minimum_levels.setdefault(recipe_id, level)
        if not minimum_levels:
            continue
        island_id = _positive(row['IslandID'])
        if island_id not in islands:
            raise ValueError('Missing public island reference')
        work_type = row['FactoryWorkType']
        if type(work_type) is not int or (work_type != 0 and work_type not in work_types):
            raise ValueError('Missing public work-type reference')
        joined_recipes = []
        for recipe_id, minimum_level in minimum_levels.items():
            if recipe_id not in recipes:
                raise ValueError('Missing public recipe reference')
            recipe = recipes[recipe_id]
            products = item_list(recipe['Reward'])
            if not products:
                raise ValueError('Missing public recipe product')
            joined_recipes.append({'recipe_id': str(recipe_id), 'min_building_level': minimum_level,
                                   'materials': item_list(recipe['Cost']), 'products': products})
        name = row.get('Name_zh')
        result.append({'building_id': str(building_id),
                       'name': name if type(name) is str and name.strip() else f'建筑 #{building_id}',
                       'island_id': island_id, 'island_name': _name(islands[island_id].get('Name_zh')),
                       'work_type': _name(work_types[work_type].get('Name_zh')) if work_type else None,
                       'recipes': joined_recipes})
    source = document['source']
    return {'schema_version': 1,
            'source': {'client_version': '1.5.3', 'hot_version': source['hot_version'],
                       'game_sha256': source['game_sha256'], 'catalog_sha256': source['catalog_sha256'],
                       'report_sha256': source_sha256,
                       'table_sha256': {name: document['table_facts'][name]['sha256'] for name in SOURCE_TABLES}},
            'buildings': result}


def build_document(path):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError('Public home source identity changed')
    return project_production(json.loads(raw), source_sha256=SOURCE_SHA256)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    document = build_document(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n',
                           encoding='utf-8', newline='\n')
    print(json.dumps({'buildings': len(document['buildings']),
                      'recipes': sum(len(row['recipes']) for row in document['buildings']),
                      'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
