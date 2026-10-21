"""Exact hot65 order/collection wire types from fixed public resources."""

from functools import lru_cache
import hashlib
from importlib.resources import files
import json

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from .proto import BUSINESS_MESSAGE_LIMIT


ORDER_CLIENT_VERSION = "1.5.3"
ORDER_HOT_VERSION = 65
# hot69 keeps the consumed wire contract; see docs/hot69-runtime-compatibility.public.json.
ORDER_HOT_VERSIONS = frozenset({65, 69, 71, 72})
_GAME_SHA = "ab029ae5433cbaa8b214a290387b40ae762a154cbc04096a26c886eba4eab4b3"
_CATALOG_SHA = "b0f474de9795fe81b842b2f495cb8679531e14fe31cd0698df06b605af19343a"
_BUNDLE_SHA = {"game": "da0ca2402b9251cb6f6d915c1752c5961d4bfd1e8cfad9774b7d849e52a0b2c4",
               "vib": "2fee3ffbc597ae35adfbe9a49651f848893f2cd9ca7bf6acd9a4041c7a3e60dd"}
_DESCRIPTOR_SHA = "446b75c38ede4ea70d9f0916c7694ec3505011eddc23270a908b23c003d6437a"
_BINDINGS = {2016: "Cmd_Notify_Combine_Info", 3001: "Cmd_Notify_HomeAll",
             3301: "Cmd_Notify_HomeOrderArchiChange", 3302: "Cmd_Home_Order_Complete_Req",
             3303: "Cmd_Home_Order_Complete_Ack", 3304: "Cmd_Home_Order_Refresh_Req",
             3305: "Cmd_Home_Order_Refresh_Ack", 3025: "Cmd_Home_ArchitecturePickWorkReward_Req",
             3026: "Cmd_Home_ArchitecturePickWorkReward_Ack",
             3027: "Cmd_Home_ArchitecturePickAllWorkReward_Req",
             3028: "Cmd_Home_ArchitecturePickAllWorkReward_Ack", 3029: "Cmd_Home_TryUpdateWork_Req",
             3030: "Cmd_Home_TryUpdateWork_Ack", 3040: "Cmd_Notify_AdventureBuffChange"}


class OrderCodecError(ValueError):
    def __init__(self):
        super().__init__("GAME_ORDER_PROTOCOL_INVALID")


@lru_cache(maxsize=1)
def _order_schema():
    root = files(__package__)
    metadata = root.joinpath("order-source.json").read_bytes()
    if len(metadata) > 128 * 1024:
        raise OrderCodecError()
    source = json.loads(metadata)
    raw = root.joinpath("order.descriptor.pb").read_bytes()
    expected = {command: "SNKProto." + name for command, name in _BINDINGS.items()}
    bindings = {row["rpc_id"]: row["message_type"] for row in source["rpc_bindings"]}
    if (source.get("schema_version") != 1 or source.get("client_version") != ORDER_CLIENT_VERSION
            or source.get("hot_version") != ORDER_HOT_VERSION or source.get("source_game_sha256") != _GAME_SHA
            or source.get("catalog_sha256") != _CATALOG_SHA or source.get("encrypted_bundle_sha256") != _BUNDLE_SHA
            or source.get("descriptor_sha256") != _DESCRIPTOR_SHA or source.get("descriptor_size") != len(raw)
            or not 0 < len(raw) <= BUSINESS_MESSAGE_LIMIT or hashlib.sha256(raw).hexdigest() != _DESCRIPTOR_SHA
            or bindings != expected or len(source["rpc_bindings"]) != len(expected)
            or len(source["selected_roots"]) != len(expected) or set(source["selected_roots"]) != set(expected.values())):
        raise OrderCodecError()
    descriptors = descriptor_pb2.FileDescriptorSet.FromString(raw)
    if not 0 < len(descriptors.file) <= 64:
        raise OrderCodecError()
    pending = {file.name: file for file in descriptors.file}
    if len(pending) != len(descriptors.file):
        raise OrderCodecError()
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
    known = {"google/protobuf/descriptor.proto"}
    while pending:
        ready = [name for name, file in pending.items() if set(file.dependency) <= known]
        if not ready:
            raise OrderCodecError()
        for name in ready:
            pool.AddSerializedFile(pending.pop(name).SerializeToString(deterministic=True))
            known.add(name)
    return {command: message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
            for command, name in expected.items()}


class OrderProtoCodec:
    def __init__(self):
        try:
            self._classes = _order_schema()
        except Exception:
            raise OrderCodecError() from None

    @property
    def known_commands(self):
        return frozenset(self._classes)

    def message_class(self, command: int):
        if type(command) is not int or command not in self._classes:
            raise OrderCodecError()
        return self._classes[command]

    def decode(self, command: int, payload: bytes):
        try:
            if type(payload) is not bytes or len(payload) > BUSINESS_MESSAGE_LIMIT:
                raise OrderCodecError()
            return self.message_class(command).FromString(payload)
        except Exception:
            raise OrderCodecError() from None

    def encode(self, command: int, **fields) -> bytes:
        try:
            payload = self.message_class(command)(**fields).SerializeToString(deterministic=True)
            if len(payload) > BUSINESS_MESSAGE_LIMIT:
                raise OrderCodecError()
            return payload
        except Exception:
            raise OrderCodecError() from None
