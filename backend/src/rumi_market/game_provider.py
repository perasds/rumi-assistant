"""Session-owned game browsing and explicitly gated native auction purchases.

The default app still uses UnverifiedGameProvider. Enabling this adapter's
account capability requires an explicit successful live-login verification.
The full diagnostic path checks auction details at query time. Ranked color
pages preserve native Brief prices without asserting purchase eligibility.
Source shop offers remain distinct from auction instances. Purchase execution
keeps Watch active through the ACK and rechecks stop and balance before send.
"""

import asyncio
import secrets
from datetime import datetime, timezone

from .errors import ApiError, provider_error
from .game.auction_execution import GameAuctionExecution, purchase_error
from .game.home_operations import GameHomeOperations
from .game.market_query import AuctionCursor
from .models import Capabilities, Category, PersonalInfoView, Role, SessionView
from .providers import MarketPage, ProviderListing
from .sdk_saved_login import copy_login_state


class IndependentGameProvider:
    def __init__(self, runtime, shop_catalog, *, login_verified: bool = False,
                 item_names=None):
        if type(login_verified) is not bool:
            raise ValueError("Invalid login verification flag")
        self._runtime = runtime
        self._shop = shop_catalog
        self._verified = login_verified
        self._item_names = dict(item_names or {})
        self._role = None
        self._game_role_id = None
        self._closed = False
        self._cursors = {}
        self._query_lock = asyncio.Lock()
        self._purchase_lock = asyncio.Lock()
        self._purchase_blocked = False
        self._home_operations = None
        self._order_operations = None
        self._order_production_operations = None

    @property
    def connection_alive(self) -> bool:
        return not self._closed and self._role is not None and self._runtime.can_query

    @property
    def gold_balance(self) -> int | None:
        return None if self._closed else getattr(self._runtime, "gold_balance", None)

    @property
    def _version_warning(self) -> str | None:
        warning = getattr(self._runtime, 'version_warning', None)
        return warning if type(warning) is str else None

    @property
    def server_time_seconds(self) -> int | None:
        return None if self._closed else self._runtime.server_time_seconds

    @property
    def season_snapshot(self) -> dict:
        if self._closed or not self._runtime.can_query:
            return {'valid': False, 'current_season_id': None}
        return self._runtime.season_snapshot

    @property
    def order_identity(self):
        result = getattr(self._runtime, 'result', None)
        if result is None or not self._runtime.can_query or self._closed:
            return None
        return (str(result.zone_id), str(result.role_id))

    @property
    def redbag_identity(self):
        return self.order_identity if self.connection_alive else None

    @property
    def redbag_available(self) -> bool:
        return bool(self.connection_alive and getattr(self._runtime, 'redbag_available', False))

    async def redbag_start(self):
        self._require_session()
        await self._runtime.redbag_operations.start()

    async def redbag_next(self):
        self._require_session()
        return await self._runtime.redbag_operations.next()

    async def redbag_claim(self, candidate, *, stop_requested):
        self._require_session()
        return await self._runtime.redbag_operations.claim(candidate, stop_requested=stop_requested)

    async def redbag_reconcile(self, candidate):
        self._require_session()
        return await self._runtime.redbag_operations.reconcile(candidate)

    async def redbag_stop(self):
        await self._runtime.redbag_stop()

    async def personal_info(self) -> PersonalInfoView:
        self._require_session()
        try:
            balance = self.gold_balance
        except Exception:
            balance = None
        self._require_session()
        return PersonalInfoView(
            role_id=str(self._game_role_id), nickname=None,
            server_name=self._role.server_name,
            balance_gold=str(balance) if type(balance) is int and 0 <= balance < 2**64 else None,
        )

    async def home_snapshot(self) -> dict:
        self._require_session()
        state = self._runtime.home_state
        if state is None:
            raise ApiError('HOME_STATE_UNAVAILABLE', '家园实时数据暂不可用', 503)
        snapshot = state.snapshot()
        self._require_session()
        return snapshot

    def _order_snapshot_now(self) -> dict:
        self._require_session()
        result = self._runtime.result
        home = self._runtime.home_state.snapshot() if self._runtime.home_state else {'valid': False}
        capacity_state = getattr(self._runtime, 'home_capacity_state', None)
        capacity = capacity_state.snapshot() if capacity_state else {'valid': False}
        home['capacity_state'] = capacity
        if capacity.get('valid') and type(capacity.get('bonus')) is int:
            from .home_work_config import HomeWorkConfig
            config = HomeWorkConfig()
            for building in home.get('architectures', []):
                level = config.level(building['id'], building.get('level'))
                stored = building.get('store_items')
                if level is not None and stored is not None:
                    building['capacity_remaining'] = max(0, level['capacity'] + capacity['bonus'] - sum(item['count'] for item in stored))
        return {'role_key': (str(result.zone_id), str(result.role_id)),
                'server_time_seconds': self._runtime.server_time_seconds,
                'orders': self._runtime.order_state.snapshot() if self._runtime.order_state else {'valid': False},
                'inventory': self._runtime.inventory_state.snapshot() if self._runtime.inventory_state else {'valid': False},
                'home': home}

    async def order_snapshot(self) -> dict:
        return self._order_snapshot_now()

    async def apply_order_operation(self, operation, *, expected_revision, stop_requested):
        from .game.order_operations import GameOrderOperations
        self._require_session()
        if operation.get('action') in {'produce', 'cancel_production'}:
            return await self._apply_order_production(operation, expected_revision, stop_requested)
        if self._order_operations is None:
            self._order_operations = GameOrderOperations(self._runtime.home_client, self._order_snapshot_now,
                can_write=lambda: not self._closed and self._runtime.can_query)
        return await self._order_operations.apply(operation, expected_revision=expected_revision, stop_requested=stop_requested)

    async def _apply_order_production(self, operation, revision, stopped):
        from .home_management import configuration
        from .order_service import order_revision
        if self._order_production_operations is not None and self._order_production_operations.blocked:
            return {'status': 'uncertain', 'rewards': []}
        attempted = False
        try:
            cancelling = operation['action'] == 'cancel_production'
            fields = {'action', 'building_uid', 'building_id', 'recipe_id'}
            if set(operation) != (fields if cancelling else fields | {'recipe_times'}):
                raise ValueError()
            for name in ('building_uid', 'building_id', 'recipe_id'):
                value = operation[name]
                if (type(value) is not str or not value.isascii() or not value.isdecimal()
                        or len(value) > 10 or value != str(int(value)) or not 0 < int(value) < 1 << 31):
                    raise ValueError()
            if not cancelling and (type(operation['recipe_times']) is not int or not 0 <= operation['recipe_times'] <= 1000):
                raise ValueError()
            def can_write():
                return (not self._closed and self._runtime.can_query and stopped() is False
                        and order_revision(self._order_snapshot_now()) == revision)
            if not can_write():
                raise ValueError()
            raw = self._order_snapshot_now()
            original = next(row for row in raw['home']['architectures'] if str(row['uid']) == operation['building_uid'])
            # Ordinary starts stay idle-only; explicit cancellation identifies the current recipe.
            expected_recipe = int(operation['recipe_id']) if cancelling else 0
            if (original.get('work_old') or {}).get('recipe_id') != expected_recipe:
                raise ValueError()
            ops = GameHomeOperations(self._runtime.home_client, self._runtime.home_state, can_write=can_write)
            self._order_production_operations = ops
            change = {'building_uid': int(operation['building_uid']), 'building_id': int(operation['building_id']),
                'recipe_id': int(operation['recipe_id'])}
            if not cancelling:
                change['recipe_times'] = operation['recipe_times']
            attempted = True
            method = ops.cancel if cancelling else ops.apply
            result = await method(change, expected=configuration(original))
            status = result.get('status')
            return {'status': 'applied' if status == 'applied' else 'uncertain' if status in {'uncertain', 'partial'} else 'rejected', 'rewards': []}
        except asyncio.CancelledError:
            raise
        except Exception:
            return {'status': 'uncertain' if attempted else 'rejected', 'rewards': []}

    async def reconcile_order_operations(self, *, expected_revision, require_orders=True):
        from .order_service import order_revision
        raw = self._order_snapshot_now()
        required = ('orders', 'inventory', 'home') if require_orders else ('inventory', 'home')
        if (order_revision(raw) != expected_revision
                or any(raw[name].get('valid') is not True for name in required)):
            raise ApiError('ORDER_STATE_CHANGED', '账号状态已变化，请重新核对', 409)
        # Only called after explicit user reconciliation. Do not replay old bytes.
        self._order_operations = None
        self._order_production_operations = None
        self._home_operations = None

    async def apply_home_change(self, change: dict, *, expected: dict) -> dict:
        self._require_session()
        if self._home_operations is None:
            state = self._runtime.home_state
            if state is None:
                raise ApiError('HOME_STATE_UNAVAILABLE', '家园实时数据暂不可用', 503)
            self._home_operations = GameHomeOperations(self._runtime.home_client, state,
                can_write=lambda: not self._closed and self._runtime.can_query)
        return await self._home_operations.apply(change, expected=expected)

    async def purchase_page(self, cursor) -> MarketPage:
        """Use independent native cursors without modifying the GUI cursor table."""
        async with self._purchase_lock:
            self._require_session()
            if cursor is not None and type(cursor) is not AuctionCursor:
                raise ApiError("INVALID_MARKET_PAGE", "商品分页游标已失效")
            failed = cancelled = False
            try:
                page = await self._runtime.market.query_color_page(cursor, page_window=40)
                self._require_session()
                rows = []
                for quote in page.quotes:
                    if (type(quote.uid) is not str or not quote.uid
                            or type(quote.lumi_id) is not int or not 0 < quote.lumi_id < 1 << 31
                            or type(quote.fixed_price) is not int
                            or not 0 <= quote.fixed_price < 1 << 63):
                        raise ValueError
                    rows.append(ProviderListing(quote.uid, "color", str(quote.lumi_id), quote.fixed_price))
                return MarketPage(rows, next_cursor=page.next_cursor, complete=page.next_cursor is None)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                failed = True
            if cancelled:
                raise asyncio.CancelledError
            if failed:
                self._require_session()
                raise ApiError("UPSTREAM_UNAVAILABLE", "商品查询暂不可用，本轮未完成", retryable=True)

    async def purchase_color(self, row, *, max_price_gold, min_balance_gold, stop_requested):
        async with self._purchase_lock:
            self._require_session()
            if self._purchase_blocked:
                raise purchase_error("PURCHASE_OUTCOME_UNKNOWN")

            def stopped():
                return self._closed or not self._runtime.can_query or stop_requested()

            failure = None
            try:
                return await GameAuctionExecution(self._runtime, self._game_role_id).execute(
                    row, max_price_gold=max_price_gold, min_balance_gold=min_balance_gold,
                    stop_requested=stopped if callable(stop_requested) else stop_requested)
            except ApiError as error:
                if error.code == "PURCHASE_OUTCOME_UNKNOWN":
                    self._purchase_blocked = True
                failure = ApiError(error.code, error.message, error.status_code)
            raise failure

    def capabilities(self) -> Capabilities:
        if not self._verified:
            return Capabilities()
        return Capabilities(integration_ready=True, methods=["account", "sms"],
                            reason_code=None, message=None)

    async def login(self, account: str, password: str) -> SessionView:
        try:
            return await self._login(account, password)
        finally:
            account = password = ""

    async def send_sms(self, mobile: str, country_code: str) -> None:
        failure, cancelled = None, False
        try:
            if not self._verified:
                raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")
            if self._closed or self._role is not None:
                raise ApiError("AUTH_UPSTREAM_FAILED", "游戏认证服务暂不可用", retryable=True)
            await self._runtime.send_sms(mobile, country_code)
            if self._closed:
                raise ApiError("AUTH_UPSTREAM_FAILED", "游戏认证服务暂不可用", retryable=True)
        except ApiError as error:
            failure = provider_error(error)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            failure = ApiError("AUTH_UPSTREAM_FAILED", "游戏认证服务暂不可用", retryable=True)
        finally:
            mobile = country_code = ""
        if failure is not None or cancelled:
            await self.close()
            if cancelled:
                raise asyncio.CancelledError()
            raise failure

    async def login_sms(self, mobile: str, country_code: str, otp: str) -> SessionView:
        try:
            return await self._login("", "", sms_login=(mobile, country_code, otp))
        finally:
            mobile = country_code = otp = ""

    def export_login_state(self) -> dict | None:
        if self._closed or self._role is None or not self._runtime.can_query:
            return None
        export = getattr(self._runtime, "export_login_state", None)
        state = export() if callable(export) else None
        return copy_login_state(state) if state is not None else None

    async def restore_login(self, state: dict) -> SessionView:
        try:
            return await self._login("", "", login_state=copy_login_state(state))
        finally:
            state = None

    async def _login(self, account: str, password: str, *, login_state=None, sms_login=None) -> SessionView:
        if not self._verified:
            raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")
        if self._closed or self._role is not None:
            raise ApiError("AUTH_UPSTREAM_FAILED", "游戏认证服务暂不可用", retryable=True)
        failure, retry_sms = None, False
        try:
            if sms_login is not None:
                result = await self._runtime.start_sms(*sms_login)
            elif login_state is None:
                result = await self._runtime.start(account, password)
            else:
                result = await self._runtime.restore_login(login_state)
        except ApiError as error:
            retry_sms = sms_login is not None and error.code == "AUTH_SMS_REJECTED"
            failure = provider_error(error)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            failure = ApiError("AUTH_UPSTREAM_FAILED", "游戏认证服务暂不可用", retryable=True)
        finally:
            account = password = ""
            login_state = None
            sms_login = None
        if failure is not None:
            if not retry_sms:
                await self.close()
            raise failure
        if result.needs_activation or result.needs_initial_pet_selection:
            message = ("请在官方游戏中完成激活后重新登录" if result.needs_activation
                       else "请在官方游戏中完成初始宠物选择后重新登录")
            await self.close()
            return SessionView(status="needs_verification", message=message)
        if (not self._runtime.can_query or type(result.role_id) is not int
                or not 0 < result.role_id < 1 << 64):
            await self.close()
            raise ApiError("AUTH_UPSTREAM_FAILED", "尚未取得可查询的游戏角色", retryable=True)
        # The handshake supplies a role ID and zone name, but no nickname.
        # Only a new application-owned role key crosses the browser boundary.
        self._role = Role(id=secrets.token_urlsafe(24), name="", server_name=result.zone_name)
        self._game_role_id = result.role_id
        return SessionView(status="connected", authenticated=True, role=self._role,
                           roles=[self._role], message=self._version_warning)

    async def start_qr(self):
        raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "扫码登录协议尚未接通")

    async def poll_qr(self, handle):
        raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "扫码登录协议尚未接通")

    async def select_role(self, role_id: str) -> SessionView:
        self._require_session()
        if role_id != self._role.id:
            raise ApiError("ROLE_REQUIRED", "未取得该游戏角色", 409)
        return SessionView(status="connected", authenticated=True, role=self._role,
                           roles=[self._role], message=self._version_warning)

    def _require_session(self):
        if self._closed or self._role is None or not self._runtime.can_query:
            raise ApiError("GAME_SESSION_EXPIRED", "游戏会话已过期，请重新登录", 401)

    def _next(self, category, phase, cursor=None):
        key = secrets.token_urlsafe(24)
        self._cursors[key] = (category, phase, cursor)
        return key

    async def _color_listing(self, quote):
        if (type(quote.uid) is not str or not quote.uid or type(quote.lumi_id) is not int
                or quote.lumi_id <= 0):
            raise ValueError("Invalid auction identity")
        detail = await self._runtime.market.query_auction_detail(quote.uid)
        self._require_session()
        if detail is None:
            return None
        if (detail.uid != quote.uid or detail.lumi_id != quote.lumi_id
                or type(detail.is_sold_out) is not bool
                or type(detail.sell_role_id) is not int or not 0 < detail.sell_role_id < 1 << 64
                or any(type(value) is not int or not 0 <= value < 1 << 63 for value in (
                    detail.fixed_price, detail.up_time, detail.expire_seconds, detail.extern_seconds))):
            raise ValueError("Incomplete auction detail")
        deadline = detail.up_time + detail.expire_seconds + detail.extern_seconds
        now = self._runtime.server_time_seconds
        if (deadline >= 1 << 63 or type(now) is not int or not 0 < now < 1 << 63):
            raise ValueError("Invalid auction clock")
        if detail.is_sold_out or detail.sell_role_id == self._game_role_id or now >= deadline:
            return None
        return ProviderListing(quote.uid, "color", str(quote.lumi_id), detail.fixed_price,
                               expires_at=datetime.fromtimestamp(deadline, timezone.utc))

    async def fetch_ranked_color_page(self, cursor) -> MarketPage:
        """Read one native price-ordered Brief page without opening a watch."""
        async with self._query_lock:
            self._require_session()
            if cursor is None:
                self._cursors.clear()
                native_cursor = None
            else:
                state = self._cursors.get(cursor) if type(cursor) is str else None
                if state is None or state[0] != "ranked_color":
                    raise ApiError("INVALID_MARKET_PAGE", "商品分页游标已失效")
                self._cursors.pop(cursor)
                native_cursor = state[2]
            cancelled = False
            try:
                page = await self._runtime.market.query_color_page(native_cursor, page_window=10)
                self._require_session()
                rows = []
                for quote in page.quotes:
                    if (type(quote.uid) is not str or not quote.uid
                            or type(quote.lumi_id) is not int or not 0 < quote.lumi_id < 1 << 31
                            or type(quote.fixed_price) is not int
                            or not 0 <= quote.fixed_price < 1 << 63):
                        raise ValueError("Incomplete auction brief")
                    # Brief expiry units are unknown; preserve the source order and price only.
                    rows.append(ProviderListing(quote.uid, "color", str(quote.lumi_id),
                                                quote.fixed_price))
                next_cursor = (self._next("ranked_color", 0, page.next_cursor)
                               if page.next_cursor is not None else None)
                return MarketPage(rows, next_cursor=next_cursor, complete=next_cursor is None)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                pass
            if cancelled:
                raise asyncio.CancelledError
            self._require_session()
            raise ApiError("UPSTREAM_UNAVAILABLE", "商品查询暂不可用，本轮未完成", retryable=True)

    async def fetch_page(self, category: Category, cursor) -> MarketPage:
        async with self._query_lock:
            self._require_session()
            if type(category) is not str or category not in ("all", "color", "normal", "item"):
                raise ApiError("INVALID_MARKET_PAGE", "商品类别不合法")
            if cursor is None:
                self._cursors.clear()
                phase, native_cursor = (0 if category in ("all", "color")
                                        else 1 if category == "normal" else 2), None
            else:
                state = self._cursors.get(cursor) if type(cursor) is str else None
                if state is None or state[0] != category:
                    raise ApiError("INVALID_MARKET_PAGE", "商品分页游标已失效")
                self._cursors.pop(cursor)
                _, phase, native_cursor = state
            failed = False
            try:
                if phase == 0:
                    page = await self._runtime.market.query_color_page(native_cursor)
                    self._require_session()
                    rows = []
                    for quote in page.quotes:
                        row = await self._color_listing(quote)
                        if row is not None:
                            rows.append(row)
                    if page.next_cursor is not None:
                        next_cursor = self._next(category, 0, page.next_cursor)
                    else:
                        next_cursor = self._next(category, 1) if category == "all" else None
                else:
                    kind = "normal_pet" if phase == 1 else "item"
                    batch = await self._runtime.market.query_system_shop(kind, self._shop.candidates(kind))
                    self._require_session()
                    rows = []
                    for quote in batch.quotes:
                        if quote.price <= 0 or quote.today_init_price <= 0:
                            continue
                        if phase == 1:
                            pet_id = self._shop.normal_pet_mapping.get(quote.id)
                            if pet_id is None:
                                raise ValueError("Unmapped normal pet")
                            rows.append(ProviderListing(f"normal:{quote.id}:{quote.quality}",
                                                        "normal", str(pet_id), quote.price))
                        else:
                            rows.append(ProviderListing(f"item:{quote.id}", "item", None,
                                                        quote.price, item_name=self._item_names.get(quote.id),
                                                        item_id=str(quote.id)))
                    next_cursor = self._next(category, 2) if category == "all" and phase == 1 else None
                return MarketPage(rows, next_cursor=next_cursor, complete=next_cursor is None)
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
            if failed:
                # Never expose raw response objects or SDK exception text.
                self._require_session()
                raise ApiError("UPSTREAM_UNAVAILABLE", "商品查询暂不可用，本轮未完成", retryable=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cursors.clear()
        self._role = None
        self._game_role_id = None
        failed = False
        try:
            await self._runtime.close()
        except Exception:
            failed = True
        if failed:
            raise ApiError("UPSTREAM_UNAVAILABLE", "游戏连接关闭失败", retryable=True)
