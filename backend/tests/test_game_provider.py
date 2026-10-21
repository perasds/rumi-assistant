"""The API adapter must preserve all categories and each session's own cursors."""

import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from rumi_market.errors import ApiError
from rumi_market.game.market_query import (AuctionCursor, AuctionPage, AuctionQuote,
                                         SystemShopBatch, SystemShopQuote)


def provider(runtime, *, verified=True):
    from rumi_market.game_provider import IndependentGameProvider
    return IndependentGameProvider(runtime, ShopCatalog(), login_verified=verified)


class ShopCatalog:
    normal_pet_mapping = {100101: "1000101"}

    def candidates(self, kind):
        return ()


class Runtime:
    def __init__(self):
        self.closed = False
        self.can_query = True
        self.started = False
        self.market = self
        self.server_time_seconds = 1000
        self.result = SimpleNamespace(role_id=123456789, zone_name="真实响应区服",
                                      needs_activation=False, needs_initial_pet_selection=False)
        self.calls = []

    async def start(self, account, password):
        self.started = True
        return self.result

    async def query_color_page(self, cursor=None):
        self.calls.append(("color", cursor))
        row = AuctionQuote("private-listing", 1000101, 0, 55, 1234, 5)
        if cursor is None:
            return AuctionPage(0, 1, 41, (row,), "private-course",
                               AuctionCursor(begin_index=42, course="private-course"))
        return AuctionPage(0, 42, 82, (), "", None)

    async def query_system_shop(self, kind, candidates):
        self.calls.append((kind, candidates))
        if kind == "normal_pet":
            return SystemShopBatch(kind, 2, (
                SystemShopQuote(100101, 4, 4, 900, 1000, 0),
                SystemShopQuote(100101, 4, 5, 800, 1000, 0),
                SystemShopQuote(100101, 4, 4, 0, 1000, 0),
            ))
        return SystemShopBatch(kind, 1, (
            SystemShopQuote(10031, 2, 0, 50, 60, 0),
            SystemShopQuote(10032, 2, 0, 50, 0, 0),
        ))

    async def query_auction_detail(self, uid):
        self.calls.append(("detail", uid))
        return SimpleNamespace(uid=uid, lumi_id=1000101, fixed_price=0,
                               sell_role_id=987654321, is_sold_out=False,
                               up_time=1000, expire_seconds=234, extern_seconds=0)

    async def close(self):
        self.closed = True
        self.can_query = False


@pytest.mark.asyncio
@pytest.mark.parametrize('warning', [None, '工具适配版本：1.5.3 / 热更 72；游戏实际版本：1.5.3 / 热更 73', False, {}])
async def test_login_and_role_selection_preserve_only_string_version_warning(warning):
    runtime = Runtime()
    runtime.version_warning = warning
    adapter = provider(runtime)
    try:
        view = await adapter.login('a', 'p')
        expected = warning if type(warning) is str else None
        assert view.authenticated and view.message == expected
        selected = await adapter.select_role(view.role.id)
        assert selected.authenticated and selected.message == expected
    finally:
        await adapter.close()


class RankedRuntime(Runtime):
    def __init__(self):
        super().__init__()
        self.pages = [
            AuctionPage(0, 1, 11, (
                AuctionQuote("private-first", 1000102, 9, None, -1, None),
                AuctionQuote("private-zero", 1000101, 0, None, None, None),
                AuctionQuote("private-same-price", 1000103, 9, None, 2**63 - 1, None),
            ), "private-course", AuctionCursor(begin_index=12, course="private-course")),
            AuctionPage(0, 12, 22, (), "", None),
        ]

    async def query_color_page(self, cursor=None, *, page_window=None):
        if page_window is None:
            return await super().query_color_page(cursor)
        self.calls.append(("ranked_color", cursor, page_window))
        return self.pages.pop(0)


@pytest.mark.asyncio
async def test_ranked_color_uses_brief_order_and_zero_price_without_watch_or_expiry_guess():
    runtime = RankedRuntime()
    runtime.server_time_seconds = None  # Brief expiry units have not been established.
    adapter = provider(runtime)
    await adapter.login("a", "p")
    first = await adapter.fetch_ranked_color_page(None)
    assert [(row.raw_id, row.pet_id, row.direct_buy_price) for row in first.items] == [
        ("private-first", "1000102", 9), ("private-zero", "1000101", 0),
        ("private-same-price", "1000103", 9)]
    assert all(row.category == "color" and row.expires_at is None for row in first.items)
    assert not first.complete and "private" not in first.next_cursor
    second = await adapter.fetch_ranked_color_page(first.next_cursor)
    assert second.complete and second.items == [] and second.next_cursor is None
    assert runtime.calls == [
        ("ranked_color", None, 10),
        ("ranked_color", AuctionCursor(begin_index=12, course="private-course"), 10)]
    with pytest.raises(ApiError, match="^INVALID_MARKET_PAGE$"):
        await adapter.fetch_ranked_color_page(first.next_cursor)
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("uid", None), ("uid", ""), ("uid", 123),
    ("lumi_id", None), ("lumi_id", 0), ("lumi_id", -1), ("lumi_id", True),
    ("fixed_price", None), ("fixed_price", -1), ("fixed_price", True),
    ("fixed_price", 2**63),
])
async def test_ranked_color_invalid_brief_fails_safely_without_publishing_cursor(field, value):
    runtime = RankedRuntime()
    fields = dict(uid="private-invalid-row", lumi_id=1000101, fixed_price=9,
                  bid_price=None, expire_time=None, quality=None)
    fields[field] = value
    runtime.pages[0] = AuctionPage(0, 1, 11, (AuctionQuote(**fields),), "private-course",
                                 AuctionCursor(begin_index=12, course="private-course"))
    adapter = provider(runtime)
    await adapter.login("a", "p")
    with pytest.raises(ApiError) as caught:
        await adapter.fetch_ranked_color_page(None)
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    assert "private" not in str(caught.value) and caught.value.__context__ is None
    assert adapter._cursors == {} and not adapter._query_lock.locked()
    assert [call[0] for call in runtime.calls] == ["ranked_color"]
    await adapter.close()


@pytest.mark.asyncio
async def test_ranked_color_cursors_are_session_owned_and_separate_from_legacy_pages():
    runtime = RankedRuntime()
    adapter, other = provider(runtime), provider(RankedRuntime())
    await adapter.login("a", "p")
    await other.login("b", "p")
    old = await adapter.fetch_page("color", None)
    with pytest.raises(ApiError) as wrong_scope:
        await adapter.fetch_ranked_color_page(old.next_cursor)
    assert wrong_scope.value.code == "INVALID_MARKET_PAGE"
    first = await adapter.fetch_ranked_color_page(None)
    for owner, method in ((adapter, "fetch_page"), (other, "fetch_ranked_color_page")):
        with pytest.raises(ApiError) as invalid:
            if method == "fetch_page":
                await owner.fetch_page("color", first.next_cursor)
            else:
                await owner.fetch_ranked_color_page(first.next_cursor)
        assert invalid.value.code == "INVALID_MARKET_PAGE"
    with pytest.raises(ApiError):
        await adapter.fetch_page("color", old.next_cursor)
    assert [call[0] for call in runtime.calls] == ["color", "detail", "ranked_color"]
    await adapter.close()
    await other.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [True, b"private-cursor", [], "unknown-private-cursor"])
async def test_ranked_color_invalid_cursor_fails_before_query(cursor):
    runtime = RankedRuntime()
    adapter = provider(runtime)
    await adapter.login("a", "p")
    with pytest.raises(ApiError) as caught:
        await adapter.fetch_ranked_color_page(cursor)
    assert caught.value.code == "INVALID_MARKET_PAGE" and runtime.calls == []
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_page", [False, True])
async def test_ranked_color_cancelled_query_leaves_no_cursor_or_lock(next_page):
    runtime = RankedRuntime()
    adapter = provider(runtime)
    await adapter.login("a", "p")
    cursor = (await adapter.fetch_ranked_color_page(None)).next_cursor if next_page else None
    entered = asyncio.Event()
    async def blocked(cursor=None, *, page_window=None):
        assert page_window == 10
        entered.set()
        await asyncio.Event().wait()
    runtime.query_color_page = blocked
    pending = asyncio.create_task(adapter.fetch_ranked_color_page(cursor))
    await entered.wait()
    pending.cancel("private-cancellation-message")
    with pytest.raises(asyncio.CancelledError) as caught:
        await pending
    assert caught.value.args == () and caught.value.__context__ is None
    assert adapter._cursors == {} and not adapter._query_lock.locked()
    await adapter.close()


@pytest.mark.asyncio
async def test_ranked_color_late_response_after_close_cannot_restore_cursor():
    runtime = RankedRuntime()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.query_color_page
    async def blocked(cursor=None, *, page_window=None):
        entered.set()
        await release.wait()
        return await original(cursor, page_window=page_window)
    runtime.query_color_page = blocked
    adapter = provider(runtime)
    await adapter.login("a", "p")
    pending = asyncio.create_task(adapter.fetch_ranked_color_page(None))
    await entered.wait()
    await adapter.close()
    release.set()
    with pytest.raises(ApiError) as caught:
        await pending
    assert caught.value.code == "GAME_SESSION_EXPIRED" and caught.value.status_code == 401
    assert adapter._cursors == {} and not adapter._query_lock.locked()


@pytest.mark.asyncio
async def test_ranked_color_upstream_error_is_safe_and_can_retry_after_lock_release():
    runtime = RankedRuntime()
    original = runtime.query_color_page
    async def failed(cursor=None, *, page_window=None):
        raise ValueError("private-raw-response")
    runtime.query_color_page = failed
    adapter = provider(runtime)
    await adapter.login("a", "p")
    with pytest.raises(ApiError) as caught:
        await adapter.fetch_ranked_color_page(None)
    assert caught.value.code == "UPSTREAM_UNAVAILABLE" and caught.value.retryable
    assert caught.value.__context__ is None and "private" not in str(caught.value)
    assert adapter._cursors == {} and not adapter._query_lock.locked()
    runtime.query_color_page = original
    assert not (await adapter.fetch_ranked_color_page(None)).complete
    await adapter.close()


@pytest.mark.asyncio
async def test_ranked_color_and_legacy_queries_share_the_session_query_lock():
    runtime = RankedRuntime()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.query_color_page
    async def blocked(cursor=None, *, page_window=None):
        entered.set()
        await release.wait()
        return await original(cursor, page_window=page_window)
    runtime.query_color_page = blocked
    adapter = provider(runtime)
    await adapter.login("a", "p")
    ranked = asyncio.create_task(adapter.fetch_ranked_color_page(None))
    await entered.wait()
    legacy = asyncio.create_task(adapter.fetch_page("normal", None))
    await asyncio.sleep(0)
    assert runtime.calls == []
    release.set()
    assert not (await ranked).complete
    assert (await legacy).complete
    assert [call[0] for call in runtime.calls] == ["ranked_color", "normal_pet"]
    await adapter.close()


@pytest.mark.asyncio
async def test_unverified_login_is_disabled_before_any_network_start():
    runtime = Runtime()
    adapter = provider(runtime, verified=False)
    assert adapter.capabilities().methods == []
    with pytest.raises(ApiError) as exc:
        await adapter.login("private-account", "private-password")
    assert exc.value.code == "AUTH_PROTOCOL_UNVERIFIED"
    assert not runtime.started
    await adapter.close()


@pytest.mark.asyncio
async def test_login_exposes_only_own_opaque_role_key_and_real_zone_fact():
    adapter = provider(Runtime())
    view = await adapter.login("private-account", "private-password")
    assert view.authenticated and view.status == "connected"
    assert view.role.name == ""  # Unknown nickname must not be invented.
    assert view.role.server_name == "真实响应区服"
    assert "123456789" not in view.model_dump_json()
    assert adapter.capabilities().methods == ["account", "sms"]
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("is_sold_out", True), ("sell_role_id", 123456789),
    ("up_time", 766),  # Exact expiry equality is no longer an active offer.
])
async def test_color_excludes_sold_expired_and_own_listings(field, value):
    runtime = Runtime()
    original = runtime.query_auction_detail
    async def detail(uid):
        result = await original(uid)
        setattr(result, field, value)
        return result
    runtime.query_auction_detail = detail
    adapter = provider(runtime)
    await adapter.login("a", "p")
    page = await adapter.fetch_page("color", None)
    assert page.items == []
    assert not page.complete
    await adapter.close()


@pytest.mark.asyncio
async def test_color_uses_current_detail_price_and_extension_time():
    runtime = Runtime()
    original = runtime.query_auction_detail
    async def detail(uid):
        result = await original(uid)
        result.fixed_price = 777
        result.up_time = 700
        result.extern_seconds = 100
        return result
    runtime.query_auction_detail = detail
    adapter = provider(runtime)
    await adapter.login("a", "p")
    page = await adapter.fetch_page("color", None)
    assert page.items[0].direct_buy_price == 777
    assert page.items[0].expires_at == datetime.fromtimestamp(1034, timezone.utc)
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("is_sold_out", None), ("sell_role_id", None), ("up_time", None),
    ("fixed_price", -1), ("uid", "another-private-uid"), ("lumi_id", 999),
])
async def test_incomplete_or_mismatched_detail_cannot_complete_an_active_listing(field, value):
    runtime = Runtime()
    original = runtime.query_auction_detail
    async def detail(uid):
        result = await original(uid)
        setattr(result, field, value)
        return result
    runtime.query_auction_detail = detail
    adapter = provider(runtime)
    await adapter.login("a", "p")
    with pytest.raises(ApiError) as error:
        await adapter.fetch_page("color", None)
    assert error.value.code == "UPSTREAM_UNAVAILABLE"
    assert "private" not in str(error.value)
    await adapter.close()


@pytest.mark.asyncio
async def test_missing_detail_is_not_a_visible_direct_offer():
    runtime = Runtime()
    async def detail(uid):
        return None
    runtime.query_auction_detail = detail
    adapter = provider(runtime)
    await adapter.login("a", "p")
    assert (await adapter.fetch_page("color", None)).items == []
    await adapter.close()


@pytest.mark.asyncio
async def test_normal_shop_requires_both_source_price_fields():
    runtime = Runtime()
    async def quote(kind, candidates):
        return SystemShopBatch(kind, 2, (
            SystemShopQuote(100101, 4, 4, 900, 0, 0),
            SystemShopQuote(100101, 4, 5, 800, 1000, 0),
        ))
    runtime.query_system_shop = quote
    adapter = provider(runtime)
    await adapter.login("a", "p")
    rows = (await adapter.fetch_page("normal", None)).items
    assert [row.direct_buy_price for row in rows] == [800]
    await adapter.close()


@pytest.mark.asyncio
async def test_all_paging_reaches_both_shop_types_and_keeps_quality_prices():
    runtime = Runtime()
    adapter = provider(runtime)
    await adapter.login("a", "p")
    first = await adapter.fetch_page("all", None)
    assert first.items[0].direct_buy_price == 0
    assert first.items[0].expires_at == datetime.fromtimestamp(1234, timezone.utc)
    assert not first.complete
    assert "private-course" not in first.next_cursor
    second = await adapter.fetch_page("all", first.next_cursor)
    assert not second.complete
    normal = await adapter.fetch_page("all", second.next_cursor)
    assert [(row.pet_id, row.direct_buy_price) for row in normal.items] == [
        ("1000101", 900), ("1000101", 800)]
    assert len({row.raw_id for row in normal.items}) == 2
    assert not normal.complete
    items = await adapter.fetch_page("all", normal.next_cursor)
    assert items.complete and items.next_cursor is None
    assert [row.direct_buy_price for row in items.items] == [50]
    assert [row.item_id for row in items.items] == ["10031"]
    assert [call[0] for call in runtime.calls] == ["color", "detail", "color", "normal_pet", "item"]
    await adapter.close()


@pytest.mark.asyncio
async def test_other_session_cannot_use_a_private_cursor():
    first, second = provider(Runtime()), provider(Runtime())
    await first.login("a", "p")
    await second.login("b", "p")
    page = await first.fetch_page("color", None)
    with pytest.raises(ApiError):
        await second.fetch_page("color", page.next_cursor)
    with pytest.raises(ApiError):
        await first.fetch_page("normal", page.next_cursor)
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_new_refresh_invalidates_old_cursor_and_close_stops_queries():
    runtime = Runtime()
    adapter = provider(runtime)
    await adapter.login("a", "p")
    old = await adapter.fetch_page("color", None)
    await adapter.fetch_page("color", None)
    with pytest.raises(ApiError):
        await adapter.fetch_page("color", old.next_cursor)
    await adapter.close()
    assert runtime.closed
    with pytest.raises(ApiError) as exc:
        await adapter.fetch_page("all", None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_native_gate_is_shown_without_a_connected_session_or_fake_role():
    runtime = Runtime()
    runtime.result.needs_initial_pet_selection = True
    runtime.can_query = False
    adapter = provider(runtime)
    view = await adapter.login("a", "p")
    assert view.status == "needs_verification"
    assert not view.authenticated and view.role is None
    assert runtime.closed


@pytest.mark.asyncio
async def test_unknown_normal_mapping_fails_instead_of_dropping_a_visible_row():
    runtime = Runtime()
    adapter = provider(runtime)
    adapter._shop.normal_pet_mapping = {}
    await adapter.login("a", "p")
    with pytest.raises(ApiError):
        await adapter.fetch_page("normal", None)
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("category,kind,outer,id,quality", [
    ("normal", "normal_pet", 2, 100101, 4), ("item", "item", 1, 10031, 0),
])
async def test_proto3_omitted_inner_type_uses_verified_outer_category(category, kind, outer, id, quality):
    from rumi_market.game.proto import GameProtoCodec
    codec = GameProtoCodec()
    raw = codec.encode("SNKProto.Cmd_SystemShop_QueryItemPrice_Ack", {
        "type": outer, "item_list": [{"id": id, "quality": quality,
                                      "price": 80, "today_init_price": 90}],
    })
    ack = codec.decode("SNKProto.Cmd_SystemShop_QueryItemPrice_Ack", raw).message
    assert ack.item_list[0].type == 0
    runtime = Runtime()
    async def quote(kind, candidates):
        return SystemShopBatch(kind, ack.type, tuple(SystemShopQuote(
            row.id, row.type, row.quality, row.price, row.today_init_price, row.today_init_time)
            for row in ack.item_list))
    runtime.query_system_shop = quote
    adapter = provider(runtime)
    await adapter.login("a", "p")
    page = await adapter.fetch_page(category, None)
    assert page.complete and page.items[0].category == category
    assert page.items[0].direct_buy_price == 80
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["color", "normal", "item"])
async def test_late_query_result_after_close_cannot_rebuild_private_cursors(category):
    import asyncio
    runtime = Runtime()
    entered, release = asyncio.Event(), asyncio.Event()
    original_color, original_shop = runtime.query_color_page, runtime.query_system_shop
    async def color(cursor=None):
        entered.set()
        await release.wait()
        return await original_color(cursor)
    async def shop(kind, candidates):
        entered.set()
        await release.wait()
        return await original_shop(kind, candidates)
    runtime.query_color_page, runtime.query_system_shop = color, shop
    adapter = provider(runtime)
    await adapter.login("a", "p")
    pending = asyncio.create_task(adapter.fetch_page(category, None))
    await entered.wait()
    await adapter.close()
    release.set()
    with pytest.raises(ApiError) as failure:
        await pending
    assert failure.value.status_code == 401
    assert adapter._cursors == {}


@pytest.mark.asyncio
async def test_adapter_api_refresh_name_filter_and_logout_do_not_share_snapshots(tmp_path):
    import asyncio
    import json
    import httpx
    from rumi_market.app import create_app
    from rumi_market.catalog import PNG_SIGNATURE
    from login_helpers import async_login_body
    images = tmp_path / "pets/images"
    images.mkdir(parents=True)
    (images / "1000101.png").write_bytes(PNG_SIGNATURE + b"synthetic avatar")
    catalog_path = images.parent / "catalog.json"
    catalog_path.write_text(json.dumps({"pets": [{"pet_id": "1000101", "name": "蜜桃",
        "name_verified": True, "sources": ["synthetic fixture"],
        "image_path": "images/1000101.png"}]}), encoding="utf-8")
    app = create_app(provider_factory=lambda: provider(Runtime()), catalog_path=catalog_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (httpx.AsyncClient(transport=transport, base_url="http://test") as first,
                    httpx.AsyncClient(transport=transport, base_url="http://test") as second):
            body = await async_login_body(first, "fixture", "fixture")
            assert (await first.post("/api/auth/login", json=body)).status_code == 200
            assert (await second.get("/api/market/listings")).status_code == 401
            response = await first.post("/api/market/refresh", json={"category": "all"})
            assert response.status_code == 200
            async with asyncio.timeout(2):
                while True:
                    snapshot = (await first.get("/api/market/listings", params={"query": "蜜桃"})).json()
                    if snapshot["status"] != "loading":
                        break
                    await asyncio.sleep(0)
            assert snapshot["complete"] is True
            assert snapshot["loaded_count"] == 4 and snapshot["matched_count"] == 3
            assert {row["category"] for row in snapshot["items"]} == {"color", "normal"}
            assert all(row["name"] == "蜜桃" for row in snapshot["items"])
            assert all(row["image_url"] == "/api/pets/1000101/image" for row in snapshot["items"])
            assert "private-listing" not in str(snapshot)
            assert (await first.delete("/api/session")).status_code == 200
            assert (await first.get("/api/market/listings")).status_code == 401
