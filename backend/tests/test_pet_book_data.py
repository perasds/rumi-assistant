"""Integration checks against the shipped public snapshot, without game access."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from rumi_market.app import create_app
from rumi_market.catalog import PNG_SIGNATURE


DATA = Path(__file__).resolve().parents[2] / "data" / "pets"
pytestmark = pytest.mark.external_materials


@pytest.fixture(autouse=True)
def original_pet_materials(monkeypatch, external_materials_root):
    monkeypatch.setitem(globals(), "DATA", external_materials_root / "data/pets")


def forbidden_provider():
    raise AssertionError("The public pet book must not connect to the game")


def test_anonymous_pet_book_and_all_its_images_remain_public():
    app = create_app(provider_factory=forbidden_provider, catalog_path=DATA / "catalog.json")
    with TestClient(app) as client:
        catalog = client.get('/api/pets')
        assert catalog.status_code == 200
        pet = next(row for row in catalog.json()['items'] if row['image_url'])
        detail = client.get(f"/api/pets/{pet['pet_id']}")
        assert detail.status_code == 200
        details = (client.get(f"/api/pets/{row['pet_id']}").json() for row in catalog.json()['items'])
        skill = next(row['fixed_skill'] for row in details
                     if row['fixed_skill'] and row['fixed_skill']['image_url'])
        for url in [pet['image_url'], skill['image_url']]:
            image = client.get(url)
            assert image.status_code == 200
            assert image.content.startswith(PNG_SIGNATURE)
            assert image.headers['cache-control'] == 'public, max-age=3600'
            assert 'set-cookie' not in image.headers
        assert client.get('/api/market/listings').status_code == 401
        assert client.get('/api/home/production').status_code == 401
        assert app.state.sessions.sessions == {}
        assert not client.cookies


def test_shipped_catalog_details_and_skill_images_are_complete_and_isolated():
    stored = json.loads((DATA / "details.json").read_text(encoding="utf-8"))
    assert stored["source"]["hot_version"] == 64
    assert stored["source"]["element_localization_verified"] is True
    assert len(stored["pets"]) == 246
    assert len(stored["skills"]) == 533
    assert len(stored["traits"]) == 175
    app = create_app(provider_factory=forbidden_provider, catalog_path=DATA / "catalog.json")
    with TestClient(app) as client:
        catalog = client.get("/api/pets").json()
        assert (catalog["total"], catalog["named_count"], catalog["image_count"]) == (306, 246, 297)
        assert sum(len(row["elements"]) == 1 for row in catalog["items"]) == 197
        assert sum(len(row["elements"]) == 2 for row in catalog["items"]) == 49
        assert sum(not row["elements"] for row in catalog["items"]) == 60
        available, missing, skill_images = set(), set(), {}
        for summary in catalog["items"]:
            assert summary["elements"] == stored["pets"].get(summary["pet_id"], {}).get("elements", [])
            response = client.get(f"/api/pets/{summary['pet_id']}")
            assert response.status_code == 200
            detail = response.json()
            assert all(detail[key] == value for key, value in summary.items())
            if not detail["available"]:
                missing.add(detail["pet_id"])
                assert detail["fixed_skill"] is None and detail["replaceable_skills"] == []
                continue
            available.add(detail["pet_id"])
            assert len(detail["stats"]) == 4 and detail["elements"]
            assert detail["fixed_skill"] is not None and detail["replaceable_skills"]
            assert not any(value.startswith(("fixed_skill:", "replaceable_skills:", "traits:"))
                           for value in detail["missing_fields"])
            for skill in [detail["fixed_skill"], *detail["replaceable_skills"]]:
                assert skill["description"] and skill["image_url"] and skill["cost_text"]
                skill_images[skill["skill_id"]] = skill["image_url"]
            assert all(trait["description"] for trait in detail["traits"])
        assert available == set(stored["pets"]) and len(missing) == 60
        assert len(skill_images) == 318
        skill_images.update({skill_id: f"/api/pet-skills/{skill_id}/image"
                             for skill_id, row in stored["skills"].items() if row["image_path"]})
        assert len(skill_images) == 319
        for skill_id, url in skill_images.items():
            image = client.get(url)
            assert image.status_code == 200 and image.headers["content-type"] == "image/png"
            assert image.content.startswith(PNG_SIGNATURE)
            assert image.content == (DATA / "images" / "skills" / f"{skill_id}.png").read_bytes()
        assert app.state.sessions.sessions == {}


def test_tomato_snapshot_matches_verified_fixed_and_replaceable_skill_mapping():
    app = create_app(provider_factory=forbidden_provider, catalog_path=DATA / "catalog.json")
    with TestClient(app) as client:
        detail = client.get("/api/pets/1000502").json()
    assert detail["name"] == "番茄侠"
    assert detail["normal_attack"] == {
        "element": {"id": 11, "name": "暗"}, "power": 9,
        "energy_gain": 0.7, "interval_seconds": 0.9,
    }
    fixed = detail["fixed_skill"]
    assert (fixed["skill_id"], fixed["name"], fixed["power"], fixed["cost_text"]) == (
        "100050202", "番茄激光", 40, "5点能量")
    assert [skill["skill_id"] for skill in detail["replaceable_skills"]] == [
        "5001", "8001", "14001", "1004", "11002", "12002", "1003"]
    ice = next(skill for skill in detail["replaceable_skills"] if skill["skill_id"] == "8001")
    assert (ice["name"], ice["power"], ice["cost_text"]) == ("冰砾", 50, "4点能量")
    assert "20%" in ice["description"] and "冻伤" in ice["description"]
    assert detail["traits"][0]["name"] == "番茄，来收人了"
    assert "100%" in detail["traits"][0]["description"]
