"""Public production configuration; independent of game sessions and RPCs."""
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import ApiError
from .item_catalog import ItemCatalog

DEFAULT_HOME_CATALOG = Path(__file__).resolve().parents[3] / 'data/home/production.json'
Identifier = Annotated[str, Field(pattern=r'^[1-9][0-9]{0,9}$')]
Name = Annotated[str, Field(min_length=1)]


class _Record(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ProductionItem(_Record):
    item_id: Identifier
    name: Name
    count: int = Field(ge=1)
    image_url: str | None = None


class ProductionRecipe(_Record):
    recipe_id: Identifier
    min_building_level: int = Field(ge=1)
    materials: list[ProductionItem]
    products: list[ProductionItem] = Field(min_length=1)


class ProductionBuilding(_Record):
    building_id: Identifier
    name: Name
    island_id: int = Field(ge=1)
    island_name: Name
    work_type: Name | None
    recipes: list[ProductionRecipe] = Field(min_length=1)


class HomeProductionView(_Record):
    scope: Literal['configuration'] = 'configuration'
    buildings: list[ProductionBuilding]
    total: int
    recipe_count: int


class _Document(_Record):
    schema_version: int = Field(ge=1, le=1)
    source: dict
    buildings: list[ProductionBuilding]


class HomeProductionCatalog:
    def __init__(self, path: Path, items: ItemCatalog):
        self.path = Path(path)
        self.items = items
        self._buildings = None

    def snapshot(self) -> HomeProductionView:
        if self._buildings is None:
            try:
                document = _Document.model_validate(json.loads(self.path.read_text(encoding='utf-8')))
                ids = [row.building_id for row in document.buildings]
                if len(ids) != len(set(ids)):
                    raise ValueError()
                for building in document.buildings:
                    recipe_ids = [recipe.recipe_id for recipe in building.recipes]
                    if len(recipe_ids) != len(set(recipe_ids)):
                        raise ValueError()
                self._buildings = document.buildings
            except (OSError, ValueError, TypeError):
                raise ApiError('HOME_CATALOG_UNAVAILABLE', '家园生产目录暂不可用', 503, retryable=True) from None
        buildings = [row.model_copy(deep=True) for row in self._buildings]
        for building in buildings:
            for recipe in building.recipes:
                for item in recipe.materials + recipe.products:
                    item.image_url = f'/api/items/{item.item_id}/image' if self.items.image_path(item.item_id) else None
        return HomeProductionView(buildings=buildings, total=len(buildings),
                                  recipe_count=sum(len(row.recipes) for row in buildings))
