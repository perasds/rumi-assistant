import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from rumi_market.errors import ApiError
from rumi_market.providers import MarketPage
from rumi_market.rush import RushPurchaseService, RushSettings
from test_rush_purchase import Provider, listing, settings, setup, settled, cleanup


class Tags:
    values = {'101': 2, '102': 3, '104': 1, '105': 3}
    def tag_for(self, pet):
        return self.values.get(pet)
    def ids_for(self, season):
        return frozenset(pet for pet, tag in self.values.items() if tag == season + 1)


class SeasonalProvider(Provider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.season = {'valid': True, 'current_season_id': 2}
    @property
    def season_snapshot(self):
        return deepcopy(self.season)


def seasonal_settings(**changes):
    return settings(pet_ids=[], quantity_mode='total', current_season_only=True, **changes)


@pytest.mark.parametrize('change', [
    {'pet_ids': ['101']}, {'quantity_mode': 'each_once'}, {'quantity_mode': 'custom'},
    {'pet_quantities': {'101': 1}}, {'current_season_only': 'true'}, {'current_season_only': 1},
])
def test_season_mode_rejects_mixed_custom_selection_and_non_total_quantity(change):
    body = dict(pet_ids=[], max_price_gold=100, min_balance_gold=0, quantity_per_pet=2,
                current_season_only=True, quantity_mode='total')
    body.update(change)
    with pytest.raises(ValidationError):
        RushSettings(**body)


async def make(catalog_path, provider):
    _, session, store, catalog = await setup(catalog_path, provider)
    return RushPurchaseService(catalog, store, season_catalog=Tags(), interval=30), session, store, catalog


async def test_current_season_filters_actual_ids_and_purchases_only_the_total(catalog_path):
    provider = SeasonalProvider([MarketPage([
        listing('old-season', '101'), listing('mainline', '104'), listing('unmapped', '999'),
        listing('current-one', '102'), listing('current-two', '102'), listing('current-three', '105')], complete=True)])
    service, session, store, catalog = await make(catalog_path, provider)
    try:
        value = service.start(session, seasonal_settings(quantity_per_pet=2))
        assert value.target_season_id == 2 and value.season_ready
        result = await settled(service, session)
        assert result.status == 'completed' and result.counts == {'102': 2}
        assert provider.purchases == ['current-one', 'current-two']
        assert result.settings.pet_ids == [] and result.settings.quantity_mode == 'total'
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('season', [None, {'valid': False, 'current_season_id': 2},
    {'valid': True, 'current_season_id': None}, {'valid': True, 'current_season_id': 0},
    {'valid': True, 'current_season_id': True}, {'valid': True, 'current_season_id': 9}])
async def test_unknown_inactive_or_unadapted_season_never_starts_or_falls_back_to_s1(catalog_path, season):
    provider = SeasonalProvider()
    provider.season = season
    service, session, store, catalog = await make(catalog_path, provider)
    try:
        with pytest.raises(ApiError):
            service.start(session, seasonal_settings())
        assert session.rush is None and provider.purchases == [] and provider.calls == []
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('change_at', ['after_page', 'before_send'])
async def test_season_change_after_page_or_before_native_send_stops_without_buying(catalog_path, change_at):
    class ChangingProvider(SeasonalProvider):
        async def purchase_page(self, cursor):
            page = await super().purchase_page(cursor)
            if change_at == 'after_page':
                self.season['current_season_id'] = 1
            return page
        async def purchase_color(self, row, **kwargs):
            if change_at == 'before_send':
                self.season['valid'] = False
            return await super().purchase_color(row, **kwargs)
    provider = ChangingProvider([MarketPage([listing('current', '102')], complete=True)])
    service, session, store, catalog = await make(catalog_path, provider)
    try:
        service.start(session, seasonal_settings())
        result = await settled(service, session)
        assert result.status == 'stopped' and result.target_season_id == 2
        assert provider.purchases == []
    finally:
        await cleanup(store, catalog)


async def test_missing_season_does_not_block_ordinary_unrestricted_mode(catalog_path):
    provider = Provider([MarketPage([listing('ordinary', '101')], complete=True)])
    service, session, store, catalog = await make(catalog_path, provider)
    try:
        value = service.start(session, settings(pet_ids=[], quantity_mode='total'))
        assert value.settings.current_season_only is False
        assert (await settled(service, session)).status == 'completed'
        assert provider.purchases == ['ordinary']
    finally:
        await cleanup(store, catalog)
