import asyncio
import secrets
from datetime import datetime, timezone
from time import monotonic

from fastapi import APIRouter, Request, Response

from .errors import ApiError, auth_required, provider_error
from .login_encryption import (consume_challenge, decrypt_login, decrypt_sms_login,
                               decrypt_sms_send, invalid_payload, new_challenge,
                               read_envelope, reject_plaintext_fields)
from .models import Capabilities, LoginChallengeView, PersonalInfoView, QrPollView, QrView, RoleRequest, SessionView
from .sessions import Session, SessionStore
from .web_captcha import WebCaptchaChannel, current_channel, not_found, read_input, web_mode

COOKIE_NAME = "rumi_session"


def set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    response.set_cookie(COOKIE_NAME, session_id, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")


def session_for(sessions: SessionStore, request: Request, response: Response) -> Session:
    session = sessions.get(request.cookies.get(COOKIE_NAME))
    if session is None:
        session = sessions.create()
        request.state.created_session = session.id
        set_session_cookie(response, request, session.id)
    return session


def require_session(sessions: SessionStore, request: Request, *, connected: bool = False) -> Session:
    session = sessions.get(request.cookies.get(COOKIE_NAME))
    if session is None or not session.active or not session.view.authenticated:
        raise auth_required()
    if connected and (session.view.status != "connected" or session.view.role is None):
        raise ApiError("ROLE_REQUIRED", "请先选择游戏角色", 409)
    return session


def require_method(session: Session, method: str) -> None:
    capabilities = session.provider.capabilities()
    if not capabilities.integration_ready or method not in capabilities.methods:
        raise ApiError("AUTH_PROTOCOL_UNVERIFIED", "游戏登录协议尚未接通")


async def provider_capabilities(sessions: SessionStore) -> Capabilities:
    provider = sessions.provider_factory()
    try:
        return provider.capabilities()
    finally:
        await provider.close()


def create_auth_router(sessions: SessionStore, *, login_timeout: float = 30) -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/capabilities", response_model=Capabilities)
    async def capabilities():
        value = await provider_capabilities(sessions)
        return value.model_copy(update={'remember_login': False})

    @router.get("/api/session", response_model=SessionView)
    async def get_session(request: Request, response: Response):
        session = session_for(sessions, request, response)
        async with session.lock:
            await sessions.refresh_connection(session)
            return session.view

    @router.get("/api/me", response_model=PersonalInfoView)
    async def get_personal_info(request: Request):
        session = require_session(sessions, request, connected=True)
        async with session.lock:
            if not session.active or not session.view.authenticated:
                raise auth_required()
            try:
                return PersonalInfoView.model_validate(await session.provider.personal_info())
            except ApiError as error:
                if error.code == "GAME_SESSION_EXPIRED":
                    await sessions.expire(session)
                raise provider_error(error) from None
            except Exception:
                raise ApiError("UPSTREAM_UNAVAILABLE", "个人信息暂不可用，请稍后刷新", 503, True) from None

    def captcha_session(request: Request) -> Session:
        session = sessions.get(request.cookies.get(COOKIE_NAME))
        if (session is None or not session.active
                or (session.view.status == 'expired' and not session.login_in_progress)):
            raise auth_required()
        return session

    def active_captcha(request: Request, challenge_id: str):
        channel = captcha_session(request).captcha
        if (channel is None or not channel.active or not challenge_id.isascii()
                or not secrets.compare_digest(channel.challenge_id, challenge_id)):
            raise not_found()
        return channel

    @router.get('/api/auth/captcha')
    async def captcha(request: Request):
        channel = captcha_session(request).captcha
        if channel is None or not channel.active:
            return {'status': 'waiting'}
        try:
            return await channel.snapshot()
        except ApiError as error:
            if error.code == 'CAPTCHA_NOT_FOUND':
                return {'status': 'waiting'}
            raise

    @router.post('/api/auth/captcha/{challenge_id}/input')
    async def captcha_input(challenge_id: str, request: Request):
        channel = active_captcha(request, challenge_id)
        body = await read_input(request)
        await channel.input(body)
        result = {'status': 'ok'}
        if body['type'] in {'click', 'pointerup'}:
            try:
                result['frame'] = await channel.snapshot()
            except ApiError as error:
                if error.code == 'CAPTCHA_NOT_FOUND':
                    result['frame'] = {'status': 'waiting'}
                elif error.status_code != 503:
                    raise
            except TimeoutError:
                # Input was acknowledged; optional frame failure must not invite a replay.
                pass
        return result

    @router.delete('/api/auth/captcha/{challenge_id}')
    async def captcha_cancel(challenge_id: str, request: Request):
        await active_captcha(request, challenge_id).close()
        return {'status': 'ok'}

    @router.get("/api/auth/login-challenge", response_model=LoginChallengeView)
    async def login_challenge(request: Request, response: Response):
        session = session_for(sessions, request, response)
        async with session.lock:
            if not session.active:
                raise auth_required()
            session.login_challenge = new_challenge()
            return session.login_challenge.view()

    @router.delete('/api/auth/pending/{challenge_id}')
    async def cancel_pending(challenge_id: str, request: Request):
        session = sessions.get(request.cookies.get(COOKIE_NAME))
        if session is not None and session.active and challenge_id.isascii():
            if session.login_challenge is not None and secrets.compare_digest(
                    session.login_challenge.challenge_id, challenge_id):
                session.login_challenge = None
            task = session.auth_task
            if (session.auth_challenge_id is not None
                    and secrets.compare_digest(session.auth_challenge_id, challenge_id)):
                session.auth_cancel_requested = True
                if task is not None and not task.done():
                    task.cancel()
        # Idempotent, and never reveal another browser's pending authentication.
        return {'status': 'ok'}

    @router.post("/api/auth/login", response_model=SessionView)
    async def login(request: Request, response: Response):
        return await encrypted_auth(request, operation='account')

    @router.post('/api/auth/sms/send')
    async def send_sms(request: Request):
        return await encrypted_auth(request, operation='sms_send')

    @router.post('/api/auth/sms/login', response_model=SessionView)
    async def login_sms(request: Request):
        return await encrypted_auth(request, operation='sms_login')

    async def encrypted_auth(request: Request, *, operation: str):
        body = credentials = None
        web_slot = False
        operation_started = False
        session = None
        try:
            body = await read_envelope(request)
            session = sessions.get(request.cookies.get(COOKIE_NAME))
            if session is None:
                reject_plaintext_fields(body)
                raise invalid_payload()
            async with session.lock:
                if not session.active or session.logout_requested:
                    raise auth_required()
                challenge = session.login_challenge
                if challenge is not None and body.get("challenge_id") == challenge.challenge_id:
                    # Consume matching attempts even when later crypto/shape checks fail.
                    session.login_challenge = None
                reject_plaintext_fields(body)
                consume_challenge(challenge, body.get("challenge_id"))
                if operation == 'account':
                    credentials = decrypt_login(body, allow_remember=True)
                elif operation == 'sms_send':
                    credentials = decrypt_sms_send(body)
                else:
                    credentials = decrypt_sms_login(body)
                if operation == 'sms_send':
                    if session.view.authenticated:
                        raise ApiError('AUTH_ALREADY_CONNECTED', '当前账号已登录', 409)
                    if monotonic() < session.sms_next_send_at:
                        raise ApiError('AUTH_SMS_COOLDOWN', '请稍后再获取验证码', 429)
                if web_mode():
                    if sessions.web_login_lock.locked():
                        raise ApiError('AUTH_LOGIN_BUSY', '已有登录正在处理，请稍后重试', 429, retryable=True)
                    await sessions.web_login_lock.acquire()
                    web_slot = True
                session.login_in_progress = True
                operation_started = True
                session.auth_challenge_id = challenge.challenge_id
                session.auth_cancel_requested = False
                if session.view.authenticated or session.view.status == "expired":
                    await sessions.close_provider(session)
                    session.provider = sessions.provider_factory()
                if session.auth_cancel_requested:
                    session.view = SessionView()
                    raise ApiError('AUTH_LOGIN_CANCELLED', '已取消登录', 401)
                require_method(session, 'account' if operation == 'account' else 'sms')
                session.view = SessionView(status="authenticating")
                failure, cancelled = None, False
                channel = WebCaptchaChannel() if web_slot else None
                token = current_channel.set(channel) if channel is not None else None
                session.captcha = channel
                async def authenticate():
                    if operation == 'account':
                        return await session.provider.login(credentials.account, credentials.password.get_secret_value())
                    if operation == 'sms_send':
                        # Also limit uncertain send attempts; never automatically resend a text.
                        session.sms_next_send_at = monotonic() + 60
                        await session.provider.send_sms(credentials.mobile, credentials.country_code)
                        return SessionView()
                    return await session.provider.login_sms(credentials.mobile,
                        credentials.country_code, credentials.otp.get_secret_value())
                try:
                    async with asyncio.timeout(login_timeout):
                        session.auth_task = asyncio.create_task(authenticate())
                        session.view = await session.auth_task
                except asyncio.CancelledError:
                    cancelled = True
                except ApiError as error:
                    failure = provider_error(error)
                except Exception:
                    failure = ApiError("AUTH_UPSTREAM_FAILED", "游戏认证服务暂不可用", retryable=True)
                finally:
                    if token is not None:
                        current_channel.reset(token)
                    if channel is not None:
                        await channel.close()
                    session.captcha = None
                if failure is not None or cancelled:
                    session.view = SessionView()
                    # A rejected code leaves the prepared SMS ticket available for correction.
                    if cancelled or operation != 'sms_login' or failure.code != 'AUTH_SMS_REJECTED':
                        await sessions.close_provider(session)
                        session.provider = sessions.provider_factory()
                    if cancelled:
                        if session.auth_cancel_requested:
                            raise ApiError('AUTH_LOGIN_CANCELLED', '已取消登录', 401)
                        raise asyncio.CancelledError()
                    raise failure
                if session.logout_requested or session.auth_cancel_requested or not session.active:
                    session.view = SessionView()
                    await sessions.close_provider(session)
                    session.provider = sessions.provider_factory()
                    raise auth_required()
                if operation == 'sms_send':
                    return {'resend_after_seconds': 60}
                if not session.view.authenticated:
                    await sessions.close_provider(session)
                    session.provider = sessions.provider_factory()
                if session.logout_requested or session.auth_cancel_requested or not session.active:
                    session.view = SessionView()
                    await sessions.close_provider(session)
                    session.provider = sessions.provider_factory()
                    raise auth_required()
                return session.view
        finally:
            if operation_started:
                session.login_in_progress = False
                session.auth_task = session.auth_challenge_id = None
                session.auth_cancel_requested = False
            if web_slot:
                sessions.web_login_lock.release()
            body = credentials = None

    @router.post("/api/auth/qr", response_model=QrView)
    async def start_qr(request: Request, response: Response):
        session = session_for(sessions, request, response)
        async with session.lock:
            if not session.active:
                raise auth_required()
            if session.view.authenticated or session.view.status == "expired":
                session.view = SessionView()
                await sessions.close_provider(session)
                session.provider = sessions.provider_factory()
            session.qr.clear()
            require_method(session, "qr")
            try:
                async with asyncio.timeout(30):
                    challenge = await session.provider.start_qr()
            except ApiError as error:
                raise provider_error(error) from None
            except Exception:
                raise ApiError("AUTH_UPSTREAM_FAILED", "游戏扫码服务暂不可用", retryable=True) from None
            challenge_id = secrets.token_urlsafe(16)
            session.qr[challenge_id] = challenge
            return QrView(challenge_id=challenge_id, qr_payload=challenge.qr_payload,
                          expires_at=challenge.expires_at)

    @router.get("/api/auth/qr/{challenge_id}", response_model=QrPollView)
    async def poll_qr(challenge_id: str, request: Request, response: Response):
        session = session_for(sessions, request, response)
        require_method(session, "qr")
        async with session.lock:
            if not session.active:
                raise auth_required()
            challenge = session.qr.get(challenge_id)
            if challenge is None:
                raise ApiError("QR_NOT_FOUND", "扫码任务不存在或已结束", 404)
            if challenge.expires_at <= datetime.now(timezone.utc):
                session.qr.pop(challenge_id)
                return QrPollView(status="expired")
            try:
                async with asyncio.timeout(30):
                    result = await session.provider.poll_qr(challenge.handle)
            except ApiError as error:
                raise provider_error(error) from None
            except Exception:
                raise ApiError("AUTH_UPSTREAM_FAILED", "游戏扫码服务暂不可用", retryable=True) from None
            if result.session is not None and result.status == "confirmed":
                session.cancel_refresh()
                session.view = result.session
            if result.status in {"confirmed", "expired", "failed"}:
                session.qr.pop(challenge_id)
            return result

    @router.post("/api/session/role", response_model=SessionView)
    async def select_role(body: RoleRequest, request: Request):
        session = require_session(sessions, request)
        async with session.lock:
            if not session.active:
                raise auth_required()
            if body.role_id not in {role.id for role in session.view.roles}:
                raise ApiError("ROLE_NOT_FOUND", "角色不属于当前游戏会话", 404)
            session.cancel_refresh()
            try:
                async with asyncio.timeout(30):
                    session.view = await session.provider.select_role(body.role_id)
            except ApiError as error:
                if error.status_code == 401:
                    await sessions.expire(session)
                raise provider_error(error) from None
            except Exception:
                raise ApiError("AUTH_UPSTREAM_FAILED", "游戏角色服务暂不可用", retryable=True) from None
            return session.view

    @router.delete("/api/session", response_model=SessionView)
    async def logout(request: Request, response: Response):
        session = sessions.get(request.cookies.get(COOKIE_NAME))
        closing = {session.id: session} if session is not None else {}
        for value in closing.values():
            value.logout_requested = True
        for session in closing.values():
            # A login holds session.lock while awaiting human verification.
            if session.captcha is not None:
                await session.captcha.close()
            async with session.lock:
                await sessions.remove(session)
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        response.delete_cookie('rumi_remember', path='/', httponly=True, samesite='strict',
                               secure=request.url.scheme == 'https')
        return SessionView()

    return router
