"""Private, memory-only values passed between the worker and game adapter."""
from dataclasses import dataclass

from pydantic import Field

from .models import InputModel


class RedbagStartRequest(InputModel):
    request_id: str = Field(strict=True, pattern=r'^[A-Za-z0-9_-]{16,64}$')


@dataclass(frozen=True, repr=False)
class RedbagCandidate:
    id: str
    sender_role_id: str
    expires_at: int = 0


@dataclass(frozen=True, repr=False)
class RedbagReceipt:
    outcome: str
    stamina: int = 0
