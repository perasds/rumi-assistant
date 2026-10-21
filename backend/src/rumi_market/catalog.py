import json
import sqlite3
from pathlib import Path

from .item_catalog import ItemCatalog
from .models import PetResponse, PetView

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class Catalog:
    """SQLite holds public names and paths only, never user sessions."""

    def __init__(self, path: Path):
        self.root = path.parent.resolve()
        self.items = ItemCatalog(self.root.parent / "market" / "items.json")
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE pets (pet_id TEXT PRIMARY KEY, name TEXT, image_path TEXT);
            CREATE TABLE names (pet_id TEXT NOT NULL, value TEXT NOT NULL);
        """)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"pets": []}
        names_path = self.root / "names.json"
        names = json.loads(names_path.read_text(encoding="utf-8")) if names_path.exists() else {}
        names = names.get("names", names)
        for row in data.get("pets", []):
            pet_id = str(row["pet_id"])
            name = row.get("name") if row.get("name_verified") and row.get("sources") else None
            aliases = row.get("aliases", []) if name else []
            mapped = names.get(pet_id)
            if isinstance(mapped, dict) and mapped.get("source") and mapped.get("name"):
                name, aliases = mapped["name"], mapped.get("aliases", [])
            self.db.execute("INSERT INTO pets VALUES (?, ?, ?)",
                            (pet_id, name, row.get("image_path")))
            for value in ([name] + aliases if name else []):
                if isinstance(value, str) and value.strip():
                    self.db.execute("INSERT INTO names VALUES (?, ?)",
                                    (pet_id, value.strip().casefold()))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def matching_ids(self, query: str) -> set[str]:
        return {row[0] for row in self.db.execute(
            "SELECT DISTINCT pet_id FROM names WHERE instr(value, ?) > 0",
            (query.strip().casefold(),))}

    def _safe_image(self, image_path: str | None) -> Path | None:
        if not image_path:
            return None
        relative = Path(image_path)
        if relative.drive or relative.root or not relative.parts or relative.parts[0] != "images":
            return None
        path = (self.root / relative).resolve()
        images = (self.root / "images").resolve()
        if (not images.is_relative_to(self.root) or not path.is_relative_to(images)
                or path.suffix.lower() != ".png" or not path.is_file()):
            return None
        with path.open("rb") as image:
            return path if image.read(8) == PNG_SIGNATURE else None

    def image_path(self, pet_id: str) -> Path | None:
        row = self.db.execute("SELECT image_path FROM pets WHERE pet_id = ?", (pet_id,)).fetchone()
        return self._safe_image(row[0]) if row else None

    def pet(self, pet_id: str) -> PetView:
        row = self.db.execute("SELECT * FROM pets WHERE pet_id = ?", (pet_id,)).fetchone()
        name = row["name"] if row else None
        return PetView(pet_id=pet_id, name=name,
                       display_name=name or f"未识别宠物 · {pet_id}",
                       name_verified=bool(name),
                       image_url=f"/api/pets/{pet_id}/image" if self.image_path(pet_id) else None)

    def query(self, query: str = "") -> PetResponse:
        rows = self.db.execute("SELECT pet_id FROM pets ORDER BY rowid").fetchall()
        ids = self.matching_ids(query) if query.strip() else None
        pets = [self.pet(row[0]) for row in rows]
        items = [pet for pet in pets if ids is None or pet.pet_id in ids]
        return PetResponse(items=items, total=len(pets),
                           named_count=sum(pet.name is not None for pet in pets),
                           image_count=sum(pet.image_url is not None for pet in pets))
