"""Independent inspection review: synthetic native collection preconditions."""

from types import SimpleNamespace

import pytest

from rumi_market.game.order_operations import GameOrderOperations, _Rejected
from rumi_market.order_service import order_revision


def snapshot(orders_valid):
    return {'role_key': ('7', '123'), 'server_time_seconds': 1000,
        'orders': {'valid': orders_valid, 'orders': []},
        'inventory': {'valid': True, 'sequence': 1, 'items': {}},
        'home': {'valid': True, 'sequence': 1, 'architectures': [{
            'uid': 12, 'id': 10101, 'work_old': {}, 'cur_recipe': None,
            'has_farm_data': False, 'is_hide': False, 'is_need_cut_ribbon': False,
            'level_up_complete_time': 0, 'store_items': [{'item_id': '10031', 'count': 1}]}]}}


@pytest.mark.parametrize('orders_valid', [False, True])
def test_native_inspection_claim_depends_on_home_and_inventory_not_order_baseline(orders_valid):
    raw = snapshot(orders_valid)
    operations = GameOrderOperations(SimpleNamespace(closed=False), lambda: raw, lambda: True)
    command, ack, fields = operations._context(
        {'action': 'claim', 'building_uid': '12', 'building_id': '10101'},
        order_revision(raw), lambda: False)
    assert (command, ack, fields) == (3025, 3026, {'archi_uid': 12, 'archi_id': 10101})


def test_native_order_completion_still_requires_an_order_baseline():
    raw = snapshot(False)
    operations = GameOrderOperations(SimpleNamespace(closed=False), lambda: raw, lambda: True)
    with pytest.raises(_Rejected):
        operations._context({'action': 'complete', 'slot_id': '0'}, order_revision(raw), lambda: False)


@pytest.mark.parametrize('change', ['deadline', 'clock_unknown', 'inventory_changed', 'stop'])
async def test_actual_native_production_gate_rechecks_inspection_clock_inventory_and_stop(change):
    from rumi_market.game.client import GameClientError
    from rumi_market.game_provider import IndependentGameProvider
    from rumi_market.models import Role, SessionView
    from rumi_market.order_models import OrderSettings
    from rumi_market.order_service import OrderRun, OrderService
    from rumi_market.sessions import Session, SessionStore
    from test_home_management_integration import SyntheticRuntime

    runtime = SyntheticRuntime()
    runtime.result = SimpleNamespace(zone_id=7, role_id=123)
    runtime.server_time_seconds = 1000
    provider = IndependentGameProvider(runtime, None, login_verified=True)
    provider._role = Role(id='synthetic-browser', name='', server_name='test')
    inventory = {'valid': True, 'sequence': 1, 'items': {'20000': 10, '20001': 50}}

    def current():
        return {'role_key': ('7', '123'), 'server_time_seconds': runtime.server_time_seconds,
                'orders': {'valid': True, 'orders': []}, 'inventory': inventory,
                'home': runtime.home_state.snapshot()}

    provider._order_snapshot_now = current
    session = Session('synthetic-inspection-native-review', provider,
        view=SessionView(status='connected', authenticated=True, role=provider._role))
    service = OrderService(SessionStore(lambda: provider), catalog={'recipes': [], 'names': {}})
    initial = current()
    run = OrderRun(('7', '123'), 'synthetic-review-request', order_revision(initial), OrderSettings(), 1001)
    service.roles[run.role_key] = run
    original = runtime.request

    async def before_transport_send(command, ack, payload, *, before_send):
        if change == 'deadline':
            runtime.server_time_seconds = run.deadline
        elif change == 'clock_unknown':
            runtime.server_time_seconds = None
        elif change == 'inventory_changed':
            inventory['items']['20001'] = 0
            inventory['sequence'] += 1
        else:
            run.request_stop()
        if not before_send():
            raise GameClientError('GAME_REQUEST_NOT_SENT')
        return await original(command, ack, payload, before_send=before_send)

    runtime.request = before_transport_send
    try:
        result = await provider.apply_order_operation({'action': 'produce', 'building_uid': '12',
            'building_id': '10101', 'recipe_id': '1', 'recipe_times': 5},
            expected_revision=order_revision(initial),
            stop_requested=lambda: service._inspection_stopped(session, run, initial))
        assert result['status'] == 'rejected'
        assert runtime.calls == []
    finally:
        await provider.close()
