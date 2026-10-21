"""The local probe must never serialize credentials or raw listing IDs."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rumi_market.errors import ApiError
from rumi_market.game.market_query import (AuctionPage, AuctionQuote, SystemShopBatch,
                                         SystemShopCandidate, SystemShopQuote)


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/probe_account_login.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("local_login_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Catalog:
    def pet(self, pet_id):
        return SimpleNamespace(name="蜜桃" if pet_id == "1000101" else None)


class Runtime:
    def __init__(self, *, result=None, failure=None):
        self.result = result or SimpleNamespace(needs_activation=False,
                                               needs_initial_pet_selection=False,
                                               role_id=12345678)
        self.failure = failure
        self.can_query = not (self.result.needs_activation
                              or self.result.needs_initial_pet_selection)
        self.market = self
        self.closes = 0
        self.started = False
        self.queried = False

    async def start(self, account, password):
        assert account == "private-account"
        assert password == "private-password"
        self.started = True
        if self.failure:
            raise self.failure
        return self.result

    async def iter_color_pages(self):
        self.queried = True
        yield AuctionPage(0, 1, 41, (
            AuctionQuote("private-listing-id", 1000101, 220000, None, 999, 5),
        ), "private-course", SimpleNamespace(begin_index=42))
        yield AuctionPage(0, 42, 82, (
            AuctionQuote("private-listing-id", 1000101, 220000, None, 999, 5),
            AuctionQuote("private-listing-id-2", 9999999, 0, None, None, 5),
        ), "", None)

    async def close(self):
        self.closes += 1

    async def query_system_shop(self, kind, candidates):
        assert all(row.price == 0 for row in candidates)
        if kind == "normal_pet":
            return SystemShopBatch(kind, 2, (
                SystemShopQuote(100101, 4, 4, 1000, 900, 0),
                SystemShopQuote(100101, 4, 5, 0, 900, 0),
            ))
        return SystemShopBatch(kind, 1, (
            SystemShopQuote(10031, 2, 0, 50, 50, 0),
            SystemShopQuote(10032, 2, 0, 50, 0, 0),
        ))


class ShopCatalog:
    normal_pet_mapping = {100101: "1000101"}

    def candidates(self, kind):
        pairs = ((100101, 4), (100101, 5)) if kind == "normal_pet" else ((10031, 0), (10032, 0))
        return tuple(SystemShopCandidate(config_id=key, quality=quality, price=0)
                     for key, quality in pairs)


@pytest.mark.asyncio
async def test_probe_counts_complete_pages_without_serializing_private_values():
    probe = load_probe()
    runtime = Runtime()
    report = await probe.run_probe(runtime, "private-account", "private-password",
                                   Catalog())
    assert report["game_handshake_completed"] is True
    # Until all categories have been queried the overall product is not complete.
    assert report["market_complete"] is False
    assert report["categories"]["color"] == {
        "complete": True, "pages": 2, "received_rows": 3,
        "unique_rows": 2, "named_rows": 1, "missing_price_rows": 0,
    }
    assert runtime.closes == 1
    rendered = json.dumps(report)
    for private in ("private-account", "private-password", "private-listing-id",
                    "private-course", "12345678"):
        assert private not in rendered


@pytest.mark.asyncio
async def test_probe_queries_both_shop_categories_and_keeps_quality_variants():
    probe = load_probe()
    runtime = Runtime()
    report = await probe.run_probe(runtime, "private-account", "private-password",
                                   Catalog(), ShopCatalog())
    assert report["status"] == "raw_market_quotes_observed"
    assert report["market_complete"] is True
    assert report["categories"]["normal"] == {
        "complete": True, "candidate_rows": 2, "received_rows": 2,
        "visible_price_rows": 1, "named_rows": 1,
    }
    assert report["categories"]["item"] == {
        "complete": True, "candidate_rows": 2, "received_rows": 2,
        "visible_price_rows": 1,
    }
    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_unmapped_shop_response_never_claims_complete_market():
    probe = load_probe()
    runtime = Runtime()
    async def unexpected(kind, candidates):
        return SystemShopBatch(kind, 2, (SystemShopQuote(999, 4, 4, 1, 1, 0),))
    runtime.query_system_shop = unexpected
    report = await probe.run_probe(runtime, "private-account", "private-password",
                                   Catalog(), ShopCatalog())
    assert report["status"] == "failed"
    assert report["market_complete"] is False
    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_probe_accepts_proto3_default_inner_type_on_shop_quotes():
    probe = load_probe()
    runtime = Runtime()
    original = runtime.query_system_shop
    async def no_inner_type(kind, candidates):
        batch = await original(kind, candidates)
        return SystemShopBatch(kind, batch.type, tuple(SystemShopQuote(
            row.id, 0, row.quality, row.price, row.today_init_price, row.today_init_time)
            for row in batch.quotes))
    runtime.query_system_shop = no_inner_type
    report = await probe.run_probe(runtime, "private-account", "private-password",
                                   Catalog(), ShopCatalog())
    assert report["market_complete"] is True
    assert report["status"] == "raw_market_quotes_observed"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["needs_activation", "needs_initial_pet_selection"])
async def test_probe_preserves_normal_account_gate_without_querying(flag):
    probe = load_probe()
    result = SimpleNamespace(needs_activation=False,
                             needs_initial_pet_selection=False, role_id=None)
    setattr(result, flag, True)
    runtime = Runtime(result=result)
    report = await probe.run_probe(runtime, "private-account", "private-password", Catalog())
    assert report["status"] == flag
    assert report["market_complete"] is False
    assert not runtime.queried
    assert runtime.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [
    (ApiError("AUTH_REJECTED", "private-password", 401), "AUTH_REJECTED"),
    (ValueError("private-account private-password"), "PROBE_FAILED"),
    (ApiError("private-password", "private-account", 503), "PROBE_FAILED"),
])
async def test_failure_report_allows_fixed_error_codes_only(failure, expected):
    probe = load_probe()
    runtime = Runtime(failure=failure)
    report = await probe.run_probe(runtime, "private-account", "private-password", Catalog())
    assert report["status"] == "failed"
    assert report["error_code"] == expected
    assert "private" not in json.dumps(report)
    assert runtime.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("number,expected", [(-625, -625), ("200006", 200006),
                                           ("private-server-value", None), (2**40, None)])
async def test_sdk_rejection_report_keeps_only_safe_stage_and_numeric_code(number, expected):
    from rumi_market.sdk_http import SdkResponseError
    probe = load_probe()
    runtime = Runtime(failure=SdkResponseError(response_code=number,
                                             request_stage="sdk_password"))
    report = await probe.run_probe(runtime, "private-account", "private-password", Catalog())
    assert report["status"] == "failed" and report["error_code"] == "AUTH_REJECTED"
    assert report["request_stage"] == "sdk_password"
    assert report.get("response_code") == expected
    assert "private" not in json.dumps(report)


@pytest.mark.asyncio
async def test_cancellation_closes_runtime_and_propagates():
    import asyncio
    probe = load_probe()
    runtime = Runtime(failure=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await probe.run_probe(runtime, "private-account", "private-password", Catalog())
    assert runtime.closes == 1


def test_generated_game_device_id_is_separate_and_explicitly_sourced():
    from rumi_market.sdk_device import SdkDeviceIdentity
    probe = load_probe()
    context = probe.make_client_context()
    identity = SdkDeviceIdentity.create()
    assert context.game_device_id
    assert context.game_device_id not in (identity.udid, identity.bd_id)
    assert context.device_id_source == "project_generated_uuid_for_independent_client"


def test_client_timezone_uses_standard_offset_instead_of_current_dst(monkeypatch):
    from datetime import datetime, timedelta, timezone
    probe = load_probe()
    class SummerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 1, tzinfo=timezone(timedelta(hours=2)))
        def astimezone(self, tz=None):
            return self
    monkeypatch.setattr(probe, "datetime", SummerClock)
    import time
    monkeypatch.setattr(time, "timezone", -3600)
    assert probe.make_client_context().time_zone_hours == 1


def test_cli_has_no_credential_command_line_options(capsys):
    probe = load_probe()
    parser = probe.argument_parser()
    for option in ("--account", "--password", "--token", "--access-key"):
        with pytest.raises(SystemExit):
            parser.parse_args([option, "private-password"])
    captured = capsys.readouterr()
    assert "private-password" not in captured.out + captured.err


def test_gui_input_can_be_selected_without_credential_arguments():
    probe = load_probe()
    assert probe.argument_parser().parse_args(["--gui"]).gui is True


def test_manual_captcha_cli_option_requires_no_private_values():
    probe = load_probe()
    args = probe.argument_parser().parse_args(["--gui", "--manual-captcha"])
    assert args.gui and args.manual_captcha


@pytest.mark.asyncio
async def test_main_passes_manual_handler_only_when_explicitly_selected(monkeypatch):
    import rumi_market.game.runtime as runtime_module
    probe = load_probe()
    runtime = Runtime()
    sentinel = object()
    options = {}
    def make_runtime(*args, **kwargs):
        options.update(kwargs)
        return runtime
    monkeypatch.setattr(runtime_module, "GameRuntime", make_runtime)
    monkeypatch.setattr(probe, "load_contexts", lambda path: (None, None, None))
    monkeypatch.setattr(probe, "load_manual_handler", lambda: sentinel, raising=False)
    monkeypatch.setattr(probe, "read_gui_credentials", lambda: ("private-account", "private-password"))
    await probe._main(SimpleNamespace(gui=True, manual_captcha=True, preflight=False, apk=Path("unused")))
    assert options["captcha_handler"] is sentinel


@pytest.mark.asyncio
async def test_cancelled_captcha_report_is_public_and_keeps_market_unverified():
    probe = load_probe()
    runtime = Runtime(failure=ApiError("AUTH_CAPTCHA_CANCELLED", "private-message"))
    report = await probe.run_probe(runtime, "private-account", "private-password", Catalog())
    assert report["status"] == "cancelled"
    assert report["error_code"] == "AUTH_CAPTCHA_CANCELLED"
    assert not report["game_handshake_completed"] and not report["market_complete"]
    assert "private" not in json.dumps(report)


@pytest.mark.asyncio
async def test_main_gui_branch_uses_local_form_instead_of_hidden_console(monkeypatch):
    import rumi_market.game.runtime as runtime_module
    probe = load_probe()
    runtime = Runtime()
    monkeypatch.setattr(runtime_module, "GameRuntime", lambda *args: runtime)
    monkeypatch.setattr(probe, "load_contexts", lambda path: (None, None, None))
    monkeypatch.setattr(probe, "read_gui_credentials", lambda: ("private-account", "private-password"))
    def console_would_be_wrong():
        raise AssertionError("console input should not be used")
    monkeypatch.setattr(probe, "read_credentials", console_would_be_wrong)
    report = await probe._main(SimpleNamespace(gui=True, preflight=False, apk=Path("unused")))
    assert runtime.started and runtime.closes >= 1
    assert report["game_handshake_completed"] is True
    assert "private" not in json.dumps(report)


def test_local_secret_input_never_falls_back_to_echoed_stdin(monkeypatch):
    import getpass
    import warnings
    probe = load_probe()
    def insecure_read(prompt):
        warnings.warn("cannot hide input", getpass.GetPassWarning)
        return "private-password"
    monkeypatch.setattr(getpass, "getpass", insecure_read)
    with pytest.raises(probe.ProbeInputError, match="PROBE_HIDDEN_INPUT_UNAVAILABLE"):
        probe.read_credentials()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["runtime_init", "catalog_init", "catalog_close"])
async def test_main_always_closes_owned_resources_on_startup_and_teardown_failure(monkeypatch, failure_point):
    import sys
    from types import ModuleType
    import rumi_market.game.runtime as runtime_module
    probe = load_probe()
    runtime = Runtime()
    version = SimpleNamespace(closed=False)
    async def close_version():
        version.closed = True
    version.close = close_version
    monkeypatch.setattr(probe, "load_contexts", lambda path: (None, None, version))
    module = ModuleType("rumi_market.game.shop_catalog")
    module.load_shop_catalog = lambda: ShopCatalog()
    monkeypatch.setitem(sys.modules, "rumi_market.game.shop_catalog", module)
    def make_runtime(*args):
        if failure_point == "runtime_init":
            raise ValueError("private-constructor-error")
        return runtime
    monkeypatch.setattr(runtime_module, "GameRuntime", make_runtime)
    def make_catalog(*args):
        if failure_point == "catalog_init":
            raise ValueError("private-catalog-error")
        def close():
            raise ValueError("private-close-error")
        return SimpleNamespace(close=close)
    monkeypatch.setattr(probe, "Catalog", make_catalog)
    def reject_input():
        raise probe.ProbeInputError("PROBE_EMPTY_CREDENTIALS")
    monkeypatch.setattr(probe, "read_credentials", reject_input)
    with pytest.raises(ValueError):
        await probe._main(SimpleNamespace(preflight=False, apk=Path("unused.apk")))
    if failure_point == "runtime_init":
        assert version.closed
    else:
        assert runtime.closes == 1
