"""Exercise HTTP -> real provider -> native codec -> live HomeState without networking."""
import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.auth import COOKIE_NAME
from rumi_market.game.envelope import ResponseEnvelope
from rumi_market.game.home_codec import HomeProtoCodec
from rumi_market.game.home_state import HomeState
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.models import Role, SessionView
from rumi_market.sessions import Session
from login_helpers import login_body


UID1, UID2 = 9007199254740993, 9007199254740994


class SyntheticRuntime:
    def __init__(self):
        self.can_query = True
        self.home_state = HomeState(verified_version=True)
        self.codec = HomeProtoCodec()
        self.architecture = {'architecture_uid': 12, 'architecture_id': 10101, 'architecture_level': 1,
            'work_old': {'lumi_uid': UID1, 'lumi_id': 102601, 'recipe_id': 0,
                         'recipe_intend_times': 0, 'recipe_complete_times': 0}}
        self.calls = []
        self.home_client = self
        self.closed = False
        self.observe(2971, lumi_list=[{'lumi_uid': UID1, 'lumi_id': 102601, 'lumi_state': 2},
                                     {'lumi_uid': UID2, 'lumi_id': 102601, 'lumi_state': 0}])
        self.observe(2913)
        self.observe(3001, architecture_list=[self.architecture])
        self.observe(10093, info={'id': 77})

    def observe(self, command, **fields):
        self.home_state.observe(ResponseEnvelope(command, 0, 1, 0, 0, self.codec.encode(command, **fields)))

    async def request(self, command, response_command, payload, *, before_send):
        assert before_send() is True
        request = self.codec.decode(command, payload)
        assert request.archi_uid == 12 and request.archi_id == 10101 and request.op == 1
        self.calls.append(command)
        ack = {'archi_uid': request.archi_uid, 'archi_id': request.archi_id, 'op': request.op}
        if command == 3401:
            assert request.lumi_uid == UID2
            self.architecture['work_old']['lumi_uid'] = request.lumi_uid
            ack['lumi_uid'] = request.lumi_uid
        else:
            assert command == 3403 and request.recipe_id == 1 and request.recipe_times == 5
            self.architecture['work_old'].update(recipe_id=request.recipe_id, recipe_intend_times=request.recipe_times)
        # The receive observer can deliver the real notification before ACK correlation.
        self.observe(3003, change_list=[{'change_type': 5, 'architecture_info': copy.deepcopy(self.architecture)}])
        return ResponseEnvelope(response_command, 1, 0, 0, 0, self.codec.encode(response_command, **ack))

    async def close(self):
        self.can_query = False
        self.closed = True
        self.home_state.close()

    async def start(self, account, password):
        return SimpleNamespace(role_id=123, zone_name='synthetic zone',
            needs_activation=False, needs_initial_pet_selection=False)


@pytest.mark.parametrize('replace_worker', [False, True])
@pytest.mark.parametrize('live_factory', [False, True])
def test_http_apply_uses_native_commands_and_requires_actual_state_confirmation(replace_worker, live_factory):
    runtime = SyntheticRuntime()
    if live_factory:
        from rumi_market.live_app import create_live_app
        from rumi_market.live_runtime import LiveProviderFactory
        async def builder():
            return runtime, SimpleNamespace(items=(), normal_pet_mapping={})
        app = create_live_app(provider_factory=LiveProviderFactory(runtime_builder=builder))
    else:
        provider = IndependentGameProvider(runtime, None, login_verified=True)
        role = Role(id='synthetic-integrated-role', name='', server_name='测试区服')
        provider._role = role
        app = create_app()
        session = Session('synthetic-integrated-session', provider,
                          view=SessionView(status='connected', authenticated=True, role=role))
        app.state.sessions.sessions[session.id] = session
    with TestClient(app) as client:
        if live_factory:
            response = client.post('/api/auth/login', json=login_body(client, 'synthetic-account', 'synthetic-password'))
            assert response.status_code == 200 and response.json()['status'] == 'connected'
        else:
            client.cookies.set(COOKIE_NAME, session.id)
        before = client.get('/api/home/state')
        assert before.status_code == 200
        state = before.json()
        assert state['roster'][1]['lumi_uid'] == str(UID2)
        assert state['roster'][1]['eligible_building_uids'] == ['12']
        change = {'building_uid': '12', 'recipe_id': '1', 'production_count': 5}
        if replace_worker:
            change['lumi_uids'] = [str(UID2)]
        body = {'request_id': 'synthetic-integrated-001', 'expected_revision': state['revision'], 'changes': [change]}
        response = client.put('/api/home/settings', json=body)
        assert response.status_code == 200
        after = response.json()
        assert after['last_apply']['status'] == 'applied'
        assert after['buildings'][0]['current_recipe_id'] == '1'
        assert after['buildings'][0]['production_count'] == 5
        assert after['buildings'][0]['assigned_lumi_uids'] == [str(UID2 if replace_worker else UID1)]
        assert runtime.calls == ([3401, 3403] if replace_worker else [3403])
        assert client.put('/api/home/settings', json=body).json()['last_apply']['status'] == 'applied'
        assert len(runtime.calls) == (2 if replace_worker else 1)
