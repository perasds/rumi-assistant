from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Protocol

from .errors import ApiError
from .models import Capabilities, Category, PersonalInfoView, QrPollView, SessionView

if TYPE_CHECKING:
    from .game.market_query import AuctionCursor
    from .redbag_models import RedbagCandidate, RedbagReceipt


@dataclass(frozen=True)
class ProviderListing:
    """Listing quotes, not purchase eligibility; raw_id stays in memory."""

    raw_id: str
    category: str
    pet_id: str | None
    direct_buy_price: int
    expires_at: datetime | None = None
    item_name: str | None = None
    item_id: str | None = None


@dataclass(frozen=True)
class MarketPage:
    items: list[ProviderListing]
    next_cursor: bytes | str | AuctionCursor | None = None
    complete: bool = False
    total_count: int | None = None


@dataclass(frozen=True)
class QrChallenge:
    handle: str
    qr_payload: str
    expires_at: datetime


@dataclass(frozen=True, repr=False)
class ProviderPurchaseReceipt:
    raw_id: str
    pet_id: str
    price: int
    balance_ready: bool = True


class GameProvider(Protocol):
    @property
    def gold_balance(self) -> int | None: ...
    @property
    def season_snapshot(self) -> dict: ...
    @property
    def server_time_seconds(self) -> int | None: ...
    @property
    def redbag_identity(self) -> tuple[str, str] | None: ...
    @property
    def redbag_available(self) -> bool: ...
    async def redbag_start(self) -> None: ...
    async def redbag_next(self) -> RedbagCandidate: ...
    async def redbag_claim(self, candidate: RedbagCandidate, *, stop_requested: Callable[[], bool]) -> RedbagReceipt | None: ...
    async def redbag_reconcile(self, candidate: RedbagCandidate) -> RedbagReceipt | None: ...
    async def redbag_stop(self) -> None: ...
    def capabilities(self) -> Capabilities: ...
    async def personal_info(self) -> PersonalInfoView: ...
    async def home_snapshot(self) -> dict: ...
    async def order_snapshot(self) -> dict: ...
    async def apply_order_operation(self, operation: dict, *, expected_revision: str, stop_requested: Callable[[], bool]) -> dict: ...
    async def reconcile_order_operations(self, *, expected_revision: str, require_orders: bool = True) -> None: ...
    async def apply_home_change(self, change: dict, *, expected: dict) -> dict: ...
    async def login(self, account: str, password: str) -> SessionView: ...
    async def send_sms(self, mobile: str, country_code: str) -> None: ...
    async def login_sms(self, mobile: str, country_code: str, otp: str) -> SessionView: ...
    async def start_qr(self) -> QrChallenge: ...
    async def poll_qr(self, handle: str) -> QrPollView: ...
    async def select_role(self, role_id: str) -> SessionView: ...
    async def fetch_page(self, category: Category,
                         cursor: bytes | str | None) -> MarketPage: ...
    async def fetch_ranked_color_page(self, cursor: bytes | str | None) -> MarketPage: ...
    async def purchase_page(self, cursor: AuctionCursor | None) -> MarketPage: ...
    async def purchase_color(self, row: ProviderListing, *, max_price_gold: int,
                             min_balance_gold: int, stop_requested: Callable[[], bool]
                             ) -> ProviderPurchaseReceipt | None: ...
    async def close(self) -> None: ...


class UnverifiedGameProvider:
    """Production boundary until independent game authentication is verified."""

    @property
    def gold_balance(self) -> int | None:
        return None

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def personal_info(self) -> PersonalInfoView:
        raise self._unavailable()

    async def home_snapshot(self) -> dict:
        raise self._unavailable()

    async def apply_home_change(self, change: dict, *, expected: dict) -> dict:
        raise self._unavailable()

    @staticmethod
    def _unavailable() -> ApiError:
        return ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")

    async def login(self, account: str, password: str) -> SessionView:
        raise self._unavailable()

    async def send_sms(self, mobile: str, country_code: str) -> None:
        raise self._unavailable()

    async def login_sms(self, mobile: str, country_code: str, otp: str) -> SessionView:
        raise self._unavailable()

    async def start_qr(self) -> QrChallenge:
        raise self._unavailable()

    async def poll_qr(self, handle: str) -> QrPollView:
        raise self._unavailable()

    async def select_role(self, role_id: str) -> SessionView:
        raise self._unavailable()

    async def fetch_page(self, category: Category,
                         cursor: bytes | str | None) -> MarketPage:
        raise self._unavailable()

    async def fetch_ranked_color_page(self, cursor: bytes | str | None) -> MarketPage:
        raise self._unavailable()

    async def purchase_page(self, cursor) -> MarketPage:
        raise self._unavailable()

    async def purchase_color(self, row, *, max_price_gold, min_balance_gold,
                             stop_requested) -> ProviderPurchaseReceipt | None:
        raise self._unavailable()

    async def close(self) -> None:
        pass
