import asyncio
from contextlib import asynccontextmanager

import httpx

from rumi_market.app import create_app
from rumi_market.errors import ApiError
from rumi_market.models import Capabilities, QrPollView
from rumi_market.providers import MarketPage, ProviderListing

from providers import DelayedProvider, ScriptedProvider
from login_helpers import async_login_body


def row(raw_id, pet_id="101", price=100, category="color"):
    return ProviderListing(raw_id, category, pet_id, price)


@asynccontextmanager
async def clients(catalog_path, factory, **app_options):
    app = create_app(provider_factory=factory, catalog_path=catalog_path, **app_options)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as one:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as two:
                yield one, two


async def connect(client, account="one"):
    response = await client.post("/api/auth/login", json=await async_login_body(client, account, "test"))
    assert response.status_code == 200
    assert response.json()["authenticated"] is True


async def finished(client):
    for _ in range(30):
        response = await client.get("/api/market/listings")
        assert response.status_code == 200
        data = response.json()
        if data["status"] != "loading":
            return data
        await asyncio.sleep(0)
    raise AssertionError("refresh did not settle")


async def test_pages_deduplicate_and_require_explicit_end(catalog_path):
    provider = ScriptedProvider([
        MarketPage([row("raw-one")], next_cursor=b"private-cursor"),
        MarketPage([row("raw-one"), row("raw-two", "102"), row("raw-three", "104")],
                   complete=True, total_count=3),
    ])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        response = await client.post("/api/market/refresh", json={"category": "all"})
        assert response.status_code == 200
        data = await finished(client)
        assert data["complete"] is True
        assert data["status"] == "complete"
        assert data["loaded_count"] == data["matched_count"] == 3
        assert data["total_count"] == 3
        assert len({item["listing_key"] for item in data["items"]}) == 3
        assert data["name_coverage"] == {"named": 2, "total": 3}
        assert provider.calls == [("all", None), ("all", b"private-cursor")]
        assert "raw-one" not in str(data)
        assert "private-cursor" not in str(data)


async def test_name_and_explicit_ids_are_intersected(catalog_path):
    provider = ScriptedProvider([MarketPage([
        row("one", "101"), row("two", "102"), row("three", "104")], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        assert (await client.post("/api/market/refresh", json={})).status_code == 200
        await finished(client)
        response = await client.get("/api/market/listings", params={
            "query": "蜜桃", "pet_ids": "102,104"})
        assert [item["pet_id"] for item in response.json()["items"]] == ["102"]
        assert response.json()["loaded_count"] == 3
        assert response.json()["matched_count"] == 1
        assert (await client.get("/api/market/listings", params={"query": "未知"})).json()["items"] == []


async def test_different_cookies_do_not_share_roles_or_listings(catalog_path):
    made = []

    def factory():
        provider = ScriptedProvider([MarketPage([row(f"raw-{len(made)}")], complete=True)])
        made.append(provider)
        return provider

    async with clients(catalog_path, factory) as (one, two):
        await connect(one, "one")
        await connect(two, "two")
        assert (await one.post("/api/market/refresh", json={})).status_code == 200
        assert (await finished(one))["loaded_count"] == 1
        assert (await two.get("/api/market/listings")).json()["loaded_count"] == 0
        assert (await one.get("/api/session")).json()["role"]["id"] == "role-one"
        assert (await two.get("/api/session")).json()["role"]["id"] == "role-two"
        assert one.cookies["rumi_session"] != two.cookies["rumi_session"]


async def test_repeated_cursor_stops_with_partial_results(catalog_path):
    provider = ScriptedProvider([
        MarketPage([row("one")], next_cursor="same-cursor"),
        MarketPage([row("two", "102")], next_cursor="same-cursor"),
    ])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        assert (await client.post("/api/market/refresh", json={})).status_code == 200
        data = await finished(client)
        assert data["status"] == "partial"
        assert data["complete"] is False
        assert data["loaded_count"] == 2
        assert len(provider.calls) == 2
        assert "重复" in data["message"]


async def test_failure_does_not_expose_raw_upstream_body(catalog_path):
    provider = ScriptedProvider([MarketPage([row("one")], next_cursor="next"),
                                 RuntimeError("private-test-authentication-material")])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        assert (await client.post("/api/market/refresh", json={})).status_code == 200
        data = await finished(client)
        assert data["status"] == "partial"
        assert data["complete"] is False
        assert "private-test-authentication-material" not in str(data)


async def test_new_refresh_discards_late_old_response(catalog_path):
    provider = DelayedProvider(MarketPage([row("old", "101")], complete=True),
                               MarketPage([row("fresh", "102")], complete=True))
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        first = await client.post("/api/market/refresh", json={})
        assert first.status_code == 200
        await asyncio.wait_for(provider.fetch_started.wait(), timeout=1)
        second = await client.post("/api/market/refresh", json={})
        assert second.status_code == 200
        assert first.json()["refresh_id"] != second.json()["refresh_id"]
        data = await finished(client)
        assert [item["pet_id"] for item in data["items"]] == ["102"]
        provider.release_old.set()
        await asyncio.wait_for(provider.old_finished.wait(), timeout=1)
        after = (await client.get("/api/market/listings")).json()
        assert after["refresh_id"] == second.json()["refresh_id"]
        assert [item["pet_id"] for item in after["items"]] == ["102"]


async def test_logout_clears_connection_cookie_and_refresh(catalog_path):
    provider = DelayedProvider(MarketPage([row("old")], complete=True), MarketPage([], complete=True))
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        assert (await client.post("/api/market/refresh", json={})).status_code == 200
        await asyncio.wait_for(provider.fetch_started.wait(), timeout=1)
        response = await client.delete("/api/session")
        assert response.status_code == 200
        assert response.json()["status"] == "anonymous"
        assert provider.closed is True
        assert "rumi_session" not in client.cookies
        assert (await client.get("/api/market/listings")).status_code == 401


async def test_game_session_expiry_clears_provider(catalog_path):
    provider = ScriptedProvider([ApiError("GAME_SESSION_EXPIRED", "游戏会话已过期", 401)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        assert (await client.post("/api/market/refresh", json={})).status_code == 200
        for _ in range(20):
            session = (await client.get("/api/session")).json()
            if session["status"] == "expired":
                break
            await asyncio.sleep(0)
        assert session["status"] == "expired"
        assert session["authenticated"] is False
        assert provider.closed is True
        assert (await client.get("/api/market/listings")).status_code == 401


async def test_qr_challenges_belong_to_one_cookie_only(catalog_path):
    async with clients(catalog_path, ScriptedProvider) as (one, two):
        response = await one.post("/api/auth/qr")
        assert response.status_code == 200
        challenge = response.json()
        assert challenge["challenge_id"] != "private-upstream-task"
        own = await one.get(f"/api/auth/qr/{challenge['challenge_id']}")
        assert own.status_code == 200
        assert own.json()["status"] == "waiting"
        foreign = await two.get(f"/api/auth/qr/{challenge['challenge_id']}")
        assert foreign.status_code == 404
        assert (await one.get("/api/session")).json()["authenticated"] is False
        await one.delete("/api/session")
        assert (await one.get(f"/api/auth/qr/{challenge['challenge_id']}")).status_code == 404


async def test_role_selection_cannot_use_other_sessions_role(catalog_path):
    async with clients(catalog_path, ScriptedProvider) as (one, two):
        await connect(one, "one")
        await connect(two, "two")
        rejected = await two.post("/api/session/role", json={"role_id": "role-one"})
        assert rejected.status_code == 404
        allowed = await one.post("/api/session/role", json={"role_id": "role-one"})
        assert allowed.status_code == 200
        assert allowed.json()["role"]["id"] == "role-one"


async def test_unverified_capability_blocks_even_a_success_returning_provider(catalog_path):
    class UnreadyProvider(ScriptedProvider):
        def capabilities(self):
            return Capabilities()

    async with clients(catalog_path, UnreadyProvider) as (client, _):
        response = await client.post("/api/auth/login", json=await async_login_body(client, "one", "test"))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AUTH_PROTOCOL_UNVERIFIED"
        qr = await client.post("/api/auth/qr")
        assert qr.status_code == 503
        assert (await client.get("/api/session")).json()["authenticated"] is False


async def test_upstream_auth_error_cannot_supply_public_error_text(catalog_path):
    class RejectingProvider(ScriptedProvider):
        async def login(self, account, password):
            raise ApiError("AUTH_REJECTED", "upstream-private-test-credential", 401)

    async with clients(catalog_path, RejectingProvider) as (client, _):
        response = await client.post("/api/auth/login", json=await async_login_body(client, "one", "test"))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_REJECTED"
        assert "upstream-private-test-credential" not in response.text
        assert (await client.get("/api/session")).json()["authenticated"] is False


async def test_missing_cursor_cannot_be_mistaken_for_completion(catalog_path):
    provider = ScriptedProvider([MarketPage([row("one")], complete=False)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        assert (await client.post("/api/market/refresh", json={})).status_code == 200
        data = await finished(client)
        assert data["status"] == "partial"
        assert data["complete"] is False
        assert len(provider.calls) == 1


async def test_relogin_clears_old_account_listings(catalog_path):
    async with clients(catalog_path, lambda: ScriptedProvider([
            MarketPage([row("one")], complete=True)])) as (client, _):
        await connect(client, "one")
        await client.post("/api/market/refresh", json={})
        assert (await finished(client))["loaded_count"] == 1
        await connect(client, "two")
        assert (await client.get("/api/market/listings")).json()["status"] == "idle"


async def test_category_filter_keeps_loaded_count(catalog_path):
    provider = ScriptedProvider([MarketPage([row("one"), row("two", "102", category="normal")],
                                            complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        await client.post("/api/market/refresh", json={})
        await finished(client)
        result = (await client.get("/api/market/listings", params={"category": "normal"})).json()
        assert result["loaded_count"] == 2
        assert result["matched_count"] == 1
        assert result["items"][0]["category"] == "normal"


async def test_qr_confirmation_clears_previous_accounts_listings(catalog_path):
    class ConfirmingProvider(ScriptedProvider):
        async def poll_qr(self, handle):
            view = await self.login("qr-account", "test")
            return QrPollView(status="confirmed", session=view)

    factory = lambda: ConfirmingProvider([MarketPage([row("previous-account-row")], complete=True)])
    async with clients(catalog_path, factory) as (client, _):
        # Create the challenge while anonymous so QR-start cleanup cannot hide
        # a missing cleanup at confirmation itself.
        challenge = (await client.post("/api/auth/qr")).json()
        await connect(client, "previous-account")
        assert (await client.post("/api/market/refresh", json={})).status_code == 200
        assert (await finished(client))["loaded_count"] == 1
        confirmation = await client.get(f"/api/auth/qr/{challenge['challenge_id']}")
        assert confirmation.status_code == 200
        assert confirmation.json()["session"]["role"]["id"] == "role-qr-account"
        after = (await client.get("/api/market/listings")).json()
        assert after["status"] == "idle"
        assert after["items"] == []
        assert after["loaded_count"] == 0


async def test_expired_session_gets_new_provider_before_starting_qr(catalog_path):
    made = []

    class CannotReuseClosedProvider(ScriptedProvider):
        async def start_qr(self):
            if self.closed:
                raise RuntimeError("a closed provider cannot start authentication")
            return await super().start_qr()

    def factory():
        provider = CannotReuseClosedProvider([
            ApiError("GAME_SESSION_EXPIRED", "游戏会话已过期", 401)])
        made.append(provider)
        return provider

    async with clients(catalog_path, factory) as (client, _):
        await connect(client)
        assert (await client.post("/api/market/refresh", json={})).status_code == 200
        for _ in range(20):
            session = (await client.get("/api/session")).json()
            if session["status"] == "expired":
                break
            await asyncio.sleep(0)
        assert session["status"] == "expired"
        assert made[0].closed is True
        response = await client.post("/api/auth/qr")
        assert response.status_code == 200
        assert len(made) == 2
        assert made[1].closed is False
        assert (await client.get("/api/session")).json()["authenticated"] is False


async def test_item_names_and_icons_join_config_id_and_support_name_filter(catalog_path):
    from test_item_catalog import save_item_catalog

    save_item_catalog(catalog_path.parent.parent / "market")
    provider = ScriptedProvider([MarketPage([
        ProviderListing("item-raw-id", "item", None, 345, item_id="10031"),
        ProviderListing("unknown-item-raw-id", "item", None, 123, item_id="19999"),
        row("pet-raw-id"),
    ], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        await client.post("/api/market/refresh", json={})
        data = await finished(client)
        item = next(item for item in data["items"] if item["direct_buy_price"] == 345)
        assert item["name"] == item["display_name"] == "噜咪卡"
        assert item["image_url"] == "/api/items/10031/image"
        assert item["pet_id"] is None
        assert "item_id" not in item
        unknown = next(item for item in data["items"] if item["direct_buy_price"] == 123)
        assert unknown["name"] is None
        assert unknown["display_name"] == "未识别物品"
        assert unknown["image_url"] is None
        assert data["name_coverage"] == {"named": 1, "total": 1}
        filtered = (await client.get("/api/market/listings", params={"query": "噜咪卡"})).json()
        assert filtered["matched_count"] == 1
        assert filtered["items"][0]["image_url"] == "/api/items/10031/image"
        assert "item-raw-id" not in str(data)
        assert "raw_by_listing_key" not in str(data)
        assert (await client.get("/api/items/10031/image")).status_code == 200


async def test_missing_item_image_still_preserves_verified_name(catalog_path):
    from test_item_catalog import save_item_catalog

    path = save_item_catalog(catalog_path.parent.parent / "market")
    (path.parent / "images/10031.png").unlink()
    provider = ScriptedProvider([MarketPage([
        ProviderListing("item-raw-id", "item", None, 345, item_id="10031"),
    ], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        await client.post("/api/market/refresh", json={})
        data = await finished(client)
        assert data["items"][0]["name"] == "噜咪卡"
        assert data["items"][0]["image_url"] is None


async def test_pet_join_takes_precedence_over_incidental_item_configuration_id(catalog_path):
    from test_item_catalog import save_item_catalog

    save_item_catalog(catalog_path.parent.parent / "market")
    provider = ScriptedProvider([MarketPage([
        ProviderListing("pet-raw-id", "color", "101", 345, item_id="10031"),
    ], complete=True)])
    async with clients(catalog_path, lambda: provider) as (client, _):
        await connect(client)
        await client.post("/api/market/refresh", json={})
        data = await finished(client)
        assert data["items"][0]["name"] == "蜜桃"
        assert data["items"][0]["image_url"] is None


async def test_raw_lookup_is_session_owned_private_and_cleared_on_refresh_and_logout(catalog_path):
    from rumi_market.catalog import Catalog
    from rumi_market.market import MarketService
    from rumi_market.sessions import SessionStore

    catalog = Catalog(catalog_path)
    sessions = SessionStore(lambda: ScriptedProvider([
        MarketPage([row("private-raw-one")], complete=True),
        MarketPage([row("private-raw-two")], complete=True),
    ]))
    market = MarketService(catalog, sessions)
    one, two = sessions.create(), sessions.create()
    try:
        for session in (one, two):
            session.view = await session.provider.login("test", "test")
            market.start(session, "all")
            await asyncio.wait_for(session.market.task, 1)
        old = one.market
        other = two.market
        key = old.snapshot.items[0].listing_key
        assert old.raw_by_listing_key == {key: "private-raw-one"}
        assert other.raw_by_listing_key != old.raw_by_listing_key
        assert key not in other.raw_by_listing_key
        assert "private-raw-one" not in repr(old)
        assert "private-raw-one" not in market.snapshot(one).model_dump_json()
        assert "raw_by_listing_key" not in market.snapshot(one).model_dump_json()
        market.start(one, "all")
        assert old.raw_by_listing_key == {}
        assert old.raw_ids == set()
        await asyncio.wait_for(one.market.task, 1)
        current = one.market
        assert set(current.raw_by_listing_key.values()) == {"private-raw-two"}
        assert len(other.raw_by_listing_key) == 1
        await sessions.remove(one)
        assert one.market is None
        assert current.raw_by_listing_key == {}
        assert current.raw_ids == set()
        assert len(other.raw_by_listing_key) == 1
    finally:
        await sessions.close()
        catalog.close()


async def test_late_cancelled_page_cannot_restore_discarded_raw_lookup(catalog_path):
    from rumi_market.catalog import Catalog
    from rumi_market.market import MarketService
    from rumi_market.sessions import SessionStore

    provider = DelayedProvider(MarketPage([row("late-private-raw")], complete=True),
                               MarketPage([row("new-private-raw")], complete=True))
    catalog = Catalog(catalog_path)
    sessions = SessionStore(lambda: provider)
    session = sessions.create()
    market = MarketService(catalog, sessions)
    try:
        session.view = await provider.login("test", "test")
        market.start(session, "all")
        old = session.market
        await asyncio.wait_for(provider.fetch_started.wait(), 1)
        market.start(session, "all")
        await asyncio.wait_for(session.market.task, 1)
        provider.release_old.set()
        await asyncio.wait_for(provider.old_finished.wait(), 1)
        assert old.raw_by_listing_key == {}
        assert set(session.market.raw_by_listing_key.values()) == {"new-private-raw"}
    finally:
        provider.release_old.set()
        await sessions.close()
        catalog.close()
