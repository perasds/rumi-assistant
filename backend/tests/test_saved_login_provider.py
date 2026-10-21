import asyncio
from types import SimpleNamespace

import pytest

from rumi_market.errors import ApiError
from rumi_market.game_provider import IndependentGameProvider
from rumi_market.live_runtime import LiveProviderFactory
from test_sdk_saved_login import saved_state
from test_game_runtime import key


class Runtime:
    def __init__(self, *, failure=None, wait=None):
        self.closed = False
        self.can_query = True
        self.state = None
        self.failure, self.wait = failure, wait
        self.calls = []
    async def restore_login(self, state):
        self.calls.append("restore")
        if self.wait:
            await self.wait.wait()
        if self.failure:
            raise self.failure
        self.state = state
        return SimpleNamespace(role_id=123, zone_name="synthetic zone",
            needs_activation=False, needs_initial_pet_selection=False)
    def export_login_state(self):
        return self.state
    async def close(self):
        self.closed = True
        self.can_query = False


@pytest.mark.asyncio
@pytest.mark.parametrize('warning', [None, '工具适配版本：1.5.3 / 热更 72；游戏实际版本：1.5.3 / 热更 73', False, {}])
async def test_restore_and_role_selection_preserve_only_string_version_warning(warning):
    runtime = Runtime()
    runtime.version_warning = warning
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=()), login_verified=True)
    try:
        view = await provider.restore_login(saved_state())
        expected = warning if type(warning) is str else None
        assert view.authenticated and view.message == expected
        selected = await provider.select_role(view.role.id)
        assert selected.authenticated and selected.message == expected
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_restore_and_export_are_internal_copied_and_session_owned():
    runtime = Runtime()
    provider = IndependentGameProvider(runtime, SimpleNamespace(items=()), login_verified=True)
    assert provider.export_login_state() is None
    view = await provider.restore_login(saved_state())
    assert view.authenticated and runtime.calls == ["restore"]
    assert "access_key" not in str(view.model_dump())
    state = provider.export_login_state()
    state["identity"]["udid"] = "mutated"
    assert provider.export_login_state() == saved_state()
    await provider.close()
    assert provider.export_login_state() is None and runtime.closed


@pytest.mark.asyncio
async def test_lazy_restore_forwards_saved_context_to_builder_and_runtime():
    seen = []
    runtime = Runtime()
    async def builder(*, login_state=None):
        seen.append(login_state)
        return runtime, SimpleNamespace(items=())
    provider = LiveProviderFactory(runtime_builder=builder)()
    assert provider.export_login_state() is None
    view = await provider.restore_login(saved_state())
    assert view.authenticated and seen == [saved_state()] and runtime.calls == ["restore"]
    assert provider.export_login_state() == saved_state()
    with pytest.raises(ApiError):
        await provider.restore_login(saved_state())
    await provider.close()
    assert provider.export_login_state() is None


@pytest.mark.asyncio
async def test_lazy_restore_rejects_malformed_state_before_building():
    async def forbidden(**kwargs):
        pytest.fail("malformed saved login must never create a runtime")
    provider = LiveProviderFactory(runtime_builder=forbidden)()
    with pytest.raises(ApiError) as caught:
        await provider.restore_login({"access_key": "synthetic-private-token"})
    assert caught.value.code == "AUTH_REJECTED"
    assert "synthetic" not in str(caught.value.body())
    assert caught.value.__context__ is caught.value.__cause__ is None
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["AUTH_REJECTED", "AUTH_UPSTREAM_FAILED", "AUTH_PROTOCOL_UNVERIFIED"])
async def test_lazy_restore_failure_preserves_fixed_error_classification(code):
    runtime = Runtime(failure=ApiError(code, "synthetic-private-error"))
    async def builder(**kwargs):
        return runtime, SimpleNamespace(items=())
    provider = LiveProviderFactory(runtime_builder=builder)()
    with pytest.raises(ApiError) as caught:
        await provider.restore_login(saved_state())
    assert caught.value.code == code and runtime.closed
    assert caught.value.status_code == (401 if code == "AUTH_REJECTED" else 503)
    assert "synthetic" not in str(caught.value.body())
    assert provider.export_login_state() is None


@pytest.mark.asyncio
async def test_cancelled_restore_closes_runtime_and_drops_cancel_message():
    runtime = Runtime(wait=asyncio.Event())
    async def builder(**kwargs):
        return runtime, SimpleNamespace(items=())
    provider = LiveProviderFactory(runtime_builder=builder)()
    task = asyncio.create_task(provider.restore_login(saved_state()))
    await asyncio.sleep(0)
    task.cancel("synthetic-private-cancel")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert runtime.closed and not caught.value.args and provider.export_login_state() is None


@pytest.mark.asyncio
async def test_live_builder_restores_only_project_identity_before_version_client(monkeypatch, key):
    import rumi_market.live_runtime as module
    from test_game_runtime import materials
    sdk, _, _, _ = materials(key)
    captured = []
    assets = SimpleNamespace(origins=("https://example.invalid",), gm_secret="synthetic-key",
        channel="Android_CN_CDN", version="1.5.3", build_version="17", profile_source="synthetic")
    monkeypatch.setattr(module.SdkRequestContext, "from_apk", lambda path: sdk)
    monkeypatch.setattr(module, "load_version_assets", lambda path: assets)
    monkeypatch.setattr(module, "load_shop_catalog", lambda: SimpleNamespace(items=()))
    monkeypatch.setattr(module, "VersionHttpClient", lambda origin, context: captured.append(context) or object())
    monkeypatch.setattr(module, "GameTransport", lambda **kwargs: object())
    monkeypatch.setattr(module, "AccountProfileArchive", lambda: None)
    monkeypatch.setattr(module, "GameRuntime", lambda restored_sdk, version, client, transport, **kwargs:
                        captured.append((restored_sdk, client)) or object())
    state = saved_state()
    await module.build_live_runtime(login_state=state)
    restored_sdk, client = captured[1]
    assert restored_sdk.identity is captured[0].identity
    assert restored_sdk.identity.udid == state["identity"]["udid"]
    assert restored_sdk.identity.bd_id == state["identity"]["bd_id"]
    assert client.game_device_id == captured[0].device_id == state["identity"]["game_device_id"]
    assert sdk.identity.udid != restored_sdk.identity.udid


@pytest.mark.asyncio
async def test_failed_password_forwarder_clears_its_argument_frame():
    async def builder():
        raise RuntimeError("synthetic-private-builder")
    provider = LiveProviderFactory(runtime_builder=builder)()
    with pytest.raises(ApiError) as caught:
        await provider.login("synthetic-account", "synthetic-private-password")
    trace = caught.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_name in ("login", "_login"):
            assert trace.tb_frame.f_locals.get("account") in (None, "")
            assert trace.tb_frame.f_locals.get("password") in (None, "")
        trace = trace.tb_next
