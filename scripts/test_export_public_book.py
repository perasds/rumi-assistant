import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).with_name("export_public_book.py")
PNG = b"\x89PNG\r\n\x1a\npublic fixture"


def exporter():
    assert SCRIPT.is_file(), "The public book exporter must exist"
    spec = importlib.util.spec_from_file_location("export_public_book", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.export_public_book


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sources(tmp_path):
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "book.html").write_text(
        '<html><script src="/assets/book-AbCd1234.js"></script></html>', encoding="utf-8")
    (frontend / "app-icon.svg").write_bytes(PNG)
    (assets / "book-AbCd1234.js").write_text("console.log('book')", encoding="utf-8")
    (assets / "book-AbCd1234.css").write_text("body{color:black}", encoding="utf-8")
    (assets / "logo-AbCd1234.png").write_bytes(PNG)
    pets = tmp_path / "data" / "pets"
    images = pets / "images"
    skills = images / "skills"
    skills.mkdir(parents=True)
    (images / "101.png").write_bytes(PNG)
    (images / "103.png").write_bytes(b"invalid png")
    (images / "unused.png").write_bytes(PNG + b"unused")
    (skills / "3001.png").write_bytes(PNG)
    (skills / "3002.png").write_bytes(b"invalid png")
    (skills / "3003.png").write_bytes(PNG + b"unused")
    catalog = pets / "catalog.json"
    save_json(catalog, {"source": "PRIVATE-EVIDENCE", "pets": [
        {"pet_id": "101", "name": "蜜桃", "name_verified": True,
         "sources": ["PRIVATE-EVIDENCE"], "aliases": ["PRIVATE-ALIAS"],
         "image_path": "images/101.png"},
        {"pet_id": "102", "image_path": "images/missing.png"},
        {"pet_id": "103", "image_path": "images/103.png"},
    ]})
    save_json(pets / "details.json", {
        "schema_version": 1, "source": {"description": "PRIVATE-EVIDENCE"},
        "pets": {"101": {
            "pokedex_id": "001", "elements": [{"id": 2, "name": "水"}],
            "stats": [{"key": "hp", "label": "体力", "min": 38, "max": 42}],
            "fixed_skill_id": "3001", "replaceable_skill_ids": ["3002"],
        }, "999": {"fixed_skill_id": "3003"}},
        "skills": {
            str(number): {"skill_id": str(number), "name": f"技能{number}",
                          "image_path": f"images/skills/{number}.png"}
            for number in (3001, 3002, 3003)
        }, "traits": {},
    })
    return frontend, catalog, tmp_path / "output"


def test_exports_only_public_responses_and_referenced_images(sources):
    frontend, catalog_path, output = sources
    manifest = exporter()(frontend, catalog_path, output)

    from rumi_market.catalog import Catalog
    from rumi_market.pet_details import PetDetails

    catalog = Catalog(catalog_path)
    try:
        details = PetDetails(catalog)
        actual = json.loads((output / "book-data/catalog.json").read_text("utf-8"))
        assert actual == details.catalog_query().model_dump(mode="json")
        for row in actual["items"]:
            saved = json.loads((output / f"book-data/pets/{row['pet_id']}.json").read_text("utf-8"))
            assert saved == details.lookup(row["pet_id"]).model_dump(mode="json")
    finally:
        catalog.close()

    expected = {
        "index.html", "app-icon.svg", "assets/book-AbCd1234.js",
        "assets/book-AbCd1234.css", "assets/logo-AbCd1234.png", "book-data/catalog.json",
        "book-data/pets/101.json", "book-data/pets/102.json", "book-data/pets/103.json",
        "book-data/pet-images/101.png", "book-data/skill-images/3001.png",
    }
    assert {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()} == expected
    assert (output / "index.html").read_bytes() == (frontend / "book.html").read_bytes()
    assert (output / "book-data/pet-images/101.png").read_bytes() == PNG
    assert (output / "book-data/skill-images/3001.png").read_bytes() == PNG
    for path in output.rglob("*.json"):
        text = path.read_text("utf-8")
        assert "PRIVATE-" not in text
        assert "image_path" not in text
        assert '"source"' not in text
    assert manifest["counts"] == {
        "catalog": 3, "details": 3, "available_details": 1,
        "pet_images": 1, "skill_images": 1, "files": len(expected),
    }
    assert [row["path"] for row in manifest["files"]] == sorted(expected)
    for row in manifest["files"]:
        raw = (output / row["path"]).read_bytes()
        assert row == {"path": row["path"], "bytes": len(raw),
                       "sha256": hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize("details_present", [False, True])
def test_preserves_missing_details_and_image_placeholders(sources, details_present):
    frontend, catalog, output = sources
    if not details_present:
        (catalog.parent / "details.json").unlink()
    exporter()(frontend, catalog, output)
    missing = json.loads((output / "book-data/pets/102.json").read_text("utf-8"))
    assert missing["available"] is False
    assert missing["image_url"] is None
    assert missing["fixed_skill"] is None
    assert missing["missing_fields"] == [
        "pokedex_id", "elements", "stats", "normal_attack", "traits",
        "fixed_skill", "replaceable_skills",
    ]
    invalid = json.loads((output / "book-data/pets/103.json").read_text("utf-8"))
    assert invalid["image_url"] is None
    assert not (output / "book-data/pet-images/103.png").exists()
    if details_present:
        known = json.loads((output / "book-data/pets/101.json").read_text("utf-8"))
        assert known["image_url"] == "/api/pets/101/image"
        assert known["fixed_skill"]["image_url"] == "/api/pet-skills/3001/image"
        assert known["replaceable_skills"][0]["image_url"] is None


def test_keeps_every_catalog_row_even_when_all_306_details_are_missing(sources):
    frontend, catalog, output = sources
    save_json(catalog, {"pets": [{"pet_id": str(number)} for number in range(1, 307)]})
    (catalog.parent / "details.json").unlink()
    manifest = exporter()(frontend, catalog, output)
    saved = json.loads((output / "book-data/catalog.json").read_text("utf-8"))
    assert saved["total"] == len(saved["items"]) == 306
    assert len(list((output / "book-data/pets").glob("*.json"))) == 306
    assert manifest["counts"]["available_details"] == 0


@pytest.mark.parametrize("kind", ["file", "empty", "nonempty"])
def test_refuses_every_existing_output_without_changes(sources, kind):
    frontend, catalog, output = sources
    if kind == "file":
        output.write_text("keep", encoding="utf-8")
    else:
        output.mkdir()
        if kind == "nonempty":
            (output / "keep.txt").write_text("keep", encoding="utf-8")
    before = [(p.name, p.read_bytes()) for p in output.iterdir()] if output.is_dir() else output.read_bytes()
    with pytest.raises(ValueError, match="output"):
        exporter()(frontend, catalog, output)
    after = [(p.name, p.read_bytes()) for p in output.iterdir()] if output.is_dir() else output.read_bytes()
    assert after == before


@pytest.mark.parametrize("location", ["frontend", "catalog", "traversal"])
def test_refuses_unsafe_output_locations_before_writing(sources, location):
    frontend, catalog, _ = sources
    output = {"frontend": frontend / "export", "catalog": catalog.parent / "export",
              "traversal": frontend.parent / "absent" / ".." / "export"}[location]
    with pytest.raises(ValueError, match="output"):
        exporter()(frontend, catalog, output)
    assert not output.exists()


@pytest.mark.parametrize("relative", [
    ".env", "index.html", "src/main.ts", "assets/book-AbCd1234.js.map",
    "assets/unhashed.js", "assets/private.json", "assets/.hidden/book-AbCd1234.js",
])
def test_rejects_unknown_frontend_assets_before_writing(sources, relative):
    frontend, catalog, output = sources
    extra = frontend / relative
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("must stay private", encoding="utf-8")
    with pytest.raises(ValueError, match="frontend"):
        exporter()(frontend, catalog, output)
    assert not output.exists()


def make_link(link, target, directory=True):
    if os.name == "nt" and directory:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            pytest.skip(f"Symlinks are unavailable: {error}")


@pytest.mark.parametrize("location", ["frontend", "assets", "file", "output_parent"])
def test_rejects_frontend_links_and_output_link_ancestors(sources, location):
    frontend, catalog, output = sources
    outside = frontend.parent / "outside"
    outside.mkdir()
    (outside / "book-AbCd1234.js").write_text("private", encoding="utf-8")
    if location == "frontend":
        linked = frontend.parent / "linked_frontend"
        make_link(linked, frontend)
        frontend = linked
    elif location == "assets":
        make_link(frontend / "assets" / "linked", outside)
    elif location == "file":
        make_link(frontend / "assets" / "private-AbCd1234.js",
                  outside / "book-AbCd1234.js", directory=False)
    else:
        parent = frontend.parent / "linked_output"
        make_link(parent, outside)
        output = parent / "export"
    with pytest.raises(ValueError, match="link|reparse"):
        exporter()(frontend, catalog, output)
    assert not output.exists()


@pytest.mark.parametrize("filename", ["names.json", "details.json", "items.json"])
@pytest.mark.parametrize("directory", [False, True], ids=["symlink", "junction"])
def test_rejects_known_companion_json_links_before_writing(sources, filename, directory):
    frontend, catalog, output = sources
    path = (catalog.parent.parent / "market" / filename if filename == "items.json"
            else catalog.parent / filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    outside = frontend.parent / "outside_companion"
    outside.mkdir()
    target = outside if directory else outside / filename
    if not directory:
        save_json(target, {"names": {}} if filename == "names.json" else {
            "schema_version": 1, "source": {}, "pets": {}, "skills": {}, "traits": {},
        })
    make_link(path, target, directory=directory)
    with pytest.raises(ValueError, match="link|reparse"):
        exporter()(frontend, catalog, output)
    assert not output.exists()


def test_rejects_market_json_parent_junction_before_writing(sources):
    frontend, catalog, output = sources
    outside = frontend.parent / "outside_market"
    outside.mkdir()
    save_json(outside / "items.json", {"items": []})
    make_link(catalog.parent.parent / "market", outside)
    with pytest.raises(ValueError, match="link|reparse"):
        exporter()(frontend, catalog, output)
    assert not output.exists()


@pytest.mark.parametrize("pet_id", ["../private", "1/2", "0", "001", "１２３"])
def test_rejects_noncanonical_pet_ids_before_writing(sources, pet_id):
    frontend, catalog, output = sources
    save_json(catalog, {"pets": [{"pet_id": pet_id}]})
    with pytest.raises(ValueError, match="pet.*ID"):
        exporter()(frontend, catalog, output)
    assert not output.exists()


def test_rejects_noncanonical_public_skill_id_before_writing(sources):
    frontend, catalog, output = sources
    path = catalog.parent / "details.json"
    details = json.loads(path.read_text("utf-8"))
    details["pets"]["101"]["fixed_skill_id"] = "../private"
    details["skills"]["../private"] = {"skill_id": "../private", "name": "bad"}
    save_json(path, details)
    with pytest.raises(ValueError, match="skill.*ID"):
        exporter()(frontend, catalog, output)
    assert not output.exists()


def test_cli_reports_manifest_outside_public_site(sources):
    frontend, catalog, output = sources
    assert SCRIPT.is_file(), "The public book exporter must exist"
    result = subprocess.run([sys.executable, str(SCRIPT), "--frontend", str(frontend),
                             "--catalog", str(catalog), "--output", str(output)],
                            capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    manifest = json.loads(result.stdout)
    assert manifest["counts"]["catalog"] == 3
    assert not (output / "manifest.public.json").exists()
    assert str(catalog.parent) not in result.stdout


def test_module_import_does_not_load_backend_service_or_game_provider():
    assert SCRIPT.is_file(), "The public book exporter must exist"
    code = (
        "import importlib.util, sys; "
        f"spec = importlib.util.spec_from_file_location('export_public_book', {str(SCRIPT)!r}); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not any(name in sys.modules for name in "
        "('rumi_market.app', 'rumi_market.live_app', 'rumi_market.provider', 'rumi_market.game.provider'))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
