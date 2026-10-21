import copy
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.auth import COOKIE_NAME
from rumi_market.catalog import PNG_SIGNATURE
from rumi_market.models import Role, SessionView
from rumi_market.providers import UnverifiedGameProvider
from rumi_market.sessions import Session


def document():
    return {'schema_version': 1, 'source': {'private_analysis_path': 'synthetic-private-path'},
            'buildings': [{'building_id': '10101', 'name': '简易制卡台', 'island_id': 1,
                           'island_name': '雨林', 'work_type': '手工', 'recipes': [
                               {'recipe_id': '1', 'min_building_level': 1,
                                'materials': [{'item_id': '20001', 'name': '木头', 'count': 5, 'image_url': None}],
                                'products': [{'item_id': '10031', 'name': '噜咪卡', 'count': 1, 'image_url': None}]}
                           ]}]}


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def connected_session(client):
    role = Role(id='synthetic-role', name='测试角色', server_name='测试区服')
    session = Session('synthetic-session', UnverifiedGameProvider(),
                      view=SessionView(status='connected', authenticated=True, role=role, roles=[role]))
    client.app.state.sessions.sessions[session.id] = session
    client.cookies.set(COOKIE_NAME, session.id)
    return session


@pytest.mark.parametrize(('state', 'status_code', 'error_code'), [
    ('no_cookie', 401, 'AUTH_REQUIRED'),
    ('unknown_cookie', 401, 'AUTH_REQUIRED'),
    ('anonymous', 401, 'AUTH_REQUIRED'),
    ('role_selection', 409, 'ROLE_REQUIRED'),
])
def test_home_catalog_requires_a_connected_session(catalog_path, tmp_path, state, status_code, error_code):
    path = tmp_path / 'production.json'
    save(path, document())
    app = create_app(catalog_path=catalog_path, home_catalog_path=path)
    with TestClient(app) as client:
        if state == 'unknown_cookie':
            client.cookies.set(COOKIE_NAME, 'unknown-session')
        elif state in {'anonymous', 'role_selection'}:
            session = connected_session(client)
            session.view = SessionView(status=state, authenticated=state == 'role_selection')
        response = client.get('/api/home/production')
        assert response.status_code == status_code
        assert response.json()['error']['code'] == error_code
        assert 'buildings' not in response.json()
        assert response.headers['cache-control'] == 'no-store'
        assert 'set-cookie' not in response.headers


def test_connected_static_catalog_uses_no_game_provider_and_exposes_only_contract(catalog_path, tmp_path):
    path = tmp_path / 'production.json'
    save(path, document())
    def forbidden():
        pytest.fail('Home catalog must reuse the existing session without creating a game provider')
    app = create_app(provider_factory=forbidden, catalog_path=catalog_path, home_catalog_path=path)
    with TestClient(app) as client:
        session = connected_session(client)
        response = client.get('/api/home/production')
        assert response.status_code == 200
        assert response.json() == {'scope': 'configuration', 'buildings': document()['buildings'], 'total': 1, 'recipe_count': 1}
        assert response.headers['cache-control'] == 'no-store'
        assert 'set-cookie' not in response.headers
        assert 'synthetic-private' not in response.text
        assert app.state.sessions.sessions == {session.id: session}


@pytest.mark.parametrize('mode', ['missing', 'json', 'schema', 'duplicate', 'duplicate_recipe', 'bad_count', 'extra_field'])
def test_missing_or_invalid_home_catalog_returns_safe_503_and_keeps_pets_working(catalog_path, tmp_path, mode):
    path = tmp_path / 'synthetic-private-path.json'
    data = document()
    if mode == 'json': path.write_text('{synthetic-private', encoding='utf-8')
    elif mode != 'missing':
        if mode == 'schema': data['schema_version'] = True
        elif mode == 'duplicate': data['buildings'].append(copy.deepcopy(data['buildings'][0]))
        elif mode == 'duplicate_recipe': data['buildings'][0]['recipes'].append(copy.deepcopy(data['buildings'][0]['recipes'][0]))
        elif mode == 'bad_count': data['buildings'][0]['recipes'][0]['products'][0]['count'] = True
        elif mode == 'extra_field': data['buildings'][0]['private'] = 'synthetic-private'
        save(path, data)
    with TestClient(create_app(catalog_path=catalog_path, home_catalog_path=path)) as client:
        assert client.get('/api/home/production').status_code == 401
        connected_session(client)
        response = client.get('/api/home/production')
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'HOME_CATALOG_UNAVAILABLE'
        assert 'synthetic-private' not in response.text and str(path) not in response.text
        assert client.get('/api/pets').status_code == 200


def test_images_use_only_existing_checksum_validated_item_route(catalog_path, tmp_path):
    path = tmp_path / 'production.json'
    save(path, document())
    market = catalog_path.parent.parent / 'market'
    (market / 'images').mkdir(parents=True)
    image = market / 'images/10031.png'
    raw = PNG_SIGNATURE + b'synthetic-public-item'
    image.write_bytes(raw)
    save(market / 'items.json', {'items': [
        {'item_id': '10031', 'config_id': 10031, 'name': '噜咪卡', 'name_verified': True,
         'source': 'public synthetic item', 'image_path': 'images/10031.png',
         'image_sha256': hashlib.sha256(raw).hexdigest()}]})
    with TestClient(create_app(catalog_path=catalog_path, home_catalog_path=path)) as client:
        connected_session(client)
        recipe = client.get('/api/home/production').json()['buildings'][0]['recipes'][0]
        assert recipe['materials'][0]['image_url'] is None
        assert recipe['products'][0]['image_url'] == '/api/items/10031/image'
        assert client.get(recipe['products'][0]['image_url']).content == raw
        image.write_bytes(PNG_SIGNATURE + b'changed')
        recipe = client.get('/api/home/production').json()['buildings'][0]['recipes'][0]
        assert recipe['products'][0]['image_url'] is None


def test_empty_catalog_has_explicit_zero_counts(catalog_path, tmp_path):
    path = tmp_path / 'production.json'
    data = document(); data['buildings'] = []
    save(path, data)
    with TestClient(create_app(catalog_path=catalog_path, home_catalog_path=path)) as client:
        connected_session(client)
        assert client.get('/api/home/production').json() == {'scope': 'configuration', 'buildings': [], 'total': 0, 'recipe_count': 0}


def test_shipped_catalog_contains_all_public_buildings_and_recipes(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        connected_session(client)
        result = client.get('/api/home/production').json()
        assert (result['total'], result['recipe_count']) == (21, 37)
        ids = {row['building_id'] for row in result['buildings']}
        assert {'10104', '10125', '10130', '10206'} <= ids
        assert next(row for row in result['buildings'] if row['building_id'] == '10206')['name'] == '建筑 #10206'
        assert len(ids) == len(result['buildings'])
        assert all(len({recipe['recipe_id'] for recipe in row['recipes']}) == len(row['recipes']) for row in result['buildings'])
        assert set(result) == {'scope', 'buildings', 'total', 'recipe_count'}
        assert all(set(row) == {'building_id', 'name', 'island_id', 'island_name', 'work_type', 'recipes'} for row in result['buildings'])


def test_connected_shipped_catalog_reuses_exactly_five_existing_images_without_game_calls(catalog_path):
    market = catalog_path.parent.parent / 'market'
    (market / 'images').mkdir(parents=True)
    rows = []
    for item_id in range(10031, 10036):
        raw = PNG_SIGNATURE + f'synthetic item {item_id}'.encode('ascii')
        (market / f'images/{item_id}.png').write_bytes(raw)
        rows.append({'config_id': item_id, 'item_id': str(item_id), 'name': f'Item {item_id}',
                     'name_verified': True, 'source': 'synthetic fixture',
                     'image_path': f'images/{item_id}.png', 'image_sha256': hashlib.sha256(raw).hexdigest()})
    save(market / 'items.json', {'items': rows})
    def forbidden():
        pytest.fail('Home catalog must reuse the existing session without creating a game provider')
    with TestClient(create_app(provider_factory=forbidden, catalog_path=catalog_path)) as client:
        connected_session(client)
        response = client.get('/api/home/production')
        assert response.status_code == 200
        result = response.json()
        products = [item for row in result['buildings'] for recipe in row['recipes'] for item in recipe['products']]
        images = {item['item_id']: item['image_url'] for item in products if item['image_url'] is not None}
        assert set(images) == {str(i) for i in range(10031, 10036)}
        assert len({item['item_id'] for item in products}) == 37
        for item_id, url in images.items():
            assert url == f'/api/items/{item_id}/image'
            assert client.get(url).content.startswith(PNG_SIGNATURE)


def test_missing_catalog_can_be_retried_after_public_file_is_restored(catalog_path, tmp_path):
    path = tmp_path / 'production.json'
    with TestClient(create_app(catalog_path=catalog_path, home_catalog_path=path)) as client:
        connected_session(client)
        assert client.get('/api/home/production').status_code == 503
        save(path, document())
        assert client.get('/api/home/production').status_code == 200
