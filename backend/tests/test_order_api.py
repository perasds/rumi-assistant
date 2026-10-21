import pytest
from fastapi.testclient import TestClient
from rumi_market.app import create_app
from rumi_market.auth import COOKIE_NAME
from test_order_service import setup


def client_with_orders():
    service, session, provider = setup()
    app = create_app()
    app.state.sessions.sessions[session.id] = session
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, session.id)
    return client, provider, session


def test_order_routes_require_login_and_open_page_does_not_send_operations():
    with TestClient(create_app()) as guest:
        assert guest.get('/api/home/orders/state').status_code == 401
        assert guest.post('/api/home/orders/task/stop', json={}).status_code == 401
    client, provider, _ = client_with_orders()
    with client:
        response = client.get('/api/home/orders/state')
        assert response.status_code == 200
        state = response.json()
        assert response.headers['cache-control'] == 'no-store'
        assert state['orders'][0]['materials'][0]['count'] == '10'
        saved = client.put('/api/home/orders/settings', json=state['settings'])
        assert saved.status_code == 200
        preview = client.post('/api/home/orders/plan', json={'expected_revision': state['revision'], 'settings': state['settings']})
        assert preview.status_code == 200
        assert preview.json()['inventory_selection']['count'] == '2'
        assert provider.writes == []
        assert 'role_key' not in response.text and 'work_old' not in response.text


@pytest.mark.parametrize('change', [
    {'max_orders': True}, {'horizon_hours': 0}, {'max_orders': 10001}, {'allow_production': 'true'},
    {'reserves': {'1': '-1'}}, {'reserves': {'1': '1e3'}}, {'reserves': {'1': '9223372036854775808'}},
    {'reserves': {'01': '1'}}, {'reserves': {'1': 1}}, {'account_password': 'synthetic-sensitive'}])
def test_settings_validate_without_echoing_input(change):
    client, _, _ = client_with_orders()
    with client:
        response = client.put('/api/home/orders/settings', json=change)
        assert response.status_code == 422
        assert 'synthetic-sensitive' not in response.text


def test_order_mutations_obey_same_origin():
    client, provider, _ = client_with_orders()
    with client:
        response = client.put('/api/home/orders/settings', json={}, headers={'origin': 'https://other.invalid'})
        assert response.status_code == 403 and provider.writes == []


@pytest.mark.parametrize('item_id,name', [
    ('10001', '金币'), ('10004', '体力'), ('54001', '罐头'), ('54002', '公会罐头'),
    ('999999999', '物品 999999999'),
])
def test_order_rewards_use_verified_item_names_and_preserve_unknown_ids(item_id, name):
    client, provider, _ = client_with_orders()
    provider.raw['orders']['orders'][0]['rewards'] = [{'item_id': item_id, 'count': 123}]
    with client:
        response = client.get('/api/home/orders/state')
        assert response.status_code == 200
        assert response.json()['orders'][0]['rewards'] == [
            {'item_id': item_id, 'name': name, 'count': '123'}]
        assert provider.writes == []
