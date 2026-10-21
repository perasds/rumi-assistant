"""Copy only checksum-verified public pet resources into the application."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


def import_catalog(source_catalog, target_directory):
    source_catalog = Path(source_catalog).resolve()
    source_directory = source_catalog.parent
    target_directory = Path(target_directory).resolve()
    source = json.loads(source_catalog.read_text(encoding="utf-8"))
    pets = []
    copies = []
    seen = set()
    for original in source["pets"]:
        number = str(original["pet_id"])
        if not re.fullmatch(r"[0-9]+", number) or number in seen:
            raise ValueError("Pet resource ID is invalid or repeated")
        seen.add(number)
        image_path = None
        image_sha256 = None
        if original.get("image_path"):
            image = (source_directory / original["image_path"]).resolve()
            if not image.is_relative_to(source_directory) or not image.is_file():
                raise ValueError("Image is missing or outside the source directory")
            image_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
            if image_sha256 != original["image_sha256"]:
                raise ValueError("Public image checksum mismatch")
            image_path = f"images/{number}.png"
            copies.append((image, target_directory / image_path))
        pets.append({
            "pet_id": number,
            "name": None,
            "name_verified": False,
            "aliases": [],
            "image_path": image_path,
            "image_sha256": image_sha256,
            "sources": [{"asset_path": original.get("asset_path"), "catalog_sha256": source.get("catalog_sha256")}],
        })
    target_directory.mkdir(parents=True, exist_ok=True)
    (target_directory / "images").mkdir(exist_ok=True)
    for origin, destination in copies:
        shutil.copyfile(origin, destination)
    output = {
        "schema_version": 1,
        "app_version": source.get("app_version"),
        "catalog_sha256": source.get("catalog_sha256"),
        "pets": pets,
    }
    temporary = target_directory / "catalog.json.tmp"
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target_directory / "catalog.json")
    names = target_directory / "names.json"
    if not names.exists():
        names.write_text('{"names": {}}\n', encoding="utf-8")
    return {"pet_count": len(pets), "image_count": len(copies), "names_preserved": True}


def main():
    workspace = Path(__file__).resolve().parents[2]
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=workspace / "artifacts/rumi/pet-catalog/pet-catalog.json")
    parser.add_argument("--output", type=Path, default=project / "data/pets")
    arguments = parser.parse_args()
    print(json.dumps(import_catalog(arguments.source, arguments.output)))


if __name__ == "__main__":
    main()
