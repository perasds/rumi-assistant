"""Explicit browser contracts for session-owned home management."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r'^(0|[1-9][0-9]{0,19})$')]


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class HomeChange(Record):
    building_uid: Identifier
    recipe_id: Identifier | None = None
    lumi_uids: list[Identifier] | None = Field(default=None, min_length=1, max_length=1)
    production_count: int | None = Field(default=None, ge=0, lt=1 << 31)

    @model_validator(mode='after')
    def changes_are_explicit(self):
        fields = self.model_fields_set - {'building_uid'}
        if not fields or any(getattr(self, name) is None for name in fields):
            raise ValueError('Explicit nonempty changes required')
        return self


class HomeSettingsRequest(Record):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,64}$')
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    changes: list[HomeChange] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def unique_buildings(self):
        if len({row.building_uid for row in self.changes}) != len(self.changes):
            raise ValueError('Duplicate building')
        return self


class HomeBuildingView(Record):
    building_uid: str
    building_id: str
    level: int | None
    current_recipe_id: str | None
    assigned_lumi_uids: list[str]
    production_count: int | None
    completed_count: int | None
    quantity_mode: Literal['batch', 'continuous', 'unverified']
    editable: bool
    reason: str | None
    production_editable: bool
    production_reason: str | None
    allowed_recipe_ids: list[str] | None
    assignment_limit: int | None


class HomeWorkerView(Record):
    lumi_uid: str
    lumi_id: str
    name: str
    image_url: str | None
    work_aptitude: int | None
    work_energy: int | None
    assigned_building_uid: str | None
    eligible_building_uids: list[str] | None
    selectable: bool
    reason: str | None


class HomeApplyItem(Record):
    building_uid: str
    status: Literal['applied', 'partial', 'rejected', 'uncertain', 'skipped']
    message: str


class HomeApplyResult(Record):
    request_id: str
    status: Literal['applying', 'applied', 'partial', 'rejected', 'uncertain']
    message: str
    results: list[HomeApplyItem]


class HomeAccountView(Record):
    scope: Literal['account'] = 'account'
    ready: bool
    revision: str | None
    observed_at: str | None
    message: str | None
    buildings: list[HomeBuildingView]
    roster: list[HomeWorkerView]
    last_apply: HomeApplyResult | None
