import pytest
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.models import Role
from rumi_market.order_service import order_revision
from test_home_management_integration import SyntheticRuntime


@pytest.mark.asyncio
async def test_order_production_reuses_verified_native_home_path_and_respects_stop():
    runtime = SyntheticRuntime()
    provider = IndependentGameProvider(runtime, None, login_verified=True)
    provider._role = Role(id='browser', name='', server_name='test')
    def snapshot():
        return {'role_key': ('7', '123'), 'server_time_seconds': 1000,
            'home': runtime.home_state.snapshot(), 'orders': {'valid': True, 'sequence': 1, 'orders': []},
            'inventory': {'valid': True, 'sequence': 1, 'items': {}}}
    provider._order_snapshot_now = snapshot
    operation = {'action': 'produce', 'building_uid': '12', 'building_id': '10101', 'recipe_id': '1', 'recipe_times': 5}
    rejected = await provider.apply_order_operation(operation, expected_revision=order_revision(snapshot()), stop_requested=lambda: True)
    assert rejected['status'] == 'rejected' and runtime.calls == []
    result = await provider.apply_order_operation(operation, expected_revision=order_revision(snapshot()), stop_requested=lambda: False)
    assert result['status'] == 'applied' and runtime.calls == [3403]
    assert runtime.home_state.snapshot()['architectures'][0]['work_old']['recipe_id'] == 1
    # Existing queues are never stopped or replaced to make another proposal fit.
    result = await provider.apply_order_operation({**operation, 'recipe_times': 6},
        expected_revision=order_revision(snapshot()), stop_requested=lambda: False)
    assert result['status'] == 'rejected' and runtime.calls == [3403]
