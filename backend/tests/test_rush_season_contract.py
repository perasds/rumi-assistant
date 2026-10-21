"""HTTP contracts for the opt-in current-season rush scope, using synthetic providers."""

import asyncio
from copy import deepcopy

import pytest

from rumi_market.errors import ApiError
from rumi_market.providers import MarketPage
from test_market import clients, connect
from test_rush_purchase import listing
from test_rush_purchase_api import PurchaseProvider, body, terminal


class Tags:
    values = {'101': 2, '102': 3, '104': 1, '105': 3}

    def ids_for(self, season):
        return frozenset(pet for pet, tag in self.values.items() if tag == season + 1)

    def tag_for(self, pet):
        return self.values.get(pet)


@pytest.fixture(autouse=True)
def public_season_tags(monkeypatch):
    monkeypatch.setattr('rumi_market.rush.SeasonPetCatalog', Tags)


class SeasonalPurchaseProvider(PurchaseProvider):
    def __init__(self, pages=None):
        super().__init__(pages)
        self.season = {'valid': True, 'current_season_id': 2}

    @property
    def season_snapshot(self):
        return deepcopy(self.season)


def season_body(**changes):
    return {**body(pet_ids=[], quantity_mode='total', quantity_per_pet=3, current_season_only=True), **changes}


@pytest.mark.parametrize('changes', [
    {'pet_ids': ['101']}, {'pet_quantities': {'101': 1}},
    {'quantity_mode': 'custom'}, {'quantity_mode': 'each_once'},
    {'current_season_only': 'true'}, {'current_season_only': 1}, {'current_season_only': None},
    {'target_season_id': 1},
])
async def test_http_rejects_mixed_or_forged_season_scope_without_creating_a_job(catalog_path, changes):
    provider = SeasonalPurchaseProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        response = await client.post('/api/rush-purchase/start', json=season_body(**changes))
        assert response.status_code == 422
        current = (await client.get('/api/rush-purchase')).json()
        assert current['status'] == 'idle' and current['job_id'] is None
        assert not provider.calls and not provider.purchases


@pytest.mark.parametrize('season', [None, {'valid': False, 'current_season_id': 2},
    {'valid': True, 'current_season_id': 0}, {'valid': True, 'current_season_id': True},
    {'valid': True, 'current_season_id': 9}])
async def test_http_unknown_or_unsupported_season_never_falls_back_to_s1(catalog_path, season):
    provider = SeasonalPurchaseProvider()
    provider.season = season
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        response = await client.post('/api/rush-purchase/start', json=season_body())
        assert response.status_code == 409
        assert response.json()['error']['code'] in {'SEASON_STATE_UNAVAILABLE', 'SEASON_PETS_UNAVAILABLE'}
        current = (await client.get('/api/rush-purchase')).json()
        assert current['status'] == 'idle' and current['season_ready'] is False
        assert current['target_season_id'] is None
        assert not provider.calls and not provider.purchases


async def test_http_total_and_concurrent_replay_keep_the_original_target_season(catalog_path):
    provider = SeasonalPurchaseProvider([MarketPage([
        listing('synthetic-old', '101'), listing('synthetic-mainline', '104'), listing('synthetic-unmapped', '999'),
        listing('synthetic-one', '102'), listing('synthetic-two', '102'), listing('synthetic-three', '105'),
        listing('synthetic-extra', '105'),
    ], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        request = season_body(quantity_mode=None)
        first, concurrent = await asyncio.gather(
            client.post('/api/rush-purchase/start', json=request),
            client.post('/api/rush-purchase/start', json=request))
        assert first.status_code == concurrent.status_code == 200
        assert first.json()['job_id'] == concurrent.json()['job_id']
        final = await terminal(client)
        assert final['status'] == 'completed' and final['counts'] == {'102': 2, '105': 1}
        assert final['attempts'] == 3 and final['spent_gold'] == '30'
        assert final['target_season_id'] == 2
        assert final['settings']['quantity_mode'] == 'total' and final['settings']['pet_ids'] == []
        assert final['settings']['pet_quantities'] == {}
        provider.season['current_season_id'] = 1
        replay = await client.post('/api/rush-purchase/start', json=request)
        assert replay.status_code == 200 and replay.json()['job_id'] == final['job_id']
        assert replay.json()['target_season_id'] == 2 and replay.json()['current_season_id'] == 1
        assert replay.json()['counts'] == final['counts']
        conflict = await client.post('/api/rush-purchase/start', json={**request, 'quantity_per_pet': 4})
        assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'RUSH_REQUEST_CONFLICT'
        assert provider.purchases == ['synthetic-one', 'synthetic-two', 'synthetic-three']


@pytest.mark.parametrize('change_at', ['after_page', 'before_send'])
@pytest.mark.parametrize('invalidate', [False, True])
async def test_http_season_change_or_expiry_blocks_the_purchase_callback(catalog_path, change_at, invalidate):
    class ChangingProvider(SeasonalPurchaseProvider):
        def change(self):
            self.season = {'valid': not invalidate, 'current_season_id': 1 if not invalidate else 2}

        async def purchase_page(self, cursor):
            result = await super().purchase_page(cursor)
            if change_at == 'after_page':
                self.change()
            return result

        async def purchase_color(self, row, **options):
            if change_at == 'before_send':
                self.change()
            return await super().purchase_color(row, **options)

    provider = ChangingProvider([MarketPage([listing('synthetic-blocked', '102')], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        assert (await client.post('/api/rush-purchase/start', json=season_body())).status_code == 200
        final = await terminal(client)
        assert final['status'] == 'stopped' and final['target_season_id'] == 2
        assert final['counts'] == {} and final['spent_gold'] == '0'
        assert '赛季状态已变化' in final['message']
        assert not provider.purchases


async def test_http_unknown_purchase_outcome_still_locks_season_mode_and_never_retries(catalog_path):
    provider = SeasonalPurchaseProvider([MarketPage([listing('synthetic-unknown', '102')], complete=True)])
    provider.failure = ApiError('PURCHASE_OUTCOME_UNKNOWN', '', 409)
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        request = season_body()
        assert (await client.post('/api/rush-purchase/start', json=request)).status_code == 200
        final = await terminal(client)
        assert final['status'] == 'uncertain' and final['attempts'] == 1 and final['counts'] == {}
        replay = await client.post('/api/rush-purchase/start', json=request)
        assert replay.status_code == 200 and replay.json()['status'] == 'uncertain'
        fresh = await client.post('/api/rush-purchase/start', json={**request, 'request_id': 'synthetic-new-request-0002'})
        assert fresh.status_code == 409 and fresh.json()['error']['code'] == 'PURCHASE_OUTCOME_UNKNOWN'
        assert provider.calls == [None] and not provider.purchases


async def test_http_legacy_default_still_buys_without_any_season_snapshot(catalog_path):
    provider = PurchaseProvider([MarketPage([listing('synthetic-default')], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        started = await client.post('/api/rush-purchase/start', json=body(pet_ids=[]))
        assert started.status_code == 200
        assert started.json()['settings']['current_season_only'] is False
        assert started.json()['season_ready'] is False and started.json()['current_season_id'] is None
        assert (await terminal(client))['status'] == 'completed'
        assert provider.purchases == ['synthetic-default']
