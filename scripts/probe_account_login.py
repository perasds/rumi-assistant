"""Local normal-login probe; credentials and raw responses are never persisted.

This diagnostic is not the production provider. Its generated game device ID
is an explicit identity of this independent client, not the official BUVID
algorithm or evidence that a server accepts it. No purchase requests are sent.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import importlib.util
import json
from pathlib import Path
import platform
import time
import uuid
import warnings

from rumi_market.catalog import Catalog
from rumi_market.game.profile import ProjectClientContext
from rumi_market.game.client import GameClientError
from rumi_market.sdk_http import SDK_REQUEST_STAGES, SdkResponseError
from rumi_market.material_paths import resolve_apk_path


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_APK = resolve_apk_path()
SAFE_CODES = frozenset({
    "AUTH_REJECTED", "AUTH_UPSTREAM_FAILED", "AUTH_PROTOCOL_UNVERIFIED",
    "GAME_TRANSPORT_UNAVAILABLE", "GAME_TRANSPORT_TIMEOUT", "GAME_TRANSPORT_CLOSED",
    "GAME_REQUEST_REJECTED", "GAME_REQUEST_TIMEOUT", "GAME_CLIENT_CLOSED",
    "GAME_RUNTIME_QUERY_UNAVAILABLE", "GAME_PROFILE_INVALID",
    "GAME_PROFILE_VERSION_UNSUPPORTED", "GAME_MARKET_ACK_MISMATCH",
    "GAME_MARKET_DATA_INCOMPLETE", "PROBE_TIMEOUT", "PROBE_HIDDEN_INPUT_UNAVAILABLE",
    "GAME_RUNTIME_IDENTITY_MISMATCH", "GAME_RUNTIME_PROTOCOL_INVALID",
    "GAME_RUNTIME_FAILED", "GAME_RUNTIME_CLOSE_FAILED", "GAME_PROTOCOL_INVALID",
    "GAME_NOTIFICATION_TIMEOUT", "GAME_RUNTIME_CLOSED", "PROBE_EMPTY_CREDENTIALS",
    "PROBE_CREDENTIAL_INPUT_CANCELLED",
    "PROBE_CREDENTIAL_INPUT_UNAVAILABLE",
    "AUTH_CAPTCHA_CANCELLED", "AUTH_CAPTCHA_FAILED", "AUTH_CAPTCHA_INVALID",
    "AUTH_CAPTCHA_TIMEOUT", "AUTH_CAPTCHA_UNAVAILABLE", "AUTH_CAPTCHA_PAGE_UNAVAILABLE",
})


class ProbeInputError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def make_client_context() -> ProjectClientContext:
    return ProjectClientContext(
        game_device_id=str(uuid.uuid4()),
        device_id_source="project_generated_uuid_for_independent_client",
        model="RumiMarket Python client", os_version=platform.system() + " " + platform.release(),
        network="unknown", time_zone_hours=int(-time.timezone / 3600),
        language_type=1, platform="Android",
    )


def _safe_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    return code if type(code) is str and code in SAFE_CODES else "PROBE_FAILED"


async def run_probe(runtime, account: str, password: str, catalog: Catalog,
                    shop_catalog=None) -> dict:
    """Own and close this runtime; return public aggregates, never raw identity.

    Colour rows are raw quotes. Neither a price nor this report proves purchase
    eligibility. Full market completion also requires normal/item queries.
    """
    report = {"observed_at": datetime.now(timezone.utc).isoformat(),
              "status": "failed", "game_handshake_completed": False,
              "market_complete": False, "categories": {}}
    try:
        result = await runtime.start(account, password)
        account = password = ""
        report["game_handshake_completed"] = True
        for flag in ("needs_activation", "needs_initial_pet_selection"):
            if getattr(result, flag):
                report["status"] = flag
                return report
        if not runtime.can_query:
            report["error_code"] = "GAME_RUNTIME_QUERY_UNAVAILABLE"
            return report
        counts = {"complete": False, "pages": 0, "received_rows": 0,
                  "unique_rows": 0, "named_rows": 0, "missing_price_rows": 0}
        report["categories"]["color"] = counts
        seen = set()
        async for page in runtime.market.iter_color_pages():
            counts["pages"] += 1
            counts["received_rows"] += len(page.quotes)
            for row in page.quotes:
                if not row.uid or row.lumi_id is None:
                    raise ProbeInputError("GAME_MARKET_DATA_INCOMPLETE")
                if row.uid in seen:
                    continue
                seen.add(row.uid)
                counts["unique_rows"] += 1
                counts["named_rows"] += bool(catalog.pet(str(row.lumi_id)).name)
                counts["missing_price_rows"] += row.fixed_price is None
            if page.next_cursor is None:
                counts["complete"] = True
        seen.clear()
        report["status"] = "raw_color_quotes_observed"
        if shop_catalog is not None:
            for kind, category in (("normal_pet", "normal"), ("item", "item")):
                candidates = shop_catalog.candidates(kind)
                batch = await runtime.market.query_system_shop(kind, candidates)
                expected = {(row.config_id, row.quality) for row in candidates}
                if any((row.id, row.quality) not in expected for row in batch.quotes):
                    raise ProbeInputError("GAME_MARKET_DATA_INCOMPLETE")
                visible = [row for row in batch.quotes if row.price > 0
                           and (kind == "normal_pet" or row.today_init_price > 0)]
                stats = {"complete": True, "candidate_rows": len(candidates),
                         "received_rows": len(batch.quotes), "visible_price_rows": len(visible)}
                if kind == "normal_pet":
                    stats["named_rows"] = sum(bool(catalog.pet(
                        str(shop_catalog.normal_pet_mapping[row.id])).name) for row in visible)
                report["categories"][category] = stats
            report["status"] = "raw_market_quotes_observed"
            report["market_complete"] = counts["complete"] and not counts["missing_price_rows"]
    except asyncio.CancelledError:
        raise
    except Exception as error:
        report["error_code"] = _safe_code(error)
        report["status"] = "cancelled" if report["error_code"] == "AUTH_CAPTCHA_CANCELLED" else "failed"
        if type(error) is SdkResponseError:
            if error.request_stage in SDK_REQUEST_STAGES:
                report["request_stage"] = error.request_stage
        if type(error) in (SdkResponseError, GameClientError):
            number = error.response_code
            if type(number) is int and -(2**31) <= number < 2**31:
                report["response_code"] = number
    finally:
        account = password = ""
        try:
            await runtime.close()
        except Exception:
            report["status"] = "failed"
            report["market_complete"] = False
            report["error_code"] = "PROBE_CLOSE_FAILED"
    return report


def read_credentials() -> tuple[str, str]:
    # getpass normally warns and falls back to echoed stdin. This probe forbids it.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            account = getpass.getpass("游戏账号（隐藏输入）: ")
            password = getpass.getpass("游戏密码（隐藏输入）: ")
    except (getpass.GetPassWarning, EOFError):
        raise ProbeInputError("PROBE_HIDDEN_INPUT_UNAVAILABLE") from None
    if not account or not password:
        raise ProbeInputError("PROBE_EMPTY_CREDENTIALS")
    return account, password


def read_gui_credentials() -> tuple[str, str]:
    location = PROJECT / "scripts/local_credential_form.py"
    spec = importlib.util.spec_from_file_location("local_credential_form", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_local_credentials()


def load_manual_handler():
    location = PROJECT / "scripts/manual_captcha.py"
    spec = importlib.util.spec_from_file_location("local_manual_captcha", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.manual_captcha


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error includes rejected argument values.
        self.print_usage()
        self.exit(2, "参数不合法，请使用 --help 查看支持项。\n")


def argument_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description="本机正常登录与只读市场查询验证")
    parser.add_argument("--apk", type=Path, default=DEFAULT_APK)
    parser.add_argument("--report", type=Path,
                        default=PROJECT.parent / "artifacts/rumi/account-probe.sanitized.json")
    parser.add_argument("--preflight", action="store_true", help="只验证公开版本和 SDK 配置，不登录")
    parser.add_argument("--gui", action="store_true", help="使用可核对账号和密码的本机输入表单")
    parser.add_argument("--manual-captcha", action="store_true", help="按需打开官方页面并人工完成验证码")
    return parser


def load_contexts(apk: Path):
    from rumi_market.game.version import VersionHttpClient, VersionRequestContext
    from rumi_market.game.version_assets import load_version_assets
    from rumi_market.sdk_parameters import SdkRequestContext
    sdk = SdkRequestContext.from_apk(apk)
    assets = load_version_assets(apk)
    client = make_client_context()
    version = VersionHttpClient(assets.origins[0], VersionRequestContext(
        gm_secret=assets.gm_secret, identity=sdk.identity, device_id=client.game_device_id,
        channel=assets.channel, version=assets.version, build_version=assets.build_version,
        profile_source=assets.profile_source + ";" + client.device_id_source,
    ))
    return sdk, client, version


async def preflight(apk: Path) -> dict:
    from rumi_market.sdk_http import SdkHttpClient
    sdk, _, version_client = load_contexts(apk)
    config_client = None
    try:
        version = await version_client.fetch()
        config_client = SdkHttpClient("https://line3-sdk-center-login-sh.biligame.net",
                                     sdk.app_config.app_key.get_secret_value(),
                                     common_parameters=sdk.common_parameters,
                                     extra_headers=sdk.extra_headers, purpose="config")
        configured = sdk.with_config(await config_client.fetch_config())
        return {"observed_at": datetime.now(timezone.utc).isoformat(),
                "status": "public_preflight_passed", "version": version.version,
                "hot_version": version.hot_version, "sdk_config_verified": True,
                "rule_version": configured.rule_version, "game_handshake_completed": False,
                "market_complete": False}
    finally:
        try:
            await version_client.close()
        finally:
            if config_client is not None:
                await config_client.close()


async def _main(args) -> dict:
    if args.preflight:
        return await preflight(args.apk)
    from rumi_market.game.runtime import GameRuntime
    from rumi_market.game.shop_catalog import load_shop_catalog
    from rumi_market.game.transport import GameTransport
    shop_catalog = load_shop_catalog()
    version = runtime = catalog = None
    try:
        sdk, client, version = load_contexts(args.apk)
        handler = load_manual_handler() if getattr(args, "manual_captcha", False) else None
        options = {"captcha_handler": handler} if handler is not None else {}
        runtime = GameRuntime(sdk, version, client, GameTransport(), **options)
        catalog = Catalog(PROJECT / "data/pets/catalog.json")
        print("此窗口执行正常账号登录和只读查询。账号密码不写入报告；没有购买操作。")
        if getattr(args, "gui", False):
            account, password = read_gui_credentials()
        else:
            print("输入不会显示文字或星号：先输入账号按回车，再输入密码按回车。")
            account, password = read_credentials()
        async with asyncio.timeout(300):
            report = await run_probe(runtime, account, password, catalog, shop_catalog)
            observation = getattr(handler, "observation", None)
            if type(observation) is dict:
                report["captcha_callback"] = observation
            return report
    finally:
        account = password = ""
        try:
            if catalog is not None:
                catalog.close()
        finally:
            if runtime is not None:
                await runtime.close()
            elif version is not None:
                await version.close()


def main() -> int:
    args = argument_parser().parse_args()
    report = None
    try:
        report = asyncio.run(_main(args))
    except KeyboardInterrupt:
        report = {"status": "cancelled", "market_complete": False}
    except TimeoutError:
        report = {"status": "failed", "error_code": "PROBE_TIMEOUT", "market_complete": False}
    except Exception as error:
        code = _safe_code(error)
        report = {"status": "cancelled" if code == "PROBE_CREDENTIAL_INPUT_CANCELLED" else "failed",
                  "error_code": code, "market_complete": False}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if report["status"] in ("failed", "cancelled") else 0


if __name__ == "__main__":
    raise SystemExit(main())
