"""Only the two anonymous encyclopedia PNG routes may use a public cache."""

import json
import os

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from rumi_market.app import create_app
from rumi_market.catalog import PNG_SIGNATURE
from test_item_catalog import save_item_catalog
from test_pet_details import save_details


CACHE_CONTROL = "public, max-age=3600"
IMAGE_URLS = ["/api/pets/101/image", "/api/pet-skills/3001/image"]


@pytest.fixture
def public_images(catalog_path):
    save_details(catalog_path)
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    data["pets"][0]["image_path"] = "images/101.png"
    catalog_path.write_text(json.dumps(data), encoding="utf-8")
    (catalog_path.parent / "images/101.png").write_bytes(PNG_SIGNATURE + b"public pet")
    save_item_catalog(catalog_path.parent.parent / "market")

    def forbidden_provider():
        raise AssertionError("Public images must not create game sessions")

    app = create_app(provider_factory=forbidden_provider, catalog_path=catalog_path)
    with TestClient(app) as client:
        yield client, app


@pytest.mark.parametrize("url", IMAGE_URLS)
def test_public_png_response_has_cache_validator_without_session(public_images, url):
    client, app = public_images
    response = client.get(url)
    assert response.status_code == 200
    assert response.content.startswith(PNG_SIGNATURE)
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == CACHE_CONTROL
    assert response.headers["etag"].startswith('"')
    assert "set-cookie" not in response.headers
    assert not client.cookies
    assert app.state.sessions.sessions == {}


@pytest.mark.parametrize("url", IMAGE_URLS)
@pytest.mark.parametrize("condition", ["matching", "weak", "list", "opaque_comma", "wildcard", "empty_members"])
def test_matching_if_none_match_uses_empty_304(public_images, url, condition):
    client, app = public_images
    original = client.get(url)
    etag = original.headers["etag"]
    headers = {
        "matching": etag,
        "weak": "W/" + etag,
        "list": '"unrelated", W/' + etag + ', "other"',
        "opaque_comma": '"opaque,tag", W/' + etag,
        "wildcard": "*",
        "empty_members": ", , " + etag + ",,",
    }
    response = client.get(url, headers={"If-None-Match": headers[condition]})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["cache-control"] == CACHE_CONTROL
    assert response.headers["etag"] == etag
    assert response.headers["last-modified"] == original.headers["last-modified"]
    assert "content-length" not in response.headers
    assert "set-cookie" not in response.headers
    assert not client.cookies
    assert app.state.sessions.sessions == {}


@pytest.mark.parametrize("url", IMAGE_URLS)
def test_repeated_if_none_match_headers_are_a_combined_list(public_images, url):
    client, _ = public_images
    etag = client.get(url).headers["etag"]
    response = client.get(url, headers=[("If-None-Match", '"other"'),
                                        ("If-None-Match", "W/" + etag)])
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["cache-control"] == CACHE_CONTROL


@pytest.mark.parametrize("url", IMAGE_URLS)
@pytest.mark.parametrize("condition", ['"different"', 'W/"different"', '"different,opaque"',
                                       "malformed", "* , \"different\"", "adjacent"])
def test_nonmatching_or_invalid_condition_still_returns_png(public_images, url, condition):
    client, _ = public_images
    original = client.get(url)
    if condition == "adjacent":
        condition = original.headers["etag"] + original.headers["etag"]
    response = client.get(url, headers={"If-None-Match": condition})
    assert response.status_code == 200
    assert response.content == original.content
    assert response.headers["cache-control"] == CACHE_CONTROL


@pytest.mark.parametrize("url", IMAGE_URLS)
def test_changed_public_image_has_a_new_validator(public_images, catalog_path, url):
    client, _ = public_images
    original = client.get(url)
    relative = "images/101.png" if "/pets/" in url else "images/skills/3001.png"
    image = catalog_path.parent / relative
    image.write_bytes(PNG_SIGNATURE + b"new image bytes with another length")
    os.utime(image, (image.stat().st_atime, image.stat().st_mtime + 2))
    response = client.get(url, headers={"If-None-Match": original.headers["etag"]})
    assert response.status_code == 200
    assert response.headers["etag"] != original.headers["etag"]
    assert response.content == image.read_bytes()


@pytest.mark.parametrize("url", ["/api/pets/unknown/image", "/api/pet-skills/unknown/image"])
def test_missing_public_image_is_not_cached_even_with_wildcard(public_images, url):
    client, _ = public_images
    response = client.get(url, headers={"If-None-Match": "*"})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("url", IMAGE_URLS)
def test_public_image_rechecks_png_before_conditional_response(public_images, catalog_path, url):
    client, _ = public_images
    original = client.get(url)
    relative = "images/101.png" if "/pets/" in url else "images/skills/3001.png"
    (catalog_path.parent / relative).write_bytes(b"no longer a PNG")
    response = client.get(url, headers={"If-None-Match": original.headers["etag"]})
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("url,status", [
    ("/api/pets", 200), ("/api/pets/101", 200), ("/api/time", 200),
    ("/api/market/listings", 401), ("/api/home/orders/state", 401),
    ("/api/rush-purchase", 401),
    ("/api/items/10031/image", 200), ("/api/not-found", 404),
])
def test_non_encyclopedia_images_and_all_other_apis_remain_no_store(public_images, url, status):
    client, _ = public_images
    response = client.get(url, headers={"If-None-Match": "*"})
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("url,status", [
    ("/api/auth/login-challenge", 200), ("/api/admin/login-challenge", 404),
    ("/api/admin/overview", 404), ("/api/session", 200),
])
def test_authentication_responses_remain_no_store(catalog_path, url, status):
    # The built-in unverified provider has no game/network access.
    app = create_app(catalog_path=catalog_path)
    with TestClient(app) as client:
        response = client.get(url, headers={"If-None-Match": "*"})
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"


def test_same_origin_guard_and_unsupported_methods_remain_no_store(public_images):
    client, _ = public_images
    for response in [
        client.post(IMAGE_URLS[0], headers={"Origin": "https://other.example"}),
        client.get("/api/auth/login-challenge", headers={"Origin": "https://other.example"}),
        client.head(IMAGE_URLS[0]),
    ]:
        assert response.status_code in {403, 405}
        assert response.headers["cache-control"] == "no-store"


def test_similar_routes_cannot_opt_themselves_into_public_caching(public_images):
    client, app = public_images

    @app.get("/api/test/image")
    def unrelated_png():
        return Response(PNG_SIGNATURE, media_type="image/png",
                        headers={"Cache-Control": CACHE_CONTROL, "ETag": '"test"'})

    response = client.get("/api/test/image")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
