from fastapi.testclient import TestClient

from rumi_market.app import create_app

from login_helpers import login_body


def test_anonymous_listings_are_unauthorized(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.get("/api/market/listings")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_live_adapter_never_falls_back_to_history(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.get("/api/auth/capabilities")
        assert response.status_code == 200
        capability = response.json()
        assert capability["integration_ready"] is False
        assert capability["methods"] == []
        response = client.post("/api/auth/login", json=login_body(client, "test", "not-a-real-password"))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AUTH_PROTOCOL_UNVERIFIED"
        assert client.get("/api/session").json()["authenticated"] is False


def test_validation_error_does_not_echo_password(catalog_path):
    secret = "test-credential-never-echo"
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.post("/api/auth/login", json=login_body(client, [], secret))
        assert response.status_code == 422
        assert secret not in response.text
        assert "input" not in response.text


def test_public_catalog_matches_verified_names_and_multiple_forms(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.get("/api/pets", params={"query": "蜜桃"})
        assert response.status_code == 200
        assert {row["pet_id"] for row in response.json()["items"]} == {"101", "102"}
        assert response.json()["total"] == 4
        assert response.json()["named_count"] == 2
        alias = client.get("/api/pets", params={"query": "桃桃"})
        assert [row["pet_id"] for row in alias.json()["items"]] == ["101"]
        unknown = client.get("/api/pets", params={"query": "猜测"})
        assert unknown.json()["items"] == []


def test_cross_origin_mutation_is_rejected(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.post("/api/auth/login", headers={"Origin": "https://other.example"},
                               json=login_body(client, "test", "test"))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ORIGIN_REJECTED"


def test_anonymous_cookie_is_http_only(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.get("/api/session")
        assert response.status_code == 200
        assert response.json()["status"] == "anonymous"
        cookie = response.headers["set-cookie"].lower()
        assert "rumi_session=" in cookie
        assert "httponly" in cookie
        assert "samesite=strict" in cookie


def test_unverified_qr_never_creates_a_fake_challenge(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        for method, path in [("post", "/api/auth/qr"), ("get", "/api/auth/qr/unknown")]:
            response = getattr(client, method)(path)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "AUTH_PROTOCOL_UNVERIFIED"


def test_catalog_only_serves_checked_pngs_inside_images(catalog_path):
    import json

    from rumi_market.catalog import PNG_SIGNATURE

    images = catalog_path.parent / "images"
    images.mkdir()
    (images / "101.png").write_bytes(PNG_SIGNATURE + b"test fixture")
    outside = catalog_path.parent / "private.png"
    outside.write_bytes(PNG_SIGNATURE + b"private fixture")
    (images / "103.png").write_bytes(b"not a png")
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    for row, image in zip(data["pets"], ["images/101.png", "private.png", "images/103.png", None]):
        row["image_path"] = image
    catalog_path.write_text(json.dumps(data), encoding="utf-8")
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.get("/api/pets/101/image").status_code == 200
        assert client.get("/api/pets/102/image").status_code == 404
        assert client.get("/api/pets/103/image").status_code == 404
        assert client.get("/api/pets/104/image").status_code == 404
        assert client.get("/api/pets").json()["image_count"] == 1


def test_catalog_reads_names_with_evidence_and_literal_contains(catalog_path):
    import json

    (catalog_path.parent / "names.json").write_text(json.dumps({"names": {
        "104": {"name": "甲%噜咪", "source": "verified test source", "aliases": ["四号"]},
        "103": {"name": "无来源候选"},
    }}), encoding="utf-8")
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        result = client.get("/api/pets", params={"query": "%"}).json()
        assert [pet["pet_id"] for pet in result["items"]] == ["104"]
        assert result["named_count"] == 3
        assert client.get("/api/pets", params={"query": "无来源"}).json()["items"] == []
        assert client.get("/api/pets", params={"query": "四号"}).json()["items"][0]["pet_id"] == "104"


def test_session_cookie_is_secure_on_https(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path), base_url="https://testserver") as client:
        response = client.get("/api/session")
        assert "secure" in response.headers["set-cookie"].lower()


def test_anonymous_refresh_and_role_requests_are_rejected(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.post("/api/market/refresh", json={}).status_code == 401
        assert client.post("/api/session/role", json={"role_id": "other"}).status_code == 401


def test_browser_origin_and_fetch_site_are_checked(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        for origin in ["null", "http://testserver:bad", "https://testserver"]:
            result = client.delete("/api/session", headers={"Origin": origin})
            assert result.status_code == 403
        assert client.delete("/api/session", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
        assert client.delete("/api/session", headers={"Origin": "http://testserver",
                                                    "Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_runtime_has_no_purchase_or_demo_route(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.post("/api/market/purchase", json={}).status_code == 404
        assert client.post("/api/auth/demo", json={}).status_code == 404


def test_health_does_not_claim_game_integration_is_ready(catalog_path):
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"service": "rumi-market", "status": "ok", "game_integration_ready": False}


def test_catalog_rejects_absolute_image_paths_even_with_png_signature(catalog_path):
    import json

    from rumi_market.catalog import PNG_SIGNATURE

    images = catalog_path.parent / "images"
    images.mkdir()
    image = images / "101.png"
    image.write_bytes(PNG_SIGNATURE + b"test fixture")
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    data["pets"][0]["image_path"] = str(image.resolve())
    catalog_path.write_text(json.dumps(data), encoding="utf-8")
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.get("/api/pets/101/image").status_code == 404


def test_public_item_image_is_checked_png_without_changing_pet_catalog(catalog_path):
    from test_item_catalog import PNG, save_item_catalog

    save_item_catalog(catalog_path.parent.parent / "market")
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        image = client.get("/api/items/10031/image")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content == PNG
        assert image.headers["cache-control"] == client.get("/api/pets").headers["cache-control"]
        assert client.get("/api/items/10032/image").status_code == 404
        assert client.get("/api/items/010031/image").status_code == 404
        assert client.get("/api/pets").json()["total"] == 4
        assert client.get("/api/pets").json()["named_count"] == 2


def test_item_image_endpoint_rechecks_sha256_and_does_not_serve_tampered_files(catalog_path):
    from test_item_catalog import save_item_catalog

    path = save_item_catalog(catalog_path.parent.parent / "market")
    with TestClient(create_app(catalog_path=catalog_path)) as client:
        assert client.get("/api/items/10031/image").status_code == 200
        (path.parent / "images/10031.png").write_bytes(b"changed file")
        response = client.get("/api/items/10031/image")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "IMAGE_NOT_FOUND"
