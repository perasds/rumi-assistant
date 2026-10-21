import pytest

from test_home_state import state, full, arch


def test_order_claimable_store_distinguishes_missing_empty_base_and_extra():
    tracker = state()
    tracker.observe(full(arch(9), arch(10, store_info={}), arch(11, store_info={
        'base_rewards': [{'item_id': 21001, 'count': 3}],
        'extra_rewards': [{'item_id': 21001, 'count': 2}]})))
    rows = tracker.snapshot()['architectures']
    assert rows[0]['store_items'] is None
    assert rows[1]['store_items'] == []
    assert rows[2]['store_items'] == [{'item_id': '21001', 'count': 3}]
    assert rows[2]['store_extra_items'] == [{'item_id': '21001', 'count': 2}]
    assert rows[2].get('capacity_remaining') is None  # buff-adjusted limit is not in this notification


@pytest.mark.parametrize('reward', [{'count': 1}, {'item_id': 21001, 'count': -1}])
def test_invalid_store_item_or_negative_quantity_invalidates_home(reward):
    tracker = state()
    tracker.observe(full(arch(9, store_info={'base_rewards': [reward]})))
    assert not tracker.snapshot()['valid']


def test_live_provider_combines_confirmed_capacity_bonus_and_base_store_only():
    from types import SimpleNamespace
    from rumi_market.game.home_capacity_state import HomeCapacityState
    from rumi_market.game_provider import IndependentGameProvider
    from rumi_market.models import Role
    from test_home_capacity_state import full as buff_full, buff
    tracker = state()
    tracker.observe(full(arch(9, store_info={'base_rewards': [{'item_id': 21001, 'count': 3}],
                                         'extra_rewards': [{'item_id': 21001, 'count': 2}]})))
    capacity = HomeCapacityState(True)
    runtime = SimpleNamespace(can_query=True, result=SimpleNamespace(zone_id=7, role_id=123),
        server_time_seconds=1000, home_state=tracker, home_capacity_state=capacity, inventory_state=None, order_state=None)
    provider = IndependentGameProvider(runtime, None, login_verified=True)
    provider._role = Role(id='browser', name='', server_name='test')
    assert provider._order_snapshot_now()['home']['architectures'][0].get('capacity_remaining') is None
    capacity.observe(buff_full(buff(stage=1)))
    assert provider._order_snapshot_now()['home']['architectures'][0]['capacity_remaining'] == 497
    capacity.observe(buff_full())
    assert provider._order_snapshot_now()['home']['architectures'][0]['capacity_remaining'] == 197
