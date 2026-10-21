import copy
import asyncio

import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.auth import COOKIE_NAME
from rumi_market.errors import ApiError
from rumi_market.models import Role, SessionView
from rumi_market.sessions import Session


WORKER = '9007199254740993'


def snapshot():
    return {'valid': True, 'sequence': 1, 'observed_at': '2026-10-15T00:00:00+00:00',
            'architectures': [{'uid': 123, 'id': 10101, 'level': 1, 'is_hide': False,
                'is_need_cut_ribbon': False, 'level_up_complete_time': '0',
                'work_old': {'lumi_uid': WORKER, 'lumi_id': 102601, 'recipe_id': 1,
                             'recipe_intend_times': 10, 'recipe_complete_times': 2},
                'cur_recipe': None, 'dorm': None, 'travel': None, 'interaction_uids': []}],
            'home_lumis': [], 'roster_ready': True, 'dispatch_ready': True, 'dispatch_lumi_uids': [],
            'workers': [{'uid': WORKER, 'id': 102601, 'raw_state': '0', 'work_aptitude': 80,
                         'work_energy': 100, 'occupancy': [{'kind': 'work_old', 'architecture_uid': 123}]}],
            'private_marker': 'synthetic-private-value'}


class HomeProvider:
    def __init__(self):
        self.state = snapshot()
        self.writes = []
        self.outcome = 'applied'
        self.failure = None
        self.update_state = True

    async def home_snapshot(self):
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.state)

    async def apply_home_change(self, change, *, expected):
        self.writes.append(copy.deepcopy(change))
        if self.outcome == 'applied' and self.update_state:
            work = self.state['architectures'][0]['work_old']
            if 'recipe_id' in change: work['recipe_id'] = change['recipe_id']
            if 'recipe_times' in change: work['recipe_intend_times'] = change['recipe_times']
            if 'lumi_uid' in change: work['lumi_uid'] = change['lumi_uid']
            self.state['sequence'] += 1
        return {'status': self.outcome, 'message': {'applied': '设置已同步', 'uncertain': '应用结果待核实'}.get(self.outcome, '设置未应用')}

    async def close(self):
        self.state = {'valid': False}


def client_with_session(provider=None, status='connected'):
    provider = provider or HomeProvider()
    app = create_app()
    client = TestClient(app)
    role = Role(id='synthetic-role', name='测试角色', server_name='测试区服') if status == 'connected' else None
    session = Session('synthetic-home-session', provider, view=SessionView(status=status, authenticated=status != 'anonymous', role=role))
    app.state.sessions.sessions[session.id] = session
    client.cookies.set(COOKIE_NAME, session.id)
    return client, provider, session


def request_body(revision, **change):
    return {'request_id': 'synthetic-request-id-001', 'expected_revision': revision,
            'changes': [{'building_uid': '123', **(change or {'recipe_id': '1', 'production_count': 5})}]}


def idle(provider):
    provider.state['architectures'][0]['work_old'].update(recipe_id=0, recipe_intend_times=0, recipe_complete_times=0)


@pytest.mark.parametrize(('status', 'expected'), [('anonymous', 401), ('role_selection', 409)])
def test_home_state_and_apply_require_connected_session(status, expected):
    client, provider, _ = client_with_session(status=status)
    with client:
        assert client.get('/api/home/state').status_code == expected
        assert client.put('/api/home/settings', json=request_body('a' * 64)).status_code == expected
        assert not provider.writes


def test_account_state_joins_current_instances_without_exposing_unprojected_values():
    client, _, _ = client_with_session()
    with client:
        response = client.get('/api/home/state')
        assert response.status_code == 200
        result = response.json()
        assert result['scope'] == 'account' and result['ready']
        building = result['buildings'][0]
        assert (building['building_uid'], building['building_id'], building['level']) == ('123', '10101', 1)
        assert building['allowed_recipe_ids'] == ['1']
        assert building['assigned_lumi_uids'] == [WORKER]
        assert building['production_count'] == 10 and building['completed_count'] == 2
        assert building['quantity_mode'] == 'batch'
        worker = result['roster'][0]
        assert worker['lumi_uid'] == WORKER and worker['lumi_id'] == '102601'
        assert worker['name'] == '筋肉狗'
        assert worker['assigned_building_uid'] == '123'
        assert worker['eligible_building_uids'] == ['123']
        assert 'synthetic-private-value' not in response.text
        assert 'raw_state' not in response.text and 'home_lumis' not in response.text
        assert response.headers['cache-control'] == 'no-store'


def test_actual_home_state_projection_and_api_agree_on_work_occupancy():
    from rumi_market.game.home_state import HomeState
    from rumi_market.game.home_codec import HomeProtoCodec
    from rumi_market.game.envelope import ResponseEnvelope
    tracker, codec = HomeState(verified_version=True), HomeProtoCodec()
    def observe(command, **fields):
        tracker.observe(ResponseEnvelope(command, 0, 1, 0, 0, codec.encode(command, **fields)))
    observe(2971, lumi_list=[{'lumi_uid': int(WORKER), 'lumi_id': 102601, 'lumi_state': 2}])
    observe(2913)
    observe(3001, architecture_list=[{'architecture_uid': 123, 'architecture_id': 10101,
        'architecture_level': 1, 'work_old': {'lumi_uid': int(WORKER), 'lumi_id': 102601,
        'recipe_id': 1, 'recipe_intend_times': 10, 'recipe_complete_times': 2}}])
    observe(10093, info={'id': 42})
    client, provider, _ = client_with_session()
    provider.state = tracker.snapshot()
    with client:
        response = client.get('/api/home/state')
        assert response.status_code == 200
        worker = response.json()['roster'][0]
        assert worker['lumi_uid'] == WORKER
        assert worker['assigned_building_uid'] == '123'
        assert worker['eligible_building_uids'] == ['123']


def test_live_progress_does_not_make_configuration_revision_stale():
    client, provider, _ = client_with_session()
    with client:
        first = client.get('/api/home/state').json()
        provider.state['architectures'][0]['work_old']['recipe_complete_times'] += 1
        provider.state['workers'][0]['work_energy'] -= 1
        provider.state['sequence'] += 1
        second = client.get('/api/home/state').json()
        assert first['revision'] == second['revision']


def test_unready_snapshot_never_reuses_previously_read_account_buildings():
    client, provider, _ = client_with_session()
    with client:
        assert client.get('/api/home/state').json()['buildings']
        provider.state['valid'] = False
        result = client.get('/api/home/state').json()
        assert result['ready'] is False and result['revision'] is None
        assert not result['buildings'] and not result['roster']


@pytest.mark.parametrize(('reason', 'message'), [
    ('HOME_VERSION_UNVERIFIED', '当前游戏版本尚未完成家园兼容校验，请等待适配更新'),
    ('HOME_SCHEMA_UNAVAILABLE', '家园协议资源未通过校验，请更新程序后重试'),
])
def test_unavailable_home_protocol_does_not_prompt_endless_refresh(reason, message):
    client, provider, _ = client_with_session()
    provider.state.update(valid=False, unavailable_reason=reason)
    with client:
        result = client.get('/api/home/state').json()
        assert result['ready'] is False and result['message'] == message
        assert not result['buildings'] and not result['roster']


def test_expired_game_provider_does_not_return_cached_account_state():
    client, provider, _ = client_with_session()
    provider.failure = ApiError('GAME_SESSION_EXPIRED', 'untrusted-upstream-text', 401)
    with client:
        response = client.get('/api/home/state')
        assert response.status_code == 401
        assert 'untrusted-upstream-text' not in response.text


def test_explicit_apply_uses_current_recipe_and_deduplicates_same_request():
    client, provider, _ = client_with_session()
    idle(provider)
    with client:
        revision = client.get('/api/home/state').json()['revision']
        body = request_body(revision)
        first = client.put('/api/home/settings', json=body)
        assert first.status_code == 200
        assert first.json()['last_apply']['status'] == 'applied'
        assert first.json()['buildings'][0]['production_count'] == 5
        assert provider.writes == [{'building_uid': 123, 'building_id': 10101, 'recipe_id': 1, 'recipe_times': 5}]
        retry = client.put('/api/home/settings', json=body)
        assert retry.status_code == 200
        assert retry.json()['last_apply']['request_id'] == body['request_id']
        assert len(provider.writes) == 1
        other = request_body(revision, production_count=7)
        assert client.put('/api/home/settings', json=other).status_code == 409
        assert len(provider.writes) == 1


def test_external_configuration_change_rejects_old_draft_without_writing():
    client, provider, _ = client_with_session()
    with client:
        revision = client.get('/api/home/state').json()['revision']
        provider.state['architectures'][0]['work_old']['recipe_intend_times'] = 99
        response = client.put('/api/home/settings', json=request_body(revision))
        assert response.status_code == 409
        assert not provider.writes


def test_evicted_request_cannot_write_again_when_configuration_returns_to_old_value():
    client, provider, session = client_with_session()
    idle(provider)
    with client:
        revision = client.get('/api/home/state').json()['revision']
        original = request_body(revision)
        assert client.put('/api/home/settings', json=original).status_code == 200
        idle(provider)
        for index in range(32):
            revision = client.get('/api/home/state').json()['revision']
            noop = request_body(revision, lumi_uids=[WORKER])
            noop['request_id'] = f'synthetic-noop-request-{index:03d}'
            assert client.put('/api/home/settings', json=noop).status_code == 200
        assert original['request_id'] not in session.home_requests
        replay = client.put('/api/home/settings', json=original)
        assert replay.status_code == 409
        assert replay.json()['error']['code'] == 'HOME_STATE_CONFLICT'
        assert len(provider.writes) == 1


@pytest.mark.parametrize('change', [
    {'recipe_id': '999'}, {'lumi_uids': ['123456']}, {'lumi_uids': []},
    {'production_count': 0}, {'production_count': -1}, {'production_count': 1.5},
    {'production_count': True}, {'production_count': 10, 'unexpected': 'synthetic-secret'},
])
def test_invalid_targets_and_values_never_reach_game_provider(change):
    client, provider, _ = client_with_session()
    idle(provider)
    with client:
        revision = client.get('/api/home/state').json()['revision']
        response = client.put('/api/home/settings', json=request_body(revision, **change))
        assert response.status_code == 422
        assert not provider.writes
        assert 'synthetic-secret' not in response.text


def test_uncertain_apply_is_visible_and_blocks_another_automatic_attempt():
    client, provider, _ = client_with_session()
    idle(provider)
    provider.outcome = 'uncertain'
    with client:
        revision = client.get('/api/home/state').json()['revision']
        first = client.put('/api/home/settings', json=request_body(revision))
        assert first.status_code == 200 and first.json()['last_apply']['status'] == 'uncertain'
        state = client.get('/api/home/state').json()
        assert state['last_apply']['status'] == 'uncertain'
        body = request_body(revision, production_count=6)
        body['request_id'] = 'synthetic-request-id-002'
        assert client.put('/api/home/settings', json=body).status_code == 409
        assert len(provider.writes) == 1


def test_same_origin_guard_also_covers_home_mutations():
    client, provider, _ = client_with_session()
    with client:
        response = client.put('/api/home/settings', json=request_body('a' * 64), headers={'Origin': 'https://untrusted.invalid'})
        assert response.status_code == 403 and not provider.writes


def test_ack_without_matching_state_never_becomes_applied():
    client, provider, _ = client_with_session()
    idle(provider)
    provider.update_state = False
    with client:
        revision = client.get('/api/home/state').json()['revision']
        response = client.put('/api/home/settings', json=request_body(revision))
        assert response.status_code == 200
        assert response.json()['last_apply']['status'] == 'uncertain'
        assert response.json()['buildings'][0]['production_count'] == 0


async def test_cancelled_apply_records_uncertainty_before_another_request_can_write():
    from rumi_market.catalog import Catalog
    from rumi_market.home_management import HomeManagementService
    from rumi_market.home_models import HomeSettingsRequest
    from rumi_market.app import DEFAULT_CATALOG
    from rumi_market.sessions import SessionStore
    store = SessionStore(HomeProvider)
    session = store.create()
    role = Role(id='synthetic-role', name='', server_name='测试区服')
    session.view = SessionView(status='connected', authenticated=True, role=role)
    idle(session.provider)
    catalog = Catalog(DEFAULT_CATALOG)
    service = HomeManagementService(catalog, store)
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    session.provider.apply_home_change = cancelled
    try:
        before = await service.state(session)
        body = HomeSettingsRequest.model_validate(request_body(before.revision))
        with pytest.raises(asyncio.CancelledError):
            await service.apply(session, body)
        assert session.home_write_uncertain is True
        assert session.home_last_apply.status == 'uncertain'
        another = request_body(before.revision)
        another['request_id'] = 'synthetic-request-id-003'
        with pytest.raises(ApiError) as error:
            await service.apply(session, HomeSettingsRequest.model_validate(another))
        assert error.value.code == 'HOME_OUTCOME_UNKNOWN'
    finally:
        await store.close()
        catalog.close()


def test_running_batch_blocks_production_changes_but_preserves_worker_controls():
    client, provider, _ = client_with_session()
    with client:
        before = client.get('/api/home/state').json()
        building = before['buildings'][0]
        assert building['editable'] is True
        assert building['production_editable'] is False
        assert building['production_reason']
        response = client.put('/api/home/settings', json=request_body(before['revision']))
        assert response.status_code == 422 and not provider.writes


def test_revision_changes_when_the_same_cookie_logs_in_again():
    client, provider, session = client_with_session()
    idle(provider)
    with client:
        before = client.get('/api/home/state').json()
        session.view.role = Role(id='synthetic-new-login-role', name='', server_name='测试区服')
        after = client.get('/api/home/state').json()
        assert before['revision'] != after['revision']
        assert client.put('/api/home/settings', json=request_body(before['revision'])).status_code == 409
        assert not provider.writes
