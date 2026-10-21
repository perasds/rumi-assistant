import asyncio

import pytest

from providers import ScriptedProvider
from test_market import clients, connect
from test_rush_purchase import Provider, listing
from rumi_market.providers import MarketPage


class PurchaseProvider(Provider, ScriptedProvider):
    def __init__(self, pages=None):
        ScriptedProvider.__init__(self, pages)
        Provider.__init__(self, pages)


def body(**changes):
    value = dict(request_id='synthetic-http-request-0001', pet_ids=['101'],
                 max_price_gold=100, quantity_per_pet=1, min_balance_gold=0)
    return {**value, **changes}


async def terminal(client):
    for _ in range(60):
        response = await client.get('/api/rush-purchase')
        assert response.status_code == 200
        if response.json()['status'] not in ('running', 'stopping'):
            return response.json()
        await asyncio.sleep(0)
    raise AssertionError('purchase job did not settle')


async def test_purchase_endpoints_require_login_and_do_not_create_tasks(catalog_path):
    provider = PurchaseProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        assert (await client.get('/api/rush-purchase')).status_code == 401
        assert (await client.post('/api/rush-purchase/start', json=body())).status_code == 401
        assert (await client.post('/api/rush-purchase/stop', json={'job_id': 'unknown'})).status_code == 401
        assert not provider.calls and not provider.purchases


async def test_completed_job_replay_never_buys_again_or_exposes_raw_ids(catalog_path):
    provider = PurchaseProvider([MarketPage([listing('private-native-listing')], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        idle = (await client.get('/api/rush-purchase')).json()
        assert idle['status'] == 'idle' and idle['balance_gold'] == '1000'
        response = await client.post('/api/rush-purchase/start', json=body())
        assert response.status_code == 200
        final = await terminal(client)
        assert final['status'] == 'completed' and final['counts'] == {'101': 1}
        assert final['spent_gold'] == '10'
        repeated = await client.post('/api/rush-purchase/start', json=body())
        assert repeated.status_code == 200 and repeated.json()['job_id'] == final['job_id']
        assert provider.purchases == ['private-native-listing']
        assert 'private-native' not in repeated.text


@pytest.mark.parametrize('omit_selection', [False, True])
async def test_empty_or_omitted_selection_starts_all_pets_without_auto_completion(catalog_path, omit_selection):
    provider = PurchaseProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        request = body(pet_ids=[])
        if omit_selection:
            request.pop('pet_ids')
        first = await client.post('/api/rush-purchase/start', json=request)
        assert first.status_code == 200
        assert first.json()['settings']['pet_ids'] == []
        repeated = await client.post('/api/rush-purchase/start', json=request)
        assert repeated.json()['job_id'] == first.json()['job_id']
        current = (await client.get('/api/rush-purchase')).json()
        assert current['status'] == 'running' and current['counts'] == {}
        assert (await client.post('/api/rush-purchase/stop', json={'job_id': current['job_id']})).status_code == 200
        assert (await terminal(client))['status'] == 'stopped'


async def test_all_pets_returns_confirmed_counts_for_types_missing_from_catalog(catalog_path):
    provider = PurchaseProvider([MarketPage([listing('private-native-listing', '999')], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        start = await client.post('/api/rush-purchase/start', json=body(pet_ids=[]))
        assert start.status_code == 200
        for _ in range(30):
            current = (await client.get('/api/rush-purchase')).json()
            if current['counts'].get('999') == 1:
                break
            await asyncio.sleep(0)
        assert current['counts'] == {'999': 1} and current['status'] == 'completed'
        assert 'private-native' not in str(current)
        assert (await terminal(client))['counts'] == {'999': 1}


async def test_unselected_quantity_is_total_and_replay_does_not_send_another_purchase(catalog_path):
    provider = PurchaseProvider([MarketPage([
        listing('private-a'), listing('private-b'), listing('private-c', '999'), listing('private-extra', '102'),
    ], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        request = body(pet_ids=[], quantity_per_pet=3)
        started = await client.post('/api/rush-purchase/start', json=request)
        assert started.status_code == 200 and started.json()['settings']['quantity_per_pet'] == 3
        final = await terminal(client)
        assert final['status'] == 'completed' and final['counts'] == {'101': 2, '999': 1}
        assert final['attempts'] == 3
        repeated = await client.post('/api/rush-purchase/start', json=request)
        assert repeated.status_code == 200 and repeated.json()['job_id'] == final['job_id']
        assert provider.purchases == ['private-a', 'private-b', 'private-c']
        changed = await client.post('/api/rush-purchase/start', json=body(pet_ids=[], quantity_per_pet=4))
        assert changed.status_code == 409


async def test_selected_legacy_quantity_is_one_each_in_api_state_and_idempotency(catalog_path):
    provider = PurchaseProvider([MarketPage([
        listing('private-a'), listing('private-another'), listing('private-b', '102'), listing('private-extra', '102'),
    ], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        request = body(pet_ids=['101', '102'], quantity_per_pet=7)
        started = await client.post('/api/rush-purchase/start', json=request)
        assert started.status_code == 200 and started.json()['settings']['quantity_per_pet'] == 1
        final = await terminal(client)
        assert final['status'] == 'completed' and final['counts'] == {'101': 1, '102': 1}
        repeated = await client.post('/api/rush-purchase/start', json={**request, 'quantity_per_pet': 1})
        assert repeated.status_code == 200 and repeated.json()['job_id'] == final['job_id']
        assert provider.purchases == ['private-a', 'private-b']


async def test_sessions_cannot_read_or_stop_each_others_job(catalog_path):
    async with clients(catalog_path, PurchaseProvider) as (one, two):
        await connect(one, 'one')
        await connect(two, 'two')
        first = (await one.post('/api/rush-purchase/start', json=body())).json()
        assert (await two.get('/api/rush-purchase')).json()['status'] == 'idle'
        assert (await two.post('/api/rush-purchase/stop', json={'job_id': first['job_id']})).status_code == 409
        assert (await one.get('/api/rush-purchase')).json()['status'] == 'running'
        await one.post('/api/rush-purchase/stop', json={'job_id': first['job_id']})
        assert (await terminal(one))['status'] == 'stopped'


async def test_start_and_stop_obey_same_origin_guard(catalog_path):
    provider = PurchaseProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        response = await client.post('/api/rush-purchase/start', json=body(),
                                     headers={'Origin': 'https://example.invalid'})
        assert response.status_code == 403 and not provider.purchases
        assert (await client.get('/api/rush-purchase')).json()['status'] == 'idle'


@pytest.mark.parametrize('change', [dict(max_price_gold=True), dict(quantity_per_pet=0), dict(quantity_per_pet=-1),
                                  dict(quantity_per_pet=True),
                                  dict(pet_ids=['999']), dict(password='synthetic-private')])
async def test_invalid_purchase_input_is_rejected_without_echoing_values(catalog_path, change):
    provider = PurchaseProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        response = await client.post('/api/rush-purchase/start', json=body(**change))
        assert response.status_code == 422
        assert 'synthetic-private' not in response.text and not provider.purchases


async def test_logout_clears_task_and_a_new_login_does_not_restart_it(catalog_path):
    providers = []
    def factory():
        provider = PurchaseProvider()
        providers.append(provider)
        return provider
    async with clients(catalog_path, factory) as (client, _):
        await connect(client)
        await client.post('/api/rush-purchase/start', json=body())
        assert (await client.delete('/api/session')).status_code == 200
        assert (await client.get('/api/rush-purchase')).status_code == 401
        await connect(client)
        assert (await client.get('/api/rush-purchase')).json()['status'] == 'idle'
        assert not any(provider.purchases for provider in providers)
