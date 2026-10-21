import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from .models import SessionView
from .login_encryption import LoginChallenge
from .providers import GameProvider, QrChallenge

if TYPE_CHECKING:
    from .home_management import HomeRequestRecord
    from .home_models import HomeApplyResult
    from .market import RefreshState
    from .rush import RushRun
    from .web_captcha import WebCaptchaChannel
    from .order_models import OrderSettings
    from .order_service import OrderRun
    from .redbag_service import RedbagRun


@dataclass
class Session:
    id: str
    provider: GameProvider
    view: SessionView = field(default_factory=SessionView)
    active: bool = True
    qr: dict[str, QrChallenge] = field(default_factory=dict)
    login_challenge: LoginChallenge | None = field(default=None, repr=False)
    captcha: 'WebCaptchaChannel | None' = field(default=None, repr=False)
    login_in_progress: bool = field(default=False, repr=False)
    auth_task: asyncio.Task | None = field(default=None, repr=False)
    auth_challenge_id: str | None = field(default=None, repr=False)
    auth_cancel_requested: bool = field(default=False, repr=False)
    sms_next_send_at: float = field(default=0, repr=False)
    remember_token: str | None = field(default=None, repr=False)
    logout_requested: bool = field(default=False, repr=False)
    market: "RefreshState | None" = None
    rush: "RushRun | None" = None
    rush_requests: dict[str, "RushRun"] = field(default_factory=dict, repr=False)
    home_requests: dict[str, "HomeRequestRecord"] = field(default_factory=dict, repr=False)
    home_last_apply: 'HomeApplyResult | None' = field(default=None, repr=False)
    home_write_uncertain: bool = field(default=False, repr=False)
    home_revision: int = field(default=0, repr=False)
    order_settings: 'OrderSettings | None' = field(default=None, repr=False)
    order_task: 'OrderRun | None' = field(default=None, repr=False)
    order_requests: dict[str, 'OrderRun'] = field(default_factory=dict, repr=False)
    redbag_task: 'RedbagRun | None' = field(default=None, repr=False)
    redbag_requests: dict[str, 'RedbagRun'] = field(default_factory=dict, repr=False)
    redbag_seen: dict[str, int] = field(default_factory=dict, repr=False)
    tasks: set[asyncio.Task] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def cancel_refresh(self) -> None:
        if self.redbag_task:
            self.redbag_task.request_stop()
        if self.order_task:
            self.order_task.request_stop()
        if self.rush:
            self.rush.request_stop()
        if self.market:
            self.market.stop()
            self.market.raw_ids.clear()
            self.market.raw_by_listing_key.clear()
        self.market = None
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()


class SessionStore:
    def __init__(self, provider_factory: Callable[[], GameProvider]):
        self.provider_factory = provider_factory
        self.sessions: dict[str, Session] = {}
        self.web_login_lock = asyncio.Lock()

    def get(self, session_id: str | None) -> Session | None:
        return self.sessions.get(session_id) if session_id else None

    def create(self) -> Session:
        session = Session(secrets.token_urlsafe(32), self.provider_factory())
        self.sessions[session.id] = session
        return session

    async def close(self) -> None:
        for session in list(self.sessions.values()):
            await self.remove(session)

    async def close_provider(self, session: Session) -> None:
        if session.captcha is not None:
            await session.captcha.close()
            session.captcha = None
        session.cancel_refresh()
        session.qr.clear()
        session.login_challenge = None
        try:
            async with asyncio.timeout(5):
                await session.provider.close()
        except Exception:
            # Never log upstream errors that could include credentials.
            pass
        pending = [task for task in session.tasks if task is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        session.rush = None
        session.rush_requests.clear()
        session.home_requests.clear()
        session.home_last_apply = None
        session.home_write_uncertain = False
        session.home_revision = 0
        session.order_settings = None
        session.order_requests.clear()
        session.order_task = None
        session.redbag_task = None
        session.redbag_requests.clear()
        session.redbag_seen.clear()

    async def refresh_connection(self, session: Session) -> None:
        """Caller holds session.lock; inspect local state without logging in."""
        if (session.view.status == 'connected'
                and getattr(session.provider, 'connection_alive', None) is False):
            await self.expire(session)

    async def expire(self, session: Session) -> None:
        session.view = SessionView(status="expired", message="游戏会话已过期，请重新登录")
        await self.close_provider(session)

    async def remove(self, session: Session) -> None:
        self.sessions.pop(session.id, None)
        session.active = False
        session.view = SessionView()
        await self.close_provider(session)
