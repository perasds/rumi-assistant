"""Hash-pinned public game wire schemas; this layer performs no authentication."""

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


SCHEMA_SHA256 = "8b4cf9cf1f35aaea159965131975d3f40dbb3010d3eaa3026522b63e6ad3145e"
# A business message may have been reassembled from several TCP frames.
# This is an application memory bound, not a claimed server protocol maximum.
BUSINESS_MESSAGE_LIMIT = 4 * 1024 * 1024
ENVELOPE_TYPES = frozenset({"SNKProto.ReqProto", "SNKProto.RspProto"})
MESSAGE_TYPES = frozenset("SNKProto." + name for name in (
    "ReqProto", "RspProto", "Cmd_Net_LumiLogin_Req", "Cmd_Net_LumiLogin_Ack",
    "Cmd_Role_EnterGame_Req", "Cmd_Role_EnterGame_Ack", "Cmd_Notify_LoginEnd",
    "Cmd_Heartbeat_Req", "Cmd_Heartbeat_Ack",
    "Cmd_Market_Auction_QueryLumiDetail_Req", "Cmd_Market_Auction_QueryLumiDetail_Ack",
    "Cmd_Market_Auction_Watch_Start_Req", "Cmd_Market_Auction_Watch_Start_Ack",
    "Cmd_Market_Auction_Watch_Stop_Req", "Cmd_Market_Auction_Watch_Stop_Ack",
    "Cmd_Market_Auction_Buy_Req", "Cmd_Market_Auction_Buy_Ack",
    "Cmd_SystemShop_QueryItemPrice_Req", "Cmd_SystemShop_QueryItemPrice_Ack",
))


class GameProtoError(ValueError):
    pass


@dataclass(repr=False)
class DecodedGameMessage:
    """Internal RAM-only message; default repr never formats business values."""

    message: object


@lru_cache(maxsize=1)
def _message_classes():
    result = None
    try:
        raw = files(__package__).joinpath("protocol.descriptor.pb").read_bytes()
        if len(raw) != 10458 or hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256:
            raise ValueError
        descriptors = descriptor_pb2.FileDescriptorSet.FromString(raw)
        pool = descriptor_pool.DescriptorPool()
        pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
        pending = {file.name: file for file in descriptors.file}
        if len(pending) != len(descriptors.file) or len(pending) != 24:
            raise ValueError
        known = {"google/protobuf/descriptor.proto"}
        while pending:
            ready = [name for name, file in pending.items() if set(file.dependency) <= known]
            if not ready:
                raise ValueError
            for name in ready:
                pool.AddSerializedFile(pending.pop(name).SerializeToString(deterministic=True))
                known.add(name)
        result = MappingProxyType({name: message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
                                   for name in MESSAGE_TYPES})
    except Exception:
        pass
    if result is None:
        raise GameProtoError("GAME_PROTO_SCHEMA_INVALID")
    return result


class GameProtoCodec:
    def __init__(self):
        self._classes = _message_classes()

    @property
    def message_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._classes))

    def encode(self, message_name: str, fields: dict) -> bytes:
        result = None
        try:
            if type(message_name) is str and isinstance(fields, dict) and message_name in self._classes:
                result = self._classes[message_name](**fields).SerializeToString(deterministic=True)
        except Exception:
            pass
        fields = None
        limit = 65535 if type(message_name) is str and message_name in ENVELOPE_TYPES else BUSINESS_MESSAGE_LIMIT
        if result is None or len(result) > limit:
            result = None
            raise GameProtoError("GAME_PROTO_INVALID")
        return result

    def decode(self, message_name: str, payload: bytes) -> DecodedGameMessage:
        result = None
        limit = 65535 if type(message_name) is str and message_name in ENVELOPE_TYPES else BUSINESS_MESSAGE_LIMIT
        try:
            if (type(message_name) is str and message_name in self._classes
                    and isinstance(payload, bytes) and len(payload) <= limit):
                message = self._classes[message_name]()
                message.ParseFromString(payload)
                result = DecodedGameMessage(message)
        except Exception:
            pass
        payload = None
        if result is None:
            raise GameProtoError("GAME_PROTO_INVALID")
        return result
