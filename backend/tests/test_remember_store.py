"""Remembered logins use synthetic credentials and isolated temporary files."""
import asyncio
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import re
import sqlite3
import subprocess

import pytest


SECRET = 'synthetic-sdk-token-for-tests-only'
PAYLOAD = {'sdk_token': SECRET, 'device_id': 'synthetic-device', 'server_id': '1001'}


def store_api():
    if importlib.util.find_spec('rumi_market.remember_store') is None:
        pytest.fail('RememberLoginStore is not implemented')
    return importlib.import_module('rumi_market.remember_store')


def make_store(tmp_path, **kwargs):
    return store_api().RememberLoginStore(
        tmp_path / 'remember.sqlite3', tmp_path / 'config' / 'remember.key', **kwargs)


def rows(tmp_path):
    with sqlite3.connect(tmp_path / 'remember.sqlite3') as db:
        db.row_factory = sqlite3.Row
        return db.execute('SELECT * FROM remembered_logins').fetchall()


def test_issue_load_survives_reopening_without_plaintext_on_disk(tmp_path):
    store = make_store(tmp_path, clock=lambda: 1_000, key=b'k' * 32)
    token = store.issue(PAYLOAD)
    assert re.fullmatch(r'[A-Za-z0-9_-]{43}', token)
    remembered = store.load(token)
    assert remembered.payload == PAYLOAD
    assert remembered.expires_at == 1_000 + 7 * 86_400
    assert SECRET not in repr(remembered)
    assert SECRET not in repr(store)
    saved = rows(tmp_path)
    assert len(saved) == 1
    assert saved[0]['token_digest'] == hashlib.sha256(token.encode('ascii')).hexdigest()
    assert len(saved[0]['nonce']) == 12
    raw_db = (tmp_path / 'remember.sqlite3').read_bytes()
    assert SECRET.encode() not in raw_db
    assert token.encode() not in raw_db
    assert PAYLOAD['device_id'].encode() not in raw_db
    assert (tmp_path / 'config' / 'remember.key').read_bytes() == b'k' * 32
    store.close()
    reopened = make_store(tmp_path, clock=lambda: 1_001)
    assert reopened.load(token).payload == PAYLOAD


def test_issue_uses_fresh_tokens_and_nonces(tmp_path):
    store = make_store(tmp_path)
    first = store.issue(PAYLOAD)
    second = store.issue(PAYLOAD)
    assert first != second
    saved = rows(tmp_path)
    assert saved[0]['nonce'] != saved[1]['nonce']
    assert saved[0]['ciphertext'] != saved[1]['ciphertext']


def test_update_keeps_initial_deadline(tmp_path):
    now = [1_000]
    store = make_store(tmp_path, ttl_seconds=100, clock=lambda: now[0])
    token = store.issue(PAYLOAD)
    original_nonce = rows(tmp_path)[0]['nonce']
    now[0] = 1_050
    fresh = {**PAYLOAD, 'sdk_token': 'synthetic-refreshed-token'}
    assert store.update(token, fresh) is True
    assert store.load(token).payload == fresh
    assert store.load(token).expires_at == 1_100
    assert rows(tmp_path)[0]['nonce'] != original_nonce
    now[0] = 1_100
    assert store.load(token) is None
    assert rows(tmp_path) == []


def test_update_expired_token_revokes_it(tmp_path):
    now = [1_000]
    store = make_store(tmp_path, ttl_seconds=100, clock=lambda: now[0])
    token = store.issue(PAYLOAD)
    now[0] = 1_101
    assert store.update(token, PAYLOAD) is False
    assert rows(tmp_path) == []


@pytest.mark.parametrize('token', [None, '', 'invalid', '../unsafe', 'x' * 44, b'bytes', 42])
def test_invalid_token_is_safe_for_load_update_and_revoke(tmp_path, token):
    store = make_store(tmp_path)
    real = store.issue(PAYLOAD)
    assert store.load(token) is None
    assert store.update(token, PAYLOAD) is False
    store.revoke(token)
    assert store.load(real).payload == PAYLOAD


def test_revoke_and_unknown_token_are_safe(tmp_path):
    store = make_store(tmp_path)
    token = store.issue(PAYLOAD)
    store.revoke(token)
    store.revoke(token)
    assert store.load(token) is None
    assert store.load('x' * 43) is None
    assert store.update('x' * 43, PAYLOAD) is False
    store.revoke('x' * 43)


@pytest.mark.parametrize('payload', [
    [], None, 'private', {'token': object()}, {'token': float('nan')},
    {'token': '\ud800'}, {'token': 'x' * 16_384},
])
def test_invalid_payload_has_fixed_safe_error_and_no_exception_context(tmp_path, payload):
    api = store_api()
    store = make_store(tmp_path)
    for action in [lambda: store.issue(payload), lambda: store.update('x' * 43, payload)]:
        with pytest.raises(api.RememberLoginStoreError, match='^REMEMBER_LOGIN_PAYLOAD_INVALID$') as error:
            action()
        assert error.value.__context__ is None
        assert error.value.__cause__ is None
        assert SECRET not in str(error.value)
    assert rows(tmp_path) == []


def test_serialization_errors_cannot_expose_credential_context(tmp_path):
    class PrivatePayload(dict):
        def items(self):
            raise ValueError(SECRET)

    api = store_api()
    store = make_store(tmp_path)
    with pytest.raises(api.RememberLoginStoreError) as error:
        store.issue(PrivatePayload(PAYLOAD))
    assert str(error.value) == 'REMEMBER_LOGIN_PAYLOAD_INVALID'
    assert error.value.__context__ is None
    assert SECRET not in repr(error.value)


@pytest.mark.parametrize('tamper', ['ciphertext', 'nonce', 'expires_at', 'format_version', 'swap'])
def test_authenticated_records_reject_tampering_and_swapping(tmp_path, tamper):
    store = make_store(tmp_path, clock=lambda: 1_000)
    first = store.issue(PAYLOAD)
    second = store.issue({'sdk_token': 'synthetic-other-token'})
    saved = rows(tmp_path)
    digest = hashlib.sha256(first.encode()).hexdigest()
    other = next(row for row in saved if row['token_digest'] != digest)
    with sqlite3.connect(tmp_path / 'remember.sqlite3') as db:
        if tamper == 'swap':
            db.execute('UPDATE remembered_logins SET nonce=?, ciphertext=? WHERE token_digest=?',
                       (other['nonce'], other['ciphertext'], digest))
        elif tamper in {'ciphertext', 'nonce'}:
            db.execute(f'UPDATE remembered_logins SET {tamper}=? WHERE token_digest=?', (b'bad', digest))
        elif tamper == 'expires_at':
            db.execute('UPDATE remembered_logins SET expires_at=expires_at+1 WHERE token_digest=?', (digest,))
        else:
            db.execute('UPDATE remembered_logins SET format_version=2 WHERE token_digest=?', (digest,))
    assert store.load(first) is None
    assert store.load(second).payload == {'sdk_token': 'synthetic-other-token'}
    assert len(rows(tmp_path)) == 1


def test_missing_key_with_saved_records_does_not_generate_replacement(tmp_path):
    api = store_api()
    store = make_store(tmp_path)
    store.issue(PAYLOAD)
    store.close()
    key_path = tmp_path / 'config' / 'remember.key'
    key_path.unlink()
    with pytest.raises(api.RememberLoginStoreError, match='^REMEMBER_LOGIN_KEY_MISSING$') as error:
        make_store(tmp_path)
    assert not key_path.exists()
    assert error.value.__context__ is None
    assert len(rows(tmp_path)) == 1


@pytest.mark.parametrize('key', [b'short', b'x' * 33, 'synthetic-not-bytes'])
def test_invalid_existing_key_is_not_overwritten(tmp_path, key):
    api = store_api()
    key_path = tmp_path / 'config' / 'remember.key'
    key_path.parent.mkdir()
    key_path.write_bytes(key.encode() if isinstance(key, str) else key)
    original = key_path.read_bytes()
    with pytest.raises(api.RememberLoginStoreError, match='^REMEMBER_LOGIN_KEY_INVALID$'):
        make_store(tmp_path)
    assert key_path.read_bytes() == original


@pytest.mark.parametrize('target', ['db', 'key', 'parent'])
def test_symlink_paths_are_rejected_without_reading_target(tmp_path, target):
    api = store_api()
    outside = tmp_path / 'outside'
    outside.mkdir()
    outside_file = outside / 'private'
    outside_file.write_bytes(b'synthetic-protected-data')
    db_path, key_path = tmp_path / 'remember.sqlite3', tmp_path / 'config' / 'remember.key'
    try:
        if target == 'parent':
            key_path.parent.symlink_to(outside, target_is_directory=True)
        else:
            link = db_path if target == 'db' else key_path
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(outside_file)
    except OSError as error:
        pytest.skip(f'OS does not permit symlink creation: {type(error).__name__}')
    with pytest.raises(api.RememberLoginStoreError, match='^REMEMBER_LOGIN_PATH_INVALID$'):
        api.RememberLoginStore(db_path, key_path)
    assert outside_file.read_bytes() == b'synthetic-protected-data'


def test_path_traversal_is_rejected_before_creation(tmp_path):
    api = store_api()
    with pytest.raises(api.RememberLoginStoreError, match='^REMEMBER_LOGIN_PATH_INVALID$'):
        api.RememberLoginStore(tmp_path / '..' / 'escape.sqlite3', tmp_path / 'key')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction protection')
def test_windows_junction_parent_is_rejected(tmp_path):
    api = store_api()
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'config'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                            capture_output=True, check=False)
    if result.returncode:
        pytest.skip('OS does not permit junction creation')
    try:
        with pytest.raises(api.RememberLoginStoreError, match='^REMEMBER_LOGIN_PATH_INVALID$'):
            api.RememberLoginStore(tmp_path / 'remember.sqlite3', link / 'remember.key')
        assert list(outside.iterdir()) == []
    finally:
        os.rmdir(link)


def test_corrupt_database_errors_have_fixed_message_and_no_context(tmp_path):
    api = store_api()
    db_path = tmp_path / 'remember.sqlite3'
    db_path.write_bytes(SECRET.encode())
    with pytest.raises(api.RememberLoginStoreError, match='^REMEMBER_LOGIN_STORE_FAILED$') as error:
        make_store(tmp_path)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert SECRET not in repr(error.value)


def test_created_files_are_private_where_posix_modes_apply(tmp_path):
    make_store(tmp_path)
    if os.name != 'nt':
        assert (tmp_path / 'remember.sqlite3').stat().st_mode & 0o777 == 0o600
        assert (tmp_path / 'config' / 'remember.key').stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_operations_work_from_different_worker_threads(tmp_path):
    store = make_store(tmp_path)
    tokens = await asyncio.gather(*(asyncio.to_thread(store.issue, {**PAYLOAD, 'number': n})
                                   for n in range(8)))
    loaded = await asyncio.gather(*(asyncio.to_thread(store.load, token) for token in tokens))
    assert sorted(item.payload['number'] for item in loaded) == list(range(8))
    assert await asyncio.to_thread(store.update, tokens[0], PAYLOAD) is True
    await asyncio.to_thread(store.revoke, tokens[0])
    assert await asyncio.to_thread(store.load, tokens[0]) is None
    await asyncio.to_thread(store.close)
