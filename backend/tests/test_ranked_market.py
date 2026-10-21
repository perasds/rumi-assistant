import asyncio

import pytest

from rumi_market.providers import MarketPage
from providers import DelayedProvider, ScriptedProvider
from test_market import clients, connect, finished, row


class RankedProvider(ScriptedProvider):
    async def fetch_ranked_color_page(self, cursor):
        return await self.fetch_page("ranked_color", cursor)


async def start_ranked(client, **filters):
    response = await client.post("/api/market/refresh", json={
        "category": "color", "query": "", "pet_ids": [], "limit": 10, **filters,
    })
    assert response.status_code == 200
    assert response.json()["scope"] == "top10"
    assert response.json()["status"] != "loading"
    return response.json()


async def test_ranked_stops_at_ten_and_keeps_server_order(catalog_path):
    prices = [90, 10, 80, 20, 70, 30, 60, 40, 50, 0, 1, 2]
    provider = RankedProvider([MarketPage([
        row(f"raw-{index}", price=price) for index, price in enumerate(prices)
    ], next_cursor="must-not-follow", total_count=200)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        result = await start_ranked(client)
        assert result["scope"] == "top10"
        assert result["complete"] is True
        assert result["status"] == "complete"
        assert [item["direct_buy_price"] for item in result["items"]] == prices[:10]
        assert result["loaded_count"] == result["matched_count"] == 10
        assert result["total_count"] is None
        assert provider.calls == [("ranked_color", None)]
        assert "must-not-follow" not in str(result)
        assert "raw-0" not in str(result)


async def test_ranked_filters_name_and_ids_before_deduplication_and_limit(catalog_path):
    provider = RankedProvider([
        MarketPage([row(f"other-{i}", "101") for i in range(12)] + [
            row("same", "102", price=200), row("same", "102", price=200),
            row("unnamed", "104"),
        ], next_cursor="next-page"),
        MarketPage([row(f"match-{i}", "102", price=300 + i) for i in range(15)],
                   next_cursor="unused-page"),
    ])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        result = await start_ranked(client, query=" 蜜桃 ", pet_ids=["102", "104"])
        assert result["complete"] is True
        assert [item["pet_id"] for item in result["items"]] == ["102"] * 10
        assert [item["direct_buy_price"] for item in result["items"]] == [200, *range(300, 309)]
        assert len({item["listing_key"] for item in result["items"]}) == 10
        assert provider.calls == [("ranked_color", None), ("ranked_color", "next-page")]


async def test_ranked_unknown_name_finishes_without_game_requests(catalog_path):
    provider = RankedProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        result = await start_ranked(client, query="没有已核实名称")
        assert result["items"] == []
        assert result["complete"] is True
        assert result["scope"] == "top10"
        assert provider.calls == []


async def test_top_ten_get_cannot_relabel_old_results_as_new_filter(catalog_path):
    provider = RankedProvider([MarketPage([row("one", "102")], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        await start_ranked(client, query="蜜桃", pet_ids=["102"])
        for params in [{"query": "灰灰"}, {"query": ""},
                       {"query": "蜜桃"}, {"query": "蜜桃", "pet_ids": "101"}]:
            response = await client.get("/api/market/listings", params=params)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "MARKET_FILTER_CHANGED"
        same = await client.get("/api/market/listings", params={
            "query": " 蜜桃 ", "pet_ids": "102", "category": "color"})
        assert same.status_code == 200
        assert len(same.json()["items"]) == 1
        assert len(provider.calls) == 1


async def test_ranked_post_normalizes_explicit_ids_like_poll_query(catalog_path):
    provider = RankedProvider([MarketPage([row("one", "102")], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        result = await start_ranked(client, query="蜜桃", pet_ids=[" 102 ", ""])
        assert [item["pet_id"] for item in result["items"]] == ["102"]
        poll = await client.get("/api/market/listings", params={"query": "蜜桃", "pet_ids": "102"})
        assert poll.status_code == 200


async def test_ranked_less_than_ten_finishes_only_at_explicit_page_end(catalog_path):
    provider = RankedProvider([
        MarketPage([], next_cursor="empty-page"),
        MarketPage([row("one", price=0), row("two", "102", price=50)], complete=True),
    ])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        result = await start_ranked(client)
        assert result["complete"] is True
        assert [item["direct_buy_price"] for item in result["items"]] == [0, 50]
        assert len(provider.calls) == 2


async def test_ranked_repeated_cursor_is_partial_and_not_fake_top_ten(catalog_path):
    provider = RankedProvider([
        MarketPage([row("one")], next_cursor="same"),
        MarketPage([row("two")], next_cursor="same"),
    ])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        result = await start_ranked(client)
        assert result["scope"] == "top10"
        assert result["status"] == "partial"
        assert result["complete"] is False
        assert len(provider.calls) == 2


@pytest.mark.parametrize("body", [
    {"category": "normal", "limit": 10},
    {"category": "all", "limit": 10},
    {"category": "item", "limit": 10},
    {"category": "color", "limit": 9},
    {"category": "color", "limit": 11},
    {"category": "color", "limit": "10"},
    {"category": "color", "limit": 10.0},
    {"category": "color", "limit": True},
    {"category": "color", "query": "蜜桃"},
    {"category": "color", "pet_ids": ["101"]},
])
async def test_ranked_input_contract_is_explicit(catalog_path, body):
    provider = RankedProvider()
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        response = await client.post("/api/market/refresh", json=body)
        assert response.status_code == 422
        assert provider.calls == []


class RankedDelayedProvider(DelayedProvider):
    async def fetch_ranked_color_page(self, cursor):
        return await self.fetch_page("ranked_color", cursor)


async def test_new_ranked_refresh_discards_old_late_page(catalog_path):
    provider = RankedDelayedProvider(MarketPage([row("old", "101")], complete=True),
                                    MarketPage([row("new", "102")], complete=True))
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        old_request = asyncio.create_task(client.post("/api/market/refresh", json={"category": "color", "limit": 10}))
        await asyncio.wait_for(provider.fetch_started.wait(), 1)
        fresh = await start_ranked(client, query="蜜桃", pet_ids=["102"])
        old_response = await asyncio.wait_for(old_request, 1)
        assert old_response.status_code == 409
        assert old_response.json()["error"]["code"] == "MARKET_QUERY_REPLACED"
        provider.release_old.set()
        await asyncio.wait_for(provider.old_finished.wait(), 1)
        result = (await client.get("/api/market/listings")).json()
        assert result["refresh_id"] == fresh["refresh_id"]
        assert [item["pet_id"] for item in result["items"]] == ["102"]


async def test_legacy_unbounded_refresh_still_uses_full_path(catalog_path):
    provider = RankedProvider([
        MarketPage([row("one")], next_cursor="more"),
        MarketPage([row("two", "102")], complete=True),
    ])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        assert (await client.post("/api/market/refresh", json={"category": "color"})).status_code == 200
        result = await finished(client)
        assert result["scope"] == "all"
        assert len(result["items"]) == 2
        assert provider.calls == [("color", None), ("color", "more")]


async def test_live_lazy_entry_forwards_ranked_query_without_watch(catalog_path):
    from types import SimpleNamespace
    from test_live_runtime import Runtime, factory, shop
    calls = []
    runtime = Runtime()
    async def color_page(cursor, *, page_window=None):
        calls.append((cursor, page_window))
        return SimpleNamespace(quotes=(SimpleNamespace(uid="test-native-quote", lumi_id=101,
            fixed_price=75),), next_cursor=None)
    async def no_watch(*args):
        pytest.fail("Native list must not watch details")
    runtime.market = SimpleNamespace(query_color_page=color_page, query_auction_detail=no_watch)
    async def builder():
        return runtime, shop()
    async with clients(catalog_path, factory(builder)) as (client, _):
        await connect(client)
        result = await start_ranked(client)
        assert result["complete"] is True
        assert [item["direct_buy_price"] for item in result["items"]] == [75]
        assert result["items"][0]["expires_at"] is None
        assert calls == [(None, 10)]
    assert runtime.closed
