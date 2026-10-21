"""Hash-pinned public Lumi tags, loaded only when season filtering is used."""

import hashlib
import json
from pathlib import Path

from .errors import ApiError


DEFAULT_SEASON_TAGS = Path(__file__).resolve().parents[3] / "data/pets/season-tags.json"


class SeasonPetCatalog:
    SOURCE_SHA256 = "578ade29c6242ee89500815c03f669847026d01a6deacc6c5f2addd3468db4f7"
    PRODUCT_SHA256 = "3dadb7db16004006945638c373ab923d2c1846610abaefa85a46d08d6fc1deda"
    SOURCE = {
        "hot_version": 65,
        "catalog_sha256": "b0f474de9795fe81b842b2f495cb8679531e14fe31cd0698df06b605af19343a",
        "game_sha256": "ab029ae5433cbaa8b214a290387b40ae762a154cbc04096a26c886eba4eab4b3",
        "lumi_table_sha256": "e952a0fb4678d3fbe929f78853d1ad4338ce1810493a0be86747a663edbf1793",
        "projection_sha256": "e6cca45dbec57acdb9303128194cb99e6dd87e3142b032608988946b590c9227",
        "hot65_evidence_sha256": "7fbeadf7c103e8fab2811cb7f492ad2cdeaaae0f673db1ca69db1ee01206ddea",
    }

    def __init__(self, path: Path = DEFAULT_SEASON_TAGS):
        self.path = Path(path)
        self._tags: dict[str, int] | None = None

    def _load(self) -> dict[str, int]:
        if self._tags is None:
            try:
                raw = self.path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != self.PRODUCT_SHA256:
                    raise ValueError("Season product identity changed")
                document = json.loads(raw)
                expected_source = {**self.SOURCE, "source_sha256": self.SOURCE_SHA256}
                if document["schema_version"] != 1 or document["source"] != expected_source:
                    raise ValueError("Season source identity changed")
                tags = document["tags"]
                if (type(tags) is not dict or len(tags) != 246
                        or any(not pet_id.isascii() or not pet_id.isdecimal()
                               or not 0 < int(pet_id) < 2**31 or str(int(pet_id)) != pet_id
                               or type(tag) is not int or tag <= 0
                               for pet_id, tag in tags.items())):
                    raise ValueError("Invalid public season tags")
            except (OSError, ValueError, TypeError, KeyError):
                raise ApiError("SEASON_CATALOG_UNAVAILABLE", "赛季噜咪目录暂不可用", 503) from None
            self._tags = tags
        return self._tags

    def tag_for(self, pet_id: str) -> int | None:
        return self._load().get(pet_id)

    def ids_for(self, season_id: int) -> frozenset[str]:
        if type(season_id) is not int or season_id <= 0:
            return frozenset()
        return frozenset(pet_id for pet_id, tag in self._load().items() if tag == season_id + 1)
