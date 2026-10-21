import asyncio
import gc
import json
import weakref
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

import rumi_market.redbag_models as redbag_models
from rumi_market.errors import ApiError
from rumi_market.models import Role, SessionView
from rumi_market.redbag_models import RedbagCandidate, RedbagReceipt
from rumi_market.redbag_service import RedbagService
from rumi_market.sessions import Session, SessionStore


class Provider:
    redbag_available = True
    redbag_identity = ('7', '9007199254740993')
    server_time_seconds = 1000

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.started = asyncio.Event()
        self.start_gate = asyncio.Event()
        self.start_gate.set()
        self.claim_gate = asyncio.Event()
        self.claim_gate.set()
        self.claim_started = asyncio.Event()
        self.claims = []
        self.receipts = {}
        self.reconciles = []
        self.reconcile_result = None
        self.claim_cancelled = False
        self.start_error = None
        self.stop_count = 0
        self.close_count = 0

    async def redbag_start(self):
        await self.start_gate.wait()
        if self.start_error:
            raise self.start_error
        while not self.incoming.empty():
            self.incoming.get_nowait()
        self.started.set()

    async def redbag_next(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def redbag_claim(self, candidate, *, stop_requested):
        if stop_requested():
            return RedbagReceipt('missing')
        self.claims.append(candidate.id)
        self.claim_started.set()
        try:
            await self.claim_gate.wait()
        except asyncio.CancelledError:
            self.claim_cancelled = True
            raise
        result = self.receipts.get(candidate.id, RedbagReceipt('claimed', 17))
        if isinstance(result, Exception):
            raise result
        return result

    async def redbag_reconcile(self, candidate):
        self.reconciles.append(candidate.id)
        if isinstance(self.reconcile_result, Exception):
            raise self.reconcile_result
        return self.reconcile_result

    async def redbag_stop(self):
        self.stop_count += 1

    async def close(self):
        self.close_count += 1
        self.claim_gate.set()
        self.start_gate.set()


@pytest.fixture
async def systems():
    values = []

    def create(provider=None, *, service=None, store=None):
        provider = provider or Provider()
        store = store or SessionStore(Provider)
        service = service or RedbagService(store)
        session = Session(f'synthetic-redbag-{len(values)}', provider,
            view=SessionView(status='connected', authenticated=True,
                role=Role(id='browser-role-key', name='', server_name='test')))
        # The service's narrow session contract, also used by the API wiring.
        session.redbag_task = None
        session.redbag_requests = {}
        session.redbag_seen = {}
        store.sessions[session.id] = session
        values.append((service, session, provider))
        return service, session, provider, store

    yield create
    for service, session, provider in values:
        provider.claim_gate.set()
        provider.start_gate.set()
        if session.redbag_task:
            session.redbag_task.request_stop()
            await asyncio.wait_for(asyncio.shield(session.redbag_task.task), 1)


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


async def begin(service, session, provider, request_id='synthetic-request-001'):
    await service.start(session, request_id)
    await asyncio.wait_for(provider.started.wait(), 1)


async def finish(service, session):
    await service.stop(session)
    await asyncio.wait_for(asyncio.shield(session.redbag_task.task), 1)
    return await service.state(session)


def card(identifier, *, sender='other-player', expires=2000):
    return RedbagCandidate(identifier, sender, expires)


def test_private_candidate_is_immutable_and_has_no_identifying_repr():
    candidate = card('private-redbag-id')
    assert 'private-redbag-id' not in repr(candidate)
    assert 'claimed' not in repr(RedbagReceipt('claimed', 7))
    with pytest.raises(FrozenInstanceError):
        candidate.id = 'changed'


def test_start_request_accepts_only_strict_bounded_request_id_without_extra_fields():
    assert hasattr(redbag_models, 'RedbagStartRequest')
    model = redbag_models.RedbagStartRequest
    assert model(request_id='synthetic-request-001').request_id == 'synthetic-request-001'
    for invalid in ({'request_id': 1234567890123456}, {'request_id': 'short'},
                    {'request_id': 'x' * 65}, {'request_id': 'a' * 16 + '/private'},
                    {'request_id': 'synthetic-request-001', 'unknown': 'private'}):
        with pytest.raises(ValidationError):
            model(**invalid)


@pytest.mark.asyncio
async def test_default_is_off_and_reading_state_never_starts_provider(systems):
    service, session, provider, _ = systems()
    state = await service.state(session)
    assert state['available'] is True
    assert state['running'] is False and state['status'] == 'idle'
    assert state['claimed_count'] == state['skipped_count'] == 0
    assert state['stamina_total'] == '0' and state['recent'] == []
    assert state['started_at'] is None and state['updated_at'] is None
    assert not provider.started.is_set() and provider.claims == []


@pytest.mark.asyncio
async def test_start_returns_before_provider_connection_and_registers_background_task(systems):
    service, session, provider, _ = systems()
    provider.start_gate.clear()
    result = await asyncio.wait_for(service.start(session, 'synthetic-request-001'), .1)
    assert result['status'] == 'starting' and result['running'] is True
    assert session.redbag_task.task in session.tasks
    assert not provider.started.is_set()
    await service.stop(session)
    provider.start_gate.set()


@pytest.mark.asyncio
async def test_only_new_cards_are_claimed_and_real_rewards_are_private_decimal_strings(systems):
    service, session, provider, _ = systems()
    provider.incoming.put_nowait(card('old-private-card'))
    await begin(service, session, provider)
    provider.receipts['new-private-card'] = RedbagReceipt('claimed', 9007199254740993)
    provider.incoming.put_nowait(card('new-private-card'))
    await until(lambda: session.redbag_task.claimed_count == 1)
    state = await finish(service, session)
    assert provider.claims == ['new-private-card']
    assert state['claimed_count'] == 1 and state['stamina_total'] == '9007199254740993'
    assert state['recent'][0]['stamina'] == state['stamina_total']
    assert state['recent'][0]['status'] == 'claimed'
    assert set(state['recent'][0]) == {'id', 'at', 'status', 'message', 'stamina'}
    for private in ('old-private-card', 'new-private-card', '9007199254740993'):
        if private != state['stamina_total']:
            assert private not in json.dumps(state)
    assert 'other-player' not in json.dumps(state)
    assert 'browser-role-key' not in json.dumps(state)
    assert provider.stop_count == 1 and provider.close_count == 0


@pytest.mark.asyncio
async def test_deduplicates_channels_and_filters_self_and_expired_cards(systems):
    service, session, provider, _ = systems()
    await begin(service, session, provider)
    for value in (card('one'), card('one'), card('self', sender=provider.redbag_identity[1]),
                  card('expired', expires=1000), card('unspecified', expires=0)):
        provider.incoming.put_nowait(value)
    await until(lambda: session.redbag_task.claimed_count == 2)
    state = await finish(service, session)
    assert provider.claims == ['one', 'unspecified']
    assert state['claimed_count'] == 2 and state['skipped_count'] == 3
    assert state['stamina_total'] == '34'


@pytest.mark.asyncio
async def test_stop_then_start_preserves_seen_cards_and_duplicate_request_never_restarts(systems):
    service, session, provider, _ = systems()
    await begin(service, session, provider)
    original = session.redbag_task
    await service.start(session, 'synthetic-request-002')
    assert session.redbag_task is original
    provider.incoming.put_nowait(card('already-done'))
    await until(lambda: original.claimed_count == 1)
    await finish(service, session)
    for request in ('synthetic-request-001', 'synthetic-request-002'):
        state = await service.start(session, request)
        assert state['running'] is False and session.redbag_task is original
    provider.started.clear()
    await begin(service, session, provider, 'synthetic-request-003')
    provider.incoming.put_nowait(card('already-done'))
    provider.incoming.put_nowait(card('fresh'))
    await until(lambda: session.redbag_task.claimed_count == 1)
    state = await finish(service, session)
    assert provider.claims == ['already-done', 'fresh']
    assert state['skipped_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['missing', 'expired', 'self', 'already_claimed', 'full'])
async def test_confirmed_skip_outcomes_never_count_as_claimed(systems, outcome):
    service, session, provider, _ = systems()
    provider.receipts['skip'] = RedbagReceipt(outcome, 999)
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('skip'))
    await until(lambda: session.redbag_task.skipped_count == 1)
    state = await finish(service, session)
    assert state['claimed_count'] == 0 and state['stamina_total'] == '0'
    assert state['recent'][0]['status'] == 'skipped' and state['recent'][0]['stamina'] == '0'


@pytest.mark.asyncio
@pytest.mark.parametrize(('code', 'status'), [
    ('REDBAG_DAILY_LIMIT', 'daily_limit'),
    ('REDBAG_CONDITION_NOT_MET', 'conditions_not_met'),
    ('REDBAG_RATE_LIMITED', 'rate_limited'),
    ('REDBAG_QUEUE_FULL', 'failed'),
    ('REDBAG_UPSTREAM_FAILED', 'failed'),
])
async def test_server_limits_and_failures_stop_without_retries_or_raw_messages(systems, code, status):
    service, session, provider, _ = systems()
    provider.receipts['limit'] = ApiError(code, 'private upstream chat text')
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('limit'))
    provider.incoming.put_nowait(card('later'))
    await asyncio.wait_for(session.redbag_task.task, 1)
    state = await service.state(session)
    assert state['status'] == status and state['running'] is False
    assert state['claimed_count'] == 0 and state['stamina_total'] == '0'
    assert provider.claims == ['limit'] and provider.reconciles == []
    assert 'private upstream chat text' not in json.dumps(state)


@pytest.mark.asyncio
async def test_unknown_provider_exception_uses_fixed_failure_message(systems):
    service, session, provider, _ = systems()
    provider.start_error = RuntimeError('secret-token-and-chat')
    await service.start(session, 'synthetic-request-001')
    await asyncio.wait_for(session.redbag_task.task, 1)
    state = await service.state(session)
    assert state['status'] == 'failed' and 'secret-token-and-chat' not in json.dumps(state)


@pytest.mark.asyncio
async def test_uncertain_claim_queries_once_and_counts_only_confirmed_server_receipt(systems):
    service, session, provider, _ = systems()
    provider.receipts['late'] = ApiError('REDBAG_UNCERTAIN', 'private timeout')
    provider.reconcile_result = RedbagReceipt('claimed', 8)
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('late'))
    provider.incoming.put_nowait(card('late'))
    await until(lambda: session.redbag_task.skipped_count == 1)
    state = await finish(service, session)
    assert state['claimed_count'] == 1 and state['stamina_total'] == '8'
    assert provider.claims == ['late'] and provider.reconciles == ['late']


@pytest.mark.asyncio
@pytest.mark.parametrize('reconciled', [None, RuntimeError('private query failure')])
async def test_unconfirmed_result_stays_uncertain_and_blocks_both_browser_sessions(systems, reconciled):
    service, session, provider, store = systems()
    provider.receipts['unknown'] = ApiError('REDBAG_UNCERTAIN', 'private timeout')
    provider.reconcile_result = reconciled
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('unknown'))
    await asyncio.wait_for(session.redbag_task.task, 1)
    state = await service.state(session)
    assert state['status'] == 'uncertain' and state['claimed_count'] == 0
    assert state['stamina_total'] == '0' and state['recent'][0]['status'] == 'uncertain'
    await service.stop(session)
    with pytest.raises(ApiError, match='REDBAG_UNCERTAIN'):
        await service.start(session, 'synthetic-request-002')
    _, other, _, _ = systems(service=service, store=store)
    with pytest.raises(ApiError, match='REDBAG_ROLE_BUSY'):
        await service.start(other, 'synthetic-request-003')
    assert provider.claims == ['unknown'] and provider.reconciles == ['unknown']
    assert 'private' not in json.dumps(state)


@pytest.mark.asyncio
async def test_claims_are_serial_and_stop_does_not_cancel_in_flight_main_request(systems):
    service, session, provider, _ = systems()
    provider.claim_gate.clear()
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('in-flight'))
    provider.incoming.put_nowait(card('must-not-send'))
    await asyncio.wait_for(provider.claim_started.wait(), 1)
    state = await service.stop(session)
    assert state['status'] == 'stopping' and state['running'] is True
    assert provider.claims == ['in-flight'] and provider.claim_cancelled is False
    provider.claim_gate.set()
    await asyncio.wait_for(session.redbag_task.task, 1)
    state = await service.state(session)
    assert state['status'] == 'stopped' and state['claimed_count'] == 1
    assert provider.claims == ['in-flight'] and provider.claim_cancelled is False


@pytest.mark.asyncio
async def test_stopping_waiting_worker_leaves_unrelated_task_running(systems):
    service, session, provider, _ = systems()
    unrelated_gate = asyncio.Event()
    unrelated = asyncio.create_task(unrelated_gate.wait())
    session.tasks.add(unrelated)
    await begin(service, session, provider)
    await finish(service, session)
    assert not unrelated.done()
    unrelated_gate.set()
    await unrelated
    session.tasks.discard(unrelated)


@pytest.mark.asyncio
async def test_cancelling_worker_preserves_and_settles_the_in_flight_claim(systems):
    service, session, provider, _ = systems()
    provider.claim_gate.clear()
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('protected-main-request'))
    await asyncio.wait_for(provider.claim_started.wait(), 1)
    session.redbag_task.task.cancel()
    await asyncio.sleep(0)
    assert not provider.claim_cancelled and not session.redbag_task.task.done()
    provider.claim_gate.set()
    await asyncio.wait_for(session.redbag_task.task, 1)
    state = await service.state(session)
    assert state['claimed_count'] == 1 and state['status'] == 'stopped'
    assert provider.claims == ['protected-main-request'] and not provider.claim_cancelled


@pytest.mark.asyncio
async def test_changed_generation_before_send_prevents_claim(systems):
    class DelayedSendProvider(Provider):
        def __init__(self):
            super().__init__()
            self.before_send = asyncio.Event()
            self.send_gate = asyncio.Event()

        async def redbag_claim(self, candidate, *, stop_requested):
            self.before_send.set()
            await self.send_gate.wait()
            return await super().redbag_claim(candidate, stop_requested=stop_requested)

    provider = DelayedSendProvider()
    service, session, _, _ = systems(provider)
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('stale-generation'))
    await asyncio.wait_for(provider.before_send.wait(), 1)
    run = session.redbag_task
    provider.redbag_identity = ('7', 'different-real-role')
    provider.send_gate.set()
    await asyncio.wait_for(run.task, 1)
    assert provider.claims == [] and run.claimed_count == 0


@pytest.mark.asyncio
async def test_explicit_unsent_none_after_stop_is_neither_uncertain_nor_skipped(systems):
    class UnsentProvider(Provider):
        async def redbag_claim(self, candidate, *, stop_requested):
            self.claim_started.set()
            await self.claim_gate.wait()
            assert stop_requested()
            return None  # Adapter guarantee: no main-game command was sent.

    provider = UnsentProvider()
    provider.claim_gate.clear()
    service, session, _, _ = systems(provider)
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('not-sent'))
    await asyncio.wait_for(provider.claim_started.wait(), 1)
    await service.stop(session)
    provider.claim_gate.set()
    await asyncio.wait_for(session.redbag_task.task, 1)
    state = await service.state(session)
    assert state['status'] == 'stopped'
    assert state['claimed_count'] == state['skipped_count'] == 0
    assert state['recent'] == [] and provider.reconciles == [] and provider.claims == []


@pytest.mark.asyncio
async def test_same_real_role_is_exclusive_and_logout_cleanup_releases_it(systems):
    service, session, provider, store = systems()
    await begin(service, session, provider)
    _, other, _, _ = systems(service=service, store=store)
    with pytest.raises(ApiError, match='REDBAG_ROLE_BUSY'):
        await service.start(other, 'synthetic-request-002')
    run = session.redbag_task
    await store.remove(session)
    await asyncio.wait_for(run.task, 1)
    assert run.stop.is_set() and provider.stop_count == 1
    assert session.redbag_task is None and session.redbag_seen == {}
    await service.start(other, 'synthetic-request-003')
    assert other.redbag_task is not run


@pytest.mark.asyncio
async def test_session_expiry_stops_and_calls_existing_expiry_cleanup(systems):
    service, session, provider, _ = systems()
    await begin(service, session, provider)
    run = session.redbag_task
    provider.incoming.put_nowait(ApiError('GAME_SESSION_EXPIRED', 'private expiry'))
    await asyncio.wait_for(run.task, 1)
    assert session.view.status == 'expired' and not session.view.authenticated
    assert run.stop.is_set() and provider.close_count == 1 and provider.stop_count == 1


@pytest.mark.asyncio
async def test_full_seen_buffer_stops_safely_without_evicting_unexpired_cards(systems):
    service, session, provider, _ = systems()
    session.redbag_seen.update({f'private-seen-{i}': 2000 for i in range(4096)})
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('overflow'))
    await asyncio.wait_for(session.redbag_task.task, 1)
    state = await service.state(session)
    assert state['status'] == 'failed' and provider.claims == []
    assert len(session.redbag_seen) == 4096 and 'private-seen-0' in session.redbag_seen


@pytest.mark.asyncio
async def test_recent_is_bounded_and_copied_for_each_snapshot(systems):
    service, session, provider, _ = systems()
    await begin(service, session, provider)
    for i in range(24):
        provider.incoming.put_nowait(card(f'card-{i}'))
    await until(lambda: session.redbag_task.claimed_count == 24)
    state = await finish(service, session)
    assert len(state['recent']) == 20
    assert [row['id'] for row in state['recent']] == list(range(24, 4, -1))
    state['recent'][0]['message'] = 'tampered'
    assert (await service.state(session))['recent'][0]['message'] != 'tampered'


@pytest.mark.asyncio
async def test_unavailable_or_unidentified_provider_never_starts(systems):
    service, session, provider, _ = systems()
    provider.redbag_available = False
    assert (await service.state(session))['available'] is False
    with pytest.raises(ApiError, match='REDBAG_UNAVAILABLE'):
        await service.start(session, 'synthetic-request-001')
    provider.redbag_available = True
    provider.redbag_identity = None
    with pytest.raises(ApiError, match='REDBAG_UNAVAILABLE'):
        await service.start(session, 'synthetic-request-002')
    assert not provider.started.is_set()


@pytest.mark.asyncio
async def test_logout_releases_uncertain_owner_and_its_private_provider_references():
    store = SessionStore(Provider)
    service = RedbagService(store)
    session = store.create()
    session.view = SessionView(status='connected', authenticated=True)
    provider = session.provider
    provider.receipts['unknown'] = ApiError('REDBAG_UNCERTAIN', 'private timeout')
    await begin(service, session, provider)
    provider.incoming.put_nowait(card('unknown'))
    await asyncio.wait_for(session.redbag_task.task, 1)
    assert session.redbag_task.uncertain
    reference = weakref.ref(session)
    await store.remove(session)
    del session
    del provider
    gc.collect()
    assert reference() is None
    assert not service.roles


@pytest.mark.asyncio
async def test_logout_in_progress_keeps_role_exclusive_until_old_claim_finishes(systems):
    class BlockedCloseProvider(Provider):
        def __init__(self):
            super().__init__()
            self.close_started = asyncio.Event()
            self.close_gate = asyncio.Event()

        async def close(self):
            self.close_count += 1
            self.close_started.set()
            await self.close_gate.wait()

    provider = BlockedCloseProvider()
    provider.claim_gate.clear()
    service, session, _, store = systems(provider)
    _, other, other_provider, _ = systems(service=service, store=store)
    await begin(service, session, provider)
    run = session.redbag_task
    provider.incoming.put_nowait(card('same-card'))
    await asyncio.wait_for(provider.claim_started.wait(), 1)
    removing = asyncio.create_task(store.remove(session))
    await asyncio.wait_for(provider.close_started.wait(), 1)
    try:
        assert not session.active and not run.task.done()
        with pytest.raises(ApiError, match='REDBAG_ROLE_BUSY'):
            await service.start(other, 'synthetic-request-002')
        assert other.redbag_task is None and other_provider.claims == []
    finally:
        provider.claim_gate.set()
        provider.close_gate.set()
        await asyncio.wait_for(removing, 1)
        await asyncio.wait_for(run.task, 1)
    await begin(service, other, other_provider, 'synthetic-request-003')
    assert other.redbag_task is not run


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['expired', 'success', 'uncertain', 'reconcile_expired'])
async def test_late_old_claim_after_cleanup_timeout_never_touches_replacement_provider(
        systems, monkeypatch, outcome):
    class DelayedProvider(Provider):
        async def close(self):
            self.close_count += 1
            # A bounded cleanup can return while an already-sent ACK is pending.

    original_wait = asyncio.wait

    async def bounded_cleanup_wait(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
        return await original_wait(tasks, timeout=.01 if timeout == 5 else timeout,
            return_when=return_when)

    monkeypatch.setattr('rumi_market.sessions.asyncio.wait', bounded_cleanup_wait)
    provider = DelayedProvider()
    provider.claim_gate.clear()
    provider.receipts['old-card'] = {
        'expired': ApiError('GAME_SESSION_EXPIRED', 'old private expiry'),
        'success': RedbagReceipt('claimed', 41),
        'uncertain': ApiError('REDBAG_UNCERTAIN', 'old private timeout'),
        'reconcile_expired': ApiError('REDBAG_UNCERTAIN', 'old private timeout'),
    }[outcome]
    provider.reconcile_result = (ApiError('GAME_SESSION_EXPIRED', 'old private query expiry')
        if outcome == 'reconcile_expired' else RedbagReceipt('claimed', 23))
    service, session, _, store = systems(provider)
    await begin(service, session, provider)
    old_run = session.redbag_task
    provider.incoming.put_nowait(card('old-card'))
    await asyncio.wait_for(provider.claim_started.wait(), 1)
    await store.close_provider(session)
    assert not old_run.task.done() and session.redbag_task is None

    replacement = Provider()
    replacement.redbag_identity = ('7', 'new-real-role')
    session.provider = replacement
    session.view = SessionView(status='connected', authenticated=True)
    await begin(service, session, replacement, 'synthetic-replacement-001')
    new_run = session.redbag_task
    session.redbag_seen['new-generation-only'] = 2000
    try:
        provider.claim_gate.set()
        await asyncio.wait_for(old_run.task, 1)
        assert session.view.status == 'connected' and session.view.authenticated
        assert session.provider is replacement and session.redbag_task is new_run
        assert replacement.close_count == 0 and replacement.stop_count == 0
        assert provider.stop_count == 1 and replacement.reconciles == []
        assert session.redbag_seen == {'new-generation-only': 2000}
        state = await service.state(session)
        assert state['status'] == 'running' and state['running'] is True
        assert state['claimed_count'] == state['skipped_count'] == 0
        assert state['recent'] == [] and state['stamina_total'] == '0'
        if outcome in {'uncertain', 'reconcile_expired'}:
            assert provider.reconciles == ['old-card']
    finally:
        provider.claim_gate.set()
        new_run.request_stop()
        await asyncio.wait_for(new_run.task, 1)


@pytest.mark.asyncio
async def test_same_role_provider_replacement_invalidates_old_waiting_generation(systems):
    service, session, provider, _ = systems()
    await begin(service, session, provider)
    run = session.redbag_task
    await asyncio.sleep(0)
    replacement = Provider()  # Same real role, a different login connection.
    session.provider = replacement
    provider.incoming.put_nowait(card('old-generation-card'))
    try:
        await until(lambda: run.task.done() or bool(replacement.claims))
        assert replacement.claims == [] and provider.claims == []
        assert session.redbag_seen == {} and replacement.stop_count == 0
        await asyncio.wait_for(run.task, 1)
        assert provider.stop_count == 1
    finally:
        run.request_stop()
        await asyncio.wait_for(run.task, 1)


@pytest.mark.asyncio
async def test_repeated_worker_cancellation_keeps_claim_and_role_lock_until_ack(systems):
    service, session, provider, store = systems()
    _, other, _, _ = systems(service=service, store=store)
    provider.claim_gate.clear()
    await begin(service, session, provider)
    run = session.redbag_task
    provider.incoming.put_nowait(card('repeated-cancel-card'))
    await asyncio.wait_for(provider.claim_started.wait(), 1)
    run.task.cancel()
    await until(run.stop.is_set)
    run.task.cancel()
    await asyncio.sleep(0)
    try:
        assert not run.task.done() and not provider.claim_cancelled
        with pytest.raises(ApiError, match='REDBAG_ROLE_BUSY'):
            await service.start(other, 'synthetic-request-002')
    finally:
        provider.claim_gate.set()
        await asyncio.wait_for(run.task, 1)
    assert run.claimed_count == 1 and provider.claims == ['repeated-cancel-card']


@pytest.mark.asyncio
async def test_repeated_worker_cancellation_does_not_abandon_result_reconciliation(systems):
    class DelayedQueryProvider(Provider):
        def __init__(self):
            super().__init__()
            self.query_started = asyncio.Event()
            self.query_gate = asyncio.Event()
            self.query_cancelled = False

        async def redbag_reconcile(self, candidate):
            self.reconciles.append(candidate.id)
            self.query_started.set()
            try:
                await self.query_gate.wait()
            except asyncio.CancelledError:
                self.query_cancelled = True
                raise
            return RedbagReceipt('claimed', 5)

    provider = DelayedQueryProvider()
    provider.receipts['query-card'] = ApiError('REDBAG_UNCERTAIN', 'private timeout')
    service, session, _, store = systems(provider)
    _, other, _, _ = systems(service=service, store=store)
    await begin(service, session, provider)
    run = session.redbag_task
    provider.incoming.put_nowait(card('query-card'))
    await asyncio.wait_for(provider.query_started.wait(), 1)
    run.task.cancel()
    await asyncio.sleep(0)
    run.task.cancel()
    await asyncio.sleep(0)
    try:
        assert not provider.query_cancelled and not run.task.done()
        with pytest.raises(ApiError, match='REDBAG_ROLE_BUSY'):
            await service.start(other, 'synthetic-request-002')
    finally:
        provider.query_gate.set()
        await asyncio.wait_for(run.task, 1)
    assert run.claimed_count == 1 and run.stamina_total == 5
    assert provider.reconciles == ['query-card'] and provider.claims == ['query-card']
