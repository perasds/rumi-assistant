import uuid
from dataclasses import FrozenInstanceError

import pytest


def test_normal_cache_miss_fallback_uses_uuid_and_64_byte_lowercase_bdid(monkeypatch):
    from rumi_market import sdk_device
    values=iter([uuid.UUID("12345678-1234-4234-8234-123456789abc"),
                 uuid.UUID("ABCDEF01-2345-4678-8901-ABCDEF012345"),
                 uuid.UUID("DEADBEEF-9876-4543-8210-123456789ABC")])
    monkeypatch.setattr(sdk_device.uuid,"uuid4",lambda:next(values))
    identity=sdk_device.SdkDeviceIdentity.create()
    assert identity.udid=="12345678-1234-4234-8234-123456789abc"
    assert identity.bd_id=="abcdef01-2345-4678-8901-abcdef012345-deadbeef-9876-4543-8210-123"
    assert len(identity.bd_id)==64
    assert identity.udid not in repr(identity) and identity.bd_id not in repr(identity)


def test_identity_is_reused_in_one_instance_and_fresh_for_new_clients():
    from rumi_market.sdk_device import SdkDeviceIdentity
    first,second=SdkDeviceIdentity.create(),SdkDeviceIdentity.create()
    assert str(uuid.UUID(first.udid))==first.udid
    assert uuid.UUID(first.udid).version==4
    with pytest.raises(FrozenInstanceError):
        first.udid="different-request-identity"
    assert first.udid!=second.udid and first.bd_id!=second.bd_id
