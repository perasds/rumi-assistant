"""Synchronize hash-verified public pet details; decrypted inputs stay in RAM."""

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile


PROJECT = Path(__file__).resolve().parents[1]
RESEARCH = PROJECT.parent / 'artifacts/rumi/pet-catalog'
CATALOG_URL = 'https://l3-prod-patch-lumigo.bilibiligame.net/LumiGoAndroidCN/1.5.x/catalog_4a6aa844893923742f73da99bfe7706c.bin'
CATALOG_SHA = '5c7956681b33766babb63cd43fbed4f11668f47f98a7391e1a472263e18a6e81'
GAME_SHA = '770f11ce26df555e7212a456066e9ddd345b164c9b87f7622b179859fb8bd3b4'
SCHEMA_PATH = Path(__file__).with_name('pet_detail_schema.json')
SKILL_ATLAS_SHA = '69f1435652bc6d57cee6622c427573212283f0d5f8e5cbca9ab58edb486f47aa'


def _research():
    if str(RESEARCH) not in sys.path:
        sys.path.insert(0, str(RESEARCH))


def decode_table(data, name, schemas, *, key_field='Id'):
    _research()
    from luban_pet_decoder import Reader
    reader = Reader(data)

    def read(kind, depth=0):
        if depth > 32:
            raise ValueError('Public schema depth exceeded')
        if isinstance(kind, str):
            if kind in ('int', 'float', 'bool', 'string'):
                return getattr(reader, 'read_' + kind)()
            return record(kind, depth + 1)
        category, *args = kind
        if category == 'record':
            return record(args[0], depth + 1)
        count = reader.read_size()
        if category == 'list':
            return [read(args[0], depth + 1) for _ in range(count)]
        if category == 'map':
            result = {}
            for _ in range(count):
                key, value = read(args[0], depth + 1), read(args[1], depth + 1)
                if key in result:
                    raise ValueError('Duplicate public map key')
                result[key] = value
            return result
        raise ValueError('Unsupported public schema')

    def record(record_name, depth=0):
        return {field: read(kind, depth + 1) for field, kind in schemas[record_name]}

    rows, seen = [], set()
    for _ in range(reader.read_size()):
        row = record(name)
        key = row.get(key_field)
        if key is None or key in seen:
            raise ValueError('Invalid or duplicate public table key')
        seen.add(key)
        rows.append(row)
    if reader.remaining:
        raise ValueError('Public table has unread suffix')
    return rows


def cost_text(values):
    # Native GetSkillCostConfig: one value is energy; otherwise selector-1 is
    # the cost kind. GetConfigEnergyCost returns zero for non-energy kinds.
    if (not isinstance(values, list) or not 1 <= len(values) <= 3
            or any(type(value) is not int for value in values)
            or any(value < 0 for value in values)):
        return None
    if len(values) == 1:
        return format(values[0] / 10, 'g') + '点能量'
    if values[0] == 2:
        return '最大生命的' + format(values[1] / 100, 'g') + '%'
    if values[0] != 1:
        return None
    return format(values[1] / 10, 'g') + '点能量'


def skill_pool(row):
    values = [value for field in ('SkillPool1', 'SkillPool2', 'SkillPool3') for value in row.get(field, [])]
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError('Invalid public skill reference')
    return list(dict.fromkeys(str(value) for value in values))


def project_details(pets, skill_rows, battle_rows, home_rows, texts, elements, render_text):
    """Project static species configuration; no individual account state."""
    skill_by_id = {str(row['Id']): row for row in skill_rows}
    trait_by_kind = {'battle': {str(row['Id']): row for row in battle_rows},
                     'home': {str(row['Id']): row for row in home_rows}}
    result = {'schema_version': 1, 'source': {'client_version': '1.5.3', 'hot_version': 64},
              'pets': {}, 'skills': {}, 'traits': {}}

    def element(value):
        if value == 0:
            return None
        if type(value) is not int or value not in elements:
            raise ValueError('Unverified public element reference')
        return {'id': value, 'name': elements[value]}

    def skill_reference(value):
        key = str(value)
        if type(value) is not int or value <= 0 or key not in skill_by_id:
            raise ValueError('Missing public skill reference')
        return key

    references = set()
    for row in pets:
        key = str(row['Id'])
        if key in result['pets']:
            raise ValueError('Duplicate public pet key')
        normal_id, fixed_id = skill_reference(row['NormalAttack']), skill_reference(row['ActiveSkill'])
        pools = skill_pool(row)
        for pool_id in pools:
            skill_reference(int(pool_id))
        references.update([normal_id, fixed_id, *pools])
        normal = skill_by_id[normal_id]
        powers = normal['SkillPowerList']
        interval = normal['AttackInterval']
        missing = []
        attack = {'element': element(normal['LumiTpye']),
                  'power': powers[0] if powers else None,
                  'energy_gain': normal['AddEnergy'] / 10,
                  'interval_seconds': interval / 1000 if interval > 0 else None}
        if not powers:
            missing.append('normal_attack.power')
        if interval <= 0:
            missing.append('normal_attack.interval_seconds')
        stats = [{'key': label_key, 'label': label, 'min': row['Min' + field + 'State'], 'max': row['Max' + field + 'State']}
                 for label_key, label, field in [('atk', '攻击资质', 'Atk'), ('def', '防御资质', 'Def'),
                                                  ('hp', '生命资质', 'Hp'), ('work', '工作资质', 'Work')]]
        if any(stat['min'] > stat['max'] for stat in stats):
            raise ValueError('Invalid public aptitude range')
        trait_keys = []
        for kind, field in [('battle', 'BattlePassive'), ('home', 'HomePassive')]:
            trait_id = row[field]
            if trait_id == 0:
                continue
            trait_id = str(trait_id)
            if trait_id not in trait_by_kind[kind]:
                raise ValueError('Missing public trait reference')
            trait_key = kind + ':' + trait_id
            trait_keys.append(trait_key)
            trait = trait_by_kind[kind][trait_id]
            result['traits'][trait_key] = {'trait_id': trait_id, 'kind': kind,
                'name': render_text(trait['Name'], [], texts) or ('特性 ' + trait_id),
                'description': render_text(trait['Des'], trait['DesParam'], texts)}
        result['pets'][key] = {'pokedex_id': str(row['PokedexId']) if row['PokedexId'] > 0 else None,
            'elements': [element(value) for value in dict.fromkeys([row['Type1'], row['Type2']]) if value != 0],
            'stats': stats, 'normal_attack': attack, 'trait_keys': trait_keys,
            'fixed_skill_id': fixed_id, 'replaceable_skill_ids': pools, 'missing_fields': missing}
    for key in sorted(references, key=int):
        row = skill_by_id[key]
        result['skills'][key] = {'skill_id': key,
            'name': render_text(row['Name'], [], texts) or ('技能 ' + key),
            'description': render_text(row['Des'], row['DesParam'], texts),
            'element': element(row['LumiTpye']), 'cost_text': cost_text(row['SkillCost']),
            'power': sum(row['SkillPowerList']) if row['SkillPowerList'] else None, 'image_path': None}
    return result


def save_json(path, document):
    save_bytes(path, (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8'))


def save_bytes(path, encoded):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def exact_sprite_membership(sprite, atlas):
    return (sprite['name'] in atlas['packed_names']
            and sprite['path_id'] in atlas['packed_sprite_path_ids']
            and sprite['atlas_path_id'] == atlas['path_id']
            and sprite['render_key'] in atlas['render_keys'])


def verify_skill_atlas(data, expected_size, expected_sha256=SKILL_ATLAS_SHA):
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != expected_size or digest != expected_sha256:
        raise ValueError('Public skill atlas identity changed')
    return digest


def extract_skill_images(catalog, icon_names, output_root):
    """Crop only exact catalog-bound SpriteAtlas members; return audit hashes."""
    import httpx
    import UnityPy
    from static_bundle_options import read_options
    asset = catalog.location(4490172)
    if (asset['internal_id'] != 'IconSkill.spriteatlas'
            or asset['provider'] != 'UnityEngine.ResourceManagement.ResourceProviders.BundledAssetProvider'
            or asset['dependency_record_offsets'] != [575665]):
        raise ValueError('Public skill atlas binding changed')
    dependency = catalog.location(575665)
    options = read_options(catalog, 575665)
    if (dependency['provider'] != 'UnityEngine.ResourceManagement.ResourceProviders.AssetBundleProvider'
            or options['m_Hash'] != '8362d1e1a679af2515139df61ca12619'
            or options['m_BundleSize'] != 3733413):
        raise ValueError('Public skill atlas dependency changed')
    url = CATALOG_URL.rsplit('/', 2)[0] + '/' + dependency['internal_id'].removeprefix('http://CDN_PLACEHOLDER/')
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        response = client.get(url)
        response.raise_for_status()
        data = response.content
    bundle_sha = verify_skill_atlas(data, options['m_BundleSize'])
    environment = UnityPy.load(data)
    objects = {obj.path_id: obj for obj in environment.objects}
    bundle_names = [obj.read().m_Name for obj in environment.objects if obj.type.name == 'AssetBundle']
    if options['m_BundleName'] not in {name.removesuffix('.bundle') for name in bundle_names}:
        raise ValueError('Public skill atlas bundle name mismatch')

    def render_key(value):
        guid, local_id = value
        return [guid.data_0_, guid.data_1_, guid.data_2_, guid.data_3_, local_id]

    atlases = {}
    for obj in environment.objects:
        if obj.type.name == 'SpriteAtlas':
            atlas = obj.read()
            if atlas.m_Name == 'IconSkill':
                atlases[obj.path_id] = {'path_id': obj.path_id, 'packed_names': atlas.m_PackedSpriteNamesToIndex,
                    'packed_sprite_path_ids': [ptr.m_PathID for ptr in atlas.m_PackedSprites if ptr.m_FileID == 0],
                    'render_keys': [render_key(key) for key, _ in atlas.m_RenderDataMap]}
    if len(atlases) != 1:
        raise ValueError('Public skill atlas identity is ambiguous')
    wanted, sprites = set(icon_names.values()), {}
    for obj in environment.objects:
        if obj.type.name != 'Sprite':
            continue
        sprite = obj.read()
        if sprite.m_Name not in wanted:
            continue
        if sprite.m_Name in sprites:
            raise ValueError('Duplicate public skill sprite name')
        membership = {'name': sprite.m_Name, 'path_id': obj.path_id,
                      'atlas_path_id': sprite.m_SpriteAtlas.m_PathID,
                      'render_key': render_key(sprite.m_RenderDataKey)}
        atlas = atlases.get(membership['atlas_path_id'])
        if sprite.m_SpriteAtlas.m_FileID != 0 or atlas is None or not exact_sprite_membership(membership, atlas):
            raise ValueError('Public skill sprite membership mismatch')
        rgba = sprite.image.convert('RGBA')
        encoded = BytesIO()
        rgba.save(encoded, format='PNG')
        sprites[sprite.m_Name] = (encoded.getvalue(), {'sprite_name': sprite.m_Name, 'path_id': obj.path_id,
            'atlas_path_id': membership['atlas_path_id'], 'render_key': membership['render_key'],
            'width': rgba.width, 'height': rgba.height, 'rgba_sha256': hashlib.sha256(rgba.tobytes()).hexdigest()})
    images, missing = {}, []
    for skill_id, icon_name in sorted(icon_names.items(), key=lambda item: int(item[0])):
        if not skill_id.isascii() or not skill_id.isdecimal() or int(skill_id) <= 0:
            raise ValueError('Invalid public skill image identifier')
        if icon_name not in sprites:
            missing.append(skill_id)
            continue
        encoded, evidence = sprites[icon_name]
        relative_path = 'images/skills/' + skill_id + '.png'
        save_bytes(Path(output_root) / relative_path, encoded)
        images[skill_id] = {**evidence, 'image_path': relative_path, 'png_sha256': hashlib.sha256(encoded).hexdigest()}
    return images, {'url': url, 'sha256': bundle_sha, 'size': len(data), 'catalog_options': options,
                    'membership': 'exact_name_path_id_atlas_pointer_render_key', 'images': images, 'missing_skill_ids': missing}


def public_sources_ram():
    """Fetch exactly catalog-bound official public bundles, never game servers."""
    _research()
    from lgeb_public_bundle_probe import source_dependencies, workspace_root, load_fixed_materials_ram, decrypt_bundle_ram
    source_dependencies(workspace_root())
    import httpx
    import UnityPy
    from static_survey import CatalogResourceReader
    from static_bundle_options import read_options
    base_url = CATALOG_URL.rsplit('/', 2)[0]
    materials = load_fixed_materials_ram()
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        response = client.get(CATALOG_URL)
        response.raise_for_status()
        catalog = response.content
        if hashlib.sha256(catalog).hexdigest() != CATALOG_SHA:
            raise ValueError('Public catalog identity changed')
        reader = CatalogResourceReader(catalog)
        sources = {'catalog_url': CATALOG_URL, 'catalog_sha256': CATALOG_SHA}
        environments = {}
        for name, offset, expected in [('Game', 3346383, 'Game.dll.bytes'), ('VIB', 5006058, 'VIBDatabase.asset')]:
            asset = reader.location(offset)
            if asset['internal_id'] != expected or len(asset['dependency_record_offsets']) != 1:
                raise ValueError('Public asset binding changed')
            dependency_offset = asset['dependency_record_offsets'][0]
            dependency = reader.location(dependency_offset, allow_encrypted_provider=True)
            options = read_options(reader, dependency_offset)
            internal_id = dependency['internal_id']
            if dependency['provider'] != 'EncryptedBundleProvider' or not internal_id.startswith('http://CDN_PLACEHOLDER/'):
                raise ValueError('Public dependency provider changed')
            url = base_url + '/' + internal_id.removeprefix('http://CDN_PLACEHOLDER/')
            result = client.get(url)
            result.raise_for_status()
            bundle = decrypt_bundle_ram(result.content, options['m_Hash'], materials, expected_size=options['m_BundleSize'])
            environments[name] = UnityPy.load(bundle.data)
            sources[name.lower()] = {**bundle.report, 'url': url, 'content_hash': options['m_Hash']}
    game = None
    for obj in environments['Game'].objects:
        if obj.type.name == 'TextAsset':
            value = obj.read()
            if value.m_Name == 'Game.dll':
                raw = value.m_Script
                game = raw if isinstance(raw, bytes) else raw.encode('utf-8', 'surrogateescape')
    if game is None or hashlib.sha256(game).hexdigest() != GAME_SHA:
        raise ValueError('Public Game.dll identity changed')
    tables = {}
    for obj in environments['VIB'].objects:
        if obj.type.name == 'MonoBehaviour':
            tree = obj.read_typetree()
            if tree.get('m_Name') == 'VIBDatabase':
                for row in tree['tables']:
                    if row['name'] in tables:
                        raise ValueError('Duplicate public VIB table')
                    tables[row['name']] = bytes(row['bytes'])
    return game, tables, reader, sources


def main():
    from pet_detail_text import render_text
    raw_schema = SCHEMA_PATH.read_bytes()
    schema = json.loads(raw_schema)
    if schema['source_game_sha256'] != GAME_SHA or schema['catalog_sha256'] != CATALOG_SHA:
        raise ValueError('Public schema source identity changed')
    _, table_bytes, catalog, public_sources = public_sources_ram()
    tables = {}
    for name in ('Lumi', 'ActiveSkill', 'BattlePassive', 'HomePassive', 'MultilingualCN', 'LumiTypeCounter'):
        facts = schema['table_facts'][name]
        if hashlib.sha256(table_bytes[name]).hexdigest() != facts['sha256']:
            raise ValueError('Public table source identity changed')
        tables[name] = decode_table(table_bytes[name], name, schema['schemas'], key_field=facts.get('key_field', 'Id'))
        if len(tables[name]) != facts['rows']:
            raise ValueError('Public table row count changed')
    texts = {row['Id']: row['Data'] for row in tables['MultilingualCN']}
    if {str(row['LumiType']): row['Name'] for row in tables['LumiTypeCounter']} != schema['element_name_keys']:
        raise ValueError('Public element localization binding changed')
    elements = {}
    for raw_id, localization_key in schema['element_name_keys'].items():
        value = render_text(localization_key, [], texts)
        if not value:
            raise ValueError('Public element localization missing')
        elements[int(raw_id)] = value
    document = project_details(tables['Lumi'], tables['ActiveSkill'], tables['BattlePassive'],
                               tables['HomePassive'], texts, elements, render_text)
    skill_rows = {str(row['Id']): row for row in tables['ActiveSkill']}
    icons = {key: skill_rows[key]['Icon'] for key in document['skills']}
    output_root = PROJECT / 'data/pets'
    images, icon_evidence = extract_skill_images(catalog, icons, output_root)
    for key, row in document['skills'].items():
        if key in images:
            row['image_path'] = images[key]['image_path']
        elif icons[key]:
            raise ValueError('Configured public skill icon was not found')
    document['source'].update({'catalog_sha256': CATALOG_SHA, 'catalog_url': CATALOG_URL,
        'game_sha256': GAME_SHA, 'schema_sha256': hashlib.sha256(raw_schema).hexdigest(),
        'table_sha256': {name: facts['sha256'] for name, facts in schema['table_facts'].items()},
        'skill_atlas_sha256': icon_evidence['sha256'],
        'text_renderer_sha256': hashlib.sha256(Path(__file__).with_name('pet_detail_text.py').read_bytes()).hexdigest(),
        'element_localization_verified': bool(schema['element_name_keys'])})
    existing_catalog = json.loads((output_root / 'catalog.json').read_text(encoding='utf-8'))
    catalog_ids = {row['pet_id'] for row in existing_catalog['pets']}
    if not set(document['pets']).issubset(catalog_ids):
        raise ValueError('Public species is outside saved pet catalog')
    unmapped_ids = sorted(catalog_ids - set(document['pets']), key=int)
    text_evidence = {}
    for key in document['skills']:
        row = skill_rows[key]
        text_evidence['skill:' + key] = {'name_key': row['Name'], 'description_key': row['Des'], 'parameters': row['DesParam']}
    for kind, table_name in [('battle', 'BattlePassive'), ('home', 'HomePassive')]:
        for row in tables[table_name]:
            key = kind + ':' + str(row['Id'])
            if key in document['traits']:
                text_evidence[key] = {'name_key': row['Name'], 'description_key': row['Des'], 'parameters': row['DesParam']}
    coverage = {'catalog_pet_ids': len(catalog_ids), 'species_details': len(document['pets']),
        'catalog_ids_without_species_row': len(unmapped_ids), 'skills': len(document['skills']),
        'traits': len(document['traits']), 'skill_images': len(images),
        'skills_without_configured_icon': sum(not value for value in icons.values()),
        'skill_descriptions': sum(row['description'] is not None for row in document['skills'].values()),
        'trait_descriptions': sum(row['description'] is not None for row in document['traits'].values())}
    evidence = {'schema_version': 1, 'source': public_sources, 'game_sha256': GAME_SHA,
        'schema_sha256': document['source']['schema_sha256'], 'constructors': schema['constructors'],
        'tables': schema['table_facts'], 'element_name_keys': schema['element_name_keys'],
        'display_sources': schema['display_sources'], 'element_enum_evidence': schema['element_enum_evidence'],
        'localization_verification': schema['localization_verification'],
        'coverage': coverage, 'unmapped_catalog_ids': unmapped_ids,
        'unmapped_reason': 'Catalog avatar reference has no corresponding Lumi configuration row.',
        'display_rules': {'normal_attack_power': 'first SkillPowerList entry',
            'skill_power': 'sum of SkillPowerList entries', 'normal_attack_energy': 'AddEnergy / 10',
            'normal_attack_interval_seconds': 'AttackInterval / 1000',
            'stats': 'Species Min/Max State aptitude bounds, not individual combat attributes.',
            'replaceable_skills': 'Stable unique union of species SkillPool1/2/3.',
            'energy_cost': 'Native GetSkillCostConfig kind 0 amount / 10.',
            'hp_cost': 'Native kind 1 base amount / 100 percent of maximum HP; no individual HP value calculated.'},
        'localization_inputs': text_evidence, 'skill_atlas': icon_evidence,
        'decrypted_assemblies_or_whole_tables_persisted': False, 'target_assembly_execution': False,
        'account_data_used': False}
    save_json(output_root / 'details.json', document)
    save_json(output_root / 'evidence/pet-details.public.json', evidence)
    print(json.dumps(coverage, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
