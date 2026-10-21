import json
import os

import pytest
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.catalog import PNG_SIGNATURE
from rumi_market.pet_details import PetDetails


PNG = PNG_SIGNATURE + b"public skill fixture"
DETAIL_FIELDS = ["pokedex_id", "elements", "stats", "normal_attack", "traits",
                 "fixed_skill", "replaceable_skills"]


def save_details(catalog_path):
    images = catalog_path.parent / "images" / "skills"
    images.mkdir(parents=True)
    (images / "3001.png").write_bytes(PNG)
    data = {
        "schema_version": 1,
        "source": {"description": "public test configuration"},
        "pets": {"101": {
            "pokedex_id": "001", "elements": [{"id": 2, "name": "水"}],
            "stats": [{"key": "hp", "label": "体力", "min": 38, "max": 42}],
            "normal_attack": {"element": {"id": 2, "name": "水"}, "power": 15,
                              "energy_gain": 5.0, "interval_seconds": 2.5},
            "trait_keys": ["battle:4001", "home:4002"],
            "fixed_skill_id": "3001", "replaceable_skill_ids": ["3002"],
            "missing_fields": [],
        }},
        "skills": {
            "3001": {"skill_id": "3001", "name": "水炮", "description": "造成伤害",
                     "element": {"id": 2, "name": "水"}, "cost_text": "2 点能量",
                     "power": 50, "image_path": "images/skills/3001.png"},
            "3002": {"skill_id": "3002", "name": "守护", "description": None,
                     "element": None, "cost_text": None, "power": None,
                     "image_path": None},
        },
        "traits": {
            "battle:4001": {"trait_id": "4001", "kind": "battle", "name": "激流",
                            "description": "提升水属性能力"},
            "home:4002": {"trait_id": "4002", "kind": "home", "name": "勤劳",
                          "description": None},
        },
    }
    write_details(catalog_path, data)
    return data


def write_details(catalog_path, data):
    (catalog_path.parent / "details.json").write_text(json.dumps(data), encoding="utf-8")


def test_public_detail_joins_local_data_without_creating_game_provider(catalog_path):
    save_details(catalog_path)
    provider_calls = []

    def forbidden_provider():
        provider_calls.append("created")
        raise AssertionError("Public encyclopedia must not create a game provider")

    app = create_app(provider_factory=forbidden_provider, catalog_path=catalog_path)
    with TestClient(app) as client:
        response = client.get("/api/pets/101")
        assert response.status_code == 200
        assert response.json() == {
            "pet_id": "101", "name": "蜜桃", "display_name": "蜜桃",
            "image_url": None, "name_verified": True, "available": True,
            "pokedex_id": "001", "elements": [{"id": 2, "name": "水"}],
            "stats": [{"key": "hp", "label": "体力", "min": 38.0, "max": 42.0}],
            "normal_attack": {"element": {"id": 2, "name": "水"}, "power": 15,
                              "energy_gain": 5.0, "interval_seconds": 2.5},
            "traits": [
                {"trait_id": "4001", "kind": "battle", "name": "激流", "description": "提升水属性能力"},
                {"trait_id": "4002", "kind": "home", "name": "勤劳", "description": None},
            ],
            "fixed_skill": {"skill_id": "3001", "name": "水炮", "description": "造成伤害",
                            "element": {"id": 2, "name": "水"}, "cost_text": "2 点能量",
                            "power": 50, "image_url": "/api/pet-skills/3001/image"},
            "replaceable_skills": [
                {"skill_id": "3002", "name": "守护", "description": None, "element": None,
                 "cost_text": None, "power": None, "image_url": None},
            ],
            "missing_fields": [],
        }
        image = client.get("/api/pet-skills/3001/image")
        assert image.status_code == 200
        assert image.content == PNG
        assert image.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert app.state.sessions.sessions == {}
    assert provider_calls == []


@pytest.mark.parametrize("details_present", [False, True])
def test_known_pet_without_details_returns_empty_unavailable_view(catalog_path, details_present):
    if details_present:
        save_details(catalog_path)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.get("/api/pets/103")
        assert response.status_code == 200
        detail = response.json()
        assert detail == {
            "pet_id": "103", "name": None, "display_name": "未识别宠物 · 103",
            "image_url": None, "name_verified": False, "available": False,
            "pokedex_id": None, "elements": [], "stats": [], "normal_attack": None,
            "traits": [], "fixed_skill": None, "replaceable_skills": [],
            "missing_fields": DETAIL_FIELDS,
        }


def test_detail_does_not_expose_pets_absent_from_summary_catalog(catalog_path):
    data = save_details(catalog_path)
    data["pets"]["999"] = data["pets"]["101"]
    write_details(catalog_path, data)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.get("/api/pets/999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PET_NOT_FOUND"


def test_detail_keeps_reference_order_and_stably_deduplicates(catalog_path):
    data = save_details(catalog_path)
    pet = data["pets"]["101"]
    pet["trait_keys"] = ["home:4002", "battle:4001", "home:4002"]
    pet["replaceable_skill_ids"] = ["3002", "3001", "3002"]
    pet["missing_fields"] = ["normal_attack.energy_gain", "source_note", "source_note"]
    write_details(catalog_path, data)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        detail = client.get("/api/pets/101").json()
        assert [trait["trait_id"] for trait in detail["traits"]] == ["4002", "4001"]
        assert [skill["skill_id"] for skill in detail["replaceable_skills"]] == ["3002", "3001"]
        assert detail["missing_fields"] == ["normal_attack.energy_gain", "source_note"]


def test_detail_preserves_unknown_numbers_as_null(catalog_path):
    data = save_details(catalog_path)
    data["pets"]["101"]["normal_attack"] = {
        "element": None, "power": None, "energy_gain": None, "interval_seconds": None}
    write_details(catalog_path, data)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        detail = client.get("/api/pets/101").json()
        assert detail["normal_attack"] == {
            "element": None, "power": None, "energy_gain": None, "interval_seconds": None}
        assert detail["replaceable_skills"][0]["power"] is None


def test_missing_malformed_and_mismatched_references_are_omitted(catalog_path):
    data = save_details(catalog_path)
    data["pets"]["101"].update({
        "trait_keys": ["battle:missing", "home:bad", "home:4002", "home:4002"],
        "fixed_skill_id": "missing", "replaceable_skill_ids": ["bad", "wrong", "3002", "bad"],
    })
    data["skills"]["bad"] = {"skill_id": "bad", "name": "损坏数值", "power": "50"}
    data["skills"]["wrong"] = {"skill_id": "3001", "name": "错配引用"}
    data["traits"]["home:bad"] = {"trait_id": "bad", "kind": "battle", "name": "错配特性"}
    write_details(catalog_path, data)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        detail = client.get("/api/pets/101").json()
        assert detail["fixed_skill"] is None
        assert [skill["skill_id"] for skill in detail["replaceable_skills"]] == ["3002"]
        assert [trait["trait_id"] for trait in detail["traits"]] == ["4002"]
        assert detail["missing_fields"] == [
            "traits:battle:missing", "traits:home:bad", "fixed_skill:missing",
            "replaceable_skills:bad", "replaceable_skills:wrong",
        ]


@pytest.mark.parametrize("image_path", [
    "../private.png", "images/skills/../private.png", "images/skills/3002.png",
    "images/3001.png", "images/skills/3001.jpg", "images/skills/3001.PNG",
    "images/skills/./3001.png", "images/skills/..\\private.png", "/private.png",
    "C:/private.png", "C:\\private.png", "\\\\host\\share\\private.png",
])
def test_skill_image_rejects_noncanonical_or_escaping_paths(catalog_path, image_path):
    data = save_details(catalog_path)
    data["skills"]["3001"]["image_path"] = image_path
    (catalog_path.parent / "private.png").write_bytes(PNG)
    write_details(catalog_path, data)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.get("/api/pet-skills/3001/image").status_code == 404
        detail = client.get("/api/pets/101").json()
        assert detail["fixed_skill"]["image_url"] is None
        assert "image_path" not in json.dumps(detail)


def test_skill_image_rejects_absolute_path_inside_image_root(catalog_path):
    data = save_details(catalog_path)
    data["skills"]["3001"]["image_path"] = str((catalog_path.parent / "images/skills/3001.png").resolve())
    write_details(catalog_path, data)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.get("/api/pet-skills/3001/image").status_code == 404


def test_skill_image_rechecks_png_signature_and_missing_files(catalog_path):
    save_details(catalog_path)
    image = catalog_path.parent / "images/skills/3001.png"
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.get("/api/pet-skills/3001/image").status_code == 200
        image.write_bytes(b"not png")
        assert client.get("/api/pet-skills/3001/image").status_code == 404
        image.unlink()
        assert client.get("/api/pet-skills/3001/image").status_code == 404
        assert client.get("/api/pet-skills/unknown/image").status_code == 404


def test_skill_image_rejects_directory_junction_outside_catalog(catalog_path):
    save_details(catalog_path)
    images = catalog_path.parent / "images/skills"
    (images / "3001.png").unlink()
    images.rmdir()
    outside = catalog_path.parent.parent / "private"
    outside.mkdir()
    (outside / "3001.png").write_bytes(PNG)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(images))
    else:
        images.symlink_to(outside, target_is_directory=True)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.get("/api/pet-skills/3001/image").status_code == 404


@pytest.mark.parametrize("schema_version", [None, True, 2, "1"])
def test_details_rejects_unsupported_or_ambiguous_schema(catalog_path, schema_version):
    data = save_details(catalog_path)
    data["schema_version"] = schema_version
    write_details(catalog_path, data)
    with pytest.raises(ValueError, match="pet details schema"):
        create_app(catalog_path=catalog_path)


def test_pet_summary_contract_only_adds_elements_to_summary(catalog_path):
    save_details(catalog_path)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        summary = client.get("/api/pets", params={"query": "桃桃"}).json()
        assert summary == {"total": 4, "named_count": 2, "image_count": 0, "items": [{
            "pet_id": "101", "name": "蜜桃", "display_name": "蜜桃",
            "image_url": None, "name_verified": True,
            "elements": [{"id": 2, "name": "水"}],
        }]}


@pytest.mark.parametrize("query, expected_ids", [
    ("", ["101", "102", "103", "104"]),
    (" 蜜桃 ", ["101", "102"]),
    ("桃桃", ["101"]),
    ("猜测", []),
])
def test_public_catalog_projects_elements_without_detail_or_skill_loading(
        catalog_path, monkeypatch, query, expected_ids):
    data = save_details(catalog_path)
    data["pets"]["101"]["elements"].append({"id": 9, "name": "龙"})
    data["pets"]["102"] = {"elements": [{"id": 4, "name": "草"}]}
    data["pets"]["999"] = {"elements": [{"id": 3, "name": "火"}]}
    write_details(catalog_path, data)

    def forbidden(*args, **kwargs):
        raise AssertionError("Public catalog must not load details, skill images or a game provider")

    monkeypatch.setattr(PetDetails, "lookup", forbidden)
    monkeypatch.setattr(PetDetails, "image_path", forbidden)
    with TestClient(create_app(provider_factory=forbidden, catalog_path=catalog_path)) as client:
        response = client.get("/api/pets", params={"query": query})
        assert response.status_code == 200
        summary = response.json()
        assert (summary["total"], summary["named_count"], summary["image_count"]) == (4, 2, 0)
        assert [row["pet_id"] for row in summary["items"]] == expected_ids
        for row in summary["items"]:
            assert row["elements"] == data["pets"].get(row["pet_id"], {}).get("elements", [])
            assert set(row) == {"pet_id", "name", "display_name", "image_url", "name_verified", "elements"}
        assert "set-cookie" not in response.headers
        assert client.app.state.sessions.sessions == {}
        assert not client.cookies


@pytest.mark.parametrize("details_present", [False, True])
def test_public_catalog_missing_details_defaults_elements_to_empty(catalog_path, details_present):
    if details_present:
        save_details(catalog_path)
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        summary = client.get("/api/pets").json()
        assert next(row for row in summary["items"] if row["pet_id"] == "103")["elements"] == []
        if not details_present:
            assert all(row["elements"] == [] for row in summary["items"])
