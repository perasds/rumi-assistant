"""Public host time never creates a session or contacts the game provider."""

import time

from fastapi.testclient import TestClient

from rumi_market.app import create_app


def test_server_time_is_public_precise_and_does_not_touch_sessions_or_provider(catalog_path):
    provider_calls = []

    def forbidden_provider():
        provider_calls.append(True)
        raise AssertionError('Server time must not create a game provider')

    app = create_app(catalog_path=catalog_path, provider_factory=forbidden_provider)
    with TestClient(app) as client:
        before_ms = time.time_ns() // 1_000_000
        response = client.get('/api/time')
        after_ms = time.time_ns() // 1_000_000
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert 'set-cookie' not in response.headers
        data = response.json()
        assert set(data) == {'unix_ms', 'time_zone'}
        assert type(data['unix_ms']) is int
        assert before_ms <= data['unix_ms'] <= after_ms
        assert type(data['time_zone']) is str and data['time_zone'] == 'Asia/Shanghai'
        assert not app.state.sessions.sessions
        assert not provider_calls


def test_server_time_is_generated_for_each_request(catalog_path, monkeypatch):
    app = create_app(catalog_path=catalog_path)
    with TestClient(app) as client:
        monkeypatch.setattr(time, 'time_ns', lambda: 1_800_000_000_123_999_999)
        assert client.get('/api/time').json()['unix_ms'] == 1_800_000_000_123
        monkeypatch.setattr(time, 'time_ns', lambda: 1_800_000_001_456_000_000)
        assert client.get('/api/time').json()['unix_ms'] == 1_800_000_001_456
