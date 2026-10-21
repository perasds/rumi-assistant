"""Encrypted, fixed-lifetime login credentials indexed by opaque bearer tokens."""
from contextlib import closing
from dataclasses import dataclass
from functools import wraps
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAX_PAYLOAD_BYTES = 16 * 1024
FORMAT_VERSION = 1
_ERROR_CODES = frozenset({
    'REMEMBER_LOGIN_STORE_FAILED', 'REMEMBER_LOGIN_PAYLOAD_INVALID',
    'REMEMBER_LOGIN_PATH_INVALID', 'REMEMBER_LOGIN_KEY_INVALID',
    'REMEMBER_LOGIN_KEY_MISSING',
})


class RememberLoginStoreError(Exception):
    """A fixed public error code, without private filesystem or credential details."""


def _safe_operation(method):
    @wraps(method)
    def call(*args, **kwargs):
        code = 'REMEMBER_LOGIN_STORE_FAILED'
        try:
            return method(*args, **kwargs)
        except RememberLoginStoreError as error:
            if str(error) in _ERROR_CODES:
                code = str(error)
        except Exception:
            pass
        # Raise after leaving the handler: even __context__ must not retain secrets.
        raise RememberLoginStoreError(code)
    return call


@dataclass(frozen=True, repr=False)
class RememberedLogin:
    payload: dict
    expires_at: int


def _check_path(path: Path) -> None:
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or (candidate != path and not stat.S_ISDIR(info.st_mode))
                or (candidate == path and not stat.S_ISREG(info.st_mode))):
            raise RememberLoginStoreError('REMEMBER_LOGIN_PATH_INVALID')


def _private_path(value: Path) -> Path:
    path = Path(value)
    if '..' in path.parts or any(':' in part for part in path.parts if part != path.anchor):
        raise RememberLoginStoreError('REMEMBER_LOGIN_PATH_INVALID')
    path = path.absolute()
    _check_path(path)
    return path


def _create_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _payload_bytes(payload: dict) -> bytes:
    encoded = None
    if isinstance(payload, dict):
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                 separators=(',', ':')).encode('utf-8')
        except Exception:
            pass
    if encoded is None or len(encoded) > MAX_PAYLOAD_BYTES:
        raise RememberLoginStoreError('REMEMBER_LOGIN_PAYLOAD_INVALID')
    return encoded


def _token_digest(token: str) -> str | None:
    if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is None:
        return None
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _aad(digest: str, expires_at: int) -> bytes:
    return f'rumi-remember-login:{FORMAT_VERSION}:{digest}:{expires_at}'.encode('ascii')


class RememberLoginStore:
    @_safe_operation
    def __init__(self, db_path: Path, key_path: Path, *, ttl_seconds: int = 7 * 86_400,
                 clock=time.time, key: bytes | None = None):
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise RememberLoginStoreError('REMEMBER_LOGIN_STORE_FAILED')
        if key is not None and (not isinstance(key, bytes) or len(key) != 32):
            raise RememberLoginStoreError('REMEMBER_LOGIN_KEY_INVALID')
        self._db_path = _private_path(db_path)
        self._key_path = _private_path(key_path)
        if os.path.normcase(str(self._db_path)) == os.path.normcase(str(self._key_path)):
            raise RememberLoginStoreError('REMEMBER_LOGIN_PATH_INVALID')
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        for path in (self._db_path, self._key_path):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _check_path(path)
        _create_private_file(self._db_path, b'')
        self._db_path.chmod(0o600)
        with closing(self._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS remembered_logins (
                token_digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL,
                nonce BLOB NOT NULL, ciphertext BLOB NOT NULL,
                format_version INTEGER NOT NULL)''')
            has_records = db.execute('SELECT 1 FROM remembered_logins LIMIT 1').fetchone() is not None
        if not self._key_path.exists():
            if has_records:
                raise RememberLoginStoreError('REMEMBER_LOGIN_KEY_MISSING')
            _create_private_file(self._key_path, key if key is not None else secrets.token_bytes(32))
        _check_path(self._key_path)
        stored_key = self._key_path.read_bytes()
        if len(stored_key) != 32 or (key is not None and not hmac.compare_digest(stored_key, key)):
            raise RememberLoginStoreError('REMEMBER_LOGIN_KEY_INVALID')
        self._key_path.chmod(0o600)
        self._cipher = AESGCM(stored_key)

    def _connect(self):
        _check_path(self._key_path)
        _check_path(self._db_path)
        for suffix in ('-journal', '-wal', '-shm'):
            _check_path(Path(str(self._db_path) + suffix))
        db = sqlite3.connect(self._db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _read_login(self, db, digest: str) -> RememberedLogin | None:
        row = db.execute('SELECT * FROM remembered_logins WHERE token_digest=?', (digest,)).fetchone()
        if row is None:
            return None
        try:
            expires_at = row['expires_at']
            if (type(expires_at) is not int or expires_at <= int(self._clock())
                    or row['format_version'] != FORMAT_VERSION
                    or not isinstance(row['nonce'], bytes) or len(row['nonce']) != 12
                    or not isinstance(row['ciphertext'], bytes)
                    or len(row['ciphertext']) > MAX_PAYLOAD_BYTES + 16):
                raise ValueError
            plaintext = self._cipher.decrypt(row['nonce'], row['ciphertext'], _aad(digest, expires_at))
            payload = json.loads(plaintext.decode('utf-8'))
            _payload_bytes(payload)
            return RememberedLogin(payload=payload, expires_at=expires_at)
        except Exception:
            db.execute('DELETE FROM remembered_logins WHERE token_digest=?', (digest,))
        return None

    @_safe_operation
    def issue(self, payload: dict) -> str:
        plaintext = _payload_bytes(payload)
        token = secrets.token_urlsafe(32)
        digest = _token_digest(token)
        now = int(self._clock())
        expires_at = now + self._ttl_seconds
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext, _aad(digest, expires_at))
        with closing(self._connect()) as db, db:
            db.execute('DELETE FROM remembered_logins WHERE expires_at<=?', (now,))
            db.execute('INSERT INTO remembered_logins VALUES (?, ?, ?, ?, ?)',
                       (digest, expires_at, nonce, ciphertext, FORMAT_VERSION))
        return token

    @_safe_operation
    def load(self, token: str) -> RememberedLogin | None:
        digest = _token_digest(token)
        if digest is None:
            return None
        with closing(self._connect()) as db, db:
            return self._read_login(db, digest)

    @_safe_operation
    def update(self, token: str, payload: dict) -> bool:
        plaintext = _payload_bytes(payload)
        digest = _token_digest(token)
        if digest is None:
            return False
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            remembered = self._read_login(db, digest)
            if remembered is None:
                return False
            nonce = secrets.token_bytes(12)
            ciphertext = self._cipher.encrypt(nonce, plaintext, _aad(digest, remembered.expires_at))
            db.execute('UPDATE remembered_logins SET nonce=?, ciphertext=? WHERE token_digest=?',
                       (nonce, ciphertext, digest))
        return True

    @_safe_operation
    def revoke(self, token: str) -> None:
        digest = _token_digest(token)
        if digest is None:
            return
        with closing(self._connect()) as db, db:
            db.execute('DELETE FROM remembered_logins WHERE token_digest=?', (digest,))

    def close(self) -> None:
        """Each operation closes its own connection, including worker-thread calls."""
