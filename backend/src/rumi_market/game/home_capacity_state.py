"""Home capacity bonus only; Adventure state and opaque data are never retained.

The verified hot65 capacity readers select the first type-8 buff (601) in
server-list order. They read its server Stage without local expiry filtering.
All type-9 buffs in this fixed configuration have no second effect and add 0.
"""

from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from importlib.resources import files
import json

from google.protobuf import descriptor_pb2

from .envelope import ResponseEnvelope
from .order_codec import OrderProtoCodec
from .profile_sync import _load_schema
from .proto import BUSINESS_MESSAGE_LIMIT


MAX_COMBINE_DEPTH = 4
MAX_COMBINE_UNITS = 512
MAX_BUFF_ROWS = 16384
MAX_BUFF_CHANGES = 4096
_COMMANDS = frozenset({2016, 3036, 3040})
_PROFILE_SHA = {"SNKProto.Cmd_Notify_AdventureAll": "70c6d6ec34e4805fb13de9e41e3bdad5c73df8e06fab112b2be97066162f2fd0",
                "SNKProto.CmdAdventureBuff": "5eadceab89afc8c551ef8161e9ab6d0979eda6b91af2456ef631fad4687f55a5"}
_TABLE_SHA = {"AdventureBuff": "f0ddc5608ecfcafd88158a63dca18df7d8ce8eeb15713ffa24c26698ad67a769",
              "AdventureBuffEffect": "08a9303b0c3c482f3b9e4aa6e81d62e08fed190bede07110e5e6e48bc220b61a"}


@lru_cache(maxsize=1)
def _capacity_schema():
    metadata = files(__package__).joinpath("order-source.json").read_bytes()
    if len(metadata) > 128 * 1024:
        raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
    source = json.loads(metadata)
    config = source["capacity_config"]
    if (source.get("capacity_profile_message_sha256") != _PROFILE_SHA
            or config.get("table_sha256") != _TABLE_SHA or config.get("strict_eof") is not True
            or config.get("buff_id") != 601 or config.get("buff_map_type") != 8
            or config.get("secondary_effect_id") != 602
            or config.get("bonus_by_stage") != {"1": 300, "2": 200, "other": 100}
            or config.get("building_buff_capacity_bonus") != 0
            or config.get("building_buff_ids") != [1001, 1002, 1003, 1004, 1005, 1006, 1101, 1102]
            or config.get("stage_source") != "server_buff_stage" or config.get("local_stage_updates") is not False):
        raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
    classes, _, _ = _load_schema()
    baseline = classes[3036]
    for descriptor in (baseline.DESCRIPTOR, baseline.DESCRIPTOR.fields_by_name["buff_list"].message_type):
        definition = descriptor_pb2.DescriptorProto()
        descriptor.CopyToProto(definition)
        if hashlib.sha256(definition.SerializeToString(deterministic=True)).hexdigest() != _PROFILE_SHA[descriptor.full_name]:
            raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
    return baseline


def _buff(message):
    if not message.HasField("buff_id") or message.buff_id <= 0:
        raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
    if message.buff_id != 601:
        return None
    # The actual client uses the protobuf getter's uid=0 when it is absent.
    if message.buff_uid < 0:
        raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
    return message.buff_uid, message.stage if message.HasField("stage") else None


class HomeCapacityState:
    def __init__(self, verified_version: bool):
        if type(verified_version) is not bool:
            raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
        self._verified = verified_version
        self._closed = self._baseline = self._valid = False
        self._sequence = 0
        self._observed_at = self._bonus = None
        self._buffs = []
        self._codec = self._baseline_class = None
        self._unavailable_reason = "HOME_CAPACITY_BASELINE_MISSING" if verified_version else "HOME_CAPACITY_VERSION_UNVERIFIED"
        if verified_version:
            try:
                self._codec = OrderProtoCodec()
                self._baseline_class = _capacity_schema()
            except Exception:
                self._verified = False
                self._codec = self._baseline_class = None
                self._unavailable_reason = "HOME_CAPACITY_SCHEMA_UNAVAILABLE"

    @property
    def sequence(self):
        return self._sequence

    def invalidate(self):
        self._baseline = self._valid = False
        self._buffs.clear()
        self._bonus = self._observed_at = None
        self._sequence += 1
        if self._verified:
            self._unavailable_reason = "HOME_CAPACITY_STATE_INVALID"

    def close(self):
        self._closed = True
        self.invalidate()
        self._codec = self._baseline_class = None
        self._unavailable_reason = "HOME_CAPACITY_CLOSED"

    def snapshot(self):
        return {"valid": self._valid and not self._closed, "sequence": self._sequence,
                "bonus": self._bonus if self._valid and not self._closed else None,
                "observed_at": self._observed_at, "unavailable_reason": self._unavailable_reason}

    def observe(self, response: ResponseEnvelope):
        if self._closed or not self._verified:
            return
        if not isinstance(response, ResponseEnvelope):
            self.invalidate()
            return
        if response.command not in _COMMANDS:
            return
        try:
            if response.code != 0 or response.remaining != 0:
                raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
            self._observe(response.command, response.data, depth=0, units=[0])
        except Exception:
            self.invalidate()

    def _publish(self, buffs):
        if len(buffs) > MAX_BUFF_ROWS:
            raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
        if self._baseline and buffs == self._buffs:
            return
        self._baseline, self._buffs = True, buffs
        stage = buffs[0][1] if buffs else None
        self._valid = not buffs or stage is not None
        self._bonus = (0 if not buffs else 300 if stage == 1 else 200 if stage == 2 else 100) if self._valid else None
        self._unavailable_reason = None if self._valid else "HOME_CAPACITY_STAGE_UNKNOWN"
        self._sequence += 1
        self._observed_at = datetime.now(timezone.utc).isoformat()

    def _observe(self, command, data, *, depth, units):
        if command not in _COMMANDS:
            return
        if depth > MAX_COMBINE_DEPTH or len(data) > BUSINESS_MESSAGE_LIMIT:
            raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
        message = self._baseline_class.FromString(data) if command == 3036 else self._codec.decode(command, data)
        if command == 2016:
            units[0] += len(message.unit_lst)
            if units[0] > MAX_COMBINE_UNITS:
                raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
            for unit in message.unit_lst:
                if (not unit.HasField("id") or unit.id <= 0
                        or (unit.id in _COMMANDS and not unit.HasField("msg"))):
                    raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
                self._observe(unit.id, unit.msg, depth=depth + 1, units=units)
            return
        if command == 3036:
            if len(message.buff_list) > MAX_BUFF_ROWS:
                raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
            self._publish([row for message_buff in message.buff_list if (row := _buff(message_buff)) is not None])
            return
        if len(message.change_list) > MAX_BUFF_CHANGES:
            raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
        buffs = list(self._buffs)
        for change in message.change_list:
            if (not change.HasField("change_type") or change.change_type not in (1, 2, 3)
                    or not change.HasField("buff_info")):
                raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
            row = _buff(change.buff_info)
            if row is None or not self._baseline:
                continue
            if change.change_type == 1:
                if len(buffs) >= MAX_BUFF_ROWS:
                    raise ValueError("GAME_HOME_CAPACITY_STATE_INVALID")
                buffs.append(row)
            else:
                index = next((index for index, previous in enumerate(buffs) if previous[0] == row[0]), None)
                if index is not None:
                    if change.change_type == 2:
                        del buffs[index]
                    else:
                        buffs[index] = row
        if self._baseline:
            self._publish(buffs)
