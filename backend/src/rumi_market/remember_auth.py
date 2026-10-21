"""Website-owned persistent login credentials; never serialize SDK state to HTTP."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import re

from anyio import CancelScope
from fastapi import Request, Response

from .errors import ApiError, provider_error
from .models import SessionView


REMEMBER_COOKIE = 'rumi_remember'
REMEMBER_SECONDS = 7 * 86400
_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _digest(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _unavailable():
    return ApiError('REMEMBER_LOGIN_UNAVAILABLE', '记住登录暂不可用，请稍后重试或取消勾选', 503, True)


@dataclass
class _Flight:
    lock: asyncio.Lock
    users: int = 0
    revoked: bool = False
    operation: asyncio.Task | None = field(default=None, repr=False)


class RememberLoginService:
    def __init__(self, sessions, store=None, *, timeout=30):
        self.sessions, self.store, self.timeout = sessions, store, timeout
        self._active = {}
        self._flights = {}

    @property
    def enabled(self):
        return self.store is not None

    async def _call(self, method, *args):
        failed = False
        result = None
        try:
            result = await asyncio.to_thread(getattr(self.store, method), *args)
        except Exception:
            failed = True
        if failed:
            raise _unavailable()
        return result

    async def _issue(self, state):
        # A cancelled await cannot stop a SQLite worker. Keep its result so a
        # late-issued credential can be revoked rather than becoming orphaned.
        task = asyncio.create_task(self._call('issue', state))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                try:
                    token = await asyncio.shield(task)
                    await self._call('revoke', token)
                except Exception:
                    pass
            raise

    def active_session(self, request):
        token = _token(request.cookies.get(REMEMBER_COOKIE))
        return self.sessions.get(self._active.get(_digest(token))) if token else None

    @asynccontextmanager
    async def _flight(self, token):
        digest = _digest(token)
        flight = self._flights.setdefault(digest, _Flight(asyncio.Lock()))
        flight.users += 1
        try:
            async with flight.lock:
                yield flight
        finally:
            flight.users -= 1
            if not flight.users:
                self._flights.pop(digest, None)

    async def forget(self, request, response, session=None):
        tokens = {_token(request.cookies.get(REMEMBER_COOKIE)),
                  _token(session.remember_token) if session is not None else None} - {None}
        if tokens and self.enabled:
            for token in tokens:
                flight = self._flights.get(_digest(token))
                if flight is not None:
                    flight.revoked = True
                await self._call('revoke', token)
                if flight is not None and flight.operation is not None:
                    flight.operation.cancel()
                self._active.pop(_digest(token), None)
        if session is not None:
            session.remember_token = None
        if tokens or REMEMBER_COOKIE in request.cookies:
            response.delete_cookie(REMEMBER_COOKIE, path='/', httponly=True,
                                   secure=request.url.scheme == 'https', samesite='strict')

    async def save(self, session, request, response, requested):
        self._check_logout(session)
        if not requested or not session.view.authenticated:
            await self.forget(request, response, session)
            self._check_logout(session)
            return
        if not self.enabled:
            raise _unavailable()
        state = None
        try:
            export = getattr(session.provider, 'export_login_state', None)
            state = export() if callable(export) else None
        except Exception:
            pass
        if not isinstance(state, dict):
            raise _unavailable()
        token = None
        try:
            token = await self._issue(state)
            await self.forget(request, response, session)
            self._check_logout(session)
        except BaseException:
            if token:
                with CancelScope(shield=True):
                    await self._call('revoke', token)
            raise
        finally:
            state = None
        session.remember_token = token
        self._active[_digest(token)] = session.id
        response.set_cookie(REMEMBER_COOKIE, token, max_age=REMEMBER_SECONDS,
                            httponly=True, secure=request.url.scheme == 'https',
                            samesite='strict', path='/')

    @staticmethod
    def _check_logout(session):
        if session.logout_requested:
            raise ApiError('AUTH_CANCELLED', '登录已取消', 409)

    async def _reset(self, session):
        await self.sessions.close_provider(session)
        session.provider = self.sessions.provider_factory()

    async def _expired(self, session, request, response):
        await self.forget(request, response, session)
        session.view = SessionView(status='expired', message='登录已过期，请重新登录')
        return session

    async def restore(self, session, request, response):
        async with session.lock:
            await self.sessions.refresh_connection(session)
            if not session.active or session.view.authenticated:
                return session
        raw_token = request.cookies.get(REMEMBER_COOKIE)
        if raw_token is None or not self.enabled:
            return session
        token = _token(raw_token)
        if token is None:
            return await self._expired(session, request, response)
        async with self._flight(token) as flight:
            async with session.lock:
                if not session.active:
                    return session
                await self.sessions.refresh_connection(session)
                if session.view.authenticated:
                    return session
                saved = await self._call('load', token)
                if saved is None:
                    return await self._expired(session, request, response)
                owner = self.active_session(request)
                if owner is not None and owner is not session:
                    async with owner.lock:
                        if flight.revoked or session.logout_requested or owner.logout_requested:
                            session.view = SessionView()
                            return session
                        await self.sessions.refresh_connection(owner)
                        if owner.active and owner.view.authenticated:
                            if await self._call('load', token) is None:
                                return await self._expired(session, request, response)
                            if flight.revoked or session.logout_requested or owner.logout_requested:
                                session.view = SessionView()
                                return session
                            await self.sessions.remove(session)
                            if flight.revoked or session.logout_requested or owner.logout_requested:
                                return session
                            return owner
                if (flight.revoked or session.logout_requested
                        or (owner is not None and owner.logout_requested)):
                    session.view = SessionView()
                    return session
                await self._reset(session)
                if await self._call('load', token) is None:
                    return await self._expired(session, request, response)
                if (flight.revoked or session.logout_requested
                        or (owner is not None and owner.logout_requested)):
                    session.view = SessionView()
                    return session
                restore = getattr(session.provider, 'restore_login', None)
                if not callable(restore):
                    raise _unavailable()
                failed = None
                try:
                    async with asyncio.timeout(self.timeout):
                        flight.operation = asyncio.create_task(restore(saved.payload))
                        view = await flight.operation
                    # Logout revokes before waiting for session.lock. A late
                    # upstream success must not revive the revoked credential.
                    if flight.revoked or not session.active or await self._call('load', token) is None:
                        await self._reset(session)
                        session.view = SessionView()
                        return session
                    if not view.authenticated:
                        await self._reset(session)
                        return await self._expired(session, request, response)
                    export = getattr(session.provider, 'export_login_state', None)
                    updated = export() if callable(export) else None
                    if not isinstance(updated, dict):
                        raise _unavailable()
                    if not await self._call('update', token, updated) or flight.revoked:
                        await self._reset(session)
                        session.view = SessionView()
                        return session
                    session.view = view
                    session.remember_token = token
                    self._active[_digest(token)] = session.id
                    if flight.revoked:
                        await self._reset(session)
                        session.view = SessionView()
                    return session
                except asyncio.CancelledError:
                    await self._reset(session)
                    session.view = SessionView()
                    if flight.revoked:
                        return session
                    raise
                except ApiError as error:
                    failed = provider_error(error)
                except Exception:
                    failed = ApiError('AUTH_UPSTREAM_FAILED', '游戏认证服务暂不可用，请稍后重试', 503, True)
                finally:
                    flight.operation = None
                    saved = updated = view = None
                await self._reset(session)
                session.view = SessionView()
                if failed.code in {'AUTH_REJECTED', 'GAME_SESSION_EXPIRED'}:
                    return await self._expired(session, request, response)
                raise failed
