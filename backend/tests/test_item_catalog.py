import hashlib
import json
import os
from pathlib import Path

import pytest


PNG = b"\x89PNG\r\n\x1a\n" + b"public item fixture"


def save_item_catalog(root, *, updates=None, rows=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(exist_ok=True)
    (root / "images/10031.png").write_bytes(PNG)
    row = {"config_id": 10031, "item_id": "10031", "name": "噜咪卡",
           "name_verified": True, "source": "verified public Item table",
           "image_path": "images/10031.png", "image_sha256": hashlib.sha256(PNG).hexdigest()}
    row.update(updates or {})
    path = root / "items.json"
    path.write_text(json.dumps({"schema_version": 1, "items": rows if rows is not None else [row]}),
                    encoding="utf-8")
    return path


def test_verified_item_keeps_id_name_source_and_checked_image(tmp_path):
    from rumi_market.item_catalog import ItemCatalog

    path = save_item_catalog(tmp_path / "market")
    catalog = ItemCatalog(path)
    item = catalog.lookup("10031")
    assert item.item_id == "10031"
    assert item.name == "噜咪卡"
    assert item.name_verified is True
    assert item.source == "verified public Item table"
    assert catalog.image_path("10031") == path.parent / "images/10031.png"
    assert catalog.lookup("99999") is None
    assert catalog.image_path("99999") is None


@pytest.mark.parametrize("item_id", ["0", "2147483648", "010031", "+10031", "10031.0", "../10031", "10031/1", "０１００３１", 10031, True, None])
def test_lookup_accepts_only_canonical_configuration_ids(tmp_path, item_id):
    from rumi_market.item_catalog import ItemCatalog

    catalog = ItemCatalog(save_item_catalog(tmp_path / "market"))
    assert catalog.lookup(item_id) is None
    assert catalog.image_path(item_id) is None


@pytest.mark.parametrize("updates", [
    {"item_id": "010031"}, {"item_id": 10031}, {"config_id": 10032},
    {"config_id": True}, {"config_id": 10031.0},
])
def test_manifest_rejects_ambiguous_or_mismatched_ids(tmp_path, updates):
    from rumi_market.item_catalog import ItemCatalog

    with pytest.raises(ValueError, match="configuration ID"):
        ItemCatalog(save_item_catalog(tmp_path / "market", updates=updates))


def test_duplicate_configuration_id_is_rejected(tmp_path):
    from rumi_market.item_catalog import ItemCatalog

    path = save_item_catalog(tmp_path / "market")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"] *= 2
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="configuration ID"):
        ItemCatalog(path)


@pytest.mark.parametrize("updates", [
    {"name_verified": False}, {"source": ""}, {"source": "   "},
    {"name_verified": 1}, {"name": ""},
])
def test_name_requires_explicit_verification_and_source(tmp_path, updates):
    from rumi_market.item_catalog import ItemCatalog

    item = ItemCatalog(save_item_catalog(tmp_path / "market", updates=updates)).lookup("10031")
    assert item.name is None
    assert item.name_verified is False


@pytest.mark.parametrize("image_path", [
    "../private.png", "images/../private.png", "private.png", "images/../../private.png",
    "C:/private.png", "C:\\private.png", "/private.png", "\\\\host\\share\\private.png",
    "images/10031.jpg", "images/missing.png", "images/..\\private.png",
])
def test_image_paths_cannot_escape_the_png_image_directory(tmp_path, image_path):
    from rumi_market.item_catalog import ItemCatalog

    path = save_item_catalog(tmp_path / "market", updates={"image_path": image_path})
    (path.parent / "private.png").write_bytes(PNG)
    assert ItemCatalog(path).image_path("10031") is None


def test_absolute_path_is_rejected_even_when_inside_images(tmp_path):
    from rumi_market.item_catalog import ItemCatalog

    path = save_item_catalog(tmp_path / "market")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["image_path"] = str((path.parent / "images/10031.png").resolve())
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ItemCatalog(path).image_path("10031") is None


@pytest.mark.parametrize("updates", [{"image_sha256": "0" * 64}, {"image_sha256": None}, {"image_sha256": "wrong"}])
def test_image_requires_the_manifest_sha256(tmp_path, updates):
    from rumi_market.item_catalog import ItemCatalog

    assert ItemCatalog(save_item_catalog(tmp_path / "market", updates=updates)).image_path("10031") is None


def test_non_png_and_post_load_mutation_are_not_served(tmp_path):
    from rumi_market.item_catalog import ItemCatalog

    path = save_item_catalog(tmp_path / "market")
    catalog = ItemCatalog(path)
    image = path.parent / "images/10031.png"
    image.write_bytes(PNG + b"changed")
    assert catalog.image_path("10031") is None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["image_sha256"] = hashlib.sha256(b"not png").hexdigest()
    path.write_text(json.dumps(data), encoding="utf-8")
    image.write_bytes(b"not png")
    assert ItemCatalog(path).image_path("10031") is None


def test_symlink_or_junction_cannot_escape_the_image_root(tmp_path):
    from rumi_market.item_catalog import ItemCatalog

    path = save_item_catalog(tmp_path / "market")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "10031.png").write_bytes(PNG)
    link = path.parent / "images/link"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["image_path"] = "images/link/10031.png"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ItemCatalog(path).image_path("10031") is None


def test_missing_manifest_is_empty_without_project_fallback(tmp_path):
    from rumi_market.item_catalog import ItemCatalog

    catalog = ItemCatalog(tmp_path / "market/items.json")
    assert catalog.lookup("10031") is None
    assert catalog.image_path("10031") is None


def test_catalog_loads_only_its_sibling_market_and_does_not_add_pet_counts(catalog_path):
    from rumi_market.catalog import Catalog

    save_item_catalog(catalog_path.parent.parent / "market")
    catalog = Catalog(catalog_path)
    try:
        assert catalog.items.lookup("10031").name == "噜咪卡"
        assert catalog.query().total == 4
        assert catalog.query().named_count == 2
        assert catalog.query("噜咪卡").items == []
    finally:
        catalog.close()


@pytest.mark.external_materials
def test_saved_static_items_match_their_public_source_and_png_hashes(external_materials_root):
    from rumi_market.item_catalog import ItemCatalog

    root = external_materials_root / "data/market"
    evidence = json.loads((root / "evidence/item-config.public.json").read_text(encoding="utf-8"))
    catalog = ItemCatalog(root / "items.json")
    expected = {"10031": "噜咪卡", "10032": "强化噜咪卡", "10033": "精致噜咪卡",
                "10034": "史诗噜咪卡", "10035": "传奇噜咪卡"}
    assert len(evidence["items"]) == 5
    amounts = evidence["market_price_purchase_num"]
    assert amounts["strict_eof"] and amounts["all_candidates_purchase_num_positive"]
    assert {str(row["Id"]): row["PurchaseNum"] for row in amounts["candidates"]} == {
        "10031": 500, "10032": 500, "10033": 500, "10034": 100, "10035": 50}
    for row in evidence["items"]:
        item_id = str(row["Key1"])
        item = catalog.lookup(item_id)
        assert item.name == row["name"] == expected[item_id]
        assert item.name_verified and item.source
        assert row["Name"] == row["Icon"] == row["image"]["sprite_name"]
        assert row["Atlas"] == row["image"]["atlas_name"] == "IconItem"
        assert catalog.image_path(item_id) is not None


@pytest.mark.external_materials
def test_saved_order_reward_names_match_public_configuration_without_guessed_icons(external_materials_root):
    from rumi_market.item_catalog import ItemCatalog

    root = external_materials_root / 'data/market'
    evidence = json.loads((root / 'evidence/order-rewards.public.json').read_text(encoding='utf-8'))
    catalog = ItemCatalog(root / 'items.json')
    expected = {'10001': '金币', '10004': '体力', '54001': '罐头', '54002': '公会罐头'}
    assert {str(row['Key1']): row['name'] for row in evidence['items']} == expected
    assert evidence['order_reward_item_ids'] == {
        'OrderRewardAID': 10004, 'OrderRewardBID': 10001, 'OrderRewardCID': 54001}
    for row in evidence['items']:
        item_id = str(row['Key1'])
        item = catalog.lookup(item_id)
        assert row['Name'] == f'Item_{item_id}'
        assert item.name == row['name'] and item.name_verified
        assert f'evidence/order-rewards.public.json#{item_id}' in item.source
        assert catalog.image_path(item_id) is None
