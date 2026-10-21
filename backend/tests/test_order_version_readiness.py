"""Unsupported protocols must not be presented as an unfinished login sync."""

import pytest

from test_order_service import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,expected', [
    ('ORDER_VERSION_UNVERIFIED', '当前游戏版本尚未验证，订单功能暂不可用'),
    ('ORDER_SCHEMA_UNAVAILABLE', '订单协议加载失败，请联系维护'),
    ('ORDER_STATE_INVALID', '订单数据更新异常，请重新登录'),
    ('ORDER_BASELINE_MISSING', '订单尚未同步完成'),
])
async def test_order_readiness_explains_why_the_baseline_is_unavailable(reason, expected):
    service, session, provider = setup()
    provider.raw['orders'].update(valid=False, unavailable_reason=reason)
    state = await service.state(session)
    assert not state['execution_ready']
    assert state['message'] == state['execution_reason'] == expected
    assert provider.writes == []
