"""Project exact home level and species work configuration, offline only."""
import argparse
import hashlib
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_SOURCE = PROJECT.parent / 'artifacts/rumi/home-analysis/home-config.public.json'
DEFAULT_PETS_SOURCE = PROJECT.parent / 'artifacts/rumi/home-analysis/pet-home-work.public.json'
DEFAULT_OUTPUT = PROJECT / 'data/home/management.json'
CONFIG_SHA256 = '7ccc1a15a461f1233eb9626e64d3133554056b588be0c42ed8eead0a8078539d'
PETS_SHA256 = '2754c8adbef7e7c2b093e2c1d9df51d0c91d9d513731ff6072d0d3b724ea6394'


def positive(value):
    if type(value) is not int or not 0 < value < 1 << 31:
        raise ValueError('Invalid configuration number')
    return value


def index(rows, key):
    result = {}
    for row in rows:
        number = positive(row[key])
        if number in result:
            raise ValueError('Duplicate configuration identity')
        result[number] = row
    return result


def project_management(config, pets, *, config_sha256, pets_sha256):
    if config['source']['hot_version'] != 64 or pets['hot_version'] != 64:
        raise ValueError('Unverified public source version')
    tables = config['tables']
    recipes = index(tables['Recipe'], 'RecipeID')
    buildings, recipe_ids = [], set()
    for building_id, building in sorted(index(tables['Architecture'], 'ArchID').items()):
        levels = index(building['LevelData'], 'Level')
        if not any(level['FactoryRecipe'] for level in levels.values()):
            continue
        if building['LumiLimit']:
            raise ValueError('Uninterpreted building work restriction')
        projected = []
        for level_number, level in sorted(levels.items()):
            ids = [positive(value) for value in level['FactoryRecipe']]
            if len(ids) != len(set(ids)) or any(value not in recipes for value in ids):
                raise ValueError('Ambiguous or missing recipe')
            recipe_ids.update(ids)
            projected.append({'level': level_number, 'capacity': positive(level['Volume']),
                              'recipe_ids': [str(value) for value in ids]})
        buildings.append({'building_id': str(building_id), 'architecture_type': positive(building['ArchType']),
                          'work_type_id': positive(building['FactoryWorkType']), 'levels': projected})
    recipe_rows = []
    for recipe_id in sorted(recipe_ids):
        recipe = recipes[recipe_id]
        if recipe['LumiLimit']:
            raise ValueError('Uninterpreted recipe work restriction')
        recipe_rows.append({'recipe_id': str(recipe_id), 'volume': positive(recipe['Volume']),
                            'work_amount': positive(recipe['WorkAmount'])})
    pet_rows, seen = [], set()
    for pet in pets['pets']:
        pet_id = pet['config_id']
        if type(pet_id) is not str or not pet_id.isascii() or not pet_id.isdigit():
            raise ValueError('Invalid species identity')
        positive(int(pet_id))
        if str(int(pet_id)) != pet_id or pet_id in seen:
            raise ValueError('Duplicate or noncanonical species identity')
        seen.add(pet_id)
        abilities = [{'work_type_id': positive(row['Type']), 'level': positive(row['Level'])}
                     for row in pet['work_ability']]
        if len({row['work_type_id'] for row in abilities}) != len(abilities):
            raise ValueError('Duplicate work ability')
        pet_rows.append({'pet_id': pet_id, 'work_abilities': abilities})
    return {'schema_version': 1, 'source': {'client_version': '1.5.3', 'hot_version': 64,
            'config_report_sha256': config_sha256, 'pet_report_sha256': pets_sha256},
            'buildings': buildings, 'recipes': recipe_rows,
            'pets': sorted(pet_rows, key=lambda row: int(row['pet_id']))}


def build_document(config_path, pets_path):
    config_raw, pets_raw = Path(config_path).read_bytes(), Path(pets_path).read_bytes()
    if (hashlib.sha256(config_raw).hexdigest() != CONFIG_SHA256
            or hashlib.sha256(pets_raw).hexdigest() != PETS_SHA256):
        raise ValueError('Fixed public source identity changed')
    return project_management(json.loads(config_raw), json.loads(pets_raw),
                              config_sha256=CONFIG_SHA256, pets_sha256=PETS_SHA256)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-source', type=Path, default=DEFAULT_CONFIG_SOURCE)
    parser.add_argument('--pets-source', type=Path, default=DEFAULT_PETS_SOURCE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    document = build_document(args.config_source, args.pets_source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({'buildings': len(document['buildings']), 'recipes': len(document['recipes']),
                      'species': len(document['pets']), 'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
