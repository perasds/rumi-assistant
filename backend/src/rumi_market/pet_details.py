"""Local public encyclopedia data; no game provider or session dependency."""

import json
import re
from pathlib import Path

from pydantic import Field, ValidationError

from .catalog import PNG_SIGNATURE, Catalog
from .models import (InputModel, PetCatalogItem, PetCatalogResponse, PetDetail,
                     PetElement, PetNormalAttack, PetSkill, PetStatRange, PetTrait)

DETAIL_FIELDS = ["pokedex_id", "elements", "stats", "normal_attack", "traits",
                 "fixed_skill", "replaceable_skills"]


class _PetRecord(InputModel):
    pokedex_id: str | None = None
    elements: list[PetElement] = Field(default_factory=list)
    stats: list[PetStatRange] = Field(default_factory=list)
    normal_attack: PetNormalAttack | None = None
    trait_keys: list[str] = Field(default_factory=list)
    fixed_skill_id: str | None = None
    replaceable_skill_ids: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class PetDetails:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.root = catalog.root
        self._known_ids = {pet.pet_id for pet in catalog.query().items}
        path = self.root / "details.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema_version": 1, "source": {}, "pets": {}, "skills": {}, "traits": {}}
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data["schema_version"] != 1
                or any(not isinstance(data.get(key), dict) for key in ("source", "pets", "skills", "traits"))):
            raise ValueError("Invalid pet details schema")
        self._pets = {pet_id: _PetRecord.model_validate(row) for pet_id, row in data["pets"].items()}
        self._skills = data["skills"]
        self._traits = data["traits"]

    def catalog_query(self, query: str = "") -> PetCatalogResponse:
        summary = self.catalog.query(query)
        items = []
        for pet in summary.items:
            record = self._pets.get(pet.pet_id)
            items.append(PetCatalogItem(**pet.model_dump(),
                                       elements=record.elements if record else []))
        return PetCatalogResponse(items=items, total=summary.total,
                                  named_count=summary.named_count,
                                  image_count=summary.image_count)

    def _skill_record(self, skill_id: str) -> PetSkill | None:
        row = self._skills.get(skill_id)
        if not isinstance(row, dict):
            return None
        fields = {key: value for key, value in row.items() if key != "image_path"}
        try:
            skill = PetSkill.model_validate(fields)
        except ValidationError:
            return None
        return skill if skill.skill_id == skill_id else None

    def image_path(self, skill_id: str) -> Path | None:
        if (re.fullmatch(r"[1-9][0-9]*", skill_id) is None
                or self._skill_record(skill_id) is None):
            return None
        relative = self._skills[skill_id].get("image_path")
        if relative != f"images/skills/{skill_id}.png":
            return None
        try:
            images = (self.root / "images").resolve()
            skills = (self.root / "images" / "skills").resolve()
            path = (self.root / relative).resolve()
            if (not images.is_relative_to(self.root) or not skills.is_relative_to(images)
                    or not path.is_relative_to(skills) or path.suffix != ".png" or not path.is_file()):
                return None
            with path.open("rb") as image:
                return path if image.read(8) == PNG_SIGNATURE else None
        except (OSError, RuntimeError, ValueError):
            return None

    def _skill(self, skill_id: str) -> PetSkill | None:
        skill = self._skill_record(skill_id)
        if skill is None:
            return None
        return skill.model_copy(update={
            "image_url": f"/api/pet-skills/{skill_id}/image" if self.image_path(skill_id) else None})

    def lookup(self, pet_id: str) -> PetDetail | None:
        if pet_id not in self._known_ids:
            return None
        summary = self.catalog.pet(pet_id).model_dump()
        record = self._pets.get(pet_id)
        if record is None:
            return PetDetail(**summary, missing_fields=DETAIL_FIELDS)
        missing = list(record.missing_fields)
        traits = []
        for key in dict.fromkeys(record.trait_keys):
            try:
                trait = PetTrait.model_validate(self._traits.get(key))
            except ValidationError:
                trait = None
            if trait is None or f"{trait.kind}:{trait.trait_id}" != key:
                missing.append(f"traits:{key}")
            else:
                traits.append(trait)
        fixed = self._skill(record.fixed_skill_id) if record.fixed_skill_id is not None else None
        if record.fixed_skill_id is not None and fixed is None:
            missing.append(f"fixed_skill:{record.fixed_skill_id}")
        replaceable = []
        for skill_id in dict.fromkeys(record.replaceable_skill_ids):
            skill = self._skill(skill_id)
            if skill is None:
                missing.append(f"replaceable_skills:{skill_id}")
            else:
                replaceable.append(skill)
        return PetDetail(**summary, available=True, pokedex_id=record.pokedex_id,
                         elements=record.elements, stats=record.stats,
                         normal_attack=record.normal_attack, traits=traits,
                         fixed_skill=fixed, replaceable_skills=replaceable,
                         missing_fields=list(dict.fromkeys(missing)))
