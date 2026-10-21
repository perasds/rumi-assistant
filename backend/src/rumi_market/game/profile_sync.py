"""Bounded, credential-free snapshots of the current normal login's replies.

Only the checked-in manifest's verified command bindings may be decoded.
Unknown payloads and protobuf byte fields never enter the archive. This is a
login snapshot, not a claim to have decoded every personal state or its deltas.
"""

from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import logging
from pathlib import Path
import re

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from ..profile_archive import AccountProfileArchive, export_profile_message
from .envelope import ResponseEnvelope
from .proto import BUSINESS_MESSAGE_LIMIT


MAX_MESSAGES = 512
MAX_COMBINE_DEPTH = 4
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_ITEM_CHANGES = 4096
_LOGGER = logging.getLogger(__name__)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_schema():
    """Validate the public manifest and exact descriptor before exposing types."""
    root = files(__package__)
    manifest_bytes = root.joinpath("profile-source.json").read_bytes()
    if len(manifest_bytes) > 128 * 1024:
        raise ValueError
    manifest = json.loads(manifest_bytes)
    raw = root.joinpath("profile.descriptor.pb").read_bytes()
    digest, size = manifest.get("descriptor_sha256"), manifest.get("descriptor_size")
    roots, bindings = manifest.get("selected_roots"), manifest.get("rpc_bindings")
    source_digest = manifest.get("source_game_sha256")
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or type(digest) is not str or _DIGEST.fullmatch(digest) is None
            or type(source_digest) is not str or _DIGEST.fullmatch(source_digest) is None
            or type(size) is not int or not 0 < size <= BUSINESS_MESSAGE_LIMIT
            or len(raw) != size or hashlib.sha256(raw).hexdigest() != digest
            or type(roots) is not list or not 0 < len(roots) <= 64
            or any(type(name) is not str or not name.startswith("SNKProto.Cmd_Notify_") for name in roots)
            or len(set(roots)) != len(roots)
            or type(bindings) is not list or len(bindings) != len(roots)):
        raise ValueError
    commands = {}
    for binding in bindings:
        if type(binding) is not dict:
            raise ValueError
        command, name = binding.get("rpc_id"), binding.get("message_type")
        if (type(command) is not int or not 0 < command < 1 << 32
                or type(name) is not str or name not in roots or command in commands):
            raise ValueError
        commands[command] = name
    if set(commands.values()) != set(roots):
        raise ValueError
    descriptors = descriptor_pb2.FileDescriptorSet.FromString(raw)
    if not 0 < len(descriptors.file) <= 64:
        raise ValueError
    pending = {file.name: file for file in descriptors.file}
    if len(pending) != len(descriptors.file):
        raise ValueError
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
    known = {"google/protobuf/descriptor.proto"}
    while pending:
        ready = [name for name, file in pending.items() if set(file.dependency) <= known]
        if not ready:
            raise ValueError
        for name in ready:
            pool.AddSerializedFile(pending.pop(name).SerializeToString(deterministic=True))
            known.add(name)
    classes = {command: message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
               for command, name in commands.items()}
    item = message_factory.GetMessageClass(pool.FindMessageTypeByName("SNKProto.CmdItemReal"))
    provenance = {"descriptor_sha256": digest, "descriptor_size": size,
                  "source_game_sha256": manifest.get("source_game_sha256"),
                  "command_bindings": {str(command): name for command, name in commands.items()}}
    return classes, item, provenance


class LoginProfileCollector:
    """Collect one login in RAM, then save once with an authenticated role key.

    The client owns fragment reassembly and positive notification deduplication.
    Observation never changes the game request/notification delivery contract.
    """

    def __init__(self, archive: AccountProfileArchive):
        self._archive = archive
        self._messages = []
        self._handshake = {}
        self._candidates = {"gold_balance_verified": False,
                            "gold_normalization": "item aliases are not resolved"}
        self._captured_bytes = 0
        self._active = True
        self._started_at = _now()
        self.archive_status = "unavailable"
        self.archive_path: Path | None = None
        self._coverage = {
            "all_personal_information_decoded": False,
            "capture_scope": "normal_login_until_finalize",
            "schema_status": "verified", "decoded": 0, "unknown_command": 0,
            "upstream_error": 0, "decode_failed": 0, "skipped_depth": 0,
            "skipped_message_limit": 0, "skipped_byte_limit": 0,
            "skipped_item_change_limit": 0,
        }
        try:
            self._classes, self._item_class, self._provenance = _load_schema()
        except Exception:
            self._classes, self._item_class, self._provenance = {}, None, {}
            self._coverage["schema_status"] = "unavailable"

    def observe(self, response: ResponseEnvelope) -> None:
        if not self._active or not isinstance(response, ResponseEnvelope):
            return
        # The root receive loop only supplies complete envelopes. Refuse an
        # incomplete envelope if an alternate caller violates that contract.
        if response.remaining != 0:
            return
        try:
            self._observe(response.command, response.data, code=response.code)
        except Exception:
            self._coverage["decode_failed"] += 1

    def _observe(self, command, data, *, code=0, depth=0, parent=None):
        if len(self._messages) >= MAX_MESSAGES:
            self._coverage["skipped_message_limit"] += 1
            return
        if depth > MAX_COMBINE_DEPTH:
            self._coverage["skipped_depth"] += 1
            return
        if len(data) > BUSINESS_MESSAGE_LIMIT or self._captured_bytes + len(data) > MAX_CAPTURE_BYTES:
            self._coverage["skipped_byte_limit"] += 1
            return
        self._captured_bytes += len(data)
        record = {"command": command, "bytes": len(data), "received_at": _now()}
        if parent is not None:
            record["parent_command"] = parent
        self._messages.append(record)
        if code:
            record.update(status="upstream_error", response_code=code)
            self._coverage["upstream_error"] += 1
            return
        cls = self._classes.get(command)
        if cls is None:
            status = ("schema_unavailable" if self._coverage["schema_status"] == "unavailable"
                      else "unknown_command")
            record["status"] = status
            self._coverage["unknown_command"] += 1
            return
        try:
            message = cls.FromString(data)
            # Bound repeated byte-wrapper projection before exporting it. The
            # wire limit alone would still allow millions of empty units to
            # inflate into much larger JSON metadata and Python dictionaries.
            if command == 2016:
                available = max(0, MAX_MESSAGES - len(self._messages))
                if len(message.unit_lst) > available:
                    self._coverage["skipped_message_limit"] += len(message.unit_lst) - available
                    del message.unit_lst[available:]
            elif command == 2942 and len(message.change_list) > MAX_ITEM_CHANGES:
                self._coverage["skipped_item_change_limit"] += len(message.change_list) - MAX_ITEM_CHANGES
                del message.change_list[MAX_ITEM_CHANGES:]
            fields = export_profile_message(message)
        except Exception:
            record["status"] = "decode_failed"
            self._coverage["decode_failed"] += 1
            return
        record.update(status="decoded", message_type=message.DESCRIPTOR.full_name, fields=fields)
        self._coverage["decoded"] += 1
        if command == 2016:
            for unit in message.unit_lst:
                if len(self._messages) >= MAX_MESSAGES:
                    self._coverage["skipped_message_limit"] += 1
                    break
                self._observe(unit.id, unit.msg, depth=depth + 1, parent=command)
        elif command == 2942:
            record["decoded_item_changes"] = self._item_changes(message)
        elif command == 2941:
            # The client uses GetItemUseId normalization. An exact raw ID sum
            # is useful evidence, but never asserted to be its GoldNum value.
            total = sum(item.count for item in message.item_list
                        if item.HasField("item_id") and item.item_id == 10001
                        and item.HasField("count"))
            self._candidates["raw_item_10001_sum"] = str(total)
            self._candidates["raw_item_source_command"] = 2941
        elif command == 5432 and "gid" in fields.get("data", {}):
            # RoleGuildShow is proto3: scalar gid has no HasField presence.
            self._candidates["self_guild_id"] = fields["data"]["gid"]
            self._candidates["self_guild_source_command"] = command

    def _item_changes(self, message):
        result = []
        for index, change in enumerate(message.change_list):
            if index >= MAX_ITEM_CHANGES:
                self._coverage["skipped_item_change_limit"] += len(message.change_list) - index
                break
            row = {"bytes": len(change.data)}
            if change.HasField("change_type"):
                row["change_type"] = change.change_type
            try:
                item = self._item_class.FromString(change.data)
                row.update(status="decoded", fields=export_profile_message(item))
            except Exception:
                row["status"] = "decode_failed"
                self._coverage["decode_failed"] += 1
            result.append(row)
        return result

    def record_handshake(self, stage: str, message, allowed_fields: tuple[str, ...]) -> None:
        if not self._active:
            return
        # Stages are internal fixed identifiers, never account or SDK text.
        if stage not in ("login_ack", "enter_ack", "login_end"):
            return
        try:
            if type(allowed_fields) is not tuple or any(type(name) is not str for name in allowed_fields):
                return
            exported = export_profile_message(message)
            self._handshake[stage] = {name: exported[name] for name in allowed_fields if name in exported}
        except Exception:
            self._coverage["decode_failed"] += 1

    def finalize(self, result) -> None:
        if not self._active:
            return
        self._active = False
        try:
            role_id, zone_id = result.role_id, result.zone_id
            if (type(role_id) is not int or not 0 < role_id < 1 << 64
                    or (zone_id is not None and (type(zone_id) is not int or not 0 <= zone_id < 1 << 31))):
                return
            document = {
                "schema_version": 1, "source": "game_server", "started_at": self._started_at,
                "finalized_at": _now(), "identity": {"role_id": str(role_id), "zone_id": zone_id},
                "provenance": self._provenance, "handshake": self._handshake,
                "messages": self._messages, "derived_candidates": self._candidates,
                "coverage": {**self._coverage, "captured_bytes": self._captured_bytes},
            }
            self.archive_path = self._archive.save(role_id, zone_id, document)
            self.archive_status = "saved"
        except Exception:
            self.archive_path = None
            self.archive_status = "failed"
            _LOGGER.warning("Login profile archive could not be saved")

    def close(self) -> None:
        self._active = False
        self._messages.clear()
        self._handshake.clear()
        self._candidates.clear()
        self._classes.clear()
        self._item_class = None
        self._provenance.clear()
        self._captured_bytes = 0
        self._archive = None
