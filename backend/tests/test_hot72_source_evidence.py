"""Hot72 provenance and the single current-encyclopedia correction."""
import hashlib
import json
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from rumi_market.app import create_app
from rumi_market.game.account_state import GOLD_HOT_VERSIONS
from rumi_market.game.home_codec import HOME_HOT_VERSIONS
from rumi_market.game.order_codec import ORDER_HOT_VERSIONS
from rumi_market.game.redbag_codec import REDBAG_HOT_VERSIONS
from rumi_market.game.season_codec import SEASON_HOT_VERSIONS
from test_pet_book_data import DATA, forbidden_provider


PROJECT = Path(__file__).resolve().parents[2]
REPORT = 'docs/hot72-runtime-compatibility.public.json'
GAME_SHA = '0867970fcfd9a33984120c85ad91644b1e17352c4ddae85e083cea5f59c089f8'
CATALOG_SHA = '1a78a543153649577058c5a461c3d37ff0466e30c429da73fc090bb812999fa1'
VIB_SHA = 'a7f165628755f46921e06ad48f2bc363eb47e277b51d759d1f9c2f69e6010049'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def assert_binding(record):
    assert record['verification_report'] == REPORT
    assert re.fullmatch(r'[0-9a-f]{64}', record['verification_report_sha256'])
    assert record['source_game_sha256'] == GAME_SHA
    assert record['source_catalog_sha256'] == CATALOG_SHA
    assert record['source_vib_bundle_sha256'] == VIB_SHA


@pytest.mark.parametrize('name,versions', [
    ('gold', GOLD_HOT_VERSIONS), ('home', HOME_HOT_VERSIONS), ('order', ORDER_HOT_VERSIONS),
    ('season', SEASON_HOT_VERSIONS), ('redbag', REDBAG_HOT_VERSIONS),
])
def test_hot72_each_gate_has_a_bound_source_record_and_unknown73_is_rejected(name, versions):
    source = read(PROJECT / f'backend/src/rumi_market/game/{name}-source.json')
    assert 72 in versions and 73 not in versions
    assert source['verified_hot_versions'] == sorted(versions)
    records = [row for row in source['additional_verified_versions'] if row['hot_version'] == 72]
    assert len(records) == 1
    assert_binding(records[0])
    assert records[0]['wire_contract_compatible'] is True
    assert records[0]['supported_execution_configuration_compatible'] is True
    assert records[0]['real_account_tested'] is False


@pytest.mark.external_materials
@pytest.mark.parametrize('name', ['gold', 'home', 'order', 'season', 'redbag'])
def test_hot72_promoted_source_matches_external_report_digest(name, external_materials_root):
    source = read(PROJECT / f'backend/src/rumi_market/game/{name}-source.json')
    record = next(row for row in source['additional_verified_versions'] if row['hot_version'] == 72)
    assert_binding(record)
    assert record['verification_report_sha256'] == hashlib.sha256((external_materials_root / REPORT).read_bytes()).hexdigest()


@pytest.mark.external_materials
def test_hot72_report_binds_current_public_resources_and_every_consumed_feature(external_materials_root):
    report = read(external_materials_root / REPORT)
    assert report['baseline_hot_version'] == 71 and report['candidate_hot_version'] == 72
    assert report['new_game_sha256'] == GAME_SHA and report['same_game_bytes'] is True
    assert report['acquisition']['manifest']['catalog_sha256'] == CATALOG_SHA
    assert report['acquisition']['manifest']['files']['vib']['encrypted_sha256'] == VIB_SHA
    assert report['real_account_tested'] is False
    assert set(report['features']) == {'gold', 'home', 'order', 'inventory', 'capacity',
        'work_inspection', 'season', 'market_purchase_login', 'profile', 'redbag', 'static_pokedex'}
    assert all(value['wire_contract_compatible'] for value in report['features'].values())


@pytest.mark.external_materials
def test_hot72_pet_details_updates_description_and_preserves_original_images(external_materials_root):
    data = external_materials_root / 'data/pets'
    stored = read(data / 'details.json')
    assert stored['source']['hot_version'] == 64
    records = [row for row in stored['source']['compatible_public_sources'] if row['hot_version'] == 72]
    assert len(records) == 1
    assert_binding(records[0])
    assert records[0]['verification_report_sha256'] == hashlib.sha256((external_materials_root / REPORT).read_bytes()).hexdigest()
    skill = stored['skills']['13930402']
    assert '每1层燃烧汲取额外附加6%吸血效果' in skill['description']
    assert '每1层毒素汲取额外提升6%伤害' in skill['description']
    assert skill['power'] == 70 and skill['image_path'] == 'images/skills/13930402.png'
    app = create_app(provider_factory=forbidden_provider, catalog_path=data / 'catalog.json')
    with TestClient(app) as client:
        response = client.get('/api/pets/139304')
        assert response.status_code == 200
        assert response.json()['fixed_skill']['description'] == skill['description']
        assert app.state.sessions.sessions == {}
