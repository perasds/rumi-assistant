"""Verified static candidates are RPC inputs, not current prices or stock."""

from dataclasses import FrozenInstanceError
import json
from types import SimpleNamespace

import pytest


def load(path=None):
    from rumi_market.game.shop_catalog import load_shop_catalog

    return load_shop_catalog(path)


def test_pinned_directory_has_304_pairs_152_mapped_pets_and_five_items():
    catalog = load()
    assert len(catalog.normal_pets) == 304 and len(catalog.items) == 5
    assert len(catalog.normal_pet_mapping) == 152
    assert len({(row.config_id, row.quality) for row in catalog.normal_pets}) == 304
    assert {row.quality for row in catalog.normal_pets} == {4, 5}
    assert {row.config_id for row in catalog.items} == {10031, 10032, 10033, 10034, 10035}
    assert all(row.quality == 0 and row.pet_id is None and row.name is None
               and row.image_path is None for row in catalog.items)


def test_normal_pet_metadata_uses_existing_verified_name_mapping():
    from rumi_market.game.shop_catalog import SHOP_CATALOG_PATH

    names = json.loads((SHOP_CATALOG_PATH.parent.parent / "pets" / "names.json")
                       .read_text(encoding="utf-8"))["names"]
    catalog = load()
    assert catalog.normal_pet_mapping[100101] == "100101"
    for row in catalog.normal_pets:
        assert row.pet_id == catalog.normal_pet_mapping[row.config_id]
        assert row.name == names[row.pet_id]["name"]
        assert names[row.pet_id]["official_configuration_field_name_confirmed"] is True
        assert row.image_path and row.image_sha256


def test_fresh_zero_is_explicit_request_state_and_not_catalog_price_metadata():
    from rumi_market.game.market_query import SystemShopCandidate

    catalog = load()
    for kind, count in (("normal_pet", 304), ("item", 5)):
        candidates = catalog.candidates(kind)
        assert type(candidates) is tuple and len(candidates) == count
        assert all(type(row) is SystemShopCandidate and row.price == 0 for row in candidates)
    assert not hasattr(catalog.normal_pets[0], "price")


def test_current_price_map_keeps_quality_variants_separate_and_does_not_mutate():
    prices = {(100101, 4): 11, (100101, 5): 22}
    candidates = load().candidates("normal_pet", current_prices=prices)
    selected = {(row.config_id, row.quality): row.price for row in candidates}
    assert selected[100101, 4] == 11 and selected[100101, 5] == 22
    assert prices == {(100101, 4): 11, (100101, 5): 22}
    assert any(row.config_id != 100101 and row.price == 0 for row in candidates)


def test_item_price_and_explicit_zero_are_preserved():
    rows = load().candidates("item", current_prices={(10031, 0): 123, (10032, 0): 0})
    assert [(row.config_id, row.quality, row.price) for row in rows[:2]] == [
        (10031, 0, 123), (10032, 0, 0),
    ]


def test_catalog_metadata_and_mapping_are_read_only():
    catalog = load()
    with pytest.raises(TypeError):
        catalog.normal_pet_mapping[100101] = "replacement"
    with pytest.raises(FrozenInstanceError):
        catalog.normal_pets[0].name = "replacement"
    with pytest.raises(FrozenInstanceError):
        catalog.items = ()


@pytest.mark.asyncio
async def test_candidates_are_accepted_by_the_real_market_query_codec():
    from rumi_market.game.market_query import GameMarketQuery
    from rumi_market.game.proto import GameProtoCodec

    codec = GameProtoCodec()
    calls = []

    class Client:
        async def request(self, request_id, ack_id, payload):
            assert (request_id, ack_id) == (3522, 3523)
            message = codec.decode("SNKProto.Cmd_SystemShop_QueryItemPrice_Req", payload).message
            calls.append(message)
            return SimpleNamespace(data=codec.encode(
                "SNKProto.Cmd_SystemShop_QueryItemPrice_Ack", {"type": message.type},
            ))

    query = GameMarketQuery(Client())
    catalog = load()
    for kind, expected_count, outer_type, resource_type in (
        ("normal_pet", 304, 2, 4), ("item", 5, 1, 2),
    ):
        result = await query.query_system_shop(kind, catalog.candidates(kind))
        message = calls[-1]
        assert message.type == outer_type and len(message.item_list) == expected_count
        assert all(row.type == resource_type and row.price == 0 for row in message.item_list)
        assert result.kind == kind and result.quotes == ()


def test_exact_verified_file_can_be_loaded_from_an_explicit_path(tmp_path):
    from rumi_market.game.shop_catalog import SHOP_CATALOG_PATH

    copy = tmp_path / "catalog.json"
    copy.write_bytes(SHOP_CATALOG_PATH.read_bytes())
    assert load(copy).normal_pet_mapping == load().normal_pet_mapping


@pytest.mark.parametrize("operation", ["missing", "tamper", "oversized"])
def test_invalid_file_returns_fixed_error_without_path_context(tmp_path, operation):
    from rumi_market.game.shop_catalog import ShopCatalogError, SHOP_CATALOG_PATH

    path = tmp_path / "synthetic-private-path.json"
    if operation == "tamper":
        path.write_bytes(SHOP_CATALOG_PATH.read_bytes() + b" ")
    elif operation == "oversized":
        path.write_bytes(b"x" * 300000)
    with pytest.raises(ShopCatalogError, match="^GAME_SHOP_CATALOG_INVALID$") as failure:
        load(path)
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("kind", ["", "other", None, True])
def test_unknown_kind_is_not_a_query_candidate_category(kind):
    from rumi_market.game.shop_catalog import ShopCatalogError

    with pytest.raises(ShopCatalogError, match="^GAME_SHOP_CATALOG_INVALID$"):
        load().candidates(kind)


@pytest.mark.parametrize("prices", [
    {(100101, 4): True}, {(100101, 4): "synthetic-private-price"},
    {(100101, 4): 2**31}, {(100101,): 1}, {100101: 1},
    {(True, 4): 1}, {(100101, False): 1}, [123],
])
def test_invalid_price_cache_has_a_fixed_private_safe_error(prices):
    from rumi_market.game.shop_catalog import ShopCatalogError

    with pytest.raises(ShopCatalogError, match="^GAME_SHOP_CATALOG_PRICE_INVALID$") as failure:
        load().candidates("normal_pet", current_prices=prices)
    assert failure.value.__context__ is None and failure.value.__cause__ is None
    assert "synthetic" not in str(failure.value)
