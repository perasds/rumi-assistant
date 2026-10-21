"""Export the public pet book without importing a service or game provider."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from rumi_market.catalog import Catalog
from rumi_market.pet_details import PetDetails


ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*-[A-Za-z0-9_-]{8,}\.(?:js|css|png)")
ASSET_DIRECTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
PUBLIC_ID = re.compile(r"[1-9][0-9]*")


def _reject_links(path: Path, label: str) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError(f"{label} must not contain a link or reparse point")


def _frontend_files(frontend: Path) -> dict[str, bytes]:
    _reject_links(frontend, "frontend")
    if not frontend.is_dir():
        raise ValueError("frontend must be a build directory")
    entries = {entry.name: entry for entry in frontend.iterdir()}
    if set(entries) != {"book.html", "app-icon.svg", "assets"}:
        raise ValueError("frontend must contain only book.html, app-icon.svg and assets")
    files = {}
    for name in ("book.html", "app-icon.svg"):
        path = entries[name]
        _reject_links(path, "frontend")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("frontend entry must be a regular file")
        files["index.html" if name == "book.html" else name] = path.read_bytes()
    assets = entries["assets"]
    _reject_links(assets, "frontend")
    if not assets.is_dir():
        raise ValueError("frontend assets must be a directory")
    pending = [assets]
    while pending:
        for path in sorted(pending.pop().iterdir()):
            _reject_links(path, "frontend")
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode) and ASSET_DIRECTORY.fullmatch(path.name):
                pending.append(path)
            elif stat.S_ISREG(mode) and ASSET_NAME.fullmatch(path.name):
                files[path.relative_to(frontend).as_posix()] = path.read_bytes()
            else:
                raise ValueError("frontend contains an unknown build asset")
    return files


def _public_id(value: str, label: str) -> str:
    if PUBLIC_ID.fullmatch(value) is None:
        raise ValueError(f"Invalid {label} ID")
    return value


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def export_public_book(frontend: Path, catalog: Path, output: Path) -> dict:
    """Write a new public directory and return its non-public integrity manifest."""
    frontend, catalog, output = Path(frontend), Path(catalog), Path(output)
    if ".." in output.parts:
        raise ValueError("output must not contain parent traversal")
    frontend, catalog, output = frontend.absolute(), catalog.absolute(), output.absolute()
    _reject_links(output, "output")
    if output.exists():
        raise ValueError("output must not already exist")
    if output.is_relative_to(frontend) or output.is_relative_to(catalog.parent):
        raise ValueError("output must be outside the input directories")
    _reject_links(catalog, "catalog")
    if not catalog.is_file():
        raise ValueError("catalog must be a regular file")
    files = _frontend_files(frontend)
    for companion in (catalog.parent / "names.json", catalog.parent / "details.json",
                      catalog.parent.parent / "market" / "items.json"):
        _reject_links(companion, "catalog companion")
    source = Catalog(catalog)
    try:
        pet_details = PetDetails(source)
        response = pet_details.catalog_query()
        files["book-data/catalog.json"] = _json_bytes(response.model_dump(mode="json"))
        available_details = 0
        pet_images = set()
        skill_images = set()
        for pet in response.items:
            pet_id = _public_id(pet.pet_id, "pet")
            detail = pet_details.lookup(pet_id)
            if detail is None:
                raise ValueError("A public pet ID has no detail response")
            files[f"book-data/pets/{pet_id}.json"] = _json_bytes(detail.model_dump(mode="json"))
            available_details += int(detail.available)
            if pet.image_url is not None:
                image = source.image_path(pet_id)
                if image is None:
                    raise ValueError("A referenced pet image is unavailable")
                files[f"book-data/pet-images/{pet_id}.png"] = image.read_bytes()
                pet_images.add(pet_id)
            skills = ([detail.fixed_skill] if detail.fixed_skill is not None else [])
            for skill in [*skills, *detail.replaceable_skills]:
                skill_id = _public_id(skill.skill_id, "skill")
                if skill.image_url is None or skill_id in skill_images:
                    continue
                image = pet_details.image_path(skill_id)
                if image is None:
                    raise ValueError("A referenced skill image is unavailable")
                files[f"book-data/skill-images/{skill_id}.png"] = image.read_bytes()
                skill_images.add(skill_id)
    finally:
        source.close()

    output.mkdir(parents=True)
    manifest_files = []
    for name, raw in sorted(files.items()):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
        manifest_files.append({"path": name, "bytes": len(raw),
                               "sha256": hashlib.sha256(raw).hexdigest()})
    return {"schema_version": 1, "counts": {
        "catalog": len(response.items), "details": len(response.items),
        "available_details": available_details, "pet_images": len(pet_images),
        "skill_images": len(skill_images), "files": len(manifest_files),
    }, "files": manifest_files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = export_public_book(args.frontend, args.catalog, args.output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
