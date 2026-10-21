"""Single-attempt native home settings with fresh-state and notify confirmation."""
import asyncio
from copy import deepcopy
import math

from ..home_work_config import HomeWorkConfig, WORK_MUTEX_MASK
from .client import GameClientError
from .home_codec import HomeProtoCodec


_EXPECTED = frozenset({'building_id', 'level', 'lumi_uid', 'recipe_id', 'recipe_times',
                       'level_up_complete_time', 'is_need_cut_ribbon'})
_CHANGE = frozenset({'building_uid', 'building_id', 'lumi_uid', 'recipe_id', 'recipe_times', 'work_type_id'})
_ACKS = {3401: 3402, 3403: 3404}


def _integer(value, minimum=1, bits=31):
    return type(value) is int and minimum <= value < 1 << bits


def _uid(value):
    return (type(value) is str and value.isascii() and value.isdecimal()
            and 0 < len(value) <= 20 and value == str(int(value)) and 0 < int(value) < 1 << 64)


def _configuration(row):
    work = row.get('work_old') or {}
    return {'building_id': row.get('id'), 'level': row.get('level'),
            'lumi_uid': work.get('lumi_uid'), 'recipe_id': work.get('recipe_id'),
            'recipe_times': work.get('recipe_intend_times'),
            'level_up_complete_time': row.get('level_up_complete_time'),
            'is_need_cut_ribbon': row.get('is_need_cut_ribbon')}


class _Rejected(Exception):
    pass


class GameHomeOperations:
    def __init__(self, client, state, *, confirmation_timeout=5.0, can_write=None):
        if (type(confirmation_timeout) not in (int, float) or not math.isfinite(confirmation_timeout)
                or not 0 < confirmation_timeout <= 60):
            raise ValueError('GAME_HOME_INVALID_TIMEOUT')
        self._client, self._state = client, state
        self._timeout, self._can_write = confirmation_timeout, can_write
        self._codec, self._config = HomeProtoCodec(), HomeWorkConfig()
        self._lock = asyncio.Lock()
        self.blocked = False

    def _context(self, change, baseline):
        if self.blocked or self._client.closed or (self._can_write is not None and self._can_write() is not True):
            raise _Rejected()
        snapshot = self._state.snapshot()
        if snapshot.get('valid') is not True:
            raise _Rejected()
        matches = [row for row in snapshot['architectures'] if row.get('uid') == change['building_uid']]
        if len(matches) != 1:
            raise _Rejected()
        row = matches[0]
        if (_configuration(row) != baseline or row.get('id') != change['building_id']
                or not isinstance(row.get('work_old'), dict) or row.get('is_hide')
                or row.get('has_farm_data') or row.get('is_need_cut_ribbon')
                or row.get('level_up_complete_time') not in (None, 0, '0')):
            raise _Rejected()
        config = self._config.building(row['id'])
        level = self._config.level(row['id'], row.get('level'))
        if not config or config['architecture_type'] == 19 or not level or not level['recipe_ids']:
            raise _Rejected()
        if 'work_type_id' in change and change['work_type_id'] != config['work_type_id']:
            raise _Rejected()
        return snapshot, row, config, level

    def _worker_allowed(self, snapshot, row, uid, config):
        if snapshot.get('roster_ready') is not True or snapshot.get('dispatch_ready') is not True:
            raise _Rejected()
        if uid in snapshot.get('dispatch_lumi_uids', ()):
            raise _Rejected()
        workers = [worker for worker in snapshot['workers'] if worker.get('uid') == uid]
        if len(workers) != 1:
            raise _Rejected()
        worker = workers[0]
        raw = worker.get('raw_state')
        if (type(raw) is not str or not raw.isascii() or not raw.isdecimal() or len(raw) > 20
                or not 0 <= int(raw) < 1 << 64 or int(raw) & WORK_MUTEX_MASK):
            raise _Rejected()
        types = self._config.work_types(worker.get('id'))
        if types is None or config['work_type_id'] not in types:
            raise _Rejected()
        for occupancy in worker.get('occupancy', ()):
            if occupancy.get('kind') != 'work_old' or occupancy.get('architecture_uid') != row['uid']:
                raise _Rejected()
        # Check actual references too; absence of a convenient occupancy list is not proof of freedom.
        for other in snapshot['architectures']:
            if other.get('uid') != row['uid'] and (other.get('work_old') or {}).get('lumi_uid') == uid:
                raise _Rejected()
            if (other.get('cur_recipe') or {}).get('recipe_lumi_uid') == uid:
                raise _Rejected()
            if uid in other.get('interaction_uids', ()) or uid in other.get('dispatch_uids', ()):
                raise _Rejected()
            if any(item.get('lumi_uid') == uid for item in (other.get('dorm') or []) + (other.get('travel') or [])):
                raise _Rejected()

    @staticmethod
    def _validate(change, baseline):
        if (type(change) is not dict or type(baseline) is not dict or set(baseline) != _EXPECTED
                or set(change) - _CHANGE or not {'building_uid', 'building_id'} <= set(change)
                or not set(change) & {'lumi_uid', 'recipe_id', 'recipe_times'}
                or not _integer(change['building_uid']) or not _integer(change['building_id'])):
            raise _Rejected()
        for key in ('recipe_id', 'recipe_times', 'work_type_id'):
            if key in change and not _integer(change[key], minimum=0 if key == 'recipe_times' else 1):
                raise _Rejected()
        if 'lumi_uid' in change and not _uid(change['lumi_uid']):
            raise _Rejected()

    def _cancel_context(self, change, baseline):
        snapshot, row, config, level = self._context(change, baseline)
        current = _configuration(row)
        if (any(type(current[key]) is not type(value) for key, value in baseline.items())
                or config['architecture_type'] in (2, 12, 19) or row.get('cur_recipe') is not None
                or change['recipe_id'] != baseline['recipe_id']
                or str(change['recipe_id']) not in level['recipe_ids']
                or not _integer(baseline['recipe_times']) or not _uid(baseline['lumi_uid'])
                or type(snapshot.get('sequence')) is not int or snapshot['sequence'] < 0):
            raise _Rejected()
        return snapshot, row, config, level

    def _matches(self, change, fields, after_sequence=None):
        snapshot = self._state.snapshot()
        if (snapshot.get('valid') is not True or (after_sequence is not None
                and (type(snapshot.get('sequence')) is not int or snapshot['sequence'] <= after_sequence))):
            return None
        row = next((row for row in snapshot['architectures'] if row.get('uid') == change['building_uid']), None)
        if row is None or row.get('id') != change['building_id']:
            return None
        work = row.get('work_old') or {}
        for key, value in fields.items():
            if work.get('recipe_intend_times' if key == 'recipe_times' else key) != value:
                return None
        return _configuration(row)

    async def _confirm(self, change, fields, after_sequence=None):
        deadline = asyncio.get_running_loop().time() + self._timeout
        while True:
            matched = self._matches(change, fields, after_sequence)
            if matched is not None:
                return matched
            snapshot = self._state.snapshot()
            remaining = deadline - asyncio.get_running_loop().time()
            if (not snapshot.get('valid') or remaining <= 0
                    or not await self._state.wait_for_update(snapshot['sequence'], remaining)):
                return None

    async def _step(self, change, baseline, command, fields, desired, worker):
        sent = False
        sent_sequence = None
        cancelling = command == 3403 and fields['op'] == 2
        def before_send():
            nonlocal sent, sent_sequence
            try:
                context = self._cancel_context if cancelling else self._context
                snapshot, row, config, _ = context(change, baseline)
                if worker is not None:
                    self._worker_allowed(snapshot, row, worker, config)
                if cancelling:
                    sent_sequence = snapshot['sequence']
            except Exception:
                return False
            sent = True
            return True
        try:
            response = await self._client.request(command, _ACKS[command], self._codec.encode(command, **fields),
                                                  before_send=before_send)
            if not sent or response.command != _ACKS[command] or response.code != 0 or response.remaining != 0:
                raise ValueError()
            ack = self._codec.decode(_ACKS[command], response.data)
            for key in ('archi_uid', 'archi_id', 'op') + (('lumi_uid',) if command == 3401 else ()):
                if not ack.HasField(key) or getattr(ack, key) != fields[key]:
                    raise ValueError()
            current = await self._confirm(change, desired, sent_sequence)
            if current is None:
                raise ValueError()
            return 'applied', current
        except asyncio.CancelledError:
            self.blocked = sent
            raise asyncio.CancelledError() from None
        except GameClientError as error:
            if error.code in {'GAME_REQUEST_REJECTED', 'GAME_REQUEST_NOT_SENT'}:
                return 'rejected', baseline
        except Exception:
            pass
        if sent:
            self.blocked = True
            return 'uncertain', baseline
        return 'rejected', baseline

    async def cancel(self, change, *, expected):
        """Stop one explicit ordinary queue; no refund or counter reset is inferred."""
        async with self._lock:
            if self.blocked:
                return {'status': 'uncertain', 'message': '上次家园应用结果待核实，请重新登录后再操作'}
            try:
                change, baseline = deepcopy(change), deepcopy(expected)
                self._validate(change, baseline)
                if set(change) != {'building_uid', 'building_id', 'recipe_id'}:
                    raise _Rejected()
                snapshot, row, config, _ = self._cancel_context(change, baseline)
                self._worker_allowed(snapshot, row, baseline['lumi_uid'], config)
            except Exception:
                return {'status': 'rejected', 'message': '家园状态已变化或取消条件不满足，请刷新后重试'}
            fields = {'archi_uid': change['building_uid'], 'archi_id': change['building_id'],
                      'recipe_id': change['recipe_id'], 'recipe_times': 0, 'op': 2}
            status, _ = await self._step(change, baseline, 3403, fields, {'recipe_id': 0}, baseline['lumi_uid'])
            return {'status': status, 'message': '生产取消已确认' if status == 'applied' else
                    '生产取消结果待核实，请在游戏中核对' if status == 'uncertain' else '生产未取消，请刷新后重试'}

    async def apply(self, change, *, expected):
        async with self._lock:
            if self.blocked:
                return {'status': 'uncertain', 'message': '上次家园应用结果待核实，请重新登录后再操作'}
            change, baseline = deepcopy(change), deepcopy(expected)
            try:
                self._validate(change, baseline)
                snapshot, row, config, level = self._context(change, baseline)
                steps = []
                desired = {}
                worker = change.get('lumi_uid', baseline['lumi_uid'])
                if worker is not None and worker != baseline['lumi_uid']:
                    self._worker_allowed(snapshot, row, worker, config)
                    steps.append((3401, {'lumi_uid': int(worker)}, {'lumi_uid': worker}, '工作噜咪'))
                if 'recipe_id' in change or 'recipe_times' in change:
                    recipe = change.get('recipe_id', baseline['recipe_id'])
                    times = change.get('recipe_times', baseline['recipe_times'])
                    if (not _integer(recipe) or str(recipe) not in level['recipe_ids']
                            or not _integer(times, minimum=0)
                            or (times != 0 if config['architecture_type'] in (2, 12) else times == 0)):
                        raise _Rejected()
                    if not _uid(worker if worker is not None else baseline['lumi_uid']):
                        raise _Rejected()
                    if recipe != baseline['recipe_id'] or times != baseline['recipe_times']:
                        if config['architecture_type'] not in (2, 12) and baseline['recipe_id'] != 0:
                            raise _Rejected()
                        steps.append((3403, {'recipe_id': recipe, 'recipe_times': times},
                                      {'recipe_id': recipe, 'recipe_times': times}, '生产设置'))
            except Exception:
                return {'status': 'rejected', 'message': '家园状态已变化或设置条件不满足，请刷新后重试'}
            confirmed = []
            for command, values, changes, label in steps:
                desired.update(changes)
                fields = {'archi_uid': change['building_uid'], 'archi_id': change['building_id'], 'op': 1, **values}
                status, _ = await self._step(change, baseline, command, fields, desired, worker)
                if status != 'applied':
                    detail = '结果待核实，请在游戏中核对' if status == 'uncertain' else '未应用，请刷新后重试'
                    return {'status': 'uncertain' if status == 'uncertain' else 'partial' if confirmed else status,
                            'message': ('、'.join(confirmed) + '已确认；' if confirmed else '') + label + detail}
                confirmed.append(label)
                baseline = {**baseline, **changes}
            return {'status': 'applied', 'message': '家园设置已同步' if confirmed else '当前家园设置已一致'}
