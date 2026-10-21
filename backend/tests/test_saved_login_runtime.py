import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from rumi_market.errors import ApiError
from test_game_runtime import PRIVATE, TOKEN, TrackingHttp, game_server, key, setup


@pytest.mark.asyncio
async def test_fresh_login_never_exports_sdk_credentials_and_old_internal_restore_is_explicit(key):
    first, game, events, _, _, _ = setup(key)
    server = asyncio.create_task(game_server(game, events))
    try:
        assert first.export_login_state() is None
        await first.start("synthetic-account", PRIVATE)
        await server
        assert first.export_login_state() is None
    finally:
        server.cancel()
        await first.close()
    assert first.export_login_state() is None

    state = {"version": 1, "uid": "synthetic-raw-sdk-uid", "access_key": TOKEN,
        "game_user": {"game_uid": "synthetic-projected-game-uid", "login_type": "0"},
        "expires": None, "renewal_expires": None,
        "identity": {"udid": "synthetic-http-udid", "bd_id": "synthetic-http-bdid",
                     "game_device_id": "synthetic-game-buvid"}}
    runtime, game, events, _, _, _ = setup(key)
    def token_response(request):
        events.append("token")
        assert request.url.path == "/api/external/user.token.oauth.login/v3"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["version"] == "1" and form["udid"] == state["identity"]["udid"]
        assert form["access_key"] == TOKEN and form["uid"] == state["uid"]
        assert not {"user_id", "pwd"} & form.keys() and request.headers["cversion"] == "7"
        return httpx.Response(200, json={"code": 0, "uid": state["uid"], "access_key": TOKEN,
            "game_open_id_enable": True, "game_open_id": state["game_user"]["game_uid"]})
    runtime._login_transport = TrackingHttp(token_response)
    server = asyncio.create_task(game_server(game, events))
    try:
        result = await runtime.restore_login(state)
        await server
        assert runtime.can_query and result.role_id == 123
        assert events == ["version", "config", "token", "game_login", "enter"]
        assert runtime.export_login_state() == state
    finally:
        server.cancel()
        await runtime.close()
    assert runtime.export_login_state() is None


@pytest.mark.asyncio
async def test_saved_login_bad_identity_fails_before_network_and_never_exports(key):
    from test_sdk_saved_login import saved_state
    runtime, game, events, _, _, _ = setup(key)
    try:
        with pytest.raises(ApiError) as caught:
            await runtime.restore_login(saved_state())
        assert caught.value.code == "AUTH_REJECTED" and "synthetic" not in str(caught.value.body())
        assert events == [] and runtime.export_login_state() is None
    finally:
        await runtime.close()
