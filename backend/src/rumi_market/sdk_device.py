"""Normal SDK cache-miss identity fallback, owned by one independent client."""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class SdkDeviceIdentity:
    udid: str
    bd_id: str

    @classmethod
    def create(cls) -> "SdkDeviceIdentity":
        return cls(str(uuid.uuid4()), (str(uuid.uuid4()) + "-" + str(uuid.uuid4()))[:64].lower())
