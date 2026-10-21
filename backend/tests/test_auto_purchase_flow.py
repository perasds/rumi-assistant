"""Full ASGI -> runtime -> wire -> balance path, with synthetic transports only."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from rumi_market.app import create_app
from rumi_market.game.proto import GameProtoCodec
from rumi_market.game_provider import IndependentGameProvider
from test_game_profile_sync import message
from test_game_runtime import PRIVATE, setup
from login_helpers import async_login_body


@pytest.mark.parametrize(('pet_ids', 'quantity'), [(['101'], 1), ([], 1), (['101'], 7)])
@pytest.mark.parametrize('hot_version', [62, 64, 65])
@pytest.mark.parametrize('ack_extras', [{'lumi_id': 101, 'lumi_uid': 2**63 + 9}, {}], ids=['full-ack', 'order-uid-only'])
async def test_explicit_start_uses_fresh_detail_price_and_waits_for_actual_gold_notification(catalog_path, pet_ids, quantity, hot_version, ack_extras):
    runtime, game, _, _, version, _ = setup(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    original_fetch = version.fetch

    async def fetch_version():
        return replace(await original_fetch(), hot_version=hot_version)

    version.fetch = fetch_version
    provider = IndependentGameProvider(runtime, SimpleNamespace(), login_verified=True)
    codec = GameProtoCodec()
    commands = []
    async def next_request(expected):
        request = await game.next_request()
        commands.append(request.ReqCmdId)
        assert request.ReqCmdId == expected
        return request

    async def server():
        login = await next_request(2901)
        game.respond(2902, login.ReqCmdSeq, codec.encode('SNKProto.Cmd_Net_LumiLogin_Ack', {'role_id': 123}))
        enter = await next_request(2911)
        game.respond(2912, enter.ReqCmdSeq, codec.encode('SNKProto.Cmd_Role_EnterGame_Ack', {
            'zone_id': 7, 'zone_name': 'synthetic-zone', 'server_time_ms': 1700000000000}))
        game.respond(2941, 0, message('Cmd_Notify_ItemAll', item_list=[
            {'item_id': 10001, 'item_uid': 0, 'count': 1000}]).SerializeToString(), notify=1)
        game.respond(2913, 0, codec.encode('SNKProto.Cmd_Notify_LoginEnd', {}), notify=2)
        query = await next_request(5325)
        fields = codec.decode('SNKProto.Cmd_Market_Auction_QueryLumiDetail_Req', query.ReqData).message
        assert (fields.lumi_id, fields.order_type, fields.desc) == (0, 3, False)
        game.respond(5326, query.ReqCmdSeq, codec.encode('SNKProto.Cmd_Market_Auction_QueryLumiDetail_Ack', {
            'course': '', 'lumi_list': [{'uid': 'synthetic-private-listing', 'lumi_id': 101, 'fixed_price': 60}]}))
        watch = await next_request(5311)
        game.respond(5312, watch.ReqCmdSeq, codec.encode('SNKProto.Cmd_Market_Auction_Watch_Start_Ack', {
            'uid': 'synthetic-private-listing', 'lumi_info': {'uid': 'synthetic-private-listing',
                'lumi_id': 101, 'sell_role_id': 999, 'is_sold_out': False, 'fixed_price': 55,
                'up_time': 1700000000, 'expire_seconds': 3600, 'extern_seconds': 0}}))
        buy = await next_request(5319)
        purchase = codec.decode('SNKProto.Cmd_Market_Auction_Buy_Req', buy.ReqData).message
        assert (purchase.uid, purchase.sell_role_id, purchase.price) == ('synthetic-private-listing', 999, 55)
        game.respond(5320, buy.ReqCmdSeq, codec.encode('SNKProto.Cmd_Market_Auction_Buy_Ack', {
            'uid': purchase.uid, **ack_extras}))
        changed = message('CmdItemReal', item_id=10001, item_uid=0, count=945)
        game.respond(2942, 0, message('Cmd_Notify_ItemChange', change_list=[{
            'change_type': 3, 'data': changed.SerializeToString()}]).SerializeToString(), notify=3)
        stop = await next_request(5313)
        game.respond(5314, stop.ReqCmdSeq)

    app = create_app(provider_factory=lambda: provider, catalog_path=catalog_path)
    server_task = asyncio.create_task(server())
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                body = await async_login_body(client, 'synthetic-account', PRIVATE)
                login = await client.post('/api/auth/login', json=body)
                assert login.status_code == 200 and login.json()['authenticated']
                assert (await client.get('/api/rush-purchase')).json()['balance_gold'] == '1000'
                # A state read never sends a market query or purchase.
                assert commands == [2901, 2911]
                start = await client.post('/api/rush-purchase/start', json={
                    'request_id': 'synthetic-complete-flow-0001', 'pet_ids': pet_ids,
                    'max_price_gold': 100, 'quantity_per_pet': quantity, 'min_balance_gold': 900})
                assert start.status_code == 200
                await asyncio.wait_for(server_task, 2)
                for _ in range(50):
                    state = (await client.get('/api/rush-purchase')).json()
                    if state['status'] != 'running':
                        break
                    await asyncio.sleep(0)
                assert state['status'] == 'completed' and state['counts'] == {'101': 1}
                assert state['settings']['quantity_per_pet'] == 1
                assert state['balance_gold'] == '945' and state['spent_gold'] == '55'
                assert 'synthetic-private' not in str(state)
                assert commands == [2901, 2911, 5325, 5311, 5319, 5313]
    finally:
        server_task.cancel()
        await runtime.close()
