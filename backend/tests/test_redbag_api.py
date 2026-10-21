import asyncio

from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.auth import COOKIE_NAME
from rumi_market.models import Role, SessionView


class RedbagProvider:
    redbag_available = True
    redbag_identity = ('2', '42')
    server_time_seconds = 1000

    def __init__(self):
        self.starts = self.stops = 0
        self.wait = asyncio.Event()

    async def redbag_start(self):
        self.starts += 1

    async def redbag_next(self):
        await self.wait.wait()

    async def redbag_stop(self):
        self.stops += 1

    async def close(self):
        pass


def connected_client():
    provider = RedbagProvider()
    app = create_app(provider_factory=lambda: provider)
    session = app.state.sessions.create()
    session.view = SessionView(status='connected', authenticated=True,
                               role=Role(id='42', name='测试', server_name='测试区服'))
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, session.id)
    return client, provider, session, app


def test_redbag_routes_require_connected_login():
    with TestClient(create_app()) as client:
        assert client.get('/api/redbags/task').status_code == 401
        assert client.post('/api/redbags/task/start', json={'request_id': 'synthetic-request-1'}).status_code == 401
        assert client.post('/api/redbags/task/stop').status_code == 401


def test_opening_redbag_status_does_not_start_chat_or_claim():
    client, provider, _, _ = connected_client()
    with client:
        response = client.get('/api/redbags/task')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        view = response.json()
        assert view['status'] == 'idle' and not view['running']
        assert view['claimed_count'] == 0 and view['stamina_total'] == '0'
        assert provider.starts == 0
        assert 'redbag_identity' not in response.text


def test_redbag_start_is_idempotent_and_stop_cleans_up_only_redbag_task():
    client, provider, session, _ = connected_client()
    with client:
        first = client.post('/api/redbags/task/start', json={'request_id': 'synthetic-request-1'})
        assert first.status_code == 200 and first.json()['running']
        again = client.post('/api/redbags/task/start', json={'request_id': 'synthetic-request-1'})
        assert again.status_code == 200
        assert provider.starts == 1
        assert len(session.tasks) == 1
        stopped = client.post('/api/redbags/task/stop')
        assert stopped.status_code == 200
        for _ in range(20):
            final = client.get('/api/redbags/task').json()
            if not final['running']:
                break
        assert final['status'] == 'stopped' and not final['running']
        assert provider.stops == 1


def test_redbag_start_rejects_cross_origin_and_unrecognized_fields_safely():
    client, provider, _, _ = connected_client()
    with client:
        rejected = client.post('/api/redbags/task/start', json={'request_id': 'synthetic-request-1'},
                               headers={'origin': 'https://different.invalid'})
        assert rejected.status_code == 403 and provider.starts == 0
        invalid = client.post('/api/redbags/task/start', json={
            'request_id': 'synthetic-request-1', 'secret': 'synthetic-sensitive'})
        assert invalid.status_code == 422
        assert 'synthetic-sensitive' not in invalid.text
        assert provider.starts == 0


def test_shutdown_stops_and_discards_redbag_session_state():
    client, provider, session, _ = connected_client()
    with client:
        assert client.post('/api/redbags/task/start', json={'request_id': 'synthetic-request-1'}).status_code == 200
        assert session.redbag_task is not None
    assert not session.active
    assert session.redbag_task is None
    assert session.redbag_requests == {} and session.redbag_seen == {}
    assert not any(not task.done() for task in session.tasks)
