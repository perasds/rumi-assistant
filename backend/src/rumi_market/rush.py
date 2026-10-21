"""Session-owned automatic purchases, started only by an explicit request.

Display searches and their cursors are independent. An unknown transaction
outcome locks the session; terminal/duplicate HTTP requests never retry it.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import secrets
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .errors import ApiError, auth_required
from .models import InputModel
from .season_catalog import SeasonPetCatalog


MAX_SAFE_INTEGER = 2**53 - 1
ACTIVE = frozenset({'running', 'stopping'})


class RushSettings(InputModel):
    pet_ids: list[str] = Field(default_factory=list, max_length=306)
    current_season_only: bool = Field(default=False, strict=True)
    max_price_gold: int = Field(gt=0, le=MAX_SAFE_INTEGER, strict=True)
    quantity_per_pet: int = Field(gt=0, le=MAX_SAFE_INTEGER, strict=True)
    min_balance_gold: int = Field(ge=0, le=MAX_SAFE_INTEGER, strict=True)
    quantity_mode: Literal['each_once', 'total', 'custom'] | None = None
    pet_quantities: dict[str, Annotated[int, Field(gt=0, le=MAX_SAFE_INTEGER, strict=True)]] = Field(
        default_factory=dict, max_length=306)

    @field_validator('pet_ids')
    @classmethod
    def unique_pet_ids(cls, values):
        if any(not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**31
               or str(int(value)) != value for value in values):
            raise ValueError('Invalid pet selection')
        return list(dict.fromkeys(values))

    @model_validator(mode='after')
    def normalize_selected_quantity(self) -> Self:
        if self.current_season_only:
            if self.pet_ids or self.pet_quantities or self.quantity_mode not in (None, 'total'):
                raise ValueError('Current season requires total quantity without custom pets')
            self.quantity_mode = 'total'
        # Omitted modes retain the old selected-one-each / unrestricted-total
        # contract. Explicit total mode may also restrict the candidate pool.
        mode = self.quantity_mode or ('each_once' if self.pet_ids else 'total')
        if mode != 'total' and not self.pet_ids:
            raise ValueError('Per-pet quantity mode requires selected pets')
        if mode == 'custom':
            if set(self.pet_quantities) != set(self.pet_ids) or sum(self.pet_quantities.values()) > MAX_SAFE_INTEGER:
                raise ValueError('Custom quantities must match selected pets within the total limit')
        elif self.pet_quantities:
            raise ValueError('Individual quantities require custom mode')
        if mode != 'total':
            self.quantity_per_pet = 1
        return self

    @property
    def quantity_targets(self) -> dict[str, int] | None:
        mode = self.quantity_mode or ('each_once' if self.pet_ids else 'total')
        if mode == 'total':
            return None
        return dict(self.pet_quantities) if mode == 'custom' else {pet: 1 for pet in self.pet_ids}


class RushStartRequest(RushSettings):
    request_id: str = Field(min_length=16, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class RushStopRequest(InputModel):
    job_id: str = Field(min_length=1, max_length=64)


class RushPurchaseState(InputModel):
    job_id: str | None = None
    status: Literal['idle', 'running', 'stopping', 'stopped', 'completed', 'failed', 'uncertain'] = 'idle'
    settings: RushSettings | None = None
    balance_gold: str | None = None
    season_ready: bool = False
    current_season_id: int | None = None
    season_message: str | None = None
    target_season_id: int | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    spent_gold: str = '0'
    message: str | None = None
    attempts: int = 0
    updated_at: datetime | None = None


@dataclass(repr=False)
class RushRun:
    request_id: str
    view: RushPurchaseState
    stop_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    seen_ids: set[str] = field(default_factory=set, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)

    def request_stop(self):
        self.stop_event.set()
        if self.view.status == 'running':
            self.view.status = 'stopping'


class RushPurchaseService:
    def __init__(self, catalog, sessions, *, interval=0.3, season_catalog=None):
        if type(interval) not in (float, int) or not 0 < interval <= 60:
            raise ValueError('Invalid monitoring interval')
        self.catalog, self.sessions, self.interval = catalog, sessions, interval
        self.season_catalog = season_catalog if season_catalog is not None else SeasonPetCatalog()

    @staticmethod
    def _season_id(session):
        try:
            value = session.provider.season_snapshot
            season = value.get('current_season_id') if type(value) is dict else None
            return season if value.get('valid') is True and type(season) is int and 0 < season < 100 else None
        except Exception:
            return None

    def _season_view(self, session):
        season = self._season_id(session)
        ready = False
        if season is not None:
            try:
                ready = bool(self.season_catalog.ids_for(season))
            except Exception:
                pass
        message = (None if ready else '当前赛季宠物资料暂不可用' if season is not None
                   else '当前赛季状态尚未同步，请刷新状态或重新登录')
        return {'season_ready': ready, 'current_season_id': season, 'season_message': message}

    @staticmethod
    def _balance(session):
        try:
            value = session.provider.gold_balance
            return value if type(value) is int and 0 <= value < 2**64 else None
        except Exception:
            return None

    def snapshot(self, session, run=None):
        value = (run or session.rush).view if run or session.rush else RushPurchaseState()
        balance = self._balance(session)
        return value.model_copy(deep=True, update={
            'balance_gold': str(balance) if balance is not None else None, **self._season_view(session)})

    def start(self, session, body: RushStartRequest):
        if not session.active or not session.view.authenticated:
            raise auth_required()
        settings = RushSettings(**body.model_dump(exclude={'request_id'}))
        previous = session.rush_requests.get(body.request_id)
        if previous is not None:
            if previous.view.settings != settings:
                raise ApiError('RUSH_REQUEST_CONFLICT', '这次启动请求的设置已变化', 409)
            # A replay must not hide a newer running or uncertain job in the UI.
            return self.snapshot(session)
        if session.rush and session.rush.view.status == 'uncertain':
            raise ApiError('PURCHASE_OUTCOME_UNKNOWN', '购买结果待核实，请先在游戏确认后重新登录', 409)
        if session.rush and session.rush.view.status in ACTIVE:
            raise ApiError('RUSH_ALREADY_RUNNING', '抢购任务正在进行，请先停止', 409)
        if len(session.rush_requests) >= 128:
            raise ApiError('RUSH_SESSION_LIMIT', '本次会话任务已达上限，请重新登录', 409)
        if any(not self.catalog.pet(pet).name_verified for pet in settings.pet_ids):
            raise ApiError('RUSH_INVALID_PET', '请选择已核实名称的噜咪', 422)
        target_season = None
        if settings.current_season_only:
            target_season = self._season_id(session)
            if target_season is None:
                raise ApiError('SEASON_STATE_UNAVAILABLE', '当前赛季状态尚未同步，请刷新状态或重新登录', 409)
            if not self.season_catalog.ids_for(target_season):
                raise ApiError('SEASON_PETS_UNAVAILABLE', '当前赛季宠物资料暂不可用', 409)
        run = RushRun(body.request_id, RushPurchaseState(
            job_id=secrets.token_urlsafe(16), status='running', settings=settings,
            target_season_id=target_season,
            counts={pet: 0 for pet in settings.pet_ids}, message='正在监测符合设置的商品',
            updated_at=datetime.now(timezone.utc)))
        session.rush = run
        session.rush_requests[body.request_id] = run
        run.task = asyncio.create_task(self._run(session, run))
        session.tasks.add(run.task)
        run.task.add_done_callback(session.tasks.discard)
        return self.snapshot(session)

    def stop(self, session, job_id):
        if session.rush is None or session.rush.view.job_id != job_id:
            raise ApiError('RUSH_JOB_CHANGED', '抢购任务已变化，请重新查询状态', 409)
        session.rush.request_stop()
        session.rush.view.updated_at = datetime.now(timezone.utc)
        return self.snapshot(session)

    @staticmethod
    def _current(session, run):
        return session.active and session.view.authenticated and session.rush is run and not run.stop_event.is_set()

    def _season_current(self, session, run):
        return (not run.view.settings.current_season_only
                or self._season_id(session) == run.view.target_season_id)

    def _require_season(self, session, run):
        if not self._season_current(session, run):
            raise ApiError('SEASON_STATE_CHANGED', '当前赛季状态已变化，抢购已停止', 409)

    @staticmethod
    def _finish(run, status, message):
        run.view.status, run.view.message = status, message
        run.view.updated_at = datetime.now(timezone.utc)

    async def _run(self, session, run):
        settings = run.view.settings
        selected_ids = frozenset(settings.pet_ids)
        targets = settings.quantity_targets
        in_purchase = False
        try:
            while self._current(session, run):
                self._require_season(session, run)
                balance = self._balance(session)
                if balance is None:
                    self._finish(run, 'failed', '实时金币状态尚未就绪，请重新登录后再试')
                    return
                if balance < settings.min_balance_gold:
                    self._finish(run, 'stopped', '金币余额已低于设置值，已停止抢购')
                    return
                cursor, cursors, round_ids = None, set(), set()
                while self._current(session, run):
                    self._require_season(session, run)
                    async with asyncio.timeout(30):
                        page = await session.provider.purchase_page(cursor)
                    if not self._current(session, run):
                        break
                    self._require_season(session, run)
                    price_boundary = False
                    for row in page.items:
                        if not self._current(session, run):
                            break
                        self._require_season(session, run)
                        if (type(row.raw_id) is not str or not row.raw_id or row.category != 'color'
                                or type(row.pet_id) is not str or not 0 < len(row.pet_id) <= 10
                                or not row.pet_id.isascii() or not row.pet_id.isdecimal()
                                or not 0 < int(row.pet_id) < 2**31 or str(int(row.pet_id)) != row.pet_id
                                or type(row.direct_buy_price) is not int or not 0 <= row.direct_buy_price < 2**63):
                            raise ValueError('Invalid purchase page')
                        # The independent native query is pinned to order_type=3, desc=False.
                        if row.direct_buy_price >= settings.max_price_gold:
                            price_boundary = True
                            break
                        if row.raw_id in round_ids or row.raw_id in run.seen_ids:
                            continue
                        round_ids.add(row.raw_id)
                        if ((selected_ids and row.pet_id not in selected_ids)
                                or (settings.current_season_only and self.season_catalog.tag_for(row.pet_id) != run.view.target_season_id + 1)
                                or (targets is not None and run.view.counts.get(row.pet_id, 0) >= targets[row.pet_id])
                                or row.direct_buy_price == 0):
                            continue
                        balance = self._balance(session)
                        if balance is None:
                            raise ApiError('PURCHASE_BALANCE_UNAVAILABLE', '', 409)
                        if balance - row.direct_buy_price < settings.min_balance_gold:
                            self._finish(run, 'stopped', '购买后金币将低于设置值，已停止抢购')
                            return
                        run.view.attempts += 1
                        in_purchase = True
                        receipt = await session.provider.purchase_color(row,
                            max_price_gold=settings.max_price_gold,
                            min_balance_gold=settings.min_balance_gold,
                            stop_requested=lambda: not self._current(session, run) or not self._season_current(session, run))
                        if receipt is None:
                            in_purchase = False
                            self._require_season(session, run)
                            continue
                        if (receipt.raw_id != row.raw_id or receipt.pet_id != row.pet_id
                                or type(receipt.price) is not int or not 0 < receipt.price < settings.max_price_gold
                                or type(receipt.balance_ready) is not bool):
                            raise ApiError('PURCHASE_OUTCOME_UNKNOWN', '', 409)
                        in_purchase = False
                        run.seen_ids.add(row.raw_id)
                        run.view.counts[row.pet_id] = run.view.counts.get(row.pet_id, 0) + 1
                        run.view.spent_gold = str(int(run.view.spent_gold) + receipt.price)
                        run.view.updated_at = datetime.now(timezone.utc)
                        if not receipt.balance_ready:
                            self._finish(run, 'failed', '购买已确认，金币同步尚未完成，已暂停后续购买')
                            return
                        if not self._current(session, run):
                            break
                        self._require_season(session, run)
                        complete = (all(run.view.counts.get(pet, 0) >= quantity for pet, quantity in targets.items())
                                    if targets is not None else sum(run.view.counts.values()) >= settings.quantity_per_pet)
                        if complete:
                            message = ('已达到每种噜咪的设置购买数量' if settings.quantity_mode == 'custom'
                                       else '已购齐指定噜咪，每种1只' if targets is not None else '已达到设置的总购买数量')
                            self._finish(run, 'completed', message)
                            return
                    if price_boundary or page.complete or not self._current(session, run):
                        break
                    if page.next_cursor is None or page.next_cursor in cursors or len(cursors) >= 256:
                        raise ValueError('Invalid purchase cursor')
                    cursors.add(page.next_cursor)
                    cursor = page.next_cursor
                    await asyncio.sleep(0)
                if self._current(session, run):
                    try:
                        await asyncio.wait_for(run.stop_event.wait(), self.interval)
                    except TimeoutError:
                        pass
            self._finish(run, 'stopped', '抢购已停止')
        except asyncio.CancelledError:
            run.stop_event.set()
            self._finish(run, 'uncertain' if in_purchase else 'stopped',
                         '购买结果待核实，请在游戏确认后重新登录' if in_purchase else '抢购已停止')
        except ApiError as error:
            code = error.code
            if code == 'PURCHASE_OUTCOME_UNKNOWN':
                self._finish(run, 'uncertain', '购买结果待核实，请在游戏确认后重新登录')
            elif code == 'SEASON_STATE_CHANGED':
                self._finish(run, 'stopped', '当前赛季状态已变化，抢购已停止')
            elif code == 'PURCHASE_BALANCE_LIMIT':
                self._finish(run, 'stopped', '购买后金币将低于设置值，已停止抢购')
            elif code == 'PURCHASE_BALANCE_UNAVAILABLE':
                self._finish(run, 'failed', '实时金币状态不可用，已停止抢购')
            elif code == 'PURCHASE_REJECTED':
                self._finish(run, 'failed', '游戏服务器拒绝本次购买，已停止抢购')
            elif code in {'PURCHASE_DETAIL_UNAVAILABLE', 'PURCHASE_INVALID'}:
                self._finish(run, 'failed', '本次未提交购买，商品详情或参数校验失败，已停止抢购')
            elif error.status_code == 401:
                self._finish(run, 'stopped', '登录已过期，抢购已停止')
                await self.sessions.expire(session)
            else:
                self._finish(run, 'uncertain' if in_purchase else 'failed',
                             '购买结果待核实，请在游戏确认后重新登录' if in_purchase else '商品监测失败，抢购已停止')
        except Exception:
            self._finish(run, 'uncertain' if in_purchase else 'failed',
                         '购买结果待核实，请在游戏确认后重新登录' if in_purchase else '商品监测失败，抢购已停止')
        finally:
            run.seen_ids.clear()
