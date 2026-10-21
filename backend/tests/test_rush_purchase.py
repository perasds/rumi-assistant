import asyncio
import importlib
from types import SimpleNamespace

import pytest

from rumi_market.catalog import Catalog
from rumi_market.errors import ApiError
from rumi_market.models import SessionView, Role
from rumi_market.providers import MarketPage, ProviderListing
from rumi_market.sessions import SessionStore


def implementation():
    try:
        return importlib.import_module('rumi_market.rush')
    except ModuleNotFoundError:
        pytest.fail('Automatic purchase service is not implemented')


def listing(uid, pet='101', price=10):
    return ProviderListing(uid, 'color', pet, price)


class Provider:
    def __init__(self, pages=None, balance=1000, failure=None):
        self.pages = pages or [MarketPage([], complete=True)]
        self.gold_balance = balance
        self.failure = failure
        self.calls, self.purchases = [], []
        self.started, self.release = asyncio.Event(), None
        self.balance_ready = True

    async def purchase_page(self, cursor):
        self.calls.append(cursor)
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]

    async def purchase_color(self, row, *, max_price_gold, min_balance_gold, stop_requested):
        self.started.set()
        if self.release:
            await self.release.wait()
        if stop_requested():
            return None
        if self.failure:
            raise self.failure
        self.purchases.append(row.raw_id)
        self.gold_balance -= row.direct_buy_price
        if not self.balance_ready:
            self.gold_balance = None
        return SimpleNamespace(raw_id=row.raw_id, pet_id=row.pet_id,
                               price=row.direct_buy_price, balance_ready=self.balance_ready)

    async def close(self):
        pass


def settings(**changes):
    values = dict(request_id='synthetic-request-0001', pet_ids=['101'],
                  max_price_gold=100, quantity_per_pet=1, min_balance_gold=0)
    values.update(changes)
    return implementation().RushStartRequest(**values)


async def setup(catalog_path, provider):
    module = implementation()
    catalog = Catalog(catalog_path)
    store = SessionStore(lambda: provider)
    session = store.create()
    session.view = SessionView(authenticated=True, status='connected',
                               role=Role(id='synthetic-role', name='', server_name=''))
    return module.RushPurchaseService(catalog, store, interval=30), session, store, catalog


async def settled(service, session):
    for _ in range(100):
        value = service.snapshot(session)
        if value.status not in ('running', 'stopping'):
            return value
        await asyncio.sleep(0)
    raise AssertionError('purchase task did not settle')


async def cleanup(store, catalog):
    await store.close()
    catalog.close()


async def test_selected_types_buy_once_each_across_pages_and_ignore_legacy_quantity_and_duplicates(catalog_path):
    provider = Provider([
        MarketPage([listing('private-a'), listing('private-a'), listing('private-another'),
                    listing('other', '104')], next_cursor='private-course'),
        MarketPage([listing('private-b', '102'), listing('private-c'), listing('private-d', '102')], complete=True),
    ])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(pet_ids=['101', '102'], quantity_per_pet=2))
        state = await settled(service, session)
        assert state.status == 'completed' and state.counts == {'101': 1, '102': 1}
        assert state.settings.quantity_per_pet == 1
        assert state.spent_gold == '20' and state.balance_gold == '980'
        assert provider.purchases == ['private-a', 'private-b']
        assert provider.calls == [None, 'private-course']
        assert 'private-' not in state.model_dump_json()
    finally:
        await cleanup(store, catalog)


async def test_unselected_pets_stop_at_total_quantity_across_types_allowing_same_type_twice(catalog_path):
    provider = Provider([MarketPage([
        listing('private-a', '101', 10), listing('private-b', '101', 11),
        listing('private-c', '104', 12), listing('private-d', '999', 13),
        listing('private-e', '102', 14), listing('too-expensive', '105', 20),
    ], complete=True)])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(pet_ids=[], max_price_gold=20, quantity_per_pet=4))
        state = await settled(service, session)
        assert state.status == 'completed' and state.settings.pet_ids == []
        assert state.counts == {'101': 2, '104': 1, '999': 1}
        assert provider.purchases == ['private-a', 'private-b', 'private-c', 'private-d']
        assert state.spent_gold == '46' and state.balance_gold == '954'
        assert state.attempts == 4
    finally:
        await cleanup(store, catalog)


async def test_unselected_pets_empty_market_does_not_complete_or_purchase(catalog_path):
    provider = Provider()
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        started = service.start(session, settings(pet_ids=[]))
        for _ in range(10):
            await asyncio.sleep(0)
        state = service.snapshot(session)
        assert state.status == 'running' and state.counts == {}
        assert provider.calls == [None] and not provider.purchases
        service.stop(session, started.job_id)
        await settled(service, session)
    finally:
        await cleanup(store, catalog)


async def test_unselected_pets_accumulate_total_across_rounds_including_same_type(catalog_path):
    class NewTypeProvider(Provider):
        def __init__(self):
            super().__init__([
                MarketPage([listing('private-a', '101')], complete=True),
                MarketPage([listing('private-b', '101'), listing('private-c', '999')], complete=True),
            ])
            self.new_type_bought = asyncio.Event()
        async def purchase_color(self, row, **options):
            receipt = await super().purchase_color(row, **options)
            if row.pet_id == '999' and receipt is not None:
                self.new_type_bought.set()
            return receipt
    provider = NewTypeProvider()
    service, session, store, catalog = await setup(catalog_path, provider)
    service.interval = .001
    try:
        service.start(session, settings(pet_ids=[], quantity_per_pet=3))
        await asyncio.wait_for(provider.new_type_bought.wait(), 1)
        state = await settled(service, session)
        assert state.status == 'completed' and state.counts == {'101': 2, '999': 1}
        assert provider.purchases == ['private-a', 'private-b', 'private-c']
        assert provider.calls == [None, None]
    finally:
        await cleanup(store, catalog)


async def test_unselected_pets_keep_balance_floor(catalog_path):
    provider = Provider([MarketPage([listing('private-a', '999', 20)], complete=True)], balance=110)
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(pet_ids=[], min_balance_gold=100))
        state = await settled(service, session)
        assert state.status == 'stopped' and not provider.purchases
    finally:
        await cleanup(store, catalog)


async def test_unselected_total_quantity_accumulates_across_pages_and_stops_without_extra_purchase(catalog_path):
    provider = Provider([
        MarketPage([listing('private-a'), listing('private-a')], next_cursor='private-page-2'),
        MarketPage([listing('private-b'), listing('private-c', '102'), listing('private-extra', '104')],
                   next_cursor='must-not-query'),
    ])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(pet_ids=[], quantity_per_pet=3))
        state = await settled(service, session)
        assert state.status == 'completed' and state.counts == {'101': 2, '102': 1}
        assert state.attempts == 3 and state.spent_gold == '30'
        assert provider.purchases == ['private-a', 'private-b', 'private-c']
        assert provider.calls == [None, 'private-page-2']
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('pet_ids', [[], ['101']])
async def test_quantity_one_sends_exactly_one_purchase_and_no_later_page(catalog_path, pet_ids):
    provider = Provider([MarketPage([listing('private-a'), listing('private-b'), listing('private-c', '102')],
                                    next_cursor='must-not-query')])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(pet_ids=pet_ids, quantity_per_pet=1))
        state = await settled(service, session)
        assert state.status == 'completed' and state.counts == {'101': 1}
        assert state.attempts == 1 and provider.purchases == ['private-a']
        assert provider.calls == [None]
    finally:
        await cleanup(store, catalog)


async def test_unselected_total_can_be_fulfilled_entirely_by_one_type(catalog_path):
    provider = Provider([MarketPage([listing('private-a'), listing('private-b'), listing('private-c'),
                                    listing('private-extra')], complete=True)])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(pet_ids=[], quantity_per_pet=3))
        state = await settled(service, session)
        assert state.status == 'completed' and state.counts == {'101': 3}
        assert provider.purchases == ['private-a', 'private-b', 'private-c']
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('pet_id', [None, '', 'bad-id', '00101', str(2**31)])
async def test_unselected_pets_cannot_purchase_malformed_pet_identity(catalog_path, pet_id):
    provider = Provider([MarketPage([listing('private-a', pet_id)], complete=True)])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(pet_ids=[]))
        state = await settled(service, session)
        assert state.status == 'failed' and not provider.purchases
    finally:
        await cleanup(store, catalog)


def test_omitted_pet_selection_defaults_to_unrestricted():
    values = settings().model_dump(exclude={'pet_ids'})
    assert implementation().RushStartRequest(**values).pet_ids == []


@pytest.mark.parametrize('quantity', [1, 2, 20, 2**53 - 1])
def test_selected_pet_quantities_normalize_to_one_after_positive_integer_validation(quantity):
    module = implementation()
    parsed = settings(pet_ids=['101', '102', '101'], quantity_per_pet=quantity)
    assert parsed.quantity_per_pet == 1 and parsed.pet_ids == ['101', '102']
    assert module.RushSettings(**parsed.model_dump(exclude={'request_id'})).quantity_per_pet == 1


@pytest.mark.parametrize('quantity', [1, 2, 20])
def test_unselected_quantity_keeps_requested_total(quantity):
    assert settings(pet_ids=[], quantity_per_pet=quantity).quantity_per_pet == quantity


async def test_balance_reserve_prevents_purchase_before_threshold_would_be_crossed(catalog_path):
    provider = Provider([MarketPage([listing('private-a', price=20)], complete=True)], balance=110)
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(min_balance_gold=100))
        state = await settled(service, session)
        assert state.status == 'stopped' and not provider.purchases
        assert state.counts == {'101': 0}
    finally:
        await cleanup(store, catalog)


async def test_equal_price_is_not_lower_and_stop_wakes_monitor_without_another_round(catalog_path):
    provider = Provider([MarketPage([listing('private-a', price=100)], complete=True)])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        started = service.start(session, settings())
        for _ in range(8):
            await asyncio.sleep(0)
        assert not provider.purchases and provider.calls == [None]
        service.stop(session, started.job_id)
        assert (await settled(service, session)).status == 'stopped'
        assert provider.calls == [None]
    finally:
        await cleanup(store, catalog)


async def test_start_is_idempotent_and_conflicting_active_job_is_rejected(catalog_path):
    provider = Provider()
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        first = service.start(session, settings())
        assert service.start(session, settings()).job_id == first.job_id
        with pytest.raises(ApiError) as caught:
            service.start(session, settings(request_id='synthetic-request-0002'))
        assert caught.value.status_code == 409
        assert service.start(session, settings(quantity_per_pet=2)).job_id == first.job_id
        with pytest.raises(ApiError):
            service.start(session, settings(max_price_gold=99))
        service.stop(session, first.job_id)
        await settled(service, session)
        assert service.start(session, settings()).job_id == first.job_id
    finally:
        await cleanup(store, catalog)


async def test_unknown_purchase_stops_without_retry_and_locks_this_session(catalog_path):
    provider = Provider([MarketPage([listing('private-a'), listing('private-b')], complete=True)],
                        failure=ApiError('PURCHASE_OUTCOME_UNKNOWN', 'synthetic-private-text', 409))
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(quantity_per_pet=2))
        state = await settled(service, session)
        assert state.status == 'uncertain' and state.attempts == 1
        assert state.counts == {'101': 0} and not provider.purchases
        assert 'synthetic-private' not in state.model_dump_json()
        with pytest.raises(ApiError):
            service.start(session, settings(request_id='synthetic-request-0002'))
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('code', ['PURCHASE_DETAIL_UNAVAILABLE', 'PURCHASE_INVALID'])
async def test_definite_preflight_failure_is_not_reported_as_a_submitted_unknown_purchase(catalog_path, code):
    provider = Provider([MarketPage([listing('private-a')], complete=True)],
                        failure=ApiError(code, 'synthetic-private-error', 409))
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings())
        final = await settled(service, session)
        assert final.status == 'failed' and not provider.purchases
        assert '未提交' in final.message and 'synthetic-private' not in final.message
        assert service.start(session, settings(request_id='synthetic-request-0002')).status == 'running'
    finally:
        await cleanup(store, catalog)


async def test_confirmed_purchase_is_counted_even_when_balance_update_is_missing(catalog_path):
    provider = Provider([MarketPage([listing('private-a'), listing('private-b')], complete=True)])
    provider.balance_ready = False
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings(quantity_per_pet=2))
        state = await settled(service, session)
        assert state.status == 'failed' and state.counts == {'101': 1}
        assert state.spent_gold == '10' and state.balance_gold is None
        assert provider.purchases == ['private-a']
    finally:
        await cleanup(store, catalog)


async def test_stop_during_preflight_prevents_purchase(catalog_path):
    provider = Provider([MarketPage([listing('private-a')], complete=True)])
    provider.release = asyncio.Event()
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        state = service.start(session, settings())
        await provider.started.wait()
        assert service.stop(session, state.job_id).status == 'stopping'
        provider.release.set()
        assert (await settled(service, session)).status == 'stopped'
        assert not provider.purchases
    finally:
        provider.release.set()
        await cleanup(store, catalog)


async def test_stop_after_submission_keeps_confirmed_count_without_sending_next_purchase(catalog_path):
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
        state = service.start(session, settings(quantity_per_pet=2))
        await provider.started.wait()
        assert service.stop(session, state.job_id).status == 'stopping'
        with pytest.raises(ApiError):
            service.start(session, settings(request_id='synthetic-request-0002'))
        provider.release.set()
        final = await settled(service, session)
        assert final.status == 'stopped' and final.counts == {'101': 1}
        assert final.spent_gold == '10' and provider.purchases == ['private-a']
    finally:
        provider.release.set()
        await cleanup(store, catalog)


async def test_empty_market_is_monitored_until_explicit_stop(catalog_path):
    class EmptyProvider(Provider):
        async def purchase_page(self, cursor):
            result = await super().purchase_page(cursor)
            if len(self.calls) >= 3:
                self.started.set()
            return result
    provider = EmptyProvider()
    service, session, store, catalog = await setup(catalog_path, provider)
    service.interval = .001
    try:
        state = service.start(session, settings())
        await asyncio.wait_for(provider.started.wait(), 1)
        service.stop(session, state.job_id)
        assert (await settled(service, session)).status == 'stopped'
        assert len(provider.calls) == 3 and not provider.purchases
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('pet', ['103', '104', '999'])
async def test_unverified_or_unknown_pet_cannot_start(catalog_path, pet):
    service, session, store, catalog = await setup(catalog_path, Provider())
    try:
        with pytest.raises(ApiError) as caught:
            service.start(session, settings(pet_ids=[pet]))
        assert caught.value.status_code == 422
        assert service.snapshot(session).status == 'idle'
    finally:
        await cleanup(store, catalog)


async def test_unknown_balance_blocks_all_queries_and_purchases(catalog_path):
    provider = Provider(balance=None)
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings())
        state = await settled(service, session)
        assert state.status == 'failed' and not provider.calls and not provider.purchases
    finally:
        await cleanup(store, catalog)


async def test_repeated_cursor_fails_without_infinite_queries(catalog_path):
    provider = Provider([MarketPage([], next_cursor='private-course')])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        service.start(session, settings())
        assert (await settled(service, session)).status == 'failed'
        assert len(provider.calls) == 2
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('unknown', [False, True])
async def test_replaying_older_request_reports_current_job_without_hiding_it(catalog_path, unknown):
    provider = Provider([MarketPage([listing('private-a')], complete=True)])
    service, session, store, catalog = await setup(catalog_path, provider)
    try:
        first = service.start(session, settings())
        assert (await settled(service, session)).status == 'completed'
        provider.pages = [MarketPage([listing('private-b')], complete=True)] if unknown else [MarketPage([], complete=True)]
        if unknown:
            provider.failure = ApiError('PURCHASE_OUTCOME_UNKNOWN', '', 409)
        current = service.start(session, settings(request_id='synthetic-request-0002'))
        if unknown:
            assert (await settled(service, session)).status == 'uncertain'
        repeated = service.start(session, settings())
        assert repeated.job_id == current.job_id != first.job_id
        assert repeated.status == ('uncertain' if unknown else 'running')
        assert provider.purchases == ['private-a']
    finally:
        await cleanup(store, catalog)


@pytest.mark.parametrize('change', [dict(max_price_gold=True), dict(quantity_per_pet=0), dict(quantity_per_pet=-1),
    dict(quantity_per_pet=True), dict(quantity_per_pet=1.5),
    dict(min_balance_gold=-1), dict(max_price_gold=1.5), dict(pet_ids=['']), dict(extra='forbidden')])
def test_settings_validate_without_coercion(change):
    module = implementation()
    with pytest.raises(ValueError):
        settings(**change)
