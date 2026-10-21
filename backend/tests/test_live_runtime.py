import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from rumi_market.errors import ApiError


class Runtime:
    def __init__(self, failure=None, wait=None):
        self.failure, self.wait = failure, wait
        self.closed = False
        self.can_query = True

    async def start(self, account, password):
        if self.wait:
            await self.wait.wait()
        if self.failure:
            raise self.failure
        return SimpleNamespace(role_id=123, zone_name="synthetic zone",
            needs_activation=False, needs_initial_pet_selection=False)

    async def close(self):
        self.closed = True
        self.can_query = False


def shop():
    return SimpleNamespace(items=(), normal_pet_mapping={})


def factory(builder):
    from rumi_market.live_runtime import LiveProviderFactory
    return LiveProviderFactory(runtime_builder=builder)


@pytest.mark.asyncio
async def test_capabilities_and_unused_close_do_not_build_or_load_any_runtime():
    async def forbidden():
        pytest.fail("capabilities must not load APK or create clients")
    providers = [factory(forbidden)() for _ in range(3)]
    for provider in providers:
        assert provider.capabilities().model_dump() == {
            "provider": "bilibili_game", "integration_ready": True,
            "methods": ["account", "sms"], "reason_code": None, "message": None, "remember_login": False}
        await provider.close()


@pytest.mark.asyncio
async def test_separate_providers_build_and_close_only_their_own_runtime():
    runtimes = []
    async def builder():
        runtime = Runtime()
        runtimes.append(runtime)
        return runtime, shop()
    first, second = factory(builder)(), factory(builder)()
    a = await first.login("synthetic-a", "synthetic-p")
    b = await second.login("synthetic-b", "synthetic-p")
    assert a.authenticated and b.authenticated and a.role.id != b.role.id
    await first.close()
    assert runtimes[0].closed and not runtimes[1].closed
    await second.close()
    assert runtimes[1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("synthetic-secret"),
    ApiError("AUTH_REJECTED", "synthetic-secret"), ApiError("synthetic-secret", "synthetic-secret")])
async def test_failure_closes_owned_runtime_without_private_exception_context(failure):
    runtime = Runtime(failure=failure)
    async def builder():
        return runtime, shop()
    provider = factory(builder)()
    with pytest.raises(ApiError) as caught:
        await provider.login("synthetic-account-secret", "synthetic-password-secret")
    assert runtime.closed
    assert "synthetic" not in str(caught.value.body())
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_cancelled_login_closes_owned_runtime_and_drops_cancel_message():
    runtime = Runtime(wait=asyncio.Event())
    async def builder():
        return runtime, shop()
    provider = factory(builder)()
    task = asyncio.create_task(provider.login("synthetic-a", "synthetic-secret"))
    await asyncio.sleep(0)
    task.cancel("synthetic-cancel-secret")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert runtime.closed and not caught.value.args
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_close_during_lazy_build_closes_late_runtime_before_login():
    release = asyncio.Event()
    runtime = Runtime()
    async def builder():
        await release.wait()
        return runtime, shop()
    provider = factory(builder)()
    task = asyncio.create_task(provider.login("synthetic-a", "synthetic-p"))
    await asyncio.sleep(0)
    await provider.close()
    release.set()
    with pytest.raises(ApiError):
        await task
    assert runtime.closed


@pytest.mark.asyncio
async def test_closed_or_second_login_cannot_build_another_runtime():
    builds = []
    async def builder():
        builds.append(True)
        return Runtime(), shop()
    provider = factory(builder)()
    await provider.login("synthetic-a", "synthetic-p")
    with pytest.raises(ApiError):
        await provider.login("synthetic-a", "synthetic-p")
    await provider.close()
    with pytest.raises(ApiError):
        await provider.login("synthetic-a", "synthetic-p")
    assert builds == [True]


@pytest.mark.asyncio
async def test_default_builder_uses_fresh_source_contexts_and_matching_game_device_id(monkeypatch, tmp_path):
    import rumi_market.live_runtime as module
    contexts, versions, runtimes = [], [], []
    assets = SimpleNamespace(origins=("https://example.invalid",), gm_secret=SecretStr("synthetic-key"),
        channel="Android_CN_CDN", version="1.5.3", build_version="17", profile_source="synthetic source")
    def from_apk(path):
        assert path == tmp_path / "synthetic.apk"
        context = SimpleNamespace(identity=SimpleNamespace(udid="synthetic-udid", bd_id="synthetic-bdid"))
        contexts.append(context)
        return context
    def version(origin, context):
        versions.append(context)
        return SimpleNamespace(close=lambda: None)
    def runtime(sdk, version_client, client, transport, **options):
        result = (sdk, client, options)
        runtimes.append(result)
        return result
    monkeypatch.setattr(module.SdkRequestContext, "from_apk", from_apk)
    monkeypatch.setattr(module, "load_version_assets", lambda path: assets)
    monkeypatch.setattr(module, "load_shop_catalog", shop)
    monkeypatch.setattr(module, "VersionHttpClient", version)
    monkeypatch.setattr(module, "GameRuntime", runtime)
    for _ in range(2):
        await module.build_live_runtime(tmp_path / "synthetic.apk")
    assert contexts[0] is not contexts[1]
    assert versions[0].identity is contexts[0].identity and versions[1].identity is contexts[1].identity
    assert versions[0].device_id == runtimes[0][1].game_device_id
    assert versions[0].device_id != versions[1].device_id
    assert versions[0].device_id not in (contexts[0].identity.udid, contexts[0].identity.bd_id)
    assert runtimes[0][2]["captcha_handler"] is module.manual_captcha
    assert isinstance(runtimes[0][2]["profile_archive"], module.AccountProfileArchive)
    assert runtimes[0][2]["profile_archive"] is not runtimes[1][2]["profile_archive"]


@pytest.mark.asyncio
async def test_lazy_manual_callback_keeps_fixed_error_without_private_context(monkeypatch):
    import rumi_market.live_runtime as module
    async def handler(*args):
        raise ApiError("AUTH_CAPTCHA_CANCELLED", "synthetic-private-message")
    monkeypatch.setattr(module, "_load_manual_captcha", lambda: handler)
    with pytest.raises(ApiError) as caught:
        await module.manual_captcha(object(), object())
    assert caught.value.code == "AUTH_CAPTCHA_CANCELLED"
    assert "synthetic" not in str(caught.value.body())
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_builder_constructor_failure_closes_both_created_clients(monkeypatch):
    import rumi_market.live_runtime as module
    resources = []
    class Resource:
        def __init__(self, *args, **kwargs):
            self.closed = False
            resources.append(self)
        async def close(self):
            self.closed = True
    sdk = SimpleNamespace(identity=object())
    assets = SimpleNamespace(origins=("https://example.invalid",), gm_secret=SecretStr("synthetic"),
        channel="source", version="1.5.3", build_version="17", profile_source="source")
    monkeypatch.setattr(module.SdkRequestContext, "from_apk", lambda path: sdk)
    monkeypatch.setattr(module, "load_version_assets", lambda path: assets)
    monkeypatch.setattr(module, "load_shop_catalog", shop)
    monkeypatch.setattr(module, "VersionHttpClient", Resource)
    monkeypatch.setattr(module, "GameTransport", Resource)
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic-private-constructor")
    monkeypatch.setattr(module, "GameRuntime", failed)
    with pytest.raises(ApiError) as caught:
        await module.build_live_runtime()
    assert len(resources) == 2 and all(value.closed for value in resources)
    assert "synthetic" not in str(caught.value.body())
    assert caught.value.__context__ is caught.value.__cause__ is None
    trace = caught.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_name == "build_live_runtime":
            assert trace.tb_frame.f_locals.get("sdk") is None
            assert trace.tb_frame.f_locals.get("assets") is None
        trace = trace.tb_next


def test_manual_bridge_import_is_lazy_and_uses_the_normal_handler_without_probe():
    import sys
    import rumi_market.live_runtime as module
    assert "probe_account_login" not in sys.modules
    handler = module._load_manual_captcha()
    assert callable(handler) and handler.__name__ == "manual_captcha"
    assert "probe_account_login" not in sys.modules


@pytest.mark.asyncio
async def test_untrusted_callback_error_property_cannot_escape_safe_copy(monkeypatch):
    import rumi_market.live_runtime as module
    class BadError(ApiError):
        def __init__(self):
            Exception.__init__(self, "synthetic-private")
        @property
        def code(self):
            raise RuntimeError("synthetic-private-code-property")
    async def handler(*args):
        raise BadError()
    monkeypatch.setattr(module, "_load_manual_captcha", lambda: handler)
    with pytest.raises(ApiError) as caught:
        await module.manual_captcha(object(), object())
    assert "synthetic" not in str(caught.value.body())
    assert caught.value.__context__ is caught.value.__cause__ is None
