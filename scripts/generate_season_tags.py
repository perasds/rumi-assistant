"""Generate fixed public species tags offline; never read account archives."""

import argparse
import hashlib
import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
SOURCE_REPORT = ROOT / "artifacts/rumi/market-season-analysis/lumi-tags-all-species.public.json"
OUTPUT = PROJECT / "data/pets/season-tags.json"
SOURCE_SHA256 = "578ade29c6242ee89500815c03f669847026d01a6deacc6c5f2addd3468db4f7"
LUMI_SHA256 = "e952a0fb4678d3fbe929f78853d1ad4338ce1810493a0be86747a663edbf1793"
PROJECTION_SHA256 = "e6cca45dbec57acdb9303128194cb99e6dd87e3142b032608988946b590c9227"
HOT65_EVIDENCE_SHA256 = "7fbeadf7c103e8fab2811cb7f492ad2cdeaaae0f673db1ca69db1ee01206ddea"
PRODUCT_SOURCE = {
    "hot_version": 65,
    "catalog_sha256": "b0f474de9795fe81b842b2f495cb8679531e14fe31cd0698df06b605af19343a",
    "game_sha256": "ab029ae5433cbaa8b214a290387b40ae762a154cbc04096a26c886eba4eab4b3",
    "lumi_table_sha256": LUMI_SHA256,
    "source_sha256": SOURCE_SHA256,
    "projection_sha256": PROJECTION_SHA256,
    "hot65_evidence_sha256": HOT65_EVIDENCE_SHA256,
}


def verified_json(path, expected, message):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(message)
    return json.loads(raw)


def generate_season_tags(source=SOURCE_REPORT, output=OUTPUT):
    report = verified_json(source, SOURCE_SHA256, "Public source identity changed")
    facts = report["source"]
    projection = verified_json(ROOT / facts["projection_source"], PROJECTION_SHA256,
                               "Referenced public evidence identity changed")
    hot65 = verified_json(ROOT / facts["hot65_identity_evidence"], HOT65_EVIDENCE_SHA256,
                          "Referenced public evidence identity changed")
    old_lumi = next(row for row in projection["source"]["tables"] if row["resource_key"] == "Lumi")
    new_lumi = hot65["config_table_facts"]["Lumi"]
    if (facts["projection_source_sha256"] != PROJECTION_SHA256
            or facts["hot65_identity_evidence_sha256"] != HOT65_EVIDENCE_SHA256
            or facts["lumi_table_sha256"] != LUMI_SHA256
            or old_lumi["sha256"] != LUMI_SHA256
            or new_lumi["serialized_sha256"] != LUMI_SHA256
            or old_lumi["size"] != new_lumi["serialized_bytes"]
            or new_lumi["rows"] != 246 or new_lumi["unread_bytes"] != 0
            or not projection["checks"]["three_tables_strict_eof"]):
        raise ValueError("Public Lumi table identity chain changed")
    if (facts["hot_version"] != 65
            or hot65["source"]["hot_version"] != 65
            or facts["catalog_sha256"] != PRODUCT_SOURCE["catalog_sha256"]
            or hot65["source"]["catalog_sha256"] != PRODUCT_SOURCE["catalog_sha256"]
            or facts["game_sha256"] != PRODUCT_SOURCE["game_sha256"]
            or hot65["source"]["game_sha256"] != PRODUCT_SOURCE["game_sha256"]):
        raise ValueError("Public hot65 source identity changed")
    tags = {row["pet_id"]: row["lumi_tag"] for row in projection["pets"]}
    if (len(tags) != 246 or len(projection["pets"]) != 246 or tags != report["tags"]
            or any(tags.get(str(row["lumi_id"])) != row["lumi_tag"]
                   for row in hot65["projection"]["market_color_lumi_species_tags"])):
        raise ValueError("Public species map differs from verified configuration")
    document = {"schema_version": 1, "source": PRODUCT_SOURCE, "tags": tags}
    encoded = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    return {"species": len(tags), "sha256": hashlib.sha256(encoded).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_REPORT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(generate_season_tags(args.source, args.output)))


if __name__ == "__main__":
    main()
