"""Account order views and explicit task controls. GET never sends game commands."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import time

from .errors import ApiError
from .home_work_config import HomeWorkConfig, WORK_MUTEX_MASK
from .order_catalog import load_order_catalog
from .order_models import OrderSettings
from .order_planner import plan_orders
from .order_replenishment import choose_replenishment, material_dependency_ids
from .order_production_policy import choose_bounded_production
from .order_production_queue import QueueBook
from .production_timing import estimate_production_time
from .work_inspection import inspect_workplaces


HOME_COLLECTION_INTERVAL_SECONDS = 60
DEFAULT_AUTO_PRODUCTION_EXCLUSIONS = frozenset({'10101', '10110', '10120'})
logger = logging.getLogger(__name__)


def order_revision(raw):
    # Exclude the ticking clock, include every resource/configuration baseline.
    values = [raw.get('role_key'), raw.get('orders'), raw.get('inventory'), raw.get('home')]
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _number(value):
    if type(value) is int:
        return value
    if type(value) is str and len(value) <= 20 and value.isascii() and value.isdecimal():
        return int(value)
    return None


@dataclass(repr=False)
class OrderRun:
    role_key: tuple
    request_id: str
    initial_revision: str
    settings: OrderSettings
    deadline: int
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    status: str = 'running'
    message: str | None = None
    completed: int = 0
    cans: int = 0
    uncertain: bool = False
    in_flight: bool = False
    kind: str = 'orders'
    inspected_actions: int = 0
    next_collection_at: float = 0.0
    production_book: QueueBook | None = field(default=None, repr=False)
    last_collection_signature: tuple | None = field(default=None, repr=False)
    uncertainty_reason: str | None = field(default=None, repr=False)

    def request_stop(self):
        self.stop.set()

    def view(self):
        return {'status': self.status, 'running': self.task is not None and not self.task.done(),
                'kind': self.kind, 'inspected_actions': self.inspected_actions,
                'completed_orders': self.completed, 'canned_received': str(self.cans),
                'message': self.message, 'deadline_server_seconds': self.deadline}


class OrderService:
    def __init__(self, sessions, *, catalog=None, item_catalog=None, pet_catalog=None, sync_timeout=5.0):
        self.sessions = sessions
        self.catalog = load_order_catalog() if catalog is None else catalog
        self.items = item_catalog
        self.pet_catalog = pet_catalog
        self.config = HomeWorkConfig()
        self.auto_production_exclusions = DEFAULT_AUTO_PRODUCTION_EXCLUSIONS
        self.sync_timeout = sync_timeout
        # Application runs one worker. Never use the random browser Role.id here.
        self.roles: dict[tuple, OrderRun] = {}
        self.home_busy: set[tuple] = set()
        # Reservations outlive a task: stopping automation does not stop game work.
        self.production_books: dict[tuple, tuple[object, QueueBook]] = {}

    @staticmethod
    def _require(session):
        if not session.active or not session.view.authenticated or session.view.status != 'connected':
            raise ApiError('GAME_SESSION_EXPIRED', '游戏会话已过期，请重新登录', 401)

    async def raw(self, session):
        self._require(session)
        try:
            raw = await session.provider.order_snapshot()
        except ApiError as error:
            if error.code == 'GAME_SESSION_EXPIRED':
                await self.sessions.expire(session)
                raise ApiError('GAME_SESSION_EXPIRED', '游戏会话已过期，请重新登录', 401) from None
            raise ApiError('ORDER_STATE_UNAVAILABLE', '订单数据暂不可用，请稍后刷新', 503) from None
        except Exception:
            raise ApiError('ORDER_STATE_UNAVAILABLE', '订单数据暂不可用，请稍后刷新', 503) from None
        self._require(session)
        if not isinstance(raw, dict):
            raise ApiError('ORDER_STATE_UNAVAILABLE', '订单数据暂不可用', 503)
        return raw

    def _name(self, item):
        name = self.catalog.get('names', {}).get(item)
        if not name and self.items is not None:
            found = self.items.lookup(item)
            name = found.name if found else None
        return name or f'物品 {item}'

    def _buildings(self, raw):
        result = []
        for row in raw.get('home', {}).get('architectures', []):
            building_config = self.config.building(row.get('id'))
            if not building_config:
                continue
            work = row.get('work_old') or {}
            reason = None
            if (not work or row.get('cur_recipe') or row.get('has_farm_data')
                    or building_config['architecture_type'] == 19):
                reason = 'production_branch_unverified'
            elif row.get('is_hide') or row.get('is_need_cut_ribbon') or row.get('level_up_complete_time') not in (None, 0, '0'):
                reason = 'building_unavailable'
            worker = work.get('lumi_uid')
            home = raw.get('home') or {}
            roster_worker = next((entry for entry in home.get('workers', []) if entry.get('uid') == worker), None)
            worker_state = _number((roster_worker or {}).get('raw_state'))
            work_types = self.config.work_types((roster_worker or {}).get('id'))
            worker_valid = bool(worker not in (None, '0', '2147483647') and home.get('roster_ready')
                and home.get('dispatch_ready') and roster_worker and worker_state is not None
                and not worker_state & WORK_MUTEX_MASK and work_types is not None
                and self.config.building(row.get('id'))['work_type_id'] in work_types
                and worker not in home.get('dispatch_lumi_uids', [])
                and all(entry.get('kind') == 'work_old' and str(entry.get('architecture_uid')) == str(row['uid'])
                        for entry in roster_worker.get('occupancy', [])))
            worker_missing = worker in ('0', '2147483647')
            worker_reason = None
            if not worker_missing:
                if worker is None or roster_worker is None:
                    worker_reason = 'worker_unknown'
                elif not home.get('roster_ready'):
                    worker_reason = 'roster_unknown'
                elif not home.get('dispatch_ready'):
                    worker_reason = 'dispatch_unknown'
                elif worker_state is None:
                    worker_reason = 'worker_state_unknown'
                elif work_types is None:
                    worker_reason = 'work_types_unknown'
                elif worker in home.get('dispatch_lumi_uids', []):
                    worker_reason = 'worker_dispatched'
                elif self.config.building(row.get('id'))['work_type_id'] not in work_types:
                    worker_reason = 'work_type_mismatch'
                elif not worker_valid:
                    worker_reason = 'worker_conflict'
            energies = [value for value in (_number(work.get('lumi_work_energy')),
                _number((roster_worker or {}).get('work_energy'))) if value is not None]
            result.append({'building_uid': str(row['uid']), 'building_id': str(row['id']),
                'auto_production_enabled': str(row['id']) not in self.auto_production_exclusions,
                'name': self.catalog.get('building_names', {}).get(str(row['id']), f'建筑 {row["id"]}'),
                'level': row.get('level'), 'worker_uid': worker,
                'current_recipe_id': str(work['recipe_id']) if work.get('recipe_id') else None,
                'assigned_lumi_uids': [worker] if worker not in (None, '0') else [],
                'production_count': _number(work.get('recipe_intend_times')),
                'completed_count': _number(work.get('recipe_complete_times')),
                'quantity_mode': 'continuous' if self.config.building(row.get('id'))['architecture_type'] in (2, 12) else 'batch',
                'reason': reason, 'production_reason': reason,
                'production_editable': reason is None and work.get('recipe_id') == 0,
                'allowed_recipe_ids': (self.config.level(row.get('id'), row.get('level')) or {}).get('recipe_ids'),
                'assignment_limit': 1,
                'worker_valid': worker_valid,
                'worker_missing': worker_missing, 'worker_reason': worker_reason,
                'recipe_id_known': type(work.get('recipe_id')) is int and work['recipe_id'] >= 0,
                'recipe_id': str(work['recipe_id']) if work.get('recipe_id') else None,
                'work_speed': _number(work.get('lumi_work_speed')),
                'work_energy': min(energies) if energies else None,
                'work_progress': _number(work.get('recipe_work_amount')),
                'recipe_update_time': _number(work.get('recipe_update_time')),
                'intend_count': _number(work.get('recipe_intend_times')),
                'completed_count': _number(work.get('recipe_complete_times')),
                'recipe_state': _number(work.get('recipe_state')),
                'capacity_remaining': row.get('capacity_remaining'),
                'store_items': row.get('store_items'),
                'editable': reason is None, 'blocked_reason': reason,
                'is_producing': work.get('recipe_state') == 1})
        return result

    def _snapshot(self, raw):
        inventory = raw.get('inventory') or {}
        return {'revision': order_revision(raw), 'orders': (raw.get('orders') or {}).get('orders', []),
                'inventory': inventory.get('items', {}) if inventory.get('valid') is True else {},
                'inventory_complete': inventory.get('valid') is True,
                'buildings': self._buildings(raw), 'server_now': raw.get('server_time_seconds')}

    @staticmethod
    def _planner_settings(settings, remaining=None):
        return {'horizon_seconds': settings.horizon_hours * 3600,
                'max_orders': settings.max_orders if remaining is None else remaining,
                'reserves': {item: int(count) for item, count in settings.reserves.items()},
                'allow_production': settings.allow_production, 'allow_refresh': settings.allow_refresh}

    def _execution_reason(self, raw):
        if not (raw.get('orders') or {}).get('valid'):
            return {
                'ORDER_VERSION_UNVERIFIED': '当前游戏版本尚未验证，订单功能暂不可用',
                'ORDER_SCHEMA_UNAVAILABLE': '订单协议加载失败，请联系维护',
                'ORDER_STATE_INVALID': '订单数据更新异常，请重新登录',
            }.get((raw.get('orders') or {}).get('unavailable_reason'), '订单尚未同步完成')
        if not (raw.get('inventory') or {}).get('valid'):
            return '背包物资尚未同步完成'
        if not (raw.get('home') or {}).get('valid'):
            if (raw.get('home') or {}).get('unavailable_reason') == 'HOME_STATE_INVALID':
                return '家园数据更新异常，请重新登录'
            return '家园尚未同步完成'
        key = raw.get('role_key')
        if not isinstance(key, (list, tuple)) or len(key) != 2 or any(_number(value) is None or _number(value) <= 0 for value in key):
            return '账号身份尚未同步完成'
        if type(raw.get('server_time_seconds')) is not int or raw['server_time_seconds'] <= 0:
            return '游戏服务器时间尚未同步完成'
        return None

    def _inspection_reason(self, raw):
        # Inspection does not depend on order notifications, but every resource
        # and identity baseline must still come from the current game session.
        reason = self._execution_reason({**raw, 'orders': {'valid': True}})
        if reason:
            return reason
        home = raw.get('home') or {}
        if home.get('roster_ready') is not True or home.get('dispatch_ready') is not True:
            return '工作噜咪和派遣状态尚未同步完成'
        return None

    def _inspection_view(self, raw, snapshot, settings):
        rows = inspect_workplaces(snapshot, self.catalog,
            {item: int(count) for item, count in settings.reserves.items()})
        public = [{key: value for key, value in row.items() if key != 'proposal'} for row in rows]
        workers = (raw.get('home') or {}).get('workers', [])
        for row in public:
            row.update(worker_name=None, worker_image_url=None, current_recipe_products=None)
            worker_uid = row['worker_uid']
            if self.pet_catalog is not None and worker_uid not in (None, '0', '2147483647'):
                worker = next((entry for entry in workers if entry.get('uid') == worker_uid), None)
                pet_id = _number((worker or {}).get('id'))
                if pet_id is not None and pet_id > 0:
                    pet = self.pet_catalog.pet(str(pet_id))
                    row.update(worker_name=pet.name, worker_image_url=pet.image_url)
            recipe_id = row['current_recipe_id']
            if recipe_id == '0':
                row['current_recipe_products'] = []
            elif recipe_id is not None:
                recipe = next((entry for entry in self.catalog.get('recipes', [])
                    if entry['building_id'] == row['building_id'] and entry['recipe_id'] == recipe_id), None)
                if recipe is not None:
                    row['current_recipe_products'] = [
                        {'item_id': product['item_id'], 'name': self._name(product['item_id']), 'count': product['count']}
                        for product in recipe['products']]
        reason = self._inspection_reason(raw)
        return {'ready': reason is None, 'reason': reason, 'buildings': public}

    def _view(self, session, raw):
        snap = self._snapshot(raw)
        reason = self._execution_reason(raw)
        inventory_ready = raw.get('inventory', {}).get('valid') is True
        settings = session.order_settings or OrderSettings()
        shared = self.roles.get(tuple(raw.get('role_key') or ()))
        task = shared if shared and (shared.uncertain or shared.view()['running']) else session.order_task
        orders = []
        for row in snap['orders']:
            materials = []
            totals = {}
            for cost in row['materials']:
                totals[cost['item_id']] = totals.get(cost['item_id'], 0) + cost['count']
            for item, count in totals.items():
                held = snap['inventory'].get(item, 0) if inventory_ready else None
                available = max(0, held - int(settings.reserves.get(item, '0'))) if held is not None else None
                materials.append({'item_id': item, 'name': self._name(item), 'count': str(count),
                    'available': str(available) if available is not None else None,
                    'shortage': str(max(0, count - available)) if available is not None else None})
            orders.append({**{k: row.get(k) for k in ('slot_id', 'is_unlock', 'order_type', 'refresh_time', 'refresh_seconds')},
                'materials': materials,
                'rewards': [{'item_id': r['item_id'], 'name': self._name(r['item_id']), 'count': str(r['count'])} for r in row['rewards']],
                'can_complete': bool(materials) and row['is_unlock'] and row.get('order_type') is not None and all(r['shortage'] == '0' for r in materials) if inventory_ready else None})
        return {'ready': reason is None, 'revision': snap['revision'],
            'observed_at': raw.get('orders', {}).get('observed_at'),
            'server_time_seconds': raw.get('server_time_seconds'), 'message': reason, 'orders': orders,
            'inventory_ready': inventory_ready, 'production_ready': raw.get('home', {}).get('valid') is True,
            'buildings': self._public_numbers(snap['buildings']), 'settings': settings.model_dump(),
            'work_inspection': self._public_numbers(self._inspection_view(raw, snap, settings)),
            'task': task.view() if task else {'status': 'idle', 'running': False,
                'completed_orders': 0, 'canned_received': '0', 'message': None},
            'execution_ready': reason is None, 'execution_reason': reason}

    @staticmethod
    def _public_numbers(value, key=None):
        if isinstance(value, list):
            return [OrderService._public_numbers(item) for item in value]
        if isinstance(value, dict):
            return {name: OrderService._public_numbers(item, name) for name, item in value.items()}
        if type(value) is int and key not in {'level', 'duration_seconds', 'next_batch_seconds', 'safe_duration_seconds',
                'safe_next_batch_seconds', 'ready_at_server_seconds', 'starts_at_server_seconds', 'horizon_seconds', 'search_budget'}:
            return str(value)
        return value

    async def state(self, session):
        return self._view(session, await self.raw(session))

    async def save_settings(self, session, settings):
        async with session.lock:
            self._require(session)
            if session.order_task and (session.order_task.view()['running'] or session.order_task.uncertain):
                raise ApiError('ORDER_TASK_ACTIVE', '请先取消任务再修改设置；待核实任务需先确认游戏结果', 409)
            raw = await self.raw(session)
            session.order_settings = settings.model_copy(deep=True)
            return self._view(session, raw)

    async def plan(self, session, revision, settings):
        raw = await self.raw(session)
        if order_revision(raw) != revision:
            raise ApiError('ORDER_STATE_CHANGED', '账号状态已变化，请刷新后重新预览', 409)
        snap = self._snapshot(raw)
        now = raw.get('server_time_seconds')
        if type(now) is not int or now <= 0 or not (raw.get('orders') or {}).get('valid'):
            raise ApiError('ORDER_STATE_UNAVAILABLE', '订单和游戏时间尚未同步完成', 409)
        result = plan_orders(snap, self.catalog, self._planner_settings(settings), now=now)
        for row in result.get('production', []):
            row['name'] = self._name(row['item_id'])
        for row in result.get('materials', []):
            row['name'] = self._name(row['item_id'])
        return self._public_numbers(result)

    @asynccontextmanager
    async def home_lease(self, session):
        # Legacy providers without order support still use the original home API.
        if not callable(getattr(session.provider, 'order_snapshot', None)):
            yield
            return
        identity = getattr(session.provider, 'order_identity', None)
        if identity is None:
            yield
            return
        key = tuple(identity)
        active = self.roles.get(key)
        if key in self.home_busy or (active is not None and (active.uncertain or active.view()['running'])):
            raise ApiError('ORDER_ROLE_BUSY', '订单任务正在使用该账号的家园，请先取消任务', 409)
        self.home_busy.add(key)
        try:
            previous = self.production_books.get(key)
            if previous and previous[0] is session.provider and self._production_materials_busy(session, key, await self.raw(session)):
                raise ApiError('ORDER_ROLE_BUSY', '该账号仍有生产材料预留，请先完成或核对生产队列', 409)
            yield
        finally:
            self.home_busy.discard(key)
            if session.home_write_uncertain and key not in self.roles:
                # A later tab/session must not bypass an unknown home write by
                # starting a different task against the same real game role.
                self.roles[key] = OrderRun(key, 'home-write-unconfirmed', '',
                    session.order_settings or OrderSettings(), 0, status='uncertain',
                    message='家园设置结果待核实，请在游戏中核对', uncertain=True)

    async def start(self, session, request_id, revision, *, kind='orders'):
        async with session.lock:
            self._require(session)
            if kind not in ('orders', 'inspection'):
                raise ValueError('Invalid order task kind')
            previous = session.order_requests.get(request_id)
            if previous:
                if previous.initial_revision != revision or previous.kind != kind:
                    raise ApiError('ORDER_REQUEST_CONFLICT', '同一请求不能使用不同状态版本', 409)
                return await self.state(session)
            raw = await self.raw(session)
            reason = self._inspection_reason(raw) if kind == 'inspection' else self._execution_reason(raw)
            if reason:
                raise ApiError('ORDER_STATE_UNAVAILABLE', reason, 409)
            if order_revision(raw) != revision:
                raise ApiError('ORDER_STATE_CHANGED', '账号状态已变化，请刷新后再开始', 409)
            key = tuple(raw['role_key'])
            owner = self.roles.get(key)
            if session.home_write_uncertain or key in self.home_busy or (owner and (owner.uncertain or owner.view()['running'])):
                raise ApiError('ORDER_ROLE_BUSY', '该账号已有订单任务，或操作结果待核实', 409)
            if kind == 'inspection' and self._production_materials_busy(session, key, raw):
                raise ApiError('ORDER_ROLE_BUSY', '该账号仍有生产材料预留，请先完成或核对生产队列', 409)
            if len(session.order_requests) >= 128:
                raise ApiError('ORDER_REQUEST_LIMIT', '本次登录任务请求已达上限，请重新登录', 409)
            settings = (session.order_settings or OrderSettings()).model_copy(deep=True)
            if kind == 'orders' and settings.allow_refresh:
                raise ApiError('ORDER_REFRESH_UNVERIFIED', '换单消耗尚未核实，请关闭自动换单后开始', 409)
            run = OrderRun(key, request_id, revision, settings, raw['server_time_seconds'] + settings.horizon_hours * 3600)
            run.kind = kind
            previous = self.production_books.get(key)
            capable = self._bounded_capable(raw)
            preserve_book = bool(previous and previous[0] is session.provider
                and (any(not queue.finished or queue.recipe_alive for queue in previous[1].active.values())
                     or any(previous[1].frozen_reserves.values())))
            if kind == 'orders' and (capable or preserve_book):
                book = previous[1] if previous and previous[0] is session.provider else QueueBook()
                if not book.refresh(raw):
                    raise ApiError('ORDER_STATE_UNAVAILABLE', '生产收支状态已变化，请重新登录后核对', 409)
                run.production_book = book
                self.production_books[key] = (session.provider, book)
            elif kind == 'orders' and previous and previous[0] is session.provider:
                self.production_books.pop(key, None)
            self.roles[key] = run
            session.order_task = run
            session.order_requests[request_id] = run
            run.task = asyncio.create_task(self._run_inspection(session, run) if kind == 'inspection' else self._run(session, run))
            session.tasks.add(run.task)
            run.task.add_done_callback(session.tasks.discard)
            return self._view(session, raw)

    async def stop(self, session):
        self._require(session)
        raw = await self.raw(session)
        shared = self.roles.get(tuple(raw.get('role_key') or ()))
        if shared:
            shared.request_stop()
        elif session.order_task:
            session.order_task.request_stop()
        return await self.state(session)

    async def resolve(self, session, revision):
        async with session.lock:
            raw = await self.raw(session)
            key = tuple(raw['role_key'])
            run = self.roles.get(key)
            reason = self._inspection_reason(raw) if run and run.kind == 'inspection' else self._execution_reason(raw)
            if reason or order_revision(raw) != revision:
                raise ApiError('ORDER_STATE_CHANGED', '请刷新完整账号状态后再核对', 409)
            if run is None or not run.uncertain or run.view()['running']:
                raise ApiError('ORDER_RESOLVE_UNAVAILABLE', '当前没有可以解除的待核实任务', 409)
            reconcile = getattr(session.provider, 'reconcile_order_operations', None)
            if callable(reconcile):
                if run.kind == 'inspection':
                    await reconcile(expected_revision=revision, require_orders=False)
                else:
                    await reconcile(expected_revision=revision)
            # The explicit user acknowledgement is a reconciliation boundary,
            # never an automatic retry or a claim that the operation was reversed.
            run.uncertain = run.in_flight = False
            run.status, run.message = 'stopped', '已按核对后的账号状态解除暂停；未重试任何操作'
            run.uncertainty_reason = None
            # Explicit reconciliation is the only boundary which may discard a
            # cancelled queue's unknown residual debt. Existing native queues
            # are conservatively reserved as external work in the next run.
            self.production_books.pop(key, None)
            run.production_book = None
            self.roles.pop(key, None)
            session.home_write_uncertain = False
            session.order_task = run
            return self._view(session, raw)

    async def _wait(self, run, seconds=.25):
        try:
            await asyncio.wait_for(run.stop.wait(), seconds)
        except TimeoutError:
            pass

    def _inspection_stopped(self, session, run, raw):
        if (run.stop.is_set() or not session.active or not session.view.authenticated
                or session.view.status != 'connected' or self.roles.get(run.role_key) is not run):
            return True
        try:
            identity = getattr(session.provider, 'order_identity', run.role_key)
            now = getattr(session.provider, 'server_time_seconds', raw.get('server_time_seconds'))
            return (identity is None or tuple(identity) != run.role_key or type(now) is not int
                    or now <= 0 or now >= run.deadline)
        except Exception:
            return True

    async def _run_inspection(self, session, run):
        try:
            while not run.stop.is_set():
                raw = await self.raw(session)
                if self._inspection_reason(raw) or tuple(raw['role_key']) != run.role_key:
                    raise ApiError('ORDER_STATE_UNAVAILABLE', '巡检状态不可用', 409)
                if raw['server_time_seconds'] >= run.deadline:
                    run.status, run.message = 'completed', '已达到巡检截止时间'
                    return
                rows = inspect_workplaces(self._snapshot(raw), self.catalog,
                    {item: int(count) for item, count in run.settings.reserves.items()})
                proposal = next((row['proposal'] for row in rows if row.get('proposal')), None)
                if proposal is None:
                    run.status, run.message = 'running', '正在巡检空闲工作单位'
                    await self._wait(run, 1)
                    continue
                run.status, run.message = 'confirming', '正在为闲置工作单位设置最低库存物品'
                run.in_flight = True
                result = await session.provider.apply_order_operation(proposal['operation'],
                    expected_revision=order_revision(raw),
                    stop_requested=lambda: self._inspection_stopped(session, run, raw))
                if result.get('status') == 'uncertain':
                    run.uncertain = True
                    break
                if result.get('status') != 'applied':
                    run.in_flight = False
                    if self._inspection_stopped(session, run, raw):
                        now = getattr(session.provider, 'server_time_seconds', raw['server_time_seconds'])
                        if not run.stop.is_set() and type(now) is int and now >= run.deadline:
                            run.status, run.message = 'completed', '已达到巡检截止时间'
                            return
                        break
                    run.status, run.message = 'failed', '生产未设置，请刷新检查工作噜咪、材料和建筑状态'
                    return
                run.inspected_actions += 1
                if proposal['costs']:
                    if not await self._finish_material_batch(session, raw, proposal, run):
                        run.uncertain = True
                        break
                run.in_flight = False
                run.status = 'running'
            run.status = 'uncertain' if run.uncertain else 'stopped'
            run.message = ('生产或库存结果待核实，请在游戏中核对' if run.uncertain
                           else '巡检已停止；游戏中已开始的生产继续运行')
        except asyncio.CancelledError:
            run.uncertain = run.uncertain or run.in_flight
            run.status = 'uncertain' if run.uncertain else 'stopped'
            run.message = '生产结果待核实，请在游戏中核对' if run.uncertain else '会话结束，巡检已停止'
        except Exception:
            run.uncertain = run.uncertain or run.in_flight
            run.status = 'uncertain' if run.uncertain else 'failed'
            run.message = '生产结果待核实，请在游戏中核对' if run.uncertain else '账号状态不可用，巡检已停止'
        finally:
            if not run.uncertain and self.roles.get(run.role_key) is run:
                self.roles.pop(run.role_key, None)

    async def _settle(self, session, before, cost, rewards, run, *, require_order=True, min_sequence=None, store_before=None, require_rewards=False, movement_proof=False):
        # ACK proves the operation; wait for absolute inventory to incorporate it.
        # Never subtract a reservation again from a post-notification baseline.
        limits = {}
        consumed, received = {}, {}
        for row in cost:
            consumed[row['item_id']] = consumed.get(row['item_id'], 0) + row['count']
            limits[row['item_id']] = limits.get(row['item_id'], before['inventory']['items'].get(row['item_id'], 0)) - row['count']
        for row in rewards:
            received[row['item_id']] = received.get(row['item_id'], 0) + row['count']
            if row['item_id'] in limits:
                limits[row['item_id']] += row['count']
        floors = {item: before['inventory']['items'].get(item, 0) - consumed.get(item, 0) + count
                  for item, count in received.items()} if require_rewards else {}
        end = time.monotonic() + self.sync_timeout
        while True:
            current = await self.raw(session)
            inventory = current.get('inventory') or {}
            order_updated = not require_order or (current.get('orders', {}).get('valid') and current['orders'].get('sequence', 0) > before['orders']['sequence'])
            store_updated = True
            if store_before is not None:
                uid, old = store_before
                home = current.get('home') or {}
                building = next((row for row in home.get('architectures', []) if str(row['uid']) == uid), None)
                stored = building.get('store_items') if building is not None else None
                store_updated = home.get('valid') and type(stored) is list and stored != old
            if (order_updated and store_updated and inventory.get('valid')
                    and inventory.get('sequence', 0) > (before['inventory']['sequence'] if min_sequence is None else min_sequence)):
                if movement_proof:
                    if self._batch_movements_confirmed(before, current, cost, rewards if require_rewards else []):
                        return True
                    if inventory.get('movement_epoch') != before['inventory'].get('movement_epoch'):
                        return False
                elif (all(inventory['items'].get(item, 0) <= count for item, count in limits.items())
                        and all(inventory['items'].get(item, 0) >= count for item, count in floors.items())):
                    return True
            if time.monotonic() >= end:
                return False
            # Stop requests do not discard an already-sent operation's confirmation.
            await asyncio.sleep(min(.05, max(0, end - time.monotonic())))

    async def _claim_building(self, session, raw, run, building_uid, building_id):
        if self._inspection_stopped(session, run, raw):
            return False
        old = next(row for row in raw['home']['architectures'] if str(row['uid']) == building_uid)
        run.status, run.message = 'confirming', '正在收取家园产物，等待物资入库'
        run.in_flight = True
        result = await session.provider.apply_order_operation({'action': 'claim',
            'building_uid': building_uid, 'building_id': building_id},
            expected_revision=order_revision(raw),
            stop_requested=lambda: self._inspection_stopped(session, run, raw))
        if result.get('status') == 'uncertain':
            run.uncertain = True
            run.stop.set()
            return False
        if result.get('status') != 'applied':
            run.in_flight = False
            if not self._inspection_stopped(session, run, raw):
                run.status, run.message = 'failed', '领取未执行，请刷新检查家园状态'
                run.stop.set()
            return False
        rewards = result.get('rewards') or (old.get('store_items') or []) + (old.get('store_extra_items') or [])
        if not await self._settle(session, raw, [], rewards, run, require_order=False, require_rewards=True,
                                  store_before=(building_uid, old.get('store_items'))):
            run.uncertain = True
            run.stop.set()
            return False
        run.in_flight = False
        return True

    async def _collect_home_products(self, session, raw, run):
        # The native production-management screen sends 3027 even when cached
        # store_info is empty. 3028 is empty: it acknowledges the request, never
        # a quantity or an inventory receipt. Plans use only absolute bag notices.
        if run.uncertain or self._inspection_stopped(session, run, raw):
            return False
        reserved = run.in_flight
        run.status, run.message = 'confirming', '正在请求收取家园产物'
        run.in_flight = True
        result = await session.provider.apply_order_operation({'action': 'claim_all'},
            expected_revision=order_revision(raw),
            stop_requested=lambda: self._inspection_stopped(session, run, raw))
        status = result.get('status')
        # No account identifiers, item payloads or credentials enter diagnostics.
        logger.info('Home collection request status=%s reserved_batch=%s',
                    status if status in {'applied', 'rejected', 'uncertain'} else 'invalid', reserved)
        if status == 'applied':
            run.in_flight = reserved
            run.next_collection_at = time.monotonic() + HOME_COLLECTION_INTERVAL_SECONDS
            run.status = 'waiting_materials' if reserved else 'running'
            run.message = '已请求收取家园产物，按实时库存继续任务'
            return True
        if status == 'uncertain':
            run.uncertain = True
            run.stop.set()
        else:
            run.in_flight = reserved
            if not self._inspection_stopped(session, run, raw):
                run.status, run.message = 'failed', '家园收取请求未执行，请刷新后检查'
                run.stop.set()
        return False

    @staticmethod
    def _queue_fields_known(row):
        work = row.get('work_old') or {}
        return (all(type(work.get(key)) is int and work[key] >= 0 for key in
                    ('recipe_id', 'recipe_intend_times', 'recipe_complete_times', 'recipe_state'))
                and work['recipe_state'] in (0, 1)
                and work['recipe_complete_times'] <= work['recipe_intend_times'])

    def _production_materials_busy(self, session, key, raw):
        previous = self.production_books.get(key)
        if not previous or previous[0] is not session.provider:
            return False
        book = previous[1]
        return not book.refresh(raw) or bool(book.reserves(raw))

    def _bounded_capable(self, raw):
        inventory = raw.get('inventory') or {}
        ordinary = [row for row in (raw.get('home') or {}).get('architectures', [])
            if (self.config.building(row.get('id')) or {}).get('architecture_type') == 3
            and row.get('work_old') and not row.get('cur_recipe') and not row.get('has_farm_data')
            and not row.get('is_hide') and not row.get('is_need_cut_ribbon')]
        return (type(inventory.get('movement_epoch')) is int
                and type(inventory.get('received_totals')) is dict
                and type(inventory.get('spent_totals')) is dict
                and bool(ordinary) and all(self._queue_fields_known(row) for row in ordinary))

    @staticmethod
    def _collection_signature(raw):
        rows = []
        for building in (raw.get('home') or {}).get('architectures', []):
            items = tuple(sorted((str(item['item_id']), item['count'])
                for item in building.get('store_items') or [] if type(item.get('count')) is int and item['count'] > 0))
            if items:
                completed = (building.get('work_old') or {}).get('recipe_complete_times')
                rows.append((str(building['uid']), items, completed if type(completed) is int else -1))
        return tuple(sorted(rows))

    def _queue_reserves(self, raw, run):
        book = run.production_book
        result = book.reserves(raw) if book is not None else {}
        owned = set(book.active) if book is not None else set()
        for building in self._buildings(raw):
            if (building['building_uid'] in owned or building['quantity_mode'] != 'batch'
                    or not building['editable'] or not building.get('recipe_id')):
                continue
            recipe = next((row for row in self.catalog['recipes']
                if row['building_id'] == building['building_id'] and row['recipe_id'] == building['recipe_id']), None)
            if recipe is None:
                continue
            intended, completed = building['intend_count'], building['completed_count']
            if type(intended) is int and type(completed) is int and intended >= completed >= 0:
                for cost in recipe['materials']:
                    result[cost['item_id']] = result.get(cost['item_id'], 0) + cost['count'] * (intended - completed)
            else:
                # Unknown external work cannot promise that its material is free.
                for cost in recipe['materials']:
                    item = cost['item_id']
                    result[item] = max(result.get(item, 0), raw['inventory']['items'].get(item, 0))
        return result

    def _external_queue_movements(self, raw, run):
        """Bound other queues before attributing account-wide bag movements."""
        spent, received, blocked = {}, {}, set()
        owned = set(run.production_book.active)
        for building in self._buildings(raw):
            if building['building_uid'] in owned:
                continue
            for product in building.get('store_items') or []:
                item, count = product['item_id'], product['count']
                if type(count) is int and count > 0:
                    received[item] = received.get(item, 0) + count
            candidates = [row for row in self.catalog['recipes']
                if row['building_id'] == building['building_id']]
            if building['recipe_id_known'] and not building['recipe_id'] and not building['reason']:
                if ((building['intend_count'] == 0 and building['completed_count'] == 0)
                        or run.production_book.is_confirmed_idle(raw, building['building_uid'])):
                    continue
                # A pre-existing completed queue has lost its recipe identity;
                # its delayed debit cannot safely count as this task's debit.
                for row in candidates:
                    blocked.update(item['item_id'] for item in row['materials'] + row['products'])
                continue
            recipe = next((row for row in candidates if row['recipe_id'] == building['recipe_id']), None)
            intended, completed = building['intend_count'], building['completed_count']
            finite = (recipe is not None and building['quantity_mode'] == 'batch'
                and building['reason'] is None and type(intended) is int
                and type(completed) is int and intended >= completed >= 0)
            if not finite:
                # An unknown or continuous source has no finite debit/credit
                # budget. Do not start an owned queue sharing its materials.
                for row in candidates:
                    blocked.update(item['item_id'] for item in row['materials'] + row['products'])
                continue
            # A potential cancellation refund is also an external receipt;
            # this allowance does not assume that the game will refund it.
            for total, amounts in ((spent, recipe['materials']),
                    (received, recipe['products'] + recipe['materials'])):
                for item in amounts:
                    # Include completed batches too: their bag notifications
                    # may arrive after their native completion notification.
                    total[item['item_id']] = total.get(item['item_id'], 0) + intended * item['count']
        return spent, received, blocked

    def _production_snapshot(self, raw, run):
        snapshot = self._snapshot(raw)
        if run.production_book is not None:
            reserves = self._queue_reserves(raw, run)
            snapshot['inventory'] = {item: max(0, count - reserves.get(item, 0))
                                     for item, count in snapshot['inventory'].items()}
        return snapshot

    def _cancelled_reservation_blocks_progress(self, raw, run):
        book = run.production_book
        if book is None or not book.frozen_reserves:
            return False
        frozen = book.frozen_reserves
        remaining = {item: max(0, count - frozen.get(item, 0))
                     for item, count in self._queue_reserves(raw, run).items()}
        snapshot = self._snapshot(raw)
        available = deepcopy(snapshot)
        available['inventory'] = {item: max(0, count - remaining.get(item, 0))
                                  for item, count in snapshot['inventory'].items()}
        settings = self._planner_settings(run.settings, run.settings.max_orders - run.completed)
        settings['allow_production'] = False
        if plan_orders(available, self.catalog, settings,
                       now=raw['server_time_seconds'])['inventory_selection']['slot_ids']:
            return True
        return choose_bounded_production(snapshot, self.catalog,
            {item: int(count) for item, count in run.settings.reserves.items()},
            remaining_orders=run.settings.max_orders - run.completed,
            queue_reserves=remaining,
            blocked_items=book.blocked_items() | self._external_queue_movements(raw, run)[2]) is not None

    async def _bounded_production_step(self, session, raw, run):
        book = run.production_book
        external_spent, external_received, external_blocked = self._external_queue_movements(raw, run)
        snapshot = self._snapshot(raw)
        native = {str(row['uid']): row for row in raw['home']['architectures']}
        # Unknown native counters retain the existing one-batch path on sessions
        # without queue evidence; mixed sessions never invent a queue baseline.
        snapshot['buildings'] = [row for row in snapshot['buildings']
            if row['quantity_mode'] == 'continuous' or self._queue_fields_known(native[row['building_uid']])]
        for building in snapshot['buildings']:
            queue = book.active.get(building['building_uid'])
            building['queue_owned'] = bool(queue and not queue.finished and queue.recipe_alive)
        proposal = choose_bounded_production(snapshot, self.catalog,
            {item: int(count) for item, count in run.settings.reserves.items()},
            remaining_orders=run.settings.max_orders - run.completed,
            queue_reserves=self._queue_reserves(raw, run),
            blocked_items=book.blocked_items() | external_blocked)
        if proposal is None:
            return await self._switch_production(session, raw, run, snapshot)
        run.status, run.message = 'confirming', '正在安排有限批次生产，其他独立建筑与交单继续调度'
        run.in_flight = True
        result = await session.provider.apply_order_operation(proposal['operation'],
            expected_revision=order_revision(raw),
            stop_requested=lambda: self._inspection_stopped(session, run, raw))
        if result.get('status') != 'applied':
            run.uncertain = result.get('status') == 'uncertain'
            run.in_flight = run.uncertain
            run.status, run.message = ('uncertain', '生产结果待核实，请在游戏中核对') if run.uncertain else ('failed', '生产未设置，请刷新后检查')
            run.stop.set()
            return True
        operation = proposal['operation']
        recipe = next(row for row in self.catalog['recipes']
            if row['recipe_id'] == operation['recipe_id'] and row['building_id'] == operation['building_id'])
        after = await self.raw(session)
        registered = (book.add(raw, after, proposal, recipe, owner_id=run.request_id,
            external_spent=external_spent, external_received=external_received) if proposal['costs'] else
            book.note_unreserved_start(raw, after, operation, recipe))
        if not registered:
            run.uncertain = True
            run.stop.set()
            return True
        run.in_flight = False
        run.status, run.message = 'running', '生产已安排，按实际库存继续交单和调度'
        return True

    async def _switch_production(self, session, raw, run, snapshot):
        book = run.production_book
        needed = {}
        for order in snapshot['orders']:
            if order.get('is_unlock') is True and order.get('order_type') is not None:
                for item in order['materials']:
                    needed[item['item_id']] = needed.get(item['item_id'], 0) + item['count']
        gaps = {item for item, count in needed.items()
            if count > max(0, snapshot['inventory'].get(item, 0) - int(run.settings.reserves.get(item, '0')))}
        wanted = material_dependency_ids(self.catalog, gaps)
        for uid, queue in list(book.active.items()):
            if (queue.owner_id != run.request_id or not queue.recipe_alive
                    or wanted.intersection(queue.products)):
                continue
            building = next((row for row in snapshot['buildings'] if row['building_uid'] == uid), None)
            recipe = next((row for row in self.catalog['recipes']
                if row['recipe_id'] == queue.recipe_id and row['building_id'] == queue.building_id), None)
            if building is None or recipe is None:
                continue
            if queue.current_completed < queue.times:
                timing = estimate_production_time(building, recipe,
                    now=raw['server_time_seconds'], revision=order_revision(raw))
                remaining = timing.get('safe_next_batch_seconds')
                if timing['time_source'] == 'unknown' or type(remaining) is not int or remaining <= 5:
                    continue
            trial = deepcopy(snapshot)
            candidate = next(row for row in trial['buildings'] if row['building_uid'] == uid)
            candidate.update(recipe_id=None, current_recipe_id=None, recipe_state=0,
                is_producing=False, intend_count=0, completed_count=0)
            blocked = {item for other_uid, other in book.active.items() if other_uid != uid and not other.finished
                       for item in other.costs.keys() | other.products.keys()}
            blocked.update(self._external_queue_movements(raw, run)[2])
            proposal = choose_bounded_production(trial, self.catalog,
                {item: int(count) for item, count in run.settings.reserves.items()},
                remaining_orders=run.settings.max_orders - run.completed,
                queue_reserves=self._queue_reserves(raw, run), blocked_items=blocked)
            if (proposal is None or proposal['operation']['building_uid'] != uid
                    or proposal['operation']['recipe_id'] == queue.recipe_id):
                continue
            run.status, run.message = 'confirming', '正在取消已满足需求的生产队列，确认后重新规划'
            run.in_flight = True
            result = await session.provider.apply_order_operation({'action': 'cancel_production',
                'building_uid': uid, 'building_id': queue.building_id, 'recipe_id': queue.recipe_id},
                expected_revision=order_revision(raw),
                stop_requested=lambda: self._inspection_stopped(session, run, raw))
            if result.get('status') != 'applied':
                run.uncertain = result.get('status') == 'uncertain'
                run.in_flight = run.uncertain
                run.status, run.message = ('uncertain', '取消生产结果待核实，请在游戏中核对') if run.uncertain else ('failed', '生产未取消，请刷新后检查')
                run.stop.set()
                return True
            if not book.retire(uid, await self.raw(session)):
                run.uncertain = True
                run.stop.set()
                return True
            run.in_flight = False
            run.status, run.message = 'running', '取消已确认，按实际库存重新安排生产；未推定材料返还'
            return True
        return False

    async def _run(self, session, run):
        try:
            while not run.stop.is_set():
                raw = await self.raw(session)
                if self._execution_reason(raw):
                    raise ApiError('ORDER_STATE_UNAVAILABLE', '账号数据不完整，任务已停止', 409)
                if tuple(raw['role_key']) != run.role_key:
                    raise ApiError('ORDER_ROLE_CHANGED', '角色已变化，任务已停止', 409)
                if run.production_book is not None and not run.production_book.refresh(raw):
                    run.uncertain = True
                    run.message = '生产队列或收支状态已变化，请核对后再继续'
                    break
                if run.completed >= run.settings.max_orders or raw['server_time_seconds'] >= run.deadline:
                    run.status, run.message = 'completed', '已达到本次任务目标或截止时间'
                    return
                signature = self._collection_signature(raw) if run.production_book is not None else None
                newly_ready = bool(signature and signature != run.last_collection_signature)
                if not run.in_flight and not run.uncertain and (newly_ready or time.monotonic() >= run.next_collection_at):
                    run.last_collection_signature = signature
                    await self._collect_home_products(session, raw, run)
                    # Replan only from a fresh post-collection inventory/revision.
                    continue
                snapshot = self._production_snapshot(raw, run)
                plan = plan_orders(snapshot, self.catalog,
                    self._planner_settings(run.settings, run.settings.max_orders - run.completed), now=raw['server_time_seconds'])
                slots = plan['inventory_selection']['slot_ids']
                if not slots:
                    if run.settings.allow_production and await self._production_step(session, raw, run, plan):
                        continue
                    if self._cancelled_reservation_blocks_progress(raw, run):
                        run.uncertain = True
                        run.uncertainty_reason = '取消生产后的材料收支待核对，已保留相关额度；请核对游戏库存后解除暂停'
                        break
                    run.status, run.message = 'waiting_materials', '等待库存或生产通知；仅使用已核实的生产和领取条件'
                    await self._wait(run)
                    continue
                slot = next(row for row in raw['orders']['orders'] if row['slot_id'] == slots[0])
                run.status, run.message = 'confirming', '正在确认交单结果'
                run.in_flight = True
                result = await session.provider.apply_order_operation({'action': 'complete', 'slot_id': slots[0]},
                    expected_revision=order_revision(raw),
                    stop_requested=lambda: self._inspection_stopped(session, run, raw))
                if result.get('status') == 'uncertain':
                    run.uncertain = True
                    break
                if result.get('status') != 'applied':
                    run.in_flight = False
                    run.status, run.message = 'failed', '交单未执行或被服务器拒绝，请刷新后检查'
                    return
                rewards = result.get('rewards', [])
                run.completed += 1
                run.cans += sum(row['count'] for row in rewards if row['item_id'] == '54001')
                if not await self._settle(session, raw, slot['materials'], rewards, run,
                                          movement_proof=run.production_book is not None):
                    run.uncertain = True
                    break
                if run.production_book is not None:
                    run.production_book.note_order(
                        [row for row in slot['materials'] if row['count'] > 0],
                        [row for row in rewards if row['count'] > 0])
                run.in_flight = False
                run.status = 'running'
            if run.uncertain:
                run.status, run.message = 'uncertain', run.uncertainty_reason or '操作或库存更新结果待核实，已停止后续操作，请在游戏中核对'
            elif run.status != 'failed':
                run.status, run.message = 'stopped', '已取消任务；已在游戏中开始的生产继续运行'
        except asyncio.CancelledError:
            run.uncertain = run.uncertain or run.in_flight
            run.status = 'uncertain' if run.uncertain else 'stopped'
            run.message = '会话结束；已发送的操作需在游戏中核对' if run.uncertain else '会话结束，订单任务已停止'
        except Exception:
            run.uncertain = run.uncertain or run.in_flight
            run.status = 'uncertain' if run.uncertain else 'failed'
            run.message = '操作结果待核实，请在游戏中核对' if run.uncertain else '账号状态不可用，订单任务已停止'
        finally:
            if not run.uncertain and self.roles.get(run.role_key) is run:
                self.roles.pop(run.role_key, None)

    async def _production_step(self, session, raw, run, plan):
        if run.production_book is not None:
            return await self._bounded_production_step(session, raw, run)
        # Readiness/actual store checks live in the native operation. Never cancel
        # existing work, move workers or buy materials to make a plan feasible.
        wanted = material_dependency_ids(self.catalog,
            {row['item_id'] for row in plan.get('materials', []) if (row.get('shortage') or 0) > 0})
        for building in self._buildings(raw):
            if run.kind == 'orders':
                # The scheduled native collect-all owns order-task collection.
                # A stale positive store must not trigger a second single claim.
                break
            if not building['editable'] or not building.get('store_items'):
                continue
            if not any(str(row['item_id']) in wanted and row['count'] > 0 for row in building['store_items']):
                continue
            await self._claim_building(session, raw, run, building['building_uid'], building['building_id'])
            return True
        proposal = choose_replenishment(self._snapshot(raw), self.catalog,
            {item: int(count) for item, count in run.settings.reserves.items()}, batch_limit=1)
        if proposal:
            if run.kind == 'orders' and proposal['costs'] and any(
                    type(raw.get('inventory', {}).get(name)) is not dict
                    for name in ('received_totals', 'spent_totals')):
                run.status, run.message = 'failed', '库存收支记录未就绪，请重新登录后再安排生产'
                run.stop.set()
                return True
            run.status, run.message = 'confirming', '正在设置闲置建筑补货；新配方耗时等待服务器生产通知'
            run.in_flight = True
            result = await session.provider.apply_order_operation(proposal['operation'],
                expected_revision=order_revision(raw), stop_requested=run.stop.is_set)
            if result.get('status') == 'uncertain':
                run.uncertain = True
                run.stop.set()
            elif result.get('status') != 'applied':
                run.in_flight = False
                run.status, run.message = 'failed', '生产未设置，请刷新检查工作噜咪、材料和建筑状态'
                run.stop.set()
            elif proposal['costs']:
                # Keep this operation's complete material allocation isolated.
                # Prepayment vs per-batch charging is not inferred: no further
                # allocation until one real batch, claim and bag settlement.
                if not await self._finish_material_batch(session, raw, proposal, run):
                    run.uncertain = True
                    run.stop.set()
                else:
                    run.in_flight = False
            else:
                run.in_flight = False
            return True
        return False

    async def _finish_material_batch(self, session, before, proposal, run):
        operation = proposal['operation']
        recipe = next(row for row in self.catalog['recipes'] if row['recipe_id'] == operation['recipe_id']
                      and row['building_id'] == operation['building_id'])
        if run.kind == 'orders':
            return await self._finish_order_material_batch(session, before, proposal['costs'], recipe['products'], run)
        old = next(row for row in before['home']['architectures'] if str(row['uid']) == operation['building_uid'])
        original = {row['item_id']: row['count'] for row in old.get('store_items') or []}
        run.status, run.message = 'waiting_materials', '已安排单批加工，等待产物；本批材料保留到领取和库存核销后'
        while not run.stop.is_set():
            current = await self.raw(session)
            reason = self._inspection_reason(current) if run.kind == 'inspection' else self._execution_reason(current)
            if reason or tuple(current['role_key']) != run.role_key or current['server_time_seconds'] >= run.deadline:
                return False
            row = next((row for row in current['home']['architectures'] if str(row['uid']) == operation['building_uid']), None)
            held = {item['item_id']: item['count'] for item in (row or {}).get('store_items') or []}
            if all(held.get(item['item_id'], 0) >= original.get(item['item_id'], 0) + item['count'] for item in recipe['products']):
                run.status, run.message = 'confirming', '产物已就绪，正在领取并核销本批材料'
                result = await session.provider.apply_order_operation({'action': 'claim',
                    'building_uid': operation['building_uid'], 'building_id': operation['building_id']},
                    expected_revision=order_revision(current),
                    stop_requested=lambda: self._inspection_stopped(session, run, current))
                if result.get('status') != 'applied':
                    return False
                rewards = result.get('rewards') or (row.get('store_items') or []) + (row.get('store_extra_items') or [])
                return await self._settle(session, before, proposal['costs'], rewards, run,
                    require_order=False, min_sequence=current['inventory']['sequence'],
                    store_before=(operation['building_uid'], row.get('store_items')), require_rewards=True)
            await self._wait(run)
        return False

    @staticmethod
    def _batch_movements_confirmed(before, current, costs, products):
        old, new = before.get('inventory') or {}, current.get('inventory') or {}
        if (not new.get('valid') or new.get('sequence', 0) <= old.get('sequence', 0)
                or new.get('movement_epoch', 0) != old.get('movement_epoch', 0)):
            return False
        for name, rows in (('spent_totals', costs), ('received_totals', products)):
            previous, latest = old.get(name), new.get(name)
            if type(previous) is not dict or type(latest) is not dict:
                return False
            required = {}
            for row in rows:
                required[row['item_id']] = required.get(row['item_id'], 0) + row['count']
            for item, count in required.items():
                start, end = previous.get(item, 0), latest.get(item, 0)
                if type(start) is not int or type(end) is not int or start < 0 or end < start + count:
                    return False
        return True

    async def _finish_order_material_batch(self, session, before, costs, products, run):
        # A material reservation prevents new allocations, not collection. Net
        # inventory alone is insufficient when collection replenishes an input:
        # use observed debits/credits, never infer quantity from the empty ACK.
        run.status, run.message = 'waiting_materials', '等待本批扣料和产物入库；每分钟继续收取家园产物'
        if not run.next_collection_at:
            run.next_collection_at = time.monotonic() + HOME_COLLECTION_INTERVAL_SECONDS
        while not run.stop.is_set():
            current = await self.raw(session)
            if self._execution_reason(current) or self._inspection_stopped(session, run, current):
                return False
            if self._batch_movements_confirmed(before, current, costs, products):
                return True
            if time.monotonic() >= run.next_collection_at:
                if not await self._collect_home_products(session, current, run):
                    return False
                continue
            await self._wait(run)
        return False
