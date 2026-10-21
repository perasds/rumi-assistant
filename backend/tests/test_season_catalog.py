import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from rumi_market.errors import ApiError


PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / "scripts/generate_season_tags.py"
REPORT = Path("artifacts/rumi/market-season-analysis/lumi-tags-all-species.public.json")


def generator():
    spec = importlib.util.spec_from_file_location("generate_season_tags", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tags_follow_each_actual_form_including_mixed_tag_groups():
    from rumi_market.season_catalog import SeasonPetCatalog

    catalog = SeasonPetCatalog()
    assert catalog.tag_for("102001") == 1
    assert catalog.tag_for("102004") == 2
    assert catalog.tag_for("120901") == 2
    assert catalog.tag_for("120902") == 1
    assert catalog.tag_for("1000101") == 102


def test_season_ids_cover_all_verified_species_without_market_only_restriction():
    from rumi_market.season_catalog import SeasonPetCatalog

    catalog = SeasonPetCatalog()
    ids = catalog.ids_for(1)
    assert isinstance(ids, frozenset)
    assert len(ids) == 89  # The market-only projection has just 48 S1 IDs.
    assert "102004" in ids and "102001" not in ids
    assert "120901" in ids and "120902" not in ids
    assert not catalog.ids_for(2)
    assert all(catalog.tag_for(pet_id) == 2 for pet_id in ids)


@pytest.mark.parametrize("season_id", [0, -1, True, "1", None])
def test_invalid_season_ids_never_select_mainline_or_other_tags(season_id):
    from rumi_market.season_catalog import SeasonPetCatalog

    assert SeasonPetCatalog().ids_for(season_id) == frozenset()


def test_all_sixty_avatar_only_ids_keep_unknown_tags():
    from rumi_market.season_catalog import DEFAULT_SEASON_TAGS, SeasonPetCatalog

    products = json.loads(DEFAULT_SEASON_TAGS.read_text(encoding="utf-8"))
    pets = json.loads((PROJECT / 'data/pets/catalog.json').read_text(encoding="utf-8"))
    unmapped = {row['pet_id'] for row in pets['pets']} - products['tags'].keys()
    assert len(unmapped) == 60
    catalog = SeasonPetCatalog()
    assert all(catalog.tag_for(pet_id) is None for pet_id in unmapped)
    assert catalog.tag_for("0102004") is None
    assert catalog.tag_for("999999") is None


def test_missing_file_is_lazy_and_raises_explicit_catalog_error(tmp_path):
    from rumi_market.season_catalog import SeasonPetCatalog

    catalog = SeasonPetCatalog(tmp_path / "missing.json")
    with pytest.raises(ApiError) as error:
        catalog.ids_for(1)
    assert error.value.code == "SEASON_CATALOG_UNAVAILABLE"
    assert error.value.status_code == 503


def test_changed_product_bytes_are_rejected(tmp_path):
    from rumi_market.season_catalog import DEFAULT_SEASON_TAGS, SeasonPetCatalog

    path = tmp_path / "changed.json"
    path.write_bytes(DEFAULT_SEASON_TAGS.read_bytes() + b" ")
    with pytest.raises(ApiError) as error:
        SeasonPetCatalog(path).tag_for("102004")
    assert error.value.code == "SEASON_CATALOG_UNAVAILABLE"
    assert error.value.status_code == 503


def test_source_identity_is_checked_independently_of_product_digest(tmp_path, monkeypatch):
    from rumi_market.season_catalog import DEFAULT_SEASON_TAGS, SeasonPetCatalog

    data = json.loads(DEFAULT_SEASON_TAGS.read_text(encoding="utf-8"))
    data["source"]["source_sha256"] = "0" * 64
    path = tmp_path / "changed-source.json"
    encoded = json.dumps(data).encode("utf-8")
    path.write_bytes(encoded)
    monkeypatch.setattr(SeasonPetCatalog, "PRODUCT_SHA256", hashlib.sha256(encoded).hexdigest())
    with pytest.raises(ApiError) as error:
        SeasonPetCatalog(path).ids_for(1)
    assert error.value.code == "SEASON_CATALOG_UNAVAILABLE"


@pytest.mark.external_materials
def test_generator_is_stable_and_matches_the_shipped_product(tmp_path, monkeypatch, external_materials_root):
    from rumi_market.season_catalog import DEFAULT_SEASON_TAGS, SeasonPetCatalog

    module = generator()
    monkeypatch.setattr(module, "ROOT", external_materials_root)
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    module.generate_season_tags(source=external_materials_root / REPORT, output=first)
    module.generate_season_tags(source=external_materials_root / REPORT, output=second)
    assert first.read_bytes() == second.read_bytes() == DEFAULT_SEASON_TAGS.read_bytes()
    assert hashlib.sha256(first.read_bytes()).hexdigest() == SeasonPetCatalog.PRODUCT_SHA256
    assert len(json.loads(first.read_text(encoding="utf-8"))["tags"]) == 246


def test_generator_rejects_modified_source_without_writing_output(tmp_path):
    module = generator()
    source, output = tmp_path / "source.json", tmp_path / "output.json"
    source.write_bytes(b'{"synthetic":"changed-source"}\n')
    with pytest.raises(ValueError, match="source identity"):
        module.generate_season_tags(source=source, output=output)
    assert not output.exists()


def synthetic_generator_materials(tmp_path, monkeypatch):
    module = generator()
    lumi_bytes = b'synthetic table identity'
    lumi_sha = hashlib.sha256(lumi_bytes).hexdigest()
    tags = {str(100000 + index): 1 + index % 2 for index in range(246)}
    source = {"hot_version": 65, "catalog_sha256": "c" * 64, "game_sha256": "e" * 64}
    projection = {"source": {"tables": [{"resource_key": "Lumi", "sha256": lumi_sha,
        "size": len(lumi_bytes)}]}, "checks": {"three_tables_strict_eof": True},
        "pets": [{"pet_id": pet_id, "lumi_tag": tag} for pet_id, tag in tags.items()]}
    identity = {"source": source, "config_table_facts": {"Lumi": {"serialized_sha256": lumi_sha,
        "serialized_bytes": len(lumi_bytes), "rows": 246, "unread_bytes": 0}},
        "projection": {"market_color_lumi_species_tags": [{"lumi_id": int(pet_id), "lumi_tag": tag}
                       for pet_id, tag in tags.items()]}}
    def save(name, value):
        raw = json.dumps(value, sort_keys=True).encode()
        (tmp_path / name).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()
    projection_sha = save('projection.json', projection)
    identity_sha = save('identity.json', identity)
    report = {"source": {**source, "projection_source": "projection.json",
        "hot65_identity_evidence": "identity.json", "projection_source_sha256": projection_sha,
        "hot65_identity_evidence_sha256": identity_sha, "lumi_table_sha256": lumi_sha}, "tags": tags}
    report_sha = save('report.json', report)
    for name, value in {"ROOT": tmp_path, "SOURCE_SHA256": report_sha, "LUMI_SHA256": lumi_sha,
        "PROJECTION_SHA256": projection_sha, "HOT65_EVIDENCE_SHA256": identity_sha,
        "PRODUCT_SOURCE": {**source, "source_sha256": report_sha, "lumi_table_sha256": lumi_sha,
            "projection_sha256": projection_sha, "hot65_evidence_sha256": identity_sha}}.items():
        monkeypatch.setattr(module, name, value)
    return module, tmp_path / 'report.json', tmp_path / 'identity.json', tags


def test_generator_is_deterministic_for_synthetic_verified_identity_chain(tmp_path, monkeypatch):
    module, source, _, tags = synthetic_generator_materials(tmp_path, monkeypatch)
    first, second = tmp_path / 'first.json', tmp_path / 'second.json'
    module.generate_season_tags(source=source, output=first)
    module.generate_season_tags(source=source, output=second)
    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text(encoding='utf-8'))['tags'] == tags


def test_generator_checks_referenced_public_evidence_hashes(tmp_path, monkeypatch):
    module, source, evidence, _ = synthetic_generator_materials(tmp_path, monkeypatch)
    evidence.write_bytes(evidence.read_bytes() + b" ")
    output = tmp_path / "output.json"
    with pytest.raises(ValueError, match="public evidence identity"):
        module.generate_season_tags(source=source, output=output)
    assert not output.exists()
