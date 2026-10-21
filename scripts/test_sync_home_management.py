import copy
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).with_name('sync_home_management.py')
spec = importlib.util.spec_from_file_location('sync_home_management', MODULE)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def sources():
    config = {'source': {'hot_version': 64}, 'tables': {
        'Architecture': [{'ArchID': 101, 'ArchType': 3, 'FactoryWorkType': 1, 'LumiLimit': [], 'LevelData': [
            {'Level': 1, 'FactoryRecipe': [1], 'Volume': 20},
            {'Level': 2, 'FactoryRecipe': [2], 'Volume': 40}]}],
        'Recipe': [{'RecipeID': 1, 'Volume': 1, 'WorkAmount': 30, 'LumiLimit': []},
                   {'RecipeID': 2, 'Volume': 2, 'WorkAmount': 50, 'LumiLimit': []}],
    }}
    pets = {'hot_version': 64, 'pets': [{'config_id': '90001', 'work_ability': [{'Type': 1, 'Level': 2}]}]}
    return config, pets


def test_preserves_exact_per_level_recipes_without_assuming_monotonic_unlocks():
    config, pets = sources()
    result = sync.project_management(config, pets, config_sha256='a' * 64, pets_sha256='b' * 64)
    assert result['buildings'] == [{'building_id': '101', 'architecture_type': 3, 'work_type_id': 1, 'levels': [
        {'level': 1, 'capacity': 20, 'recipe_ids': ['1']},
        {'level': 2, 'capacity': 40, 'recipe_ids': ['2']}]}]
    assert result['pets'] == [{'pet_id': '90001', 'work_abilities': [{'work_type_id': 1, 'level': 2}]}]
    assert result['recipes'] == [{'recipe_id': '1', 'volume': 1, 'work_amount': 30},
                                 {'recipe_id': '2', 'volume': 2, 'work_amount': 50}]


@pytest.mark.parametrize('mode', ['duplicate_building', 'duplicate_level', 'missing_recipe',
                                  'duplicate_pet', 'building_limit', 'recipe_limit', 'bad_version'])
def test_rejects_ambiguous_or_uninterpreted_configuration(mode):
    config, pets = sources()
    building = config['tables']['Architecture'][0]
    if mode == 'duplicate_building': config['tables']['Architecture'].append(copy.deepcopy(building))
    elif mode == 'duplicate_level': building['LevelData'].append(copy.deepcopy(building['LevelData'][0]))
    elif mode == 'missing_recipe': building['LevelData'][0]['FactoryRecipe'] = [999]
    elif mode == 'duplicate_pet': pets['pets'].append(copy.deepcopy(pets['pets'][0]))
    elif mode == 'building_limit': building['LumiLimit'] = [1]
    elif mode == 'recipe_limit': config['tables']['Recipe'][0]['LumiLimit'] = [1]
    elif mode == 'bad_version': pets['hot_version'] = 63
    with pytest.raises(ValueError):
        sync.project_management(config, pets, config_sha256='a' * 64, pets_sha256='b' * 64)


def test_fixed_public_sources_produce_only_work_configuration():
    result = sync.build_document(sync.DEFAULT_CONFIG_SOURCE, sync.DEFAULT_PETS_SOURCE)
    assert len(result['buildings']) == 21
    assert len(result['recipes']) == 37
    assert len(result['pets']) == 246
    assert result['source']['hot_version'] == 64
    assert all(set(row) == {'pet_id', 'work_abilities'} for row in result['pets'])
    assert all(set(row) == {'recipe_id', 'volume', 'work_amount'} for row in result['recipes'])
