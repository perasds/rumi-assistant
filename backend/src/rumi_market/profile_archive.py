"""Private local login snapshots, excluding authentication material and raw bytes.

This module never performs network calls or exposes snapshots through HTTP.
Protobuf presence is retained; absent fields are not invented from defaults.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message


DEFAULT_PROFILE_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "account-profiles"
_ARCHIVE_ID = re.compile(r"[0-9a-f]{32}\Z")
_SENSITIVE_PARTS = (
    "password", "passwd", "pwd", "token", "secret", "credential", "captcha",
    "cookie", "authorization", "sessioninfo", "cdkey", "accesskey",
    "refreshkey", "privatekey", "appkey", "signature",
)
_INT64_TYPES = frozenset({
    FieldDescriptor.TYPE_INT64, FieldDescriptor.TYPE_UINT64, FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_FIXED64, FieldDescriptor.TYPE_SFIXED64,
})
_MAX_PROFILE_EXPORT_NODES = 100_000
_MAX_PROFILE_EXPORT_DEPTH = 64


class ProfileArchiveError(ValueError):
    """Safe error codes only; callers must not log source messages or documents."""


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized == "sign" or any(part in normalized for part in _SENSITIVE_PARTS)


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"omitted": True, "bytes": len(value)}
    if type(value) is list:
        return [_json_value(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ProfileArchiveError("PROFILE_ARCHIVE_DOCUMENT_INVALID")
        return {key: _json_value(item) for key, item in value.items() if not _sensitive_key(key)}
    raise ProfileArchiveError("PROFILE_ARCHIVE_DOCUMENT_INVALID")


class _ExportBudget:
    def __init__(self):
        self.remaining = _MAX_PROFILE_EXPORT_NODES

    def visit(self, depth: int) -> None:
        if depth > _MAX_PROFILE_EXPORT_DEPTH or self.remaining <= 0:
            raise ProfileArchiveError("PROFILE_ARCHIVE_EXPORT_LIMIT")
        self.remaining -= 1


def _field_value(field, value, budget: _ExportBudget, depth: int):
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        return _export_message(value, budget, depth)
    budget.visit(depth)
    if field.type in _INT64_TYPES:
        return str(value)
    return _json_value(value)


def _export_message(message: Message, budget: _ExportBudget, depth: int) -> dict:
    budget.visit(depth)
    result = {}
    for field, value in message.ListFields():
        if _sensitive_key(field.name):
            budget.visit(depth + 1)
            continue
        if field.is_repeated and field.type == FieldDescriptor.TYPE_MESSAGE and field.message_type.GetOptions().map_entry:
            budget.visit(depth + 1)
            map_value = field.message_type.fields_by_name["value"]
            mapped = {}
            for key, item in value.items():
                budget.visit(depth + 2)
                key = str(key)
                if not _sensitive_key(key):
                    mapped[key] = _field_value(map_value, item, budget, depth + 2)
            result[field.name] = mapped
        elif field.is_repeated:
            budget.visit(depth + 1)
            result[field.name] = [_field_value(field, item, budget, depth + 2) for item in value]
        else:
            result[field.name] = _field_value(field, value, budget, depth + 1)
    return result


def export_profile_message(message: Message) -> dict:
    """Export only present, non-secret protobuf fields to JSON-safe values.

    Every protobuf 64-bit integer is decimal text, including repeated/map values.
    Binary values retain their size only; no wire payload or base64 is retained.
    One shared budget bounds each export to 100,000 traversed nodes and depth 64,
    including containers, map keys and filtered fields. Oversized messages fail
    with a safe error code rather than returning a misleading partial profile.
    """
    if not isinstance(message, Message):
        raise ProfileArchiveError("PROFILE_ARCHIVE_MESSAGE_INVALID")
    return _export_message(message, _ExportBudget(), 0)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _restrict_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)
        return
    # Build a new ACL rather than retaining pre-existing explicit grants.
    # The constant script receives the path through its own environment variable;
    # directory text is never interpolated into PowerShell code.
    script = r"""
$ErrorActionPreference = 'Stop'
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$rights = [System.Security.AccessControl.FileSystemRights]::FullControl
$inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$propagation = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
foreach ($sid in @($current, $system)) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $rights, $inheritance, $propagation, $allow)
    $acl.AddAccessRule($rule)
}
[System.IO.Directory]::SetAccessControl($env:RUMI_PROFILE_ARCHIVE_ACL_PATH, $acl)
"""
    environment = dict(os.environ)
    environment["RUMI_PROFILE_ARCHIVE_ACL_PATH"] = str(path)
    subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        env=environment, capture_output=True, check=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


class AccountProfileArchive:
    """Atomic snapshots under a private, opaque per-role directory.

    Use the returned path's stem as archive_id to replace the same login's
    snapshot as additional profile messages arrive. A new login should omit it.
    """

    def __init__(self, root: Path = DEFAULT_PROFILE_ROOT):
        try:
            if not isinstance(root, (str, Path)) or "\x00" in str(root):
                raise ValueError
            self._root = Path(root).absolute()
        except Exception:
            raise ProfileArchiveError("PROFILE_ARCHIVE_PATH_INVALID") from None
        self._secured: set[Path] = set()

    def _private_directory(self, path: Path) -> None:
        if _is_link(path):
            raise ProfileArchiveError("PROFILE_ARCHIVE_PATH_INVALID")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _is_link(path) or not path.is_dir():
            raise ProfileArchiveError("PROFILE_ARCHIVE_PATH_INVALID")
        if path not in self._secured:
            _restrict_directory(path)
            self._secured.add(path)

    def save(self, role_id: int, zone_id: int | None, document: dict, *, archive_id: str | None = None) -> Path:
        if (type(role_id) is not int or not 1 <= role_id < 2**64
                or (zone_id is not None and (type(zone_id) is not int or not 0 <= zone_id < 2**31))):
            raise ProfileArchiveError("PROFILE_ARCHIVE_IDENTITY_INVALID")
        if archive_id is not None and (type(archive_id) is not str or _ARCHIVE_ID.fullmatch(archive_id) is None):
            raise ProfileArchiveError("PROFILE_ARCHIVE_ID_INVALID")
        if type(document) is not dict:
            raise ProfileArchiveError("PROFILE_ARCHIVE_DOCUMENT_INVALID")
        try:
            safe_document = _json_value(document)
            encoded = json.dumps(safe_document, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
        except (RecursionError, TypeError, ValueError):
            raise ProfileArchiveError("PROFILE_ARCHIVE_DOCUMENT_INVALID") from None
        archive_id = archive_id or uuid.uuid4().hex
        identity = f"{zone_id if zone_id is not None else 'none'}:{role_id}".encode("ascii")
        account_path = self._root / hashlib.sha256(identity).hexdigest()
        destination = account_path / f"{archive_id}.json"
        temporary = None
        try:
            self._private_directory(self._root)
            self._private_directory(account_path)
            if account_path.resolve().parent != self._root.resolve() or _is_link(destination):
                raise ProfileArchiveError("PROFILE_ARCHIVE_PATH_INVALID")
            descriptor, name = tempfile.mkstemp(prefix=archive_id + ".", suffix=".tmp", dir=account_path)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as output:
                if os.name != "nt":
                    os.fchmod(output.fileno(), 0o600)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            temporary = None
        except ProfileArchiveError:
            raise
        except Exception:
            raise ProfileArchiveError("PROFILE_ARCHIVE_WRITE_FAILED") from None
        finally:
            # Only remove the uniquely created local temporary file, never a
            # computed directory or another snapshot/file from this account.
            if temporary is not None and temporary.parent == account_path:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return destination
