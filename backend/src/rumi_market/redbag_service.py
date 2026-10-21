"""Explicit, session-owned redbag automation; state reads never send commands."""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from weakref import WeakValueDictionary

from .errors import ApiError
from .redbag_models import RedbagCandidate, RedbagReceipt


MAX_SEEN = 4096
MAX_REQUESTS = 128
ACTIVE = frozenset({'starting', 'running', 'stopping'})
SKIP_MESSAGES = {
    'missing': '红包已不存在',
    'expired': '红包已过期',
    'self': '已跳过自己发送的红包',
    'already_claimed': '红包已经领取',
    'full': '红包已领完',
    'duplicate': '已跳过重复红包',
}
ERROR_STATES = {
    'REDBAG_DAILY_LIMIT': ('daily_limit', '今日领取已达上限，任务已停止'),
    'REDBAG_CONDITION_NOT_MET': ('conditions_not_met', '当前不满足领取条件，任务已停止'),
    'REDBAG_RATE_LIMITED': ('rate_limited', '领取请求过快，任务已停止，请稍后手动开启'),
    'REDBAG_QUEUE_FULL': ('failed', '待处理红包已达上限，任务已停止'),
    'REDBAG_UNAVAILABLE': ('failed', '红包功能暂不可用，任务已停止'),
    'REDBAG_UPSTREAM_FAILED': ('failed', '红包服务暂不可用，任务已停止'),
}
UNCERTAIN_MESSAGE = '领取结果待核实，任务已停止，请在游戏中确认后重新登录'


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(repr=False)
class RedbagRun:
    role_key: tuple[str, str]
    request_id: str
    session: object
    provider: object
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    status: str = 'starting'
    uncertain: bool = False
    claimed_count: int = 0
    skipped_count: int = 0
    stamina_total: int = 0
    message: str = '正在连接红包消息'
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    recent: list[dict] = field(default_factory=list)
    result_sequence: int = 0

    def request_stop(self):
        self.stop.set()
        if self.status in {'starting', 'running'}:
            self.status, self.message = 'stopping', '正在停止，等待已发出的领取完成'
            self.updated_at = _now()


class RedbagService:
    def __init__(self, sessions):
        self.sessions = sessions
        # Sessions own runs. An uncertainty lock must not keep a logged-out
        # session and its provider credentials alive after session cleanup.
        self.roles: WeakValueDictionary[tuple[str, str], RedbagRun] = WeakValueDictionary()
        self.lock = asyncio.Lock()

    @staticmethod
    def _require(session):
        if not session.active or not session.view.authenticated or session.view.status != 'connected':
            raise ApiError('GAME_SESSION_EXPIRED', '游戏会话已过期，请重新登录', 401)

    @staticmethod
    def _identity(session):
        try:
            value = session.provider.redbag_identity
            if (isinstance(value, tuple) and len(value) == 2
                    and all(type(part) is str and 0 < len(part) <= 128 for part in value)):
                return value
        except Exception:
            pass
        return None

    def _available(self, session):
        try:
            return self._identity(session) is not None and session.provider.redbag_available is True
        except Exception:
            return False

    @staticmethod
    def _generation_current(session, run):
        return (session.active and session.view.authenticated and session.view.status == 'connected'
                and session.redbag_task is run and session.provider is run.provider)

    def _current(self, session, run):
        return (self._generation_current(session, run) and self.roles.get(run.role_key) is run
                and self._identity(session) == run.role_key)

    def _view(self, session):
        run = session.redbag_task
        return {
            'available': self._available(session),
            'running': bool(run and run.task and not run.task.done()),
            'status': run.status if run else 'idle',
            'message': run.message if run else '自动抢红包已关闭',
            'claimed_count': run.claimed_count if run else 0,
            'skipped_count': run.skipped_count if run else 0,
            'stamina_total': str(run.stamina_total) if run else '0',
            'recent': [dict(row) for row in run.recent] if run else [],
            'started_at': run.started_at if run else None,
            'updated_at': run.updated_at if run else None,
        }

    async def state(self, session):
        self._require(session)
        return self._view(session)

    async def start(self, session, request_id):
        async with self.lock:
            self._require(session)
            if request_id in session.redbag_requests:
                return self._view(session)
            run = session.redbag_task
            if run and run.uncertain:
                raise ApiError('REDBAG_UNCERTAIN', UNCERTAIN_MESSAGE, 409)
            if len(session.redbag_requests) >= MAX_REQUESTS:
                raise ApiError('REDBAG_REQUEST_LIMIT', '本次登录任务请求已达上限，请重新登录', 409)
            if run and run.task and not run.task.done():
                session.redbag_requests[request_id] = run
                return self._view(session)
            if not self._available(session):
                raise ApiError('REDBAG_UNAVAILABLE', '红包功能暂不可用，请刷新状态或重新登录', 409)
            key = self._identity(session)
            owner = self.roles.get(key)
            if owner and owner.task and not owner.task.done():
                raise ApiError('REDBAG_ROLE_BUSY', '该角色已有红包任务或领取结果待核实', 409)
            if owner and not self._current(owner.session, owner):
                self.roles.pop(key, None)
                owner = None
            if owner and owner.uncertain:
                raise ApiError('REDBAG_ROLE_BUSY', '该角色已有红包任务或领取结果待核实', 409)
            run = RedbagRun(key, request_id, session, session.provider)
            self.roles[key] = run
            session.redbag_task = run
            session.redbag_requests[request_id] = run
            run.task = asyncio.create_task(self._run(session, run))
            session.tasks.add(run.task)
            run.task.add_done_callback(session.tasks.discard)
            return self._view(session)

    async def stop(self, session):
        self._require(session)
        if session.redbag_task:
            session.redbag_task.request_stop()
        return self._view(session)

    @staticmethod
    def _record(run, status, message, stamina=0):
        run.updated_at = _now()
        run.result_sequence += 1
        run.recent.insert(0, {'id': run.result_sequence, 'at': run.updated_at,
            'status': status, 'message': message, 'stamina': str(stamina)})
        del run.recent[20:]

    def _skip(self, run, outcome):
        run.skipped_count += 1
        self._record(run, 'skipped', SKIP_MESSAGES[outcome])

    @staticmethod
    def _server_time(run):
        try:
            value = run.provider.server_time_seconds
            return value if type(value) is int and value > 0 else None
        except Exception:
            return None

    def _remember(self, session, run, candidate):
        if (not isinstance(candidate, RedbagCandidate) or type(candidate.id) is not str
                or not 0 < len(candidate.id) <= 128 or type(candidate.sender_role_id) is not str
                or type(candidate.expires_at) is not int or candidate.expires_at < 0):
            raise ApiError('REDBAG_UPSTREAM_FAILED', '', 503)
        key = '\0'.join((*run.role_key, candidate.id))
        if key in session.redbag_seen:
            self._skip(run, 'duplicate')
            return False
        now = self._server_time(run)
        if len(session.redbag_seen) >= MAX_SEEN and now is not None:
            for old_key, expires_at in list(session.redbag_seen.items()):
                if 0 < expires_at <= now:
                    del session.redbag_seen[old_key]
        if len(session.redbag_seen) >= MAX_SEEN:
            raise ApiError('REDBAG_QUEUE_FULL', '', 503)
        session.redbag_seen[key] = candidate.expires_at
        if candidate.sender_role_id == run.role_key[1]:
            self._skip(run, 'self')
            return False
        if candidate.expires_at and now is not None and candidate.expires_at <= now:
            self._skip(run, 'expired')
            return False
        return True

    @staticmethod
    async def _next(run):
        next_task = asyncio.create_task(run.provider.redbag_next())
        stopped = asyncio.create_task(run.stop.wait())
        try:
            await asyncio.wait({next_task, stopped}, return_when=asyncio.FIRST_COMPLETED)
            return None if run.stop.is_set() else next_task.result()
        finally:
            for task in (next_task, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(next_task, stopped, return_exceptions=True)

    def _apply_receipt(self, run, receipt):
        if (not isinstance(receipt, RedbagReceipt) or type(receipt.stamina) is not int
                or not 0 <= receipt.stamina < 2**64):
            return False
        if receipt.outcome == 'claimed':
            run.claimed_count += 1
            run.stamina_total += receipt.stamina
            self._record(run, 'claimed', '领取成功', receipt.stamina)
        elif receipt.outcome in SKIP_MESSAGES and receipt.outcome != 'duplicate':
            self._skip(run, receipt.outcome)
        else:
            return False
        return True

    @staticmethod
    async def _command(run, command):
        # Keep the role occupied until the bounded main-game request settles,
        # even if repeated session cleanup cancels this worker more than once.
        pending = asyncio.create_task(command)
        while True:
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                run.request_stop()
                if pending.cancelled():
                    raise ApiError('REDBAG_UNCERTAIN', '', 503) from None

    async def _claim(self, session, run, candidate):
        try:
            receipt = await self._command(run, run.provider.redbag_claim(candidate,
                stop_requested=lambda: run.stop.is_set() or not self._current(session, run)))
            # The adapter returns None only when its final before-send guard
            # prevented sending. A timeout still raises REDBAG_UNCERTAIN.
            if receipt is None and (run.stop.is_set() or not self._current(session, run)):
                return False
        except ApiError as error:
            if error.code != 'REDBAG_UNCERTAIN':
                raise
            receipt = None
        except Exception:
            receipt = None
        if self._apply_receipt(run, receipt):
            return True
        try:
            receipt = await self._command(run, run.provider.redbag_reconcile(candidate))
        except ApiError as error:
            if error.code == 'GAME_SESSION_EXPIRED':
                run.request_stop()
                if self._generation_current(session, run):
                    await self.sessions.expire(session)
            receipt = None
        except Exception:
            receipt = None
        if self._apply_receipt(run, receipt):
            return True
        run.uncertain = True
        run.status, run.message = 'uncertain', UNCERTAIN_MESSAGE
        self._record(run, 'uncertain', UNCERTAIN_MESSAGE)
        return False

    async def _run(self, session, run):
        try:
            if run.stop.is_set() or not self._current(session, run):
                return
            await run.provider.redbag_start()
            if not run.stop.is_set() and self._current(session, run):
                run.status, run.message = 'running', '正在等待新红包'
                run.updated_at = _now()
            while not run.stop.is_set() and self._current(session, run):
                candidate = await self._next(run)
                if candidate is None or run.stop.is_set() or not self._current(session, run):
                    break
                if self._remember(session, run, candidate) and not await self._claim(session, run, candidate):
                    break
        except ApiError as error:
            if error.code == 'GAME_SESSION_EXPIRED':
                run.request_stop()
                if self._generation_current(session, run):
                    await self.sessions.expire(session)
                run.status, run.message = 'stopped', '游戏会话已过期，红包任务已停止'
            else:
                run.status, run.message = ERROR_STATES.get(error.code,
                    ('failed', '红包服务暂不可用，任务已停止'))
        except asyncio.CancelledError:
            run.request_stop()
        except Exception:
            run.status, run.message = 'failed', '红包服务暂不可用，任务已停止'
        finally:
            if run.status in ACTIVE:
                run.status, run.message = 'stopped', '自动抢红包已停止'
            run.updated_at = _now()
            try:
                await run.provider.redbag_stop()
            except Exception:
                pass
            if not run.uncertain and self.roles.get(run.role_key) is run:
                self.roles.pop(run.role_key, None)
