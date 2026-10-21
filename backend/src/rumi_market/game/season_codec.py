"""Minimal source-bound hot65 season notification wire projection."""
import hashlib
from importlib.resources import files
import json

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from .proto import BUSINESS_MESSAGE_LIMIT

# hot69 preserves the selected season fields and existing species tags.
SEASON_HOT_VERSIONS = frozenset({65, 69, 71, 72})
_GAME_SHA = "ab029ae5433cbaa8b214a290387b40ae762a154cbc04096a26c886eba4eab4b3"
_DESCRIPTOR_SHA = "aa0a0b4bfd0d15989b297fa7654cb97ff9eb35b5988feec9d92c553424e4d89c"
_BINDINGS = {8001: "SNKProto.Cmd_Notify_ACT_All", 8002: "SNKProto.Cmd_Notify_ACT_Change"}


class SeasonCodecError(ValueError):
    def __init__(self):
        super().__init__("GAME_SEASON_PROTOCOL_INVALID")


class SeasonProtoCodec:
    def __init__(self):
        try:
            root = files(__package__)
            metadata = root.joinpath("season-source.json").read_bytes()
            if len(metadata) > 128 * 1024:
                raise SeasonCodecError()
            source = json.loads(metadata)
            raw = root.joinpath("season.descriptor.pb").read_bytes()
            bindings = {row["rpc_id"]: row["message_type"] for row in source["rpc_bindings"]}
            if (type(source.get("schema_version")) is not int or source["schema_version"] != 1
                    or source.get("source_game_sha256") != _GAME_SHA
                    or source.get("client_version") != "1.5.3" or source.get("hot_version") != 65
                    or source.get("descriptor_sha256") != _DESCRIPTOR_SHA
                    or source.get("descriptor_size") != len(raw)
                    or not 0 < len(raw) <= BUSINESS_MESSAGE_LIMIT
                    or hashlib.sha256(raw).hexdigest() != _DESCRIPTOR_SHA
                    or bindings != _BINDINGS or len(source["rpc_bindings"]) != len(_BINDINGS)
                    or set(source["selected_roots"]) != set(_BINDINGS.values())):
                raise SeasonCodecError()
            descriptors = descriptor_pb2.FileDescriptorSet.FromString(raw)
            if len(descriptors.file) != 1:
                raise SeasonCodecError()
            pool = descriptor_pool.DescriptorPool()
            pool.AddSerializedFile(descriptors.file[0].SerializeToString(deterministic=True))
            self._classes = {command: message_factory.GetMessageClass(pool.FindMessageTypeByName(owner))
                             for command, owner in _BINDINGS.items()}
        except Exception:
            raise SeasonCodecError() from None

    def message_class(self, command: int):
        if type(command) is not int or command not in self._classes:
            raise SeasonCodecError()
        return self._classes[command]

    def decode(self, command: int, payload: bytes):
        try:
            if type(payload) is not bytes or len(payload) > BUSINESS_MESSAGE_LIMIT:
                raise SeasonCodecError()
            return self.message_class(command).FromString(payload)
        except Exception:
            raise SeasonCodecError() from None

    def encode(self, command: int, **fields) -> bytes:
        try:
            raw = self.message_class(command)(**fields).SerializeToString(deterministic=True)
            if len(raw) > BUSINESS_MESSAGE_LIMIT:
                raise SeasonCodecError()
            return raw
        except Exception:
            raise SeasonCodecError() from None
