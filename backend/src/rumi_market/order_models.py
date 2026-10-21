"""Validated order settings; quantities cross JSON as decimal strings."""
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal


class OrderSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    horizon_hours: int = Field(default=24, ge=1, le=168)
    max_orders: int = Field(default=1000, ge=1, le=10000)
    reserves: dict[str, str] = Field(default_factory=dict, max_length=256)
    allow_production: bool = True
    allow_refresh: bool = False

    @field_validator('reserves')
    @classmethod
    def quantities(cls, values):
        for key, value in values.items():
            if (not key.isascii() or not key.isdecimal() or len(key) > 10 or key != str(int(key))
                    or not 0 < int(key) < 1 << 31 or not value.isascii() or not value.isdecimal()
                    or len(value) > 20 or value != str(int(value)) or not 0 <= int(value) < 1 << 63):
                raise ValueError('invalid reserve')
        return values


class OrderPlanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    settings: OrderSettings


class OrderStartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,64}$')


class OrderResolveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    acknowledged: Literal[True]

    @field_validator('acknowledged', mode='before')
    @classmethod
    def acknowledgement(cls, value):
        if value is not True:
            raise ValueError('explicit acknowledgement required')
        return value
