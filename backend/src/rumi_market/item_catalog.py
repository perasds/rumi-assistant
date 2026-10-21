"""Verified public item names and checksum-checked icons, separate from pets."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _canonical_id(value: object) -> bool:
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value) is not None
            and int(value) <= 2_147_483_647)


@dataclass(frozen=True)
class ItemRecord:
    item_id: str
    name: str | None
    name_verified: bool
    source: str | None
    image_path: str | None
    image_sha256: str | None


class ItemCatalog:
    def __init__(self, path: Path):
        self.root = path.parent.resolve()
        self._items: dict[str, ItemRecord] = {}
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"items": []}
        for row in data.get("items", []):
            item_id, config_id = row.get("item_id"), row.get("config_id")
            if (not _canonical_id(item_id) or type(config_id) is not int or str(config_id) != item_id
                    or item_id in self._items):
                raise ValueError("Invalid or repeated item configuration ID")
            source = row.get("source")
            source = source if isinstance(source, str) and source.strip() else None
            name = row.get("name")
            verified = (row.get("name_verified") is True and source is not None
                        and isinstance(name, str) and bool(name.strip()))
            self._items[item_id] = ItemRecord(item_id, name if verified else None, verified, source,
                                              row.get("image_path"), row.get("image_sha256"))

    def lookup(self, item_id: str) -> ItemRecord | None:
        return self._items.get(item_id) if _canonical_id(item_id) else None

    def image_path(self, item_id: str) -> Path | None:
        item = self.lookup(item_id)
        if item is None or not isinstance(item.image_path, str) or not item.image_path:
            return None
        if not isinstance(item.image_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", item.image_sha256) is None:
            return None
        relative = Path(item.image_path)
        windows = PureWindowsPath(item.image_path)
        if (relative.drive or relative.root or windows.drive or windows.root
                or "\\" in item.image_path or ":" in item.image_path
                or not relative.parts or relative.parts[0] != "images" or ".." in relative.parts):
            return None
        try:
            path = (self.root / relative).resolve()
            images = (self.root / "images").resolve()
            if (not images.is_relative_to(self.root) or not path.is_relative_to(images)
                    or path.suffix.lower() != ".png" or not path.is_file()):
                return None
            data = path.read_bytes()
        except (OSError, RuntimeError, ValueError):
            return None
        return path if data.startswith(PNG_SIGNATURE) and hashlib.sha256(data).hexdigest() == item.image_sha256 else None
