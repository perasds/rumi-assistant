import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from rumi_market.errors import ApiError
from rumi_market.game.redbag_codec import RedbagProtoCodec
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.live_runtime import LiveProviderFactory
from rumi_market.redbag_models import RedbagCandidate, RedbagReceipt
from test_game_runtime import PRIVATE, key
from test_hot69_runtime_integration import setup_version
from test_hot71_runtime_integration import server


@asynccontextmanager
async def logged_in(key, *, hot=71, server_id=7):
    runtime, game, _ = setup_version(key, hot)
    transport = runtime._version_client._http._transport
    original = transport.handler

    async def version_response(request):
        raw = (await original(request)).json()
        raw['data']['serverList'] = [{'serverId': server_id, 'chat_gate_url': 'wss://chat.example.invalid/ws'}]
        return httpx.Response(200, json=raw)

    transport.handler = version_response
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=[]), login_verified=True)
    peer = asyncio.create_task(server(game, hot, None))
    try:
        await provider.login('synthetic-account', PRIVATE)
        await peer
        yield runtime, game, provider
    finally:
        peer.cancel()
        await provider.close()


class FakeOperations:
    instances = []

    def __init__(self, client, state, **kwargs):
        self.client, self.state, self.options = client, state, kwargs
        self.starts = self.stops = self.claims = self.queries = 0
        self.instances.append(self)

    async def start(self):
        self.starts += 1

    async def next(self):
        return RedbagCandidate('synthetic-packet', '456', 0)

    async def claim(self, candidate, *, stop_requested):
        assert not stop_requested()
        self.claims += 1
        return RedbagReceipt('claimed', 18)

    async def reconcile(self, candidate):
        self.queries += 1
        return RedbagReceipt('claimed', 18)

    async def stop(self):
        self.stops += 1


@pytest.mark.parametrize('hot,server_id,available', [(71, 7, True), (71, 8, False), (69, 7, False), (72, 7, True), (73, 7, False)])
async def test_redbag_gate_uses_verified_version_and_exact_selected_server(key, hot, server_id, available):
    async with logged_in(key, hot=hot, server_id=server_id) as (runtime, game, provider):
        assert runtime.redbag_available is available
        assert provider.redbag_available is available
        assert game.outgoing.empty()  # Logging in never authenticates chat or claims a packet.
        if not available:
            with pytest.raises(ApiError, match='REDBAG_UNAVAILABLE'):
                await provider.redbag_start()


async def test_main_chat_auth_is_observed_and_cleared_when_runtime_closes(key):
    async with logged_in(key) as (runtime, game, _):
        state = runtime.redbag_state
        assert state is not None and not state.has_auth
        game.respond(2919, 0, RedbagProtoCodec().encode(2919, params={'ticket': 'synthetic-ticket'}), notify=90)
        for _ in range(20):
            await asyncio.sleep(0)
            if state.has_auth:
                break
        assert state.has_auth
        assert await state.auth() == {'ticket': 'synthetic-ticket'}
    assert state.closed and not state.has_auth
    assert runtime.redbag_state is None and not runtime.redbag_available


async def test_provider_only_starts_chat_after_explicit_start_and_forwards_guarded_operations(key, monkeypatch):
    import rumi_market.game.runtime as module
    monkeypatch.setattr(module, 'RedbagOperations', FakeOperations, raising=False)
    FakeOperations.instances.clear()
    async with logged_in(key) as (runtime, game, provider):
        assert provider.redbag_identity == ('7', '123')
        assert FakeOperations.instances == []
        await provider.redbag_start()
        ops = FakeOperations.instances[-1]
        assert ops.starts == 1 and ops.options['chat_url'] == 'wss://chat.example.invalid/ws'
        assert ops.options['role_id'] == '123' and ops.options['zone_id'] == 7
        assert ops.options['nation_code'] == 'CN' and ops.options['language'] == 1
        candidate = await provider.redbag_next()
        assert await provider.redbag_claim(candidate, stop_requested=lambda: False) == RedbagReceipt('claimed', 18)
        assert await provider.redbag_reconcile(candidate) == RedbagReceipt('claimed', 18)
        assert ops.claims == ops.queries == 1
        await provider.redbag_stop()
        assert ops.stops == 1 and not game.closed and runtime.can_query
        assert game.outgoing.empty()
    assert ops.stops == 1


async def test_runtime_close_stops_chat_before_discarding_it(key, monkeypatch):
    import rumi_market.game.runtime as module
    monkeypatch.setattr(module, 'RedbagOperations', FakeOperations, raising=False)
    async with logged_in(key) as (runtime, _, provider):
        await provider.redbag_start()
        ops = FakeOperations.instances[-1]
    assert ops.stops == 1 and runtime.closed
    assert not provider.redbag_available and provider.redbag_identity is None


@pytest.mark.parametrize('code', [-5, -8])
async def test_expired_main_response_remains_session_expiry_at_provider_boundary(key, monkeypatch, code):
    import rumi_market.game.runtime as module
    monkeypatch.setattr(module, 'RedbagOperations', FakeOperations, raising=False)
    async with logged_in(key) as (runtime, _, provider):
        await provider.redbag_start()
        runtime.redbag_state.observe(SimpleNamespace(command=4012, code=code, data=b''))
        assert not runtime.redbag_available
        with pytest.raises(ApiError, match='GAME_SESSION_EXPIRED'):
            await provider.redbag_next()
        with pytest.raises(ApiError, match='GAME_SESSION_EXPIRED'):
            await provider.redbag_reconcile(RedbagCandidate('synthetic-packet', '456'))


async def test_lazy_live_provider_forwards_redbag_api_without_creating_a_login():
    class Delegate:
        redbag_available = True
        redbag_identity = ('7', '123')
        async def redbag_start(self):
            return 'start'
        async def redbag_next(self):
            return RedbagCandidate('synthetic-packet', '456')
        async def redbag_claim(self, candidate, *, stop_requested):
            return RedbagReceipt('claimed', 18)
        async def redbag_reconcile(self, candidate):
            return RedbagReceipt('already_claimed')
        async def redbag_stop(self):
            return 'stop'
        async def close(self):
            pass
    async def forbidden(**kwargs):
        pytest.fail('Status access must not create or restore a game login')
    provider = LiveProviderFactory(runtime_builder=forbidden)()
    assert not provider.redbag_available and provider.redbag_identity is None
    provider._delegate = Delegate()
    assert provider.redbag_available and provider.redbag_identity == ('7', '123')
    assert await provider.redbag_start() == 'start'
    card = await provider.redbag_next()
    assert await provider.redbag_claim(card, stop_requested=lambda: False) == RedbagReceipt('claimed', 18)
    assert await provider.redbag_reconcile(card) == RedbagReceipt('already_claimed')
    assert await provider.redbag_stop() == 'stop'
    await provider.close()
    assert not provider.redbag_available and provider.redbag_identity is None
