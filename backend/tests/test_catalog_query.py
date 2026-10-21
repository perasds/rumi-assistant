import json

import pytest

from rumi_market.catalog import Catalog, PNG_SIGNATURE


def add_images(catalog_path):
    images = catalog_path.parent / "images"
    images.mkdir()
    (images / "101.png").write_bytes(PNG_SIGNATURE + b"public fixture")
    (images / "103.png").write_bytes(b"not a png")
    outside = catalog_path.parent / "private.png"
    outside.write_bytes(PNG_SIGNATURE + b"private fixture")
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    for row, image in zip(data["pets"], [
        "images/101.png", "images/missing.png", "images/103.png", "private.png",
    ]):
        row["image_path"] = image
    catalog_path.write_text(json.dumps(data), encoding="utf-8")
    return images / "101.png"


@pytest.mark.parametrize(("query", "expected_ids"), [
    ("", ["101", "102", "103", "104"]),
    ("  ", ["101", "102", "103", "104"]),
    ("蜜桃", ["101", "102"]),
    (" 桃桃 ", ["101"]),
    ("猜测", []),
])
def test_query_checks_each_image_once_and_keeps_full_catalog_counts(
        catalog_path, monkeypatch, query, expected_ids):
    add_images(catalog_path)
    catalog = Catalog(catalog_path)
    original = catalog.image_path
    calls = []

    def checked_image(pet_id):
        calls.append(pet_id)
        return original(pet_id)

    monkeypatch.setattr(catalog, "image_path", checked_image)
    try:
        result = catalog.query(query)

        assert calls == ["101", "102", "103", "104"]
        assert (result.total, result.named_count, result.image_count) == (4, 2, 1)
        assert [pet.pet_id for pet in result.items] == expected_ids
        for pet in result.items:
            expected_name = "蜜桃" if pet.pet_id in {"101", "102"} else None
            assert pet.model_dump() == {
                "pet_id": pet.pet_id,
                "name": expected_name,
                "display_name": expected_name or f"未识别宠物 · {pet.pet_id}",
                "name_verified": expected_name is not None,
                "image_url": "/api/pets/101/image" if pet.pet_id == "101" else None,
            }
    finally:
        catalog.close()


@pytest.mark.parametrize("change", ["remove", "tamper"])
def test_query_rechecks_changed_images_on_the_next_query(catalog_path, change):
    image = add_images(catalog_path)
    catalog = Catalog(catalog_path)
    try:
        before = catalog.query("桃桃")
        assert before.image_count == 1
        assert before.items[0].image_url == "/api/pets/101/image"

        if change == "remove":
            image.unlink()
        else:
            image.write_bytes(b"not a png")

        after = catalog.query("桃桃")
        assert after.image_count == 0
        assert after.items[0].image_url is None

        image.write_bytes(PNG_SIGNATURE + b"replacement public fixture")
        restored = catalog.query("桃桃")
        assert restored.image_count == 1
        assert restored.items[0].image_url == "/api/pets/101/image"
    finally:
        catalog.close()
