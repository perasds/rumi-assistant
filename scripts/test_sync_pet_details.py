"""Synthetic public-table projection tests; no account or network access."""

import importlib
import hashlib
import json

import pytest


def impl():
    return importlib.import_module('sync_pet_details')


def test_nested_public_table_is_complete_unique_and_rejects_bad_suffix():
    schemas = {'Root': [['Id', 'int'], ['Refs', ['list', 'int']], ['Values', ['map', 'int', ['list', 'int']]]]}
    raw = bytes([1, 7, 2, 3, 4, 1, 9, 2, 5, 6])
    assert impl().decode_table(raw, 'Root', schemas) == [{'Id': 7, 'Refs': [3, 4], 'Values': {9: [5, 6]}}]
    for bad in (raw[:-1], raw + b'\0', bytes([2]) + raw[1:] + raw[1:]):
        with pytest.raises(ValueError):
            impl().decode_table(bad, 'Root', schemas)


def test_public_enum_key_table_preserves_its_declared_key():
    schemas = {'Element': [['LumiType', 'int'], ['Name', 'string']]}
    assert impl().decode_table(bytes([1, 2, 3]) + b'key', 'Element', schemas, key_field='LumiType') == [{'LumiType': 2, 'Name': 'key'}]


@pytest.mark.parametrize(('values', 'expected'), [([], None), ([40], '4点能量'), ([0], '0点能量'), ([1, 40], '4点能量'), ([1, 40, 20], '4点能量'), ([2, 2500, 10], '最大生命的25%'), ([2, 25], '最大生命的0.25%')])
def test_verified_cost_shape_uses_type_selector_and_tenths(values, expected):
    assert impl().cost_text(values) == expected


@pytest.mark.parametrize('bad', [[3, 10], [True, 10], [1, -1], [1, 10, 20, 30]])
def test_unknown_cost_shape_does_not_invent_display(bad):
    assert impl().cost_text(bad) is None


def test_species_skill_pool_is_stable_unique_and_individual_values_are_not_used():
    row = {'SkillPool1': [7, 9], 'SkillPool2': [9, 11], 'SkillPool3': [7], 'skill2': 99}
    assert impl().skill_pool(row) == ['7', '9', '11']


def test_atomic_json_export_is_deterministic_and_leaves_no_temporary_files(tmp_path):
    path = tmp_path / 'details.json'
    doc = {'schema_version': 1, 'pets': {'7': {'name': '合成'}}}
    impl().save_json(path, doc)
    first = path.read_bytes()
    impl().save_json(path, doc)
    assert first == path.read_bytes() and json.loads(first) == doc
    assert [p.name for p in tmp_path.iterdir()] == ['details.json']


def test_projection_uses_species_ranges_and_native_normal_attack_scaling():
    pet = {'Id': 7, 'PokedexId': 3, 'Type1': 1, 'Type2': 0, 'NormalAttack': 10, 'ActiveSkill': 20,
           'SkillPool1': [30, 30], 'SkillPool2': [20], 'SkillPool3': [], 'BattlePassive': 40, 'HomePassive': 0,
           **{prefix + field + 'State': value for prefix, value in [('Min', 10), ('Max', 20)] for field in ('Atk', 'Def', 'Hp', 'Work')}}
    skills = [{'Id': i, 'Name': 'name', 'Des': 'description', 'DesParam': [], 'LumiTpye': 1,
               'SkillCost': [1, 40], 'SkillPowerList': [5, 8], 'AddEnergy': 30, 'AttackInterval': 1500, 'Icon': 'icon'} for i in (10, 20, 30)]
    battle = [{'Id': 40, 'Name': 'name', 'Des': 'description', 'DesParam': []}]
    text = lambda key, params, texts: texts.get(key)
    args = ([pet], skills, battle, [], {'name': '合成名称', 'description': '合成描述'}, {1: '火'}, text)
    doc = impl().project_details(*args)
    actual = doc['pets']['7']
    assert actual['normal_attack'] == {'element': {'id': 1, 'name': '火'}, 'power': 5, 'energy_gain': 3, 'interval_seconds': 1.5}
    assert actual['replaceable_skill_ids'] == ['30', '20']
    assert actual['fixed_skill_id'] == '20' and actual['trait_keys'] == ['battle:40']
    assert actual['stats'][0] == {'key': 'atk', 'label': '攻击资质', 'min': 10, 'max': 20}
    assert doc['skills']['10']['power'] == 13
    assert doc == impl().project_details(*args)


def test_exact_sprite_membership_requires_all_public_bindings():
    sprite = {'name': 'Skill_7', 'path_id': 10, 'atlas_path_id': 20, 'render_key': [1, 2, 3, 4, 5]}
    atlas = {'path_id': 20, 'packed_names': ['Skill_7'], 'packed_sprite_path_ids': [10], 'render_keys': [[1, 2, 3, 4, 5]]}
    assert impl().exact_sprite_membership(sprite, atlas)
    for field, value in [('name', 'Skill_8'), ('path_id', 11), ('atlas_path_id', 21), ('render_key', [9])]:
        assert not impl().exact_sprite_membership({**sprite, field: value}, atlas)


def test_skill_atlas_bytes_must_match_verified_size_and_digest():
    raw = b'synthetic public atlas'
    digest = hashlib.sha256(raw).hexdigest()
    assert impl().verify_skill_atlas(raw, len(raw), digest) == digest
    for bad in (raw[:-1], b'x' * len(raw)):
        with pytest.raises(ValueError):
            impl().verify_skill_atlas(bad, len(raw), digest)
