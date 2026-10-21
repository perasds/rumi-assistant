"""The public-resource verifier must never attempt a game login or QR flow."""

import importlib.util
import hashlib
import json
from pathlib import Path

import httpx
import pytest


PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / "scripts/verify_runtime.py"
BASE_URL = "http://127.0.0.1:5173"
PEACH_NAMES = {
    "1000101": "蜜桃", "1000601": "粉龙蜜桃", "1000801": "小学生蜜桃",
    "1000902": "超人蜜桃", "1003401": "小蜜蜂蜜桃", "1003501": "小红帽蜜桃",
}


def run_verifier(monkeypatch, tmp_path, *, health_ready=False, capabilities_ready=False,
                 methods=None, bad_image=None, refresh_code="AUTH_REQUIRED"):
    spec = importlib.util.spec_from_file_location("public_runtime_verifier", SCRIPT)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    # Keep the real verifier and its byte comparison logic; supply only synthetic assets.
    project = tmp_path / 'public-site'
    (project / 'data/pets/images').mkdir(parents=True)
    (project / 'data/market/images').mkdir(parents=True)
    (project / 'data/pets/images/1000101.png').write_bytes(b'\x89PNG\r\n\x1a\nsynthetic avatar')
    rows = []
    for item_id in range(10031, 10036):
        raw = b'\x89PNG\r\n\x1a\n' + f'synthetic item {item_id}'.encode('ascii')
        (project / f'data/market/images/{item_id}.png').write_bytes(raw)
        rows.append({'item_id': str(item_id), 'image_path': f'images/{item_id}.png',
                     'image_sha256': hashlib.sha256(raw).hexdigest()})
    (project / 'data/market/items.json').write_text(json.dumps({'items': rows}), encoding='utf-8')
    monkeypatch.setattr(verifier, '__file__', str(project / 'scripts/verify_runtime.py'))
    methods = [] if methods is None else methods
    seen = []

    def handle(request):
        path = request.url.path
        assert "/auth/login" not in path and "/qr" not in path, "Verifier attempted game authentication"
        seen.append((request.method, path, request.content, request.headers.get("origin")))
        if path == "/api/health":
            return httpx.Response(200, json={"service": "rumi-market", "status": "ok",
                                             "game_integration_ready": health_ready})
        if path == "/api/auth/capabilities":
            return httpx.Response(200, json={"integration_ready": capabilities_ready, "methods": methods})
        if path == "/api/session":
            return httpx.Response(200, json={"authenticated": False},
                                  headers={"set-cookie": "rumi_session=synthetic-cookie; HttpOnly; SameSite=Strict"})
        if path == "/api/pets":
            items = ([{"pet_id": key, "name": name, "name_verified": True}
                      for key, name in PEACH_NAMES.items()] if request.url.params.get("query") else [{}] * 306)
            return httpx.Response(200, json={"items": items, "total": 306,
                                             "named_count": 246, "image_count": 297})
        if path == "/api/market/listings":
            return httpx.Response(401, json={"error": {"code": "AUTH_REQUIRED"}})
        if path == "/api/market/refresh":
            assert request.method == "POST" and json.loads(request.content) == {}
            if request.headers.get("origin") == BASE_URL:
                return httpx.Response(401, json={"error": {"code": refresh_code}})
            return httpx.Response(403, json={"error": {"code": "ORIGIN_REJECTED"}})
        if path.startswith("/api/pets/") and path.endswith("/image"):
            data = (project / "data/pets/images/1000101.png").read_bytes()
        elif path.startswith("/api/items/") and path.endswith("/image"):
            item_id = path.split("/")[3]
            assert item_id in {"10031", "10032", "10033", "10034", "10035"}
            data = (project / f"data/market/images/{item_id}.png").read_bytes()
        else:
            raise AssertionError(f"Unexpected verifier request: {request.method} {path}")
        return httpx.Response(200, content=b"invalid-image" if path == bad_image else data,
                              headers={"content-type": "image/png"})

    original_client = httpx.Client
    transport = httpx.MockTransport(handle)

    def make_client(**kwargs):
        assert kwargs["trust_env"] is False
        return original_client(**kwargs, transport=transport)

    monkeypatch.setattr(verifier.httpx, "Client", make_client)
    return verifier.verify(BASE_URL), seen


@pytest.mark.parametrize("ready,methods", [(False, []), (True, ["account"])])
def test_verifier_accepts_framework_or_live_without_any_login_or_qr(monkeypatch, tmp_path, ready, methods):
    report, seen = run_verifier(monkeypatch, tmp_path, health_ready=ready, capabilities_ready=ready, methods=methods)
    assert report["all_checks_passed"] is True
    assert report["checks"]["auth_capabilities_consistent"] is True
    assert "real_auth_not_faked" not in report["checks"]
    assert report["performed_game_login"] is False
    assert report["scope"] == "public_resources_and_anonymous_access_gates_only"
    assert report["game_integration_ready"] is ready
    assert [(method, path, json.loads(body), origin) for method, path, body, origin in seen
            if method == "POST"] == [
        ("POST", "/api/market/refresh", {}, BASE_URL),
        ("POST", "/api/market/refresh", {}, "https://example.invalid"),
    ]
    assert {path for method, path, _, _ in seen if path.startswith("/api/items/")} == {
        f"/api/items/{item_id}/image" for item_id in range(10031, 10036)}
    assert "synthetic-cookie" not in json.dumps(report)


@pytest.mark.parametrize("health_ready,capabilities_ready,methods", [
    (False, True, ["account"]), (True, False, []),
    (True, True, []), (False, False, ["account"]),
    (True, True, ["account", "qr"]), (0, 0, []), (None, None, []),
])
def test_verifier_rejects_inconsistent_or_unsupported_auth_capabilities(
        monkeypatch, tmp_path, health_ready, capabilities_ready, methods):
    report, _ = run_verifier(monkeypatch, tmp_path, health_ready=health_ready,
                             capabilities_ready=capabilities_ready, methods=methods)
    assert report["checks"]["auth_capabilities_consistent"] is False
    assert report["all_checks_passed"] is False
    assert report["performed_game_login"] is False


@pytest.mark.parametrize("path,check", [
    ("/api/pets/1000101/image", "real_avatar_bytes"),
    ("/api/items/10033/image", "verified_item_icon_bytes"),
])
def test_verifier_detects_changed_public_png_bytes(monkeypatch, tmp_path, path, check):
    report, _ = run_verifier(monkeypatch, tmp_path, bad_image=path)
    assert report["checks"][check] is False
    assert report["all_checks_passed"] is False


def test_same_origin_auth_gate_requires_auth_required_code(monkeypatch, tmp_path):
    report, _ = run_verifier(monkeypatch, tmp_path, refresh_code="OTHER_ERROR")
    assert report["checks"]["same_origin_proxy_and_auth_gate"] is False
    assert report["all_checks_passed"] is False
