"""Delivery-policy contracts; deployment must also run nginx -t and HTTP probes.

These tests evaluate the configured map against public/private request cases.
They do not claim to replace Nginx's own parser or real response verification.
"""

from pathlib import Path
import re

import pytest


CONFIG = Path(__file__).resolve().parents[1] / "deploy/ubuntu/rumi-assistant.nginx.conf"


def block(text, heading):
    match = re.search(r"(?m)^\s*" + re.escape(heading) + r"\s*\{", text)
    assert match, f"Missing configuration block: {heading}"
    start, depth = match.end(), 1
    for offset in range(start, len(text)):
        depth += (text[offset] == "{") - (text[offset] == "}")
        if depth == 0:
            return text[start:offset]
    raise AssertionError(f"Unclosed configuration block: {heading}")


def directives(text, name):
    return re.findall(r"(?m)^\s*" + re.escape(name) + r"\s+([^;]+);", text)


@pytest.fixture(scope="module")
def config():
    return CONFIG.read_text(encoding="utf-8")


def image_cache_control(config, method, uri, status, upstream_header):
    rules = block(config, 'map "$request_method:$uri:$upstream_status:$upstream_http_cache_control" '
                  "$rumi_public_image_cache_control")
    default = re.search(r"(?m)^\s*default\s+([^;]+);", rules)
    assert default
    value = f"{method}:{uri}:{status}:{upstream_header}"
    for pattern, result in re.findall(r'"~([^"\n]+)"\s+"([^"\n]+)";', rules):
        if re.search(pattern, value):
            return result
    return default[1].strip().strip('"')


@pytest.mark.parametrize("route", ["/api/pets/101/image", "/api/pet-skills/3001/image"])
@pytest.mark.parametrize("status", ["200", "304"])
def test_only_confirmed_public_pngs_can_pass_the_cache_header(config, route, status):
    assert image_cache_control(config, "GET", route, status, "public, max-age=3600") == \
        "public, max-age=3600"


@pytest.mark.parametrize("method,route,status,header", [
    ("HEAD", "/api/pets/101/image", "200", "public, max-age=3600"),
    ("POST", "/api/pets/101/image", "200", "public, max-age=3600"),
    ("GET", "/api/pets/101/image", "404", "public, max-age=3600"),
    ("GET", "/api/pets/101/image", "500", "public, max-age=3600"),
    ("GET", "/api/pets/101/image", "200", "no-store"),
    ("GET", "/api/pets/101/image", "200", "private, max-age=3600"),
    ("GET", "/api/pets/101/image", "200", "public, max-age=3600, private"),
    ("GET", "/api/pets/101/image", "200", ""),
    ("GET", "/api/pets/101/image", "502, 200", "public, max-age=3600"),
    ("GET", "/api/pets", "200", "public, max-age=3600"),
    ("GET", "/api/pets/101", "200", "public, max-age=3600"),
    ("GET", "/api/pets/101/extra/image", "200", "public, max-age=3600"),
    ("GET", "/api/pets/101/image/extra", "200", "public, max-age=3600"),
    ("GET", "/api/items/10031/image", "200", "public, max-age=3600"),
    ("GET", "/api/session", "200", "public, max-age=3600"),
    ("GET", "/api/home/orders/state", "200", "public, max-age=3600"),
    ("GET", "/api/admin/overview", "200", "public, max-age=3600"),
    ("GET", "/api/not-found", "404", "public, max-age=3600"),
])
def test_other_requests_and_unconfirmed_responses_are_no_store(config, method, route, status, header):
    assert image_cache_control(config, method, route, status, header) == "no-store"


def test_api_locations_keep_same_origin_proxy_identity_and_security_headers(config):
    for heading in ("location ^~ /api/", "location = /api/pets"):
        section = block(config, heading)
        assert directives(section, "proxy_pass") == ["http://127.0.0.1:18082"]
        assert set(directives(section, "proxy_set_header")) == {
            "Host $http_host", "X-Forwarded-Proto $scheme",
            "X-Real-IP $remote_addr", "X-Forwarded-For $remote_addr",
        }
        assert directives(section, "proxy_http_version") == ["1.1"]
        assert directives(section, "proxy_buffering") == ["off"]
        assert directives(section, "proxy_cache") == ["off"]
        assert directives(section, "proxy_hide_header") == ["Cache-Control"]
        headers = directives(section, "add_header")
        assert "X-Content-Type-Options nosniff always" in headers
        assert "Referrer-Policy same-origin always" in headers
        expected_cache = "Cache-Control no-store always" if heading.endswith("/api/pets") else \
            "Cache-Control $rumi_public_image_cache_control always"
        assert expected_cache in headers


def test_gzip_is_opted_in_only_for_public_text(config):
    server = block(config, "server")
    # The server starts with compression disabled, so system-wide gzip_types
    # cannot accidentally compress account or task JSON.
    server_preamble = server.split("location", 1)[0]
    assert directives(server_preamble, "gzip") == ["off"]
    assert len(re.findall(r"(?m)^\s*gzip on;", config)) == 2
    for heading, types in (("location /assets/", "text/css application/javascript"),
                           ("location = /api/pets", "application/json")):
        section = block(config, heading)
        assert directives(section, "gzip") == ["on"]
        assert directives(section, "gzip_types") == [types]
        assert directives(section, "gzip_min_length") == ["1024"]
        assert directives(section, "gzip_vary") == ["on"]
    assert not directives(block(config, "location ^~ /api/"), "gzip_types")


def test_static_and_private_path_guards_remain(config):
    assert directives(block(config, "location /assets/"), "try_files") == ["$uri =404"]
    assert directives(block(config, r"location ~ (^|/)\."), "return") == ["404"]
    private = block(config, "location ~ ^/(runtime|samples|tools|backend|scripts|data|deploy)(/|$)")
    assert directives(private, "return") == ["404"]
    for heading in ("location = /index.html",):
        assert 'Cache-Control "no-store, no-cache, must-revalidate" always' in \
            directives(block(config, heading), "add_header")


def test_retired_admin_paths_do_not_fall_back_to_the_app(config):
    for heading in ("location = /admin", "location ^~ /admin/",
                    "location = /api/admin", "location ^~ /api/admin/"):
        assert directives(block(config, heading), "return") == ["404"]
    assert "rumi-admin" not in config


def test_retired_admin_launchers_and_configuration_are_absent():
    project = CONFIG.parents[2]
    assert not (project.parent / "rumi-admin").exists()
    assert not (project / "scripts/setup_admin.py").exists()
    assert "RUMI_ADMIN_PASSWORD_FILE" not in CONFIG.with_name("rumi-assistant.env.example").read_text(encoding="utf-8")
