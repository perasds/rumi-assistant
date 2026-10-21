import asyncio

import pytest

from rumi_market.errors import ApiError
from rumi_market.providers import MarketPage
from test_market import clients, connect, row
from test_ranked_market import RankedDelayedProvider, RankedProvider


class WaitingProvider(RankedProvider):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def fetch_ranked_color_page(self, cursor):
        self.calls.append(("ranked_color", cursor))
        self.fetch_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return MarketPage([row("test-direct", price=75)], complete=True)

    async def close(self):
        self.release.set()
        await super().close()


async def test_post_waits_for_game_result_and_returns_it_without_a_get(catalog_path):
    provider = WaitingProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        pending = asyncio.create_task(client.post("/api/market/refresh", json={"category": "color", "limit": 10}))
        try:
            await asyncio.wait_for(provider.fetch_started.wait(), 1)
            done, _ = await asyncio.wait({pending}, timeout=0.02)
            assert not done, "POST must stay pending until the game result arrives"
        finally:
            provider.release.set()
            response = await asyncio.wait_for(pending, 1)
        assert response.status_code == 200
        result = response.json()
        assert result["complete"] is True
        assert result["status"] == "complete"
        assert result["loaded_count"] == result["matched_count"] == 1
        assert result["items"][0]["direct_buy_price"] == 75
        assert provider.calls == [("ranked_color", None)]


async def test_query_timeout_returns_error_and_late_page_cannot_change_state(catalog_path):
    provider = RankedDelayedProvider(MarketPage([row("late")], complete=True),
                                     MarketPage([row("new")], complete=True))
    async with clients(catalog_path, lambda: provider, market_timeout=0.02) as (client, _):
        await connect(client)
        try:
            response = await asyncio.wait_for(client.post("/api/market/refresh", json={"category": "color", "limit": 10}), 1)
            assert response.status_code == 504
            assert response.json()["error"]["code"] == "MARKET_QUERY_TIMEOUT"
            assert response.json()["error"]["retryable"] is True
        finally:
            provider.release_old.set()
        await asyncio.wait_for(provider.old_finished.wait(), 1)
        result = (await client.get("/api/market/listings")).json()
        assert result["status"] == "failed"
        assert result["items"] == []
        retry = await client.post("/api/market/refresh", json={"category": "color", "limit": 10})
        assert retry.status_code == 200
        assert retry.json()["complete"] is True


async def test_logout_ends_waiting_query_and_returns_401(catalog_path):
    provider = WaitingProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        pending = asyncio.create_task(client.post("/api/market/refresh", json={"category": "color", "limit": 10}))
        await asyncio.wait_for(provider.fetch_started.wait(), 1)
        assert (await asyncio.wait_for(client.delete("/api/session"), 1)).status_code == 200
        response = await asyncio.wait_for(pending, 1)
        assert response.status_code == 401
        assert "items" not in response.json()


async def test_game_expiry_is_returned_by_the_same_post(catalog_path):
    provider = RankedProvider([ApiError("GAME_SESSION_EXPIRED", "test-private-body", 401)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        response = await client.post("/api/market/refresh", json={"category": "color", "limit": 10})
        assert response.status_code == 401
        assert "test-private-body" not in response.text


async def test_cancelled_http_handler_cancels_its_game_query(catalog_path):
    provider = WaitingProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        pending = asyncio.create_task(client.post("/api/market/refresh", json={"category": "color", "limit": 10}))
        await asyncio.wait_for(provider.fetch_started.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(provider.cancelled.wait(), 1)
        result = (await client.get("/api/market/listings")).json()
        assert result["status"] == "failed"
        assert result["items"] == []
