"""Public configuration projection tests; no network or account sources."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


def impl():
    path = Path(__file__).with_name('sync_home_production.py')
    spec = importlib.util.spec_from_file_location('sync_home_production', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source():
    return {'source': {'hot_version': 64, 'game_sha256': 'a' * 64, 'catalog_sha256': 'b' * 64},
            'table_facts': {name: {'sha256': 'c' * 64} for name in
                            ('Architecture', 'Recipe', 'Item', 'MultilingualCN', 'HomeIsland', 'LumiWorkingLevelEfficiency')},
            'tables': {
                'Architecture': [
                    {'ArchID': 7, 'Name_zh': '合成农田', 'IslandID': 1, 'FactoryWorkType': 3,
                     'IsBuild': True, 'LevelData': [
                         {'Level': 2, 'FactoryRecipe': [2, 1]},
                         {'Level': 1, 'FactoryRecipe': [1]},
                         {'Level': 3, 'FactoryRecipe': [1, 2]}]},
                    {'ArchID': 8, 'Name': 'missing-name-key', 'IslandID': 2, 'FactoryWorkType': 0,
                     'IsBuild': False, 'LevelData': [{'Level': 1, 'FactoryRecipe': [3]}]},
                    {'ArchID': 9, 'Name_zh': '非生产建筑', 'IslandID': 1, 'FactoryWorkType': 0,
                     'LevelData': [{'Level': 1, 'FactoryRecipe': []}]},
                ],
                'Recipe': [
                    {'RecipeID': 1, 'Cost': [{'Type': 2, 'Id': 20, 'Count': 5}],
                     'Reward': [{'Type': 2, 'Id': 30, 'Count': 1}]},
                    {'RecipeID': 2, 'Cost': [], 'Reward': [{'Type': 2, 'Id': 31, 'Count': 2}]},
                    {'RecipeID': 3, 'Cost': [], 'Reward': [{'Type': 2, 'Id': 30, 'Count': 1}]},
                    {'RecipeID': 99, 'Cost': [], 'Reward': []},
                ],
                'referenced_items': [{'Key1': item_id, 'Name_zh': name} for item_id, name in
                                     [(20, '材料'), (30, '产物甲'), (31, '产物乙')]],
                'HomeIsland': [{'IslandId': 1, 'Name_zh': '雨林'}, {'IslandId': 2, 'Name_zh': '海岛'}],
                'LumiWorkingLevelEfficiency': [{'WorkingEnum': 3, 'Name_zh': '种植'}],
            }}


def test_projection_joins_items_preserves_all_buildings_and_merges_by_minimum_level():
    document = impl().project_production(source(), source_sha256='d' * 64)
    assert document['schema_version'] == 1 and document['source']['hot_version'] == 64
    buildings = document['buildings']
    assert [row['building_id'] for row in buildings] == ['7', '8']
    assert buildings[0]['name'] == '合成农田' and buildings[0]['work_type'] == '种植'
    assert buildings[1]['name'] == '建筑 #8' and buildings[1]['island_name'] == '海岛'
    assert buildings[1]['work_type'] is None
    assert [(row['recipe_id'], row['min_building_level']) for row in buildings[0]['recipes']] == [('1', 1), ('2', 2)]
    assert buildings[0]['recipes'][0]['materials'] == [
        {'item_id': '20', 'name': '材料', 'count': 5, 'image_url': None}]
    assert buildings[0]['recipes'][1]['products'] == [
        {'item_id': '31', 'name': '产物乙', 'count': 2, 'image_url': None}]
    assert document['source']['report_sha256'] == 'd' * 64
    assert 'missing-name-key' not in json.dumps(document)


@pytest.mark.parametrize('mutation', ['recipe', 'item', 'name', 'island', 'work', 'duplicate', 'kind', 'count'])
def test_incomplete_or_ambiguous_join_fails_instead_of_inventing_data(mutation):
    document = source()
    tables = document['tables']
    if mutation == 'recipe': tables['Recipe'] = tables['Recipe'][1:]
    elif mutation == 'item': tables['referenced_items'] = tables['referenced_items'][1:]
    elif mutation == 'name': tables['referenced_items'][0].pop('Name_zh')
    elif mutation == 'island': tables['HomeIsland'] = tables['HomeIsland'][1:]
    elif mutation == 'work': tables['LumiWorkingLevelEfficiency'] = []
    elif mutation == 'duplicate': tables['Architecture'].append(copy.deepcopy(tables['Architecture'][0]))
    elif mutation == 'kind': tables['Recipe'][0]['Cost'][0]['Type'] = 1
    elif mutation == 'count': tables['Recipe'][0]['Cost'][0]['Count'] = True
    with pytest.raises(ValueError):
        impl().project_production(document, source_sha256='d' * 64)


def test_real_public_source_has_exact_scope_and_source_hashes():
    path = Path(__file__).resolve().parents[2] / 'artifacts/rumi/home-analysis/home-config.public.json'
    if not path.is_file():
        pytest.skip('Local verified public research source is not distributed')
    module = impl()
    document = module.build_document(path)
    buildings = document['buildings']
    assert len(buildings) == 21 and sum(len(row['recipes']) for row in buildings) == 37
    assert {row['island_id'] for row in buildings} == {1, 2}
    missing = next(row for row in buildings if row['building_id'] == '10206')
    assert missing['name'] == '建筑 #10206'
    farm = next(row for row in buildings if row['building_id'] == '10104')
    assert [(r['recipe_id'], r['min_building_level']) for r in farm['recipes']] == [('9', 1), ('26', 2), ('44', 3), ('27', 4)]
    assert {'10125', '10130'} <= {row['building_id'] for row in buildings}
    assert [row['name'] for row in buildings if row['building_id'] in ('10104', '10125')] == ['农田', '农田']
    raw = json.loads(path.read_bytes())
    assert all(set(left['FactoryRecipe']) <= set(right['FactoryRecipe'])
               for row in raw['tables']['Architecture']
               for left, right in zip(row['LevelData'], row['LevelData'][1:]))
    assert document['source']['report_sha256'] == module.SOURCE_SHA256
    assert 'Administrator' not in json.dumps(document) and 'runtime' not in json.dumps(document)


def test_generator_rejects_changed_source_identity_before_parsing(tmp_path):
    path = tmp_path / 'changed.json'
    path.write_text(json.dumps(source()), encoding='utf-8')
    with pytest.raises(ValueError, match='identity'):
        impl().build_document(path)


def test_synthetic_projection_preserves_material_order_and_multiple_product_counts():
    data = source()
    recipe = data['tables']['Recipe'][0]
    recipe['Cost'] = [{'Type': 2, 'Id': 31, 'Count': 2}, {'Type': 2, 'Id': 20, 'Count': 5}]
    recipe['Reward'] += [{'Type': 2, 'Id': 31, 'Count': 3}]
    projected = impl().project_production(data, source_sha256='d' * 64)['buildings'][0]['recipes'][0]
    assert [(item['item_id'], item['count']) for item in projected['materials']] == [('31', 2), ('20', 5)]
    assert [(item['item_id'], item['count']) for item in projected['products']] == [('30', 1), ('31', 3)]
