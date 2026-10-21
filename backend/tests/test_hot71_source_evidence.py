"""Public hot71 provenance and the two verified encyclopedia corrections."""

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from rumi_market.app import create_app
from rumi_market.game.home_codec import HOME_HOT_VERSIONS
from rumi_market.game.order_codec import ORDER_HOT_VERSIONS
from rumi_market.game.season_codec import SEASON_HOT_VERSIONS
from rumi_market.home_management import HomeManagementService
from rumi_market.order_replenishment import choose_replenishment
from rumi_market.work_inspection import inspect_workplaces
from test_activity_farm_guards import fixture as farm_fixture
from test_pet_book_data import DATA, forbidden_provider


ROOT = Path(__file__).resolve().parents[2]
REPORT = 'docs/hot71-runtime-compatibility.public.json'
GAME_SHA = '0867970fcfd9a33984120c85ad91644b1e17352c4ddae85e083cea5f59c089f8'
CATALOG_SHA = '5ae75e7d91edf555f5776fa5eb7089162f5cc5b2918774e74d28610ca6f8cf98'
VIB_SHA = 'd9d374dd6602d52dafeabf5a2ab722d12a7eb61c588091beda639a46c1937dc8'
THUNDER_DESCRIPTION = (
    '对敌方阵营全体噜咪添加1层麻痹，随后释放雷电攻击敌方场上全体噜咪5次；'
    '若该技能将目标击败，则将其携带的麻痹扩散到其队伍的其他噜咪'
)


def source(name):
    return json.loads(files('rumi_market.game').joinpath(f'{name}-source.json').read_bytes())


def assert_report_reference(record):
    assert record.get('verification_report') == REPORT
    assert re.fullmatch(r'[0-9a-f]{64}', record.get('verification_report_sha256', ''))


def assert_report_binding(record, material_root):
    assert_report_reference(record)
    path = material_root / REPORT
    assert path.is_file(), 'The hot71 compatibility report must exist before promotion'
    assert record.get('verification_report_sha256') == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.external_materials
def test_hot71_report_binds_official_public_resources_without_claiming_real_account_execution(external_materials_root):
    path = external_materials_root / REPORT
    assert path.is_file(), 'The hot71 compatibility report has not been recorded'
    report = json.loads(path.read_bytes())
    assert report['schema_version'] == 1
    assert report['baseline_hot_version'] == 69 and report['candidate_hot_version'] == 71
    assert report['new_game_sha256'] == GAME_SHA
    manifest = report['acquisition']['manifest']
    assert manifest['hot_version'] == 71 and manifest['client_version'] == '1.5.3'
    assert manifest['catalog_sha256'] == CATALOG_SHA
    assert manifest['files']['vib']['encrypted_sha256'] == VIB_SHA
    assert report['real_account_tested'] is False


@pytest.mark.parametrize('name,versions,expected', [
    ('home', HOME_HOT_VERSIONS, [64, 65, 69, 71, 72]),
    ('order', ORDER_HOT_VERSIONS, [65, 69, 71, 72]),
    ('season', SEASON_HOT_VERSIONS, [65, 69, 71, 72]),
])
def test_hot71_source_version_list_matches_its_runtime_gate(name, versions, expected):
    assert source(name).get('verified_hot_versions') == sorted(versions) == expected
    assert 73 not in versions


@pytest.mark.parametrize('name', ['home', 'order', 'season'])
def test_hot71_each_promoted_source_binds_its_verified_wire_and_execution_configuration(name):
    candidates = [row for row in source(name).get('additional_verified_versions', [])
                  if row.get('hot_version') == 71]
    assert len(candidates) == 1, f'{name} must have exactly one hot71 evidence record'
    record = candidates[0]
    assert record['source_game_sha256'] == GAME_SHA
    assert record['source_catalog_sha256'] == CATALOG_SHA
    assert record['source_vib_bundle_sha256'] == VIB_SHA
    assert record['wire_contract_compatible'] is True
    assert record['supported_execution_configuration_compatible'] is True
    assert_report_reference(record)


@pytest.mark.external_materials
@pytest.mark.parametrize('name', ['home', 'order', 'season'])
def test_hot71_promoted_source_matches_external_report_digest(name, external_materials_root):
    record = next(row for row in source(name)['additional_verified_versions'] if row['hot_version'] == 71)
    assert_report_binding(record, external_materials_root)


@pytest.mark.external_materials
def test_hot71_pet_details_records_compatibility_without_relabeling_original_image_source(external_materials_root):
    stored = json.loads((external_materials_root / 'data/pets/details.json').read_text(encoding='utf-8'))
    assert stored['source']['hot_version'] == 64
    candidates = [row for row in stored['source'].get('compatible_public_sources', [])
                  if row.get('hot_version') == 71]
    assert len(candidates) == 1
    assert_report_binding(candidates[0], external_materials_root)


@pytest.mark.parametrize('pet_id,skill_id,field,expected,image_sha', [
    ('139304', '13930402', 'power', 70,
     '43204eaa5cfbda9c7a177322bc49473c9a17eb5455b1db6420545f910c4b4ab1'),
    ('108001', '10800102', 'description', THUNDER_DESCRIPTION,
     '4dc077c5de75ada1922c5d63c2ac015a38fed854a66c147a37ddded611965061'),
])
@pytest.mark.external_materials
def test_hot71_public_pet_details_exposes_verified_skill_correction_and_preserves_image(
        pet_id, skill_id, field, expected, image_sha, external_materials_root):
    app = create_app(provider_factory=forbidden_provider, catalog_path=external_materials_root / 'data/pets/catalog.json')
    with TestClient(app) as client:
        response = client.get(f'/api/pets/{pet_id}')
        assert response.status_code == 200
        skill = response.json()['fixed_skill']
        assert skill['skill_id'] == skill_id and skill[field] == expected
        assert skill['image_url'] == f'/api/pet-skills/{skill_id}/image'
        image = client.get(skill['image_url'])
        assert image.status_code == 200
        assert hashlib.sha256(image.content).hexdigest() == image_sha
        assert app.state.sessions.sessions == {}


@pytest.mark.external_materials
def test_hot71_skill_corrections_preserve_every_other_skill_value_and_image_path(external_materials_root):
    skills = json.loads((external_materials_root / 'data/pets/details.json').read_text(encoding='utf-8'))['skills']
    del skills['13930402']['power']
    del skills['10800102']['description']
    # The later hot72 description correction has its own provenance regression.
    del skills['13930402']['description']
    canonical = json.dumps(skills, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    assert hashlib.sha256(canonical).hexdigest() == 'bf3442b301583e9af4877604640c3f94f386acd972d4868bffc875e5d138b202'


@pytest.mark.parametrize('building_id', [10125, 10130])
def test_hot71_compatibility_promotion_keeps_activity_farm_execution_disabled(building_id):
    _, _, state, service, raw, _ = farm_fixture(building_id, 'absent')
    snapshot = service._snapshot(raw)
    building, = snapshot['buildings']
    assert building['editable'] is False and building['production_editable'] is False
    assert building['reason'] == 'production_branch_unverified'
    assert choose_replenishment(snapshot, service.catalog, {}, batch_limit=1) is None
    inspected, = inspect_workplaces(snapshot, service.catalog, {})
    assert inspected['proposal'] is None
    view = HomeManagementService(None, None)._building(state.data['architectures'][0])
    assert view.editable is False and view.production_editable is False
