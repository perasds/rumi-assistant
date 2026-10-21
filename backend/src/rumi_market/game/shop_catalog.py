"""Pinned public shop candidates and verified pet joins; no live quote data.

The data snapshot records its source hashes and the small Price=0 source chain:
MarketSystemShop.ctor/0x6001146 leaves Int32 Price at its CLR default; normal
_RequestShopItemPrice/0x6007799 and item QueryAndOpenBuyView/0x6007772 create
those objects without assigning Price, then the 3522 sender reads that field.
This is request state, not a price, stock count or permission to buy.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from .market_query import SystemShopCandidate, SystemShopKind


SHOP_CATALOG_PATH = Path(__file__).resolve().parents[4] / "data/market/shop-candidates.json"
SHOP_CATALOG_SHA256 = "29f54c0e4a490a89e2d37e0ebc359277861a1e703d16120aaeda9d3bb727cddc"
_MAX_BYTES = 128 * 1024


class ShopCatalogError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class ShopCatalogEntry:
    config_id: int
    quality: int
    pet_id: str | None
    name: str | None
    image_path: str | None  # Relative to data/pets; existence is checked by that catalog.
    image_sha256: str | None


@dataclass(frozen=True, repr=False)
class ShopCatalog:
    normal_pets: tuple[ShopCatalogEntry, ...]
    items: tuple[ShopCatalogEntry, ...]
    normal_pet_mapping: Mapping[int, str]

    def candidates(
        self, kind: SystemShopKind,
        current_prices: Mapping[tuple[int, int], int] | None = None,
    ) -> tuple[SystemShopCandidate, ...]:
        """Return immutable RPC inputs; price cache keys are (config_id, quality).

        Missing pairs use the source-confirmed fresh object default zero. Any
        supplied prices belong to the caller's query context; the static catalog
        never treats them or its default as current server quotes.
        """
        if type(kind) is not str or kind not in ("normal_pet", "item"):
            raise ShopCatalogError("GAME_SHOP_CATALOG_INVALID")
        prices = None
        try:
            if current_prices is None:
                prices = {}
            elif isinstance(current_prices, Mapping):
                supplied = dict(current_prices)
                if all(type(key) is tuple and len(key) == 2
                       and all(_int32(value) for value in key) and _int32(price)
                       for key, price in supplied.items()):
                    prices = supplied
        except Exception:
            pass
        if prices is None:
            raise ShopCatalogError("GAME_SHOP_CATALOG_PRICE_INVALID")
        rows = self.normal_pets if kind == "normal_pet" else self.items
        return tuple(SystemShopCandidate(config_id=row.config_id, quality=row.quality,
            price=prices.get((row.config_id, row.quality), 0)) for row in rows)


def _int32(value: object) -> bool:
    return type(value) is int and -(2**31) <= value < 2**31


def _entries(values: object) -> tuple[ShopCatalogEntry, ...]:
    if type(values) is not list:
        raise ValueError()
    result = tuple(ShopCatalogEntry(**row) for row in values)
    if any(not _int32(row.config_id) or row.config_id <= 0 or not _int32(row.quality)
           or row.quality < 0 or any(value is not None and type(value) is not str for value
              in (row.pet_id, row.name, row.image_path, row.image_sha256)) for row in result):
        raise ValueError()
    return result


def load_shop_catalog(path: str | Path | None = None) -> ShopCatalog:
    """Read only the exact verified snapshot, bounded in size, from a local file."""
    result = None
    try:
        selected = SHOP_CATALOG_PATH if path is None else Path(path)
        with selected.open("rb") as source:
            raw = source.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES or hashlib.sha256(raw).hexdigest() != SHOP_CATALOG_SHA256:
            raise ValueError()
        data = json.loads(raw)
        if data["schema_version"] != 1 or data["app_version"] != "1.5.3":
            raise ValueError()
        normal, items = _entries(data["normal_pets"]), _entries(data["items"])
        pairs = {(row.config_id, row.quality) for row in normal}
        if (len(normal) != 304 or len(pairs) != 304 or {row.quality for row in normal} != {4, 5}
                or len(items) != 5 or len({row.config_id for row in items}) != 5
                or any(row.quality != 0 or row.pet_id is not None for row in items)):
            raise ValueError()
        mapping = {}
        for row in normal:
            if not row.pet_id or not row.name or not row.image_path or not row.image_sha256:
                raise ValueError()
            if mapping.setdefault(row.config_id, row.pet_id) != row.pet_id:
                raise ValueError()
        if len(mapping) != 152:
            raise ValueError()
        result = ShopCatalog(normal, items, MappingProxyType(mapping))
    except Exception:
        pass
    if result is None:
        raise ShopCatalogError("GAME_SHOP_CATALOG_INVALID")
    return result
