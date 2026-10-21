"""Package validation before any privileged deployment operation."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/ubuntu/deploy_public_book.py"


def loader():
    assert SCRIPT.is_file(), "Public-only deployment validator is not implemented"
    spec = importlib.util.spec_from_file_location("deploy_public_book", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_package


def package(tmp_path, *, extra=None, bad_digest=False, link=False):
    site = {"index.html": b"public book", "book-data/catalog.json": b'{"items":[]}'}
    records = [{"path": name, "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest()} for name, body in site.items()]
    if bad_digest:
        records[0]["sha256"] = "0" * 64
    contents = {"site/" + name: body for name, body in site.items()}
    contents["nginx.conf"] = b"server { listen 443 ssl; }"
    contents["manifest.public.json"] = json.dumps({"files": records, "counts": {}}).encode()
    if extra:
        contents.update(extra)
    path = tmp_path / "public-book.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, body in contents.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
        if link:
            entry = tarfile.TarInfo("site/assets/book-link.js")
            entry.type, entry.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(entry)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_reads_exact_manifested_public_payload(tmp_path):
    path, digest = package(tmp_path)
    manifest, contents = loader()(path, digest)
    assert len(manifest["files"]) == 2
    assert contents["site/index.html"] == b"public book"


def test_rejects_modified_archive_before_extraction(tmp_path):
    path, _ = package(tmp_path)
    with pytest.raises(ValueError, match="archive digest"):
        loader()(path, "0" * 64)


def test_rejects_modified_file_digest(tmp_path):
    path, digest = package(tmp_path, bad_digest=True)
    with pytest.raises(ValueError, match="file digest"):
        loader()(path, digest)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "site/../escape",
                                   "site/runtime/private.json", "site/assets/extra.js.map"])
def test_rejects_unlisted_or_private_paths(tmp_path, name):
    path, digest = package(tmp_path, extra={name: b"not public"})
    with pytest.raises(ValueError):
        loader()(path, digest)


def test_rejects_symlink_payload(tmp_path):
    path, digest = package(tmp_path, link=True)
    with pytest.raises(ValueError, match="regular file"):
        loader()(path, digest)
