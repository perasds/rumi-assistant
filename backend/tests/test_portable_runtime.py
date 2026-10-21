"""Portable local setup checks use synthetic files and never contact the game."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def test_apk_path_defaults_to_ignored_project_materials(monkeypatch):
    from rumi_market.material_paths import DEFAULT_APK, PROJECT, resolve_apk_path
    monkeypatch.delenv('RUMI_APK_PATH', raising=False)
    assert DEFAULT_APK == PROJECT / 'external/base.apk'
    assert resolve_apk_path() == DEFAULT_APK


def test_apk_path_environment_and_explicit_override(monkeypatch, tmp_path):
    from rumi_market.material_paths import resolve_apk_path
    configured = tmp_path / 'configured.apk'
    explicit = tmp_path / 'explicit.apk'
    monkeypatch.setenv('RUMI_APK_PATH', str(configured))
    assert resolve_apk_path() == configured
    assert resolve_apk_path(explicit) == explicit


def test_apk_preflight_distinguishes_missing_invalid_and_pinned_file(monkeypatch, tmp_path):
    from rumi_market import preflight
    path = tmp_path / 'synthetic-private-name.apk'
    assert preflight.check_apk(path)['status'] == 'missing'
    path.write_bytes(b'synthetic-private-material')
    assert preflight.check_apk(path)['status'] == 'invalid'
    monkeypatch.setattr(preflight, 'APK_SHA256', hashlib.sha256(path.read_bytes()).hexdigest())
    assert preflight.check_apk(path)['status'] == 'verified'
    rendered = json.dumps(preflight.check_apk(path))
    assert 'synthetic-private' not in rendered
    assert str(tmp_path) not in rendered


def test_framework_preflight_requires_only_startup_configuration(tmp_path):
    from rumi_market.preflight import inspect_materials
    result = inspect_materials(tmp_path, framework_only=True)
    assert not result['ready']
    assert {row['component'] for row in result['checks']} == {
        'data/home/production.json', 'data/home/management.json'}
    for row in result['checks']:
        path = tmp_path / row['component']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    result = inspect_materials(tmp_path, framework_only=True)
    assert result['ready']
    assert all(row['status'] == 'present' for row in result['checks'])


def test_full_preflight_lists_external_materials_without_loading_them(tmp_path):
    from rumi_market.preflight import inspect_materials
    result = inspect_materials(tmp_path, apk_path=tmp_path / 'absent.apk')
    assert not result['ready']
    checks = {row['component']: row for row in result['checks']}
    assert checks['apk']['status'] == 'missing'
    assert checks['scripts/node_modules/playwright-core/index.mjs']['status'] == 'missing'
    assert checks['backend/src/rumi_market/game/protocol.descriptor.pb']['status'] == 'missing'
    assert checks['backend/src/rumi_market/game/gold-source.json']['status'] == 'missing'
    assert checks['data/market/shop-candidates.json']['status'] == 'missing'


def test_preflight_cli_outputs_fixed_guidance_and_fails_closed(monkeypatch, tmp_path, capsys):
    from rumi_market import preflight
    monkeypatch.setattr(preflight, 'PROJECT', tmp_path)
    assert preflight.main(['--apk', str(tmp_path / 'private-user-file.apk')]) == 1
    report = capsys.readouterr().out
    assert json.loads(report)['network_requests_sent'] is False
    assert 'RUMI_APK_PATH' in report
    assert 'private-user-file' not in report
    assert str(tmp_path) not in report


def test_live_factory_resolves_apk_configuration_on_first_login(monkeypatch, tmp_path):
    import asyncio
    import rumi_market.live_runtime as runtime
    from rumi_market.errors import ApiError
    seen = []
    monkeypatch.setenv('RUMI_APK_PATH', str(tmp_path / 'configured.apk'))
    def reject(path):
        seen.append(path)
        raise ValueError('synthetic-private')
    monkeypatch.setattr(runtime.SdkRequestContext, 'from_apk', reject)
    async def attempt():
        try:
            await runtime.LiveProviderFactory()().login('synthetic', 'synthetic')
        except ApiError as error:
            assert error.code == 'AUTH_PROTOCOL_UNVERIFIED'
    asyncio.run(attempt())
    assert seen == [tmp_path / 'configured.apk']


def test_live_runtime_import_does_not_read_protocol_materials(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src'
    code = '''from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
original_open = Path.open
def guarded_open(path, *args, **kwargs):
    if path.suffix == '.pb' or path.name.endswith('-source.json'):
        raise AssertionError('Protocol material read during module import')
    return original_open(path, *args, **kwargs)
Path.open = guarded_open
import rumi_market.live_runtime
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(source)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
