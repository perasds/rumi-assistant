"""Hash-pinned hot71 protobuf types; no account data or network access."""
from functools import lru_cache
import hashlib
from importlib.resources import files
import json

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

REDBAG_CLIENT_VERSION = "1.5.3"
REDBAG_HOT_VERSIONS = frozenset({71, 72})
PAYLOAD_LIMIT = 1024 * 1024
_DESCRIPTOR_SHA = "2be530641f6218dde1a5fa1f40629af415e73c49bf6ec753f3af607ebc31ea86"
_GAME_SHA = "0867970fcfd9a33984120c85ad91644b1e17352c4ddae85e083cea5f59c089f8"
_CATALOG_SHA = "5ae75e7d91edf555f5776fa5eb7089162f5cc5b2918774e74d28610ca6f8cf98"
_BINDINGS = {2919: "Cmd_Notify_Chat_Auth_Session",
    4009: "Cmd_Home_VisitShareQuery_Req", 4010: "Cmd_Home_VisitShareQuery_Ack",
    4011: "Cmd_Home_VisitGetShareAward_Req", 4012: "Cmd_Home_VisitGetShareAward_Ack",
    23903: "Cmd_JoinWorldChannel_Req", 23904: "Cmd_JoinWorldChannel_Ack",
    23909: "Cmd_Notify_ChatMessage_Broadcast", 23910: "Cmd_Notify_ChatMessage_History",
    23920: "Cmd_Auth_Req", 23921: "Cmd_Auth_Ack", 23922: "Cmd_Reconnect_Req",
    23923: "Cmd_Reconnect_Ack", 23938: "Cmd_Chat_Auth_Session_Req",
    23939: "Cmd_Chat_Auth_Session_Ack", 23940: "Cmd_Notify_HomeVisitShareUpdate"}


class RedbagCodecError(ValueError):
    def __init__(self):
        super().__init__("REDBAG_PROTOCOL_INVALID")


@lru_cache(maxsize=1)
def _schema():
    root = files(__package__)
    metadata = root.joinpath("redbag-source.json").read_bytes()
    if len(metadata) > 128 * 1024:
        raise ValueError
    source = json.loads(metadata)
    raw = root.joinpath("redbag.descriptor.pb").read_bytes()
    expected = {rpc: "SNKProto." + name for rpc, name in _BINDINGS.items()}
    roots = set(expected.values()) | {"SNKProto.WsClientPkg", "SNKProto.HomeVisitShareCardParam"}
    if (source.get("schema_version") != 1 or source.get("client_version") != REDBAG_CLIENT_VERSION
            or source.get("hot_version") != 71 or source.get("source_game_sha256") != _GAME_SHA
            or source.get("catalog_sha256") != _CATALOG_SHA
            or source.get("source_report_sha256") != "8ee895929acee4096b1489c07d95acf83ed90f610ec32f43595564c8aefddca9"
            or source.get("cache_manifest_sha256") != "5e63c29d606437c6e6eafa3514adac1ea2b3e3e6c301c11706c2b66cd51bf2bc"
            or any(source.get(name) is not False for name in (
                "network_requests_sent", "private_archives_read", "target_execution",
                "plaintext_assemblies_or_tables_saved"))
            or source.get("descriptor_sha256") != _DESCRIPTOR_SHA
            or source.get("descriptor_size") != len(raw) or not 0 < len(raw) <= PAYLOAD_LIMIT
            or hashlib.sha256(raw).hexdigest() != _DESCRIPTOR_SHA
            or {r["rpc_id"]: r["message_type"] for r in source["rpc_bindings"]} != expected
            or len(source["rpc_bindings"]) != len(expected)
            or set(source["selected_roots"]) != roots or len(source["selected_roots"]) != len(roots)):
        raise ValueError
    descriptors = descriptor_pb2.FileDescriptorSet.FromString(raw)
    if not 0 < len(descriptors.file) <= 64:
        raise ValueError
    pending = {f.name: f for f in descriptors.file}
    if len(pending) != len(descriptors.file):
        raise ValueError
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
    known = {"google/protobuf/descriptor.proto"}
    while pending:
        ready = [n for n, f in pending.items() if set(f.dependency) <= known]
        if not ready:
            raise ValueError
        for name in ready:
            pool.AddSerializedFile(pending.pop(name).SerializeToString(deterministic=True))
            known.add(name)
    classes = {name: message_factory.GetMessageClass(pool.FindMessageTypeByName(name)) for name in roots}
    return {**{rpc: classes[name] for rpc, name in expected.items()},
            **{name.removeprefix("SNKProto."): value for name, value in classes.items()}}


class RedbagProtoCodec:
    def __init__(self):
        failed = False
        try:
            self._classes = _schema()
        except Exception:
            failed = True
        if failed:
            raise RedbagCodecError()

    @property
    def known_commands(self):
        return frozenset(_BINDINGS)

    def message_class(self, message):
        if type(message) not in (str, int) or message not in self._classes:
            raise RedbagCodecError()
        return self._classes[message]

    def encode(self, message, **fields):
        result = None
        try:
            result = self.message_class(message)(**fields).SerializeToString(deterministic=True)
        except Exception:
            pass
        fields = None
        if result is None or len(result) > PAYLOAD_LIMIT:
            raise RedbagCodecError()
        return result

    def decode(self, message, payload):
        result = None
        try:
            if type(payload) is bytes and len(payload) <= PAYLOAD_LIMIT:
                result = self.message_class(message).FromString(payload)
        except Exception:
            pass
        payload = None
        if result is None:
            raise RedbagCodecError()
        return result

    def encode_ws(self, command, payload):
        if type(command) is not int or not 0 < command < 1 << 32 or type(payload) is not bytes:
            raise RedbagCodecError()
        return self.encode("WsClientPkg", api=str(command), seq_id="0", data=payload)

    def decode_ws(self, payload):
        package = self.decode("WsClientPkg", payload)
        api = package.api
        if (not api.isascii() or not api.isdecimal() or len(api) > 10
                or not 0 < int(api) < 1 << 32 or str(int(api)) != api):
            raise RedbagCodecError()
        return int(api), package.data
