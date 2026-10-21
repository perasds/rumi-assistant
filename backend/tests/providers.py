import asyncio
from datetime import datetime, timedelta, timezone

from rumi_market.models import Capabilities, QrPollView, Role, SessionView
from rumi_market.providers import MarketPage, QrChallenge


class ScriptedProvider:
    """Test-only provider: the runtime has no demo or historical-data switch."""

    def __init__(self, pages=None):
        self.pages = list(pages or [MarketPage([], complete=True)])
        self.calls = []
        self.account = None
        self.closed = False
        self.fetch_started = asyncio.Event()

    def capabilities(self):
        return Capabilities(integration_ready=True, methods=["account", "qr"],
                            reason_code=None, message=None)

    async def login(self, account, password):
        self.account = account
        role = Role(id=f"role-{account}", name=f"角色-{account}", server_name="测试区服")
        return SessionView(status="connected", authenticated=True, role=role, roles=[role])

    async def start_qr(self):
        return QrChallenge("private-upstream-task", "test-qr-content",
                           datetime.now(timezone.utc) + timedelta(minutes=2))

    async def poll_qr(self, handle):
        return QrPollView(status="waiting")

    async def select_role(self, role_id):
        return await self.login(self.account, "test")

    async def fetch_page(self, category, cursor):
        self.calls.append((category, cursor))
        self.fetch_started.set()
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    async def close(self):
        self.closed = True


class DelayedProvider(ScriptedProvider):
    def __init__(self, old_page, fresh_page):
        super().__init__()
        self.release_old = asyncio.Event()
        self.old_finished = asyncio.Event()
        self.old_page = old_page
        self.fresh_page = fresh_page

    async def fetch_page(self, category, cursor):
        self.calls.append((category, cursor))
        if len(self.calls) > 1:
            return self.fresh_page
        self.fetch_started.set()
        try:
            await self.release_old.wait()
        except asyncio.CancelledError:
            # Simulate an upstream that returns a late response after cancellation.
            await self.release_old.wait()
        self.old_finished.set()
        return self.old_page

    async def close(self):
        self.closed = True
        self.release_old.set()
