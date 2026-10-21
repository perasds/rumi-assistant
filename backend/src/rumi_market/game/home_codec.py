"""Exact, fixed public hot64 Home wire types; no network or private archives."""

from functools import lru_cache
import hashlib
from importlib.resources import files
import json

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from .proto import BUSINESS_MESSAGE_LIMIT


HOME_CLIENT_VERSION = "1.5.3"
HOME_HOT_VERSION = 64
# Hot65 Home descriptors, handlers, writers and configuration readers match hot64.
# Public resource hashes and comparison scope are recorded in home-source.json.
# hot69 also requires architecture change type 12 and the ordinary-work farm guard.
HOME_HOT_VERSIONS = frozenset({HOME_HOT_VERSION, 65, 69, 71, 72})
_GAME_SHA = "770f11ce26df555e7212a456066e9ddd345b164c9b87f7622b179859fb8bd3b4"
_DESCRIPTOR_SHA = "6d9c34c9ed4143f1f70276002dda13320008c8efd339300cd4055fdac70d3e16"
_BINDINGS = {
    2016: "Cmd_Notify_Combine_Info", 2913: "Cmd_Notify_LoginEnd",
    2971: "Cmd_Notify_LumiAll", 2972: "Cmd_Notify_LumiChange",
    3001: "Cmd_Notify_HomeAll", 3003: "Cmd_Notify_HomeArchitectureChange",
    3004: "Cmd_Notify_HomeLumiList", 3005: "Cmd_Notify_HomeLumiWorkEnergyChange",
    3235: "Cmd_Notify_LumiStateChange", 3387: "Cmd_Home_Enter_Req",
    3388: "Cmd_Home_Enter_Ack", 3401: "Cmd_HomeOld_ArchiLumiOp_Req",
    3402: "Cmd_HomeOld_ArchiLumiOp_Ack", 3403: "Cmd_HomeOld_ArchiRecipeOp_Req",
    3404: "Cmd_HomeOld_ArchiRecipeOp_Ack", 3998: "Cmd_Notify_HomeArchiAll",
    10083: "Cmd_Home_ActFarm_Steal_Ack", 10089: "Cmd_Home_ActHome_GetStoreAward_Ack",
    10091: "Cmd_Notify_ActFarm_StealResult", 10093: "Cmd_Notify_ActFam_All",
}


class HomeCodecError(ValueError):
    def __init__(self):
        super().__init__("GAME_HOME_PROTOCOL_INVALID")


@lru_cache(maxsize=1)
def _home_schema():
    root = files(__package__)
    metadata = root.joinpath("home-source.json").read_bytes()
    if len(metadata) > 128 * 1024:
        raise HomeCodecError()
    source = json.loads(metadata)
    raw = root.joinpath("home.descriptor.pb").read_bytes()
    bindings = {row["rpc_id"]: row["message_type"] for row in source["rpc_bindings"]}
    expected = {command: "SNKProto." + name for command, name in _BINDINGS.items()}
    if (source.get("schema_version") != 1 or source.get("source_game_sha256") != _GAME_SHA
            or source.get("client_version") != HOME_CLIENT_VERSION or source.get("hot_version") != HOME_HOT_VERSION
            or source.get("descriptor_sha256") != _DESCRIPTOR_SHA
            or source.get("descriptor_size") != len(raw) or not 0 < len(raw) <= BUSINESS_MESSAGE_LIMIT
            or hashlib.sha256(raw).hexdigest() != _DESCRIPTOR_SHA
            or bindings != expected or len(source["rpc_bindings"]) != len(expected)
            or set(source["selected_roots"]) != set(expected.values())):
        raise HomeCodecError()
    descriptors = descriptor_pb2.FileDescriptorSet.FromString(raw)
    if not 0 < len(descriptors.file) <= 64:
        raise HomeCodecError()
    pending = {file.name: file for file in descriptors.file}
    if len(pending) != len(descriptors.file):
        raise HomeCodecError()
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
    known = {"google/protobuf/descriptor.proto"}
    while pending:
        ready = [name for name, file in pending.items() if set(file.dependency) <= known]
        if not ready:
            raise HomeCodecError()
        for name in ready:
            pool.AddSerializedFile(pending.pop(name).SerializeToString(deterministic=True))
            known.add(name)
    classes = {command: message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
               for command, name in expected.items()}
    pet = message_factory.GetMessageClass(pool.FindMessageTypeByName("SNKProto.CmdLumiOne"))
    return classes, pet


class HomeProtoCodec:
    def __init__(self):
        try:
            self._classes, self._pet_class = _home_schema()
        except Exception:
            raise HomeCodecError() from None

    def message_class(self, command: int):
        if type(command) is not int or command not in self._classes:
            raise HomeCodecError()
        return self._classes[command]

    def pet_message(self, payload: bytes):
        try:
            if type(payload) is not bytes or len(payload) > BUSINESS_MESSAGE_LIMIT:
                raise HomeCodecError()
            return self._pet_class.FromString(payload)
        except Exception:
            raise HomeCodecError() from None

    def decode(self, command: int, payload: bytes):
        try:
            if type(payload) is not bytes or len(payload) > BUSINESS_MESSAGE_LIMIT:
                raise HomeCodecError()
            return self.message_class(command).FromString(payload)
        except Exception:
            raise HomeCodecError() from None

    def encode(self, command: int, **fields) -> bytes:
        try:
            payload = self.message_class(command)(**fields).SerializeToString(deterministic=True)
            if len(payload) > BUSINESS_MESSAGE_LIMIT:
                raise HomeCodecError()
            return payload
        except Exception:
            raise HomeCodecError() from None
