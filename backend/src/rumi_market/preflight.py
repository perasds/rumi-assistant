"""Offline material inventory. Never extract APK constants or contact a service."""
import argparse
import hashlib
import json
from pathlib import Path

from .material_paths import PROJECT, resolve_apk_path
from .sdk_assets import APK_MAX_BYTES, APK_SHA256


FRAMEWORK_FILES = ('data/home/production.json', 'data/home/management.json')
LIVE_FILES = (
    'data/pets/catalog.json', 'data/pets/names.json', 'data/pets/details.json',
    'data/pets/season-tags.json', 'data/market/items.json', 'data/market/shop-candidates.json',
    'scripts/node_modules/playwright-core/index.mjs',
    'backend/src/rumi_market/game/gold-source.json',
    *(f'backend/src/rumi_market/game/{name}{suffix}'
      for name in ('protocol', 'profile', 'home', 'order', 'season', 'redbag')
      for suffix in ('.descriptor.pb', '-source.json')),
)


def check_apk(apk_path) -> dict:
    status = 'missing'
    try:
        path = Path(apk_path)
        if path.is_file():
            status = 'invalid'
            with path.open('rb') as stream:
                if 0 < stream.seek(0, 2) <= APK_MAX_BYTES:
                    stream.seek(0)
                    if hashlib.file_digest(stream, 'sha256').hexdigest() == APK_SHA256:
                        status = 'verified'
    except OSError:
        status = 'invalid'
    messages = {
        'missing': 'Provide the legally obtained pinned APK at external/base.apk or set RUMI_APK_PATH.',
        'invalid': 'APK identity does not match the supported pinned source; no APK content was extracted.',
        'verified': 'Pinned APK SHA-256 verified; no APK content was extracted.',
    }
    return {'component': 'apk', 'status': status, 'message': messages[status]}


def inspect_materials(project=PROJECT, *, apk_path=None, framework_only=False) -> dict:
    project = Path(project)
    files = FRAMEWORK_FILES if framework_only else (*FRAMEWORK_FILES, *LIVE_FILES)
    checks = []
    for name in files:
        present = (project / name).is_file()
        checks.append({'component': name, 'status': 'present' if present else 'missing',
                       'message': 'File present; content is validated when used.' if present else
                       ('Run npm ci --prefix scripts.' if name.startswith('scripts/node_modules/') else
                        'Provide generated materials from your legally obtained sources; see README.')})
    if not framework_only:
        checks.insert(0, check_apk(resolve_apk_path(apk_path, project=project)))
    return {'ready': all(row['status'] in ('present', 'verified') for row in checks),
            'mode': 'framework' if framework_only else 'live',
            'network_requests_sent': False, 'checks': checks,
            'scope': ('Only framework startup file presence is checked; content is validated when used. '
                      'Real game integration remains disabled.' if framework_only else
                      'Only file presence and APK identity are checked. Runtime schema validation, an installed '
                      'Chromium (RUMI_CHROMIUM_EXECUTABLE), login and game-version compatibility remain required.')}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', type=Path, help='User-supplied APK; overrides RUMI_APK_PATH.')
    parser.add_argument('--framework-only', action='store_true', help='Check only offline framework startup files.')
    args = parser.parse_args(argv)
    result = inspect_materials(PROJECT, apk_path=args.apk, framework_only=args.framework_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
