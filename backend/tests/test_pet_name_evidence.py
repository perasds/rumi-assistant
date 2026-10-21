"""Validate the shipped public name overlay and its independently saved evidence."""

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from rumi_market.app import create_app


PET_ROOT = Path(__file__).resolve().parents[2] / "data" / "pets"
CATALOG_CONTENT_SHA256 = "672a306d47600f7477c761bd428af380f2151cb8f94b7f153f8ddee581478390"
PEACH_IDS = {"1000101", "1000601", "1000801", "1000902", "1003401", "1003501"}
pytestmark = pytest.mark.external_materials


@pytest.fixture(autouse=True)
def original_pet_materials(monkeypatch, external_materials_root):
    monkeypatch.setitem(globals(), "PET_ROOT", external_materials_root / "data/pets")


def test_shipped_name_evidence_preserves_resource_catalog_and_images():
    raw = (PET_ROOT / "catalog.json").read_bytes()
    catalog = json.loads(raw)
    canonical = json.dumps(catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    assert hashlib.sha256(canonical).hexdigest() == CATALOG_CONTENT_SHA256
    assert len(catalog["pets"]) == 306
    assert len(list((PET_ROOT / "images").glob("*.png"))) == 297
    names = json.loads((PET_ROOT / "names.json").read_text(encoding="utf-8"))["names"]
    assert len(names) == 246
    assert set(names) < {str(row["pet_id"]) for row in catalog["pets"]}


def test_every_shipped_name_matches_retained_public_configuration_evidence():
    evidence_path = PET_ROOT / "evidence" / "pet-config-names.public.json"
    raw = evidence_path.read_bytes()
    evidence = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    by_id = {row["pet_id"]: row for row in evidence["pets"]}
    names = json.loads((PET_ROOT / "names.json").read_text(encoding="utf-8"))["names"]
    assert len(by_id) == len(evidence["pets"]) == 246
    assert set(names) == set(by_id)
    assert evidence["translation_call_linkage_confirmed"] is True
    assert evidence["trade_avatar_call_linkage_confirmed"] is True
    assert evidence["availability_semantics_confirmed"] is False
    for pet_id, name in names.items():
        assert name["name"] == by_id[pet_id]["name"]
        assert by_id[pet_id]["avatar"] == "Avatar_" + pet_id
        assert name["configuration_evidence_sha256"] == digest
        assert name["configuration_evidence"] == "evidence/pet-config-names.public.json"
        assert name["official_configuration_field_name_confirmed"] is True


def test_existing_screenshot_evidence_and_names_are_retained():
    names = json.loads((PET_ROOT / "names.json").read_text(encoding="utf-8"))["names"]
    expected = {
        "1000101": ("\u871c\u6843", "edc8c30294af001c5d144f1ce36c3b91098d25ce56e2c5428bba00335b4c4484"),
        "1003401": ("\u5c0f\u871c\u8702\u871c\u6843", "5928336883d7924a79150bccaee08550124acdde5d990208e15a7b19afa82357"),
    }
    for pet_id, (name, digest) in expected.items():
        row = names[pet_id]
        assert row["name"] == name
        assert row["aliases"] == []
        assert row["source"].startswith("Game trade overview card name")
        assert row["evidence_sha256"] == digest
        image = (PET_ROOT / row["evidence_image"]).resolve()
        assert image.is_relative_to(PET_ROOT / "evidence")
        assert hashlib.sha256(image.read_bytes()).hexdigest() == digest


def test_real_public_catalog_filtering_uses_verified_names_and_keeps_unknowns():
    with TestClient(create_app(catalog_path=PET_ROOT / "catalog.json")) as client:
        response = client.get("/api/pets").json()
        assert (response["total"], response["named_count"], response["image_count"]) == (306, 246, 297)
        assert sum(row["name"] is None and not row["name_verified"] for row in response["items"]) == 60
        peach = client.get("/api/pets", params={"query": "\u871c\u6843"}).json()
        assert {row["pet_id"] for row in peach["items"]} == PEACH_IDS
        assert all(row["name_verified"] and row["image_url"] for row in peach["items"])
        dragon = client.get("/api/pets", params={"query": "\u706b\u9f99"}).json()
        assert {row["pet_id"] for row in dragon["items"]} == {
            "101601", "107901", "121201", "121301", "121304"}
