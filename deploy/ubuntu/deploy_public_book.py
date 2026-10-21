"""Activate a pinned public-only package without reading account storage."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile


PUBLIC_PATH = re.compile(
    r"(?:index\.html|app-icon\.svg|assets/[A-Za-z0-9_-]+\.(?:js|css|png)|"
    r"book-data/catalog\.json|book-data/pets/[1-9][0-9]*\.json|"
    r"book-data/(?:pet-images|skill-images)/[1-9][0-9]*\.png)\Z")


def digest(body):
    return hashlib.sha256(body).hexdigest()


def read_package(path, expected_sha256):
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("Archive too large")
    if digest(path.read_bytes()) != expected_sha256:
        raise ValueError("Unexpected archive digest")
    contents, total = {}, 0
    with tarfile.open(path, "r:gz") as archive:
        for entry in archive:
            if not entry.isfile():
                raise ValueError("Payload must contain regular files only")
            name = entry.name
            if name in contents:
                raise ValueError("Duplicate payload member")
            if name not in {"nginx.conf", "manifest.public.json"}:
                if not name.startswith("site/") or not PUBLIC_PATH.fullmatch(name[5:]):
                    raise ValueError("Unexpected public path")
            total += entry.size
            if total > 200 * 1024 * 1024:
                raise ValueError("Expanded payload too large")
            contents[name] = archive.extractfile(entry).read()
    manifest = json.loads(contents["manifest.public.json"])
    expected = {"nginx.conf", "manifest.public.json"}
    for record in manifest["files"]:
        if not PUBLIC_PATH.fullmatch(record["path"]):
            raise ValueError("Unexpected manifest path")
        name = "site/" + record["path"]
        if name in expected:
            raise ValueError("Duplicate manifest member")
        expected.add(name)
        body = contents.get(name, b"")
        if name not in contents or len(body) != record["bytes"] or digest(body) != record["sha256"]:
            raise ValueError("Unexpected file digest")
    if set(contents) != expected or "site/index.html" not in contents or "site/book-data/catalog.json" not in contents:
        raise ValueError("Payload does not match manifest")
    return manifest, contents


def run(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout.strip()


def write_new(path, body, mode=0o644):
    with path.open("xb") as output:
        output.write(body)
    path.chmod(mode)


def activate(args):
    if os.geteuid() != 0:
        raise ValueError("Run deployment with sudo")
    if not re.fullmatch(r"[0-9]{8}-book-[a-f0-9]{8}", args.release):
        raise ValueError("Invalid release name")
    manifest, contents = read_package(args.package, args.sha256)
    base = Path("/opt/rumi-assistant")
    config = Path("/etc/nginx/sites-available/rumi-assistant")
    current = base / "current"
    book_link = base / "public-book-current"
    if base.resolve() != base or not current.is_symlink() or str(current.resolve()) != args.expected_release:
        raise ValueError("Existing release changed")
    if config.is_symlink() or digest(config.read_bytes()) != args.expected_config_sha256:
        raise ValueError("Existing Nginx configuration changed")
    if os.path.lexists(book_link):
        raise ValueError("Public book link already exists; inspect before retrying")
    releases = base / "public-book-releases"
    backups = base / "book-only-backups"
    for parent, mode in ((releases, 0o755), (backups, 0o700)):
        if parent.is_symlink():
            raise ValueError("Unexpected deployment directory link")
        parent.mkdir(mode=mode, exist_ok=True)
    release = releases / args.release
    backup = backups / args.release
    release.mkdir(mode=0o755)
    backup.mkdir(mode=0o700)
    previous = config.read_bytes()
    write_new(backup / "nginx.conf", previous, 0o600)
    service_before = run("systemctl", "show", "rumi-assistant.service",
                         "-p", "ActiveState", "-p", "UnitFileState", "-p", "MainPID")
    write_new(backup / "service-before.txt", service_before.encode(), 0o600)
    for name, body in contents.items():
        if not name.startswith("site/"):
            continue
        destination = release / name[5:]
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        write_new(destination, body)
    write_new(backup / "manifest.public.json", contents["manifest.public.json"], 0o600)
    book_link.symlink_to(release, target_is_directory=True)
    replacement = config.with_name("rumi-assistant.book-new")
    write_new(replacement, contents["nginx.conf"])
    try:
        os.replace(replacement, config)
        run("nginx", "-t")
        run("systemctl", "reload", "nginx.service")
    except Exception:
        restore = config.with_name("rumi-assistant.book-restore")
        write_new(restore, previous)
        os.replace(restore, config)
        if book_link.is_symlink() and book_link.resolve() == release:
            book_link.unlink()
        run("nginx", "-t")
        run("systemctl", "reload", "nginx.service")
        raise
    # Static routing is already active, so no new account or task requests can enter.
    run("systemctl", "disable", "--now", "rumi-assistant.service")
    service_after = run("systemctl", "show", "rumi-assistant.service",
                        "-p", "ActiveState", "-p", "UnitFileState", "-p", "MainPID")
    if "ActiveState=inactive" not in service_after or "MainPID=0" not in service_after or "UnitFileState=disabled" not in service_after:
        raise ValueError("Old backend has not fully stopped")
    if str(current.resolve()) != args.expected_release:
        raise ValueError("Full application release unexpectedly changed")
    report = {"release": args.release, "public_root": str(release), "backup": str(backup),
              "counts": manifest["counts"], "old_backend": "inactive_disabled",
              "old_release_preserved": True, "private_storage_read": False,
              "nginx_sha256": digest(contents["nginx.conf"]), "archive_sha256": args.sha256}
    write_new(backup / "activation.public.json", json.dumps(report, indent=2).encode(), 0o600)
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--expected-release", required=True)
    activate(parser.parse_args())
