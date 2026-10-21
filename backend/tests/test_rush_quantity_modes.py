"""Quantity modes preserve legacy requests and stop at confirmed targets."""

import asyncio
from types import SimpleNamespace

import pytest

from rumi_market.errors import ApiError
from rumi_market.providers import MarketPage
from test_market import clients, connect
from test_rush_purchase import Provider, cleanup, listing, settings, settled, setup
from test_rush_purchase_api import PurchaseProvider, body, terminal


def mode_settings(mode, **changes):
    values = dict(pet_ids=['101', '102'], quantity_mode=mode, quantity_per_pet=3)
    if mode == 'custom':
        values['pet_quantities'] = {'101': 2, '102': 1}
    return settings(**(values | changes))


def test_quantity_modes_preserve_legacy_and_normalize_irrelevant_legacy_quantity():
    legacy = settings(pet_ids=['101', '102'], quantity_per_pet=7)
    assert legacy.quantity_mode is None and legacy.quantity_per_pet == 1
    assert legacy.pet_quantities == {}
    unrestricted = settings(pet_ids=[], quantity_per_pet=7)
    assert unrestricted.quantity_mode is None and unrestricted.quantity_per_pet == 7
    assert mode_settings('total').quantity_per_pet == 3
    assert mode_settings('each_once').quantity_per_pet == 1
    custom = mode_settings('custom', quantity_per_pet=7)
    assert custom.quantity_per_pet == 1 and custom.pet_quantities == {'101': 2, '102': 1}


@pytest.mark.parametrize('change', [
    {'quantity_mode': 'unsupported'},
    {'quantity_mode': 'each_once', 'pet_ids': []},
    {'quantity_mode': 'each_once', 'pet_quantities': {'101': 1}},
    {'quantity_mode': 'total', 'pet_quantities': {'101': 1}},
    {'quantity_mode': None, 'pet_quantities': {'101': 1}},
    {'quantity_mode': 'custom', 'pet_ids': [], 'pet_quantities': {}},
    {'quantity_mode': 'custom', 'pet_quantities': {}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': 1, '102': 1, '103': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'00101': 1, '102': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': 0, '102': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': -1, '102': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': True, '102': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': 1.5, '102': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': '2', '102': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': 2**53, '102': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': 2**53 - 1, '102': 1}},
])
def test_incompatible_modes_and_invalid_individual_quantities_are_rejected(change):
    with pytest.raises(ValueError):
        mode_settings('total', **change)


async def test_selected_total_can_fill_with_one_type_and_never_queries_later_page(catalog_path):
    provider = Provider([MarketPage([
        listing('private-other', '104'), listing('private-a'), listing('private-a'),
        listing('private-b'), listing('private-c'), listing('private-extra', '102'),
    ], next_cursor='must-not-query')])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, mode_settings('total'))
        state = await settled(service, session)
        assert state.status == 'completed' and state.counts == {'101': 3, '102': 0}
        assert state.spent_gold == '30' and state.attempts == 3
        assert provider.purchases == ['private-a', 'private-b', 'private-c']
        assert provider.calls == [None]
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('selected', [[], ['101', '102']])
async def test_explicit_total_one_stops_after_exactly_one_purchase(catalog_path, selected):
    provider = Provider([MarketPage([
        listing('private-a'), listing('private-b', '102'),
    ], next_cursor='must-not-query')])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, mode_settings('total', pet_ids=selected, quantity_per_pet=1))
        state = await settled(service, session)
        assert state.status == 'completed' and sum(state.counts.values()) == 1
        assert provider.purchases == ['private-a'] and provider.calls == [None]
    finally:
        await cleanup(store, catalog)


async def test_custom_targets_skip_fulfilled_type_and_duplicates_across_pages(catalog_path):
    provider = Provider([
        MarketPage([listing('private-a'), listing('private-a'), listing('private-b'),
                    listing('private-full-type')], next_cursor='private-page-2'),
        MarketPage([listing('private-a'), listing('private-b'), listing('private-extra'),
                    listing('private-other', '104'), listing('private-c', '102'),
                    listing('private-extra-b', '102')], next_cursor='must-not-query'),
    ])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, mode_settings('custom'))
        state = await settled(service, session)
        assert state.status == 'completed' and state.counts == {'101': 2, '102': 1}
        assert provider.purchases == ['private-a', 'private-b', 'private-c']
        assert provider.calls == [None, 'private-page-2']
        assert state.attempts == 3 and state.spent_gold == '30'
        assert 'private-' not in state.model_dump_json()
    finally:
        await cleanup(store, catalog)


async def test_custom_targets_accumulate_across_rounds_without_rebuying_confirmed_rows(catalog_path):
    provider = Provider([
        MarketPage([listing('private-a')], complete=True),
        MarketPage([listing('private-a'), listing('private-b'), listing('private-c', '102')], complete=True),
    ])
    service, session, store, catalog = await setup(catalog_path, provider)
    service.interval = .001
    try:
        service.start(session, mode_settings('custom'))
        await asyncio.wait_for(provider.started.wait(), 1)
        for _ in range(200):
            if service.snapshot(session).status == 'completed':
                break
            await asyncio.sleep(.001)
        state = service.snapshot(session)
        assert state.status == 'completed' and state.counts == {'101': 2, '102': 1}
        assert provider.purchases == ['private-a', 'private-b', 'private-c']
        assert provider.calls == [None, None]
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('mode', ['total', 'custom'])
async def test_unconfirmed_attempt_does_not_consume_mode_quota(catalog_path, mode):
    class SkippedProvider(Provider):
        async def purchase_color(self, row, **options):
            if row.raw_id == 'private-unavailable':
                return None
            return await super().purchase_color(row, **options)

    provider = SkippedProvider([MarketPage([
        listing('private-unavailable'), listing('private-a'), listing('private-b'),
        listing('private-c', '102'), listing('private-extra'),
    ], complete=True)])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, mode_settings(mode))
        state = await settled(service, session)
        assert state.status == 'completed' and state.counts == {'101': 2, '102': 1}
        assert state.attempts == 4 and provider.purchases == ['private-a', 'private-b', 'private-c']
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('mode', ['each_once', 'total', 'custom'])
async def test_new_mode_requests_are_idempotent_and_changed_targets_conflict(catalog_path, mode):
    service, session, store, catalog = await setup(catalog_path, Provider())
    request = mode_settings(mode)
    try:
        first = service.start(session, request)
        assert service.start(session, mode_settings(mode)).job_id == first.job_id
        if mode == 'custom':
            equivalent = mode_settings(mode, quantity_per_pet=99, pet_quantities={'102': 1, '101': 2})
            assert service.start(session, equivalent).job_id == first.job_id
            changed = mode_settings(mode, pet_quantities={'101': 1, '102': 2})
        elif mode == 'total':
            changed = mode_settings(mode, quantity_per_pet=4)
        else:
            changed = mode_settings('total', quantity_per_pet=2)
        with pytest.raises(ApiError) as conflict:
            service.start(session, changed)
        assert conflict.value.code == 'RUSH_REQUEST_CONFLICT'
        service.stop(session, first.job_id)
        await settled(service, session)
        assert service.start(session, request).job_id == first.job_id
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('mode', ['total', 'custom'])
async def test_mode_balance_floor_stops_before_sending_purchase(catalog_path, mode):
    provider = Provider([MarketPage([listing('private-a')], complete=True)])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, mode_settings(mode, min_balance_gold=995))
        state = await settled(service, session)
        assert state.status == 'stopped' and state.counts == {'101': 0, '102': 0}
        assert state.attempts == 0 and not provider.purchases
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('mode', ['total', 'custom'])
async def test_cancel_mode_during_preflight_does_not_submit_purchase(catalog_path, mode):
    provider = Provider([MarketPage([listing('private-a')], complete=True)])
    provider.release = asyncio.Event()
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        first = service.start(session, mode_settings(mode))
        await provider.started.wait()
        service.stop(session, first.job_id)
        provider.release.set()
        state = await settled(service, session)
        assert state.status == 'stopped' and state.counts == {'101': 0, '102': 0}
        assert not provider.purchases
    finally:
        provider.release.set()
        await cleanup(store, catalog)


@pytest.mark.parametrize('mode', ['total', 'custom'])
async def test_new_modes_do_not_count_unknown_transaction_and_lock_restart(catalog_path, mode):
    provider = Provider([MarketPage([listing('private-a'), listing('private-b')], complete=True)],
        failure=ApiError('PURCHASE_OUTCOME_UNKNOWN', 'synthetic-private-error', 409))
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, mode_settings(mode))
        state = await settled(service, session)
        assert state.status == 'uncertain' and state.attempts == 1
        assert state.counts == {'101': 0, '102': 0} and not provider.purchases
        with pytest.raises(ApiError) as locked:
            service.start(session, mode_settings(mode, request_id='synthetic-request-0002'))
        assert locked.value.code == 'PURCHASE_OUTCOME_UNKNOWN'
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('mode', ['total', 'custom'])
async def test_stop_after_mode_purchase_submission_keeps_confirmation_without_next_buy(catalog_path, mode):
    class SubmittedProvider(Provider):
        async def purchase_color(self, row, **options):
            self.purchases.append(row.raw_id)
            self.started.set()
            await self.release.wait()
            self.gold_balance -= row.direct_buy_price
            return SimpleNamespace(raw_id=row.raw_id, pet_id=row.pet_id,
                price=row.direct_buy_price, balance_ready=True)

    provider = SubmittedProvider([MarketPage([listing('private-a'), listing('private-b')], complete=True)])
    provider.release = asyncio.Event()
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        first = service.start(session, mode_settings(mode))
        await provider.started.wait()
        service.stop(session, first.job_id)
        provider.release.set()
        state = await settled(service, session)
        assert state.status == 'stopped' and state.counts == {'101': 1, '102': 0}
        assert state.spent_gold == '10' and provider.purchases == ['private-a']
    finally:
        provider.release.set()
        await cleanup(store, catalog)


@pytest.mark.parametrize('mode', ['total', 'custom'])
async def test_new_mode_stops_after_confirmed_purchase_if_balance_is_not_ready(catalog_path, mode):
    provider = Provider([MarketPage([listing('private-a'), listing('private-b')], complete=True)])
    provider.balance_ready = False
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, mode_settings(mode))
        state = await settled(service, session)
        assert state.status == 'failed' and state.counts == {'101': 1, '102': 0}
        assert state.spent_gold == '10' and state.balance_gold is None
        assert provider.purchases == ['private-a']
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('mode', ['each_once', 'total', 'custom'])
async def test_quantity_mode_api_roundtrip_and_replay(catalog_path, mode):
    provider = PurchaseProvider([MarketPage([
        listing('private-a'), listing('private-b'), listing('private-c', '102'), listing('private-extra'),
    ], complete=True)])
    request = body(**mode_settings(mode).model_dump(exclude={'request_id'}))
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        started = await client.post('/api/rush-purchase/start', json=request)
        assert started.status_code == 200
        assert started.json()['settings']['quantity_mode'] == mode
        final = await terminal(client)
        assert final['status'] == 'completed'
        assert sum(final['counts'].values()) == (2 if mode == 'each_once' else 3)
        repeated = await client.post('/api/rush-purchase/start', json=request)
        assert repeated.status_code == 200 and repeated.json()['job_id'] == final['job_id']
        assert len(provider.purchases) == (2 if mode == 'each_once' else 3)


@pytest.mark.parametrize('changes', [
    {'quantity_mode': 'total', 'pet_quantities': {'101': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': True, '102': 1}},
    {'quantity_mode': 'custom', 'pet_quantities': {'101': 1}},
])
async def test_invalid_mode_api_request_is_rejected_without_task(catalog_path, changes):
    provider = PurchaseProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        response = await client.post('/api/rush-purchase/start',
            json=body(pet_ids=['101', '102'], **changes))
        assert response.status_code == 422
        assert (await client.get('/api/rush-purchase')).json()['status'] == 'idle'
        assert not provider.calls and not provider.purchases
