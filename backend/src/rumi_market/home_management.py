"""Session-scoped home views, draft validation and bounded request deduplication."""
from dataclasses import dataclass
import asyncio
import hashlib
import json

from .errors import ApiError
from .home_models import HomeAccountView, HomeApplyItem, HomeApplyResult, HomeBuildingView, HomeWorkerView
from .home_work_config import HomeWorkConfig, WORK_MUTEX_MASK


@dataclass(repr=False)
class HomeRequestRecord:
    fingerprint: str
    result: HomeApplyResult | None = None


def configuration(building):
    work = building.get('work_old') or {}
    return {'building_id': building.get('id'), 'level': building.get('level'),
            'lumi_uid': work.get('lumi_uid'), 'recipe_id': work.get('recipe_id'),
            'recipe_times': work.get('recipe_intend_times'),
            'level_up_complete_time': building.get('level_up_complete_time'),
            'is_need_cut_ribbon': building.get('is_need_cut_ribbon')}


def _identifier(value):
    return str(value) if type(value) is int and value > 0 else None


class HomeManagementService:
    def __init__(self, catalog, sessions):
        self.catalog, self.sessions = catalog, sessions
        self.config = HomeWorkConfig()

    @staticmethod
    def _require(session):
        if not session.active or not session.view.authenticated or session.view.status != 'connected':
            raise ApiError('GAME_SESSION_EXPIRED', '游戏会话已过期，请重新登录', 401)

    async def _raw(self, session):
        self._require(session)
        try:
            snapshot = await session.provider.home_snapshot()
        except ApiError as error:
            if error.code == 'GAME_SESSION_EXPIRED':
                await self.sessions.expire(session)
                raise ApiError('GAME_SESSION_EXPIRED', '游戏会话已过期，请重新登录', 401) from None
            raise ApiError('HOME_STATE_UNAVAILABLE', '家园实时数据暂不可用，请稍后刷新', 503, True) from None
        except Exception:
            raise ApiError('HOME_STATE_UNAVAILABLE', '家园实时数据暂不可用，请稍后刷新', 503, True) from None
        self._require(session)
        if type(snapshot) is not dict:
            raise ApiError('HOME_STATE_UNAVAILABLE', '家园实时数据暂不可用', 503)
        return snapshot

    def _building(self, row):
        config = self.config.building(row.get('id'))
        if config is None:
            return None
        level = self.config.level(row.get('id'), row.get('level'))
        work = row.get('work_old')
        current = work or row.get('cur_recipe') or {}
        worker = current.get('lumi_uid') if work is not None else current.get('recipe_lumi_uid')
        allowed = level['recipe_ids'] if level is not None else None
        reason = None
        if config['architecture_type'] == 19:
            reason = '活动农田暂只支持查看'
        elif work is None:
            reason = '当前建筑的生产方式暂只支持查看'
        elif level is None or not allowed:
            reason = '尚未取得当前等级可用的配方'
        elif row.get('is_hide'):
            reason = '当前建筑已隐藏'
        elif row.get('is_need_cut_ribbon') or row.get('level_up_complete_time') not in (None, '0', 0):
            reason = '请先在游戏确认建筑升级状态'
        elif row.get('has_farm_data'):
            reason = '活动农田暂只支持查看'
        quantity_mode = ('continuous' if config['architecture_type'] in {2, 12} else 'batch') if work is not None else 'unverified'
        production_reason = reason
        if production_reason is None and quantity_mode == 'batch' and current.get('recipe_id', 0) not in (None, 0):
            production_reason = '生产进行中，请先在游戏停止当前生产再更换物品或次数'
        return HomeBuildingView(building_uid=str(row['uid']), building_id=str(row['id']), level=row.get('level'),
            current_recipe_id=_identifier(current.get('recipe_id')),
            assigned_lumi_uids=[worker] if worker not in (None, '0') else [],
            production_count=current.get('recipe_intend_times') if work is not None else None,
            completed_count=current.get('recipe_complete_times') if work is not None else None,
            quantity_mode=quantity_mode,
            editable=reason is None, reason=reason, production_editable=production_reason is None,
            production_reason=production_reason, allowed_recipe_ids=allowed, assignment_limit=1 if work is not None else None)

    def _workers(self, raw, buildings):
        result = []
        for worker in raw.get('workers', []):
            pet_id = _identifier(worker.get('id'))
            if pet_id is None:
                continue
            pet = self.catalog.pet(pet_id)
            types = self.config.work_types(pet_id)
            occupancy = worker.get('occupancy') or []
            work = next((row for row in occupancy if row['kind'] in {'work_old', 'recipe'}), None)
            reason = None
            if not raw.get('roster_ready'):
                reason = '工作噜咪尚未同步完成'
            elif not raw.get('dispatch_ready', False):
                reason = '家园外派状态尚未同步完成'
            elif worker.get('uid') in raw.get('dispatch_lumi_uids', []):
                reason = '正在外派'
            elif worker.get('raw_state') is None:
                reason = '噜咪状态尚未同步完成'
            elif int(worker['raw_state']) & WORK_MUTEX_MASK:
                reason = '当前状态不能上工'
            elif any(row['kind'] not in {'work_old', 'recipe'} for row in occupancy):
                reason = '正在宿舍、旅行或其他家园任务中'
            elif types is None:
                reason = '尚未核实该噜咪的工作能力'
            eligible = []
            if reason is None:
                for building in buildings:
                    if not building.editable:
                        continue
                    config = self.config.building(building.building_id)
                    if config['work_type_id'] in types and all(str(row['architecture_uid']) == building.building_uid for row in occupancy):
                        eligible.append(building.building_uid)
            result.append(HomeWorkerView(lumi_uid=worker['uid'], lumi_id=pet_id, name=pet.display_name,
                image_url=pet.image_url, work_aptitude=worker.get('work_aptitude'), work_energy=worker.get('work_energy'),
                assigned_building_uid=str(work['architecture_uid']) if work else None,
                eligible_building_uids=eligible if raw.get('roster_ready') else None,
                selectable=reason is None and bool(eligible), reason=reason))
        return result

    def _view(self, session, raw, *, result=None):
        last = result if result is not None else session.home_last_apply
        if raw.get('valid') is not True:
            message = {
                'HOME_VERSION_UNVERIFIED': '当前游戏版本尚未完成家园兼容校验，请等待适配更新',
                'HOME_SCHEMA_UNAVAILABLE': '家园协议资源未通过校验，请更新程序后重试',
            }.get(raw.get('unavailable_reason'), '家园数据尚未同步完成，请稍后刷新')
            return HomeAccountView(ready=False, revision=None, observed_at=raw.get('observed_at'),
                message=message, buildings=[], roster=[], last_apply=last)
        instances = [row for row in raw.get('architectures', []) if self.config.building(row.get('id'))]
        buildings = [self._building(row) for row in instances]
        signature = sorted(((str(row['uid']), configuration(row)) for row in instances), key=lambda row: int(row[0]))
        role_key = session.view.role.id if session.view.role is not None else None
        encoded = json.dumps([session.id, role_key, session.home_revision, signature], sort_keys=True, separators=(',', ':')).encode()
        revision = hashlib.sha256(encoded).hexdigest()
        return HomeAccountView(ready=True, revision=revision, observed_at=raw.get('observed_at'),
            message=None, buildings=buildings, roster=self._workers(raw, buildings), last_apply=last)

    async def state(self, session, *, result=None):
        return self._view(session, await self._raw(session), result=result)

    def _normalize(self, change, before, raw):
        building = next((row for row in before.buildings if row.building_uid == change.building_uid), None)
        if building is None or not building.editable:
            raise ApiError('HOME_INVALID_SETTINGS', '建筑不存在或当前不能修改设置', 422)
        original = next(row for row in raw['architectures'] if str(row['uid']) == change.building_uid)
        normalized = {'building_uid': original['uid'], 'building_id': original['id']}
        fields = change.model_fields_set
        if 'lumi_uids' in fields:
            uid = change.lumi_uids[0]
            if uid not in building.assigned_lumi_uids:
                worker = next((row for row in before.roster if row.lumi_uid == uid), None)
                if worker is None or not worker.selectable or building.building_uid not in (worker.eligible_building_uids or []):
                    raise ApiError('HOME_INVALID_SETTINGS', '所选噜咪当前不能在该建筑工作', 422)
                normalized['lumi_uid'] = uid
        if 'recipe_id' in fields or 'production_count' in fields:
            if not building.production_editable:
                raise ApiError('HOME_INVALID_SETTINGS', building.production_reason or '当前不能修改生产设置', 422)
            recipe_id = change.recipe_id if 'recipe_id' in fields else building.current_recipe_id
            if recipe_id is None or recipe_id not in (building.allowed_recipe_ids or []):
                raise ApiError('HOME_INVALID_SETTINGS', '配方不属于当前建筑的可用配方', 422)
            if building.quantity_mode == 'continuous':
                if 'production_count' in fields and change.production_count != 0:
                    raise ApiError('HOME_INVALID_SETTINGS', '该建筑使用持续生产，无需填写次数', 422)
                count = 0
            else:
                if 'production_count' not in fields or change.production_count <= 0:
                    raise ApiError('HOME_INVALID_SETTINGS', '请填写正整数生产次数', 422)
                count = change.production_count
            if not building.assigned_lumi_uids and 'lumi_uid' not in normalized:
                raise ApiError('HOME_INVALID_SETTINGS', '请先选择工作噜咪', 422)
            normalized.update(recipe_id=int(recipe_id), recipe_times=count)
        return normalized, configuration(original)

    async def apply(self, session, body):
        async with session.lock:
            self._require(session)
            fingerprint = hashlib.sha256(body.model_dump_json(exclude_unset=True).encode()).hexdigest()
            previous = session.home_requests.get(body.request_id)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise ApiError('HOME_REQUEST_CONFLICT', '该应用请求已经使用，请刷新后重新编辑', 409)
                return await self.state(session, result=previous.result)
            if session.home_write_uncertain:
                raise ApiError('HOME_OUTCOME_UNKNOWN', '上次应用结果尚未确认，请在游戏核对后重新登录', 409)
            raw = await self._raw(session)
            before = self._view(session, raw)
            if not before.ready or before.revision != body.expected_revision:
                raise ApiError('HOME_STATE_CONFLICT', '家园设置已变化，请刷新后重新编辑', 409)
            changes = [self._normalize(change, before, raw) for change in body.changes]
            assigned = [change['lumi_uid'] for change, _ in changes if 'lumi_uid' in change]
            if len(assigned) != len(set(assigned)):
                raise ApiError('HOME_INVALID_SETTINGS', '同一只噜咪不能分配给多个建筑', 422)
            # Retired request records must remain stale even if game settings cycle back.
            session.home_revision += 1
            while len(session.home_requests) >= 32:
                session.home_requests.pop(next(iter(session.home_requests)))
            record = HomeRequestRecord(fingerprint)
            session.home_requests[body.request_id] = record
            session.home_last_apply = HomeApplyResult(request_id=body.request_id, status='applying', message='正在应用到游戏', results=[])
            results = []
            stopped = False
            for change, expected in changes:
                uid = str(change['building_uid'])
                if stopped:
                    results.append(HomeApplyItem(building_uid=uid, status='skipped', message='前一项未完成，本项未发送'))
                    continue
                self._require(session)
                try:
                    if set(change) == {'building_uid', 'building_id'}:
                        status = 'applied'
                    else:
                        response = await session.provider.apply_home_change(change, expected=expected)
                        status = response.get('status') if type(response) is dict else 'uncertain'
                        if status not in {'applied', 'partial', 'rejected', 'uncertain'}:
                            status = 'uncertain'
                    after = await self._raw(session)
                    current = next((row for row in after.get('architectures', []) if row['uid'] == change['building_uid']), None)
                    work = current.get('work_old') or {} if current else {}
                    if status == 'applied' and (not after.get('valid') or current is None or any(work.get(field) != change[key]
                        for field, key in [('lumi_uid', 'lumi_uid'), ('recipe_id', 'recipe_id'), ('recipe_intend_times', 'recipe_times')] if key in change)):
                        status = 'uncertain'
                except asyncio.CancelledError:
                    cancelled = HomeApplyResult(request_id=body.request_id, status='uncertain',
                        message='应用连接已中断，结果待核实，请在游戏核对后重新登录',
                        results=results + [HomeApplyItem(building_uid=uid, status='uncertain', message='本项结果待核实')])
                    record.result = cancelled
                    # Preserve the unknown operation for the enclosing role
                    # lease even when session expiry caused this cancellation.
                    session.home_write_uncertain = True
                    if session.active and session.view.status == 'connected':
                        session.home_last_apply = cancelled
                    raise
                except ApiError as error:
                    if error.code == 'GAME_SESSION_EXPIRED':
                        await self.sessions.expire(session)
                        raise ApiError('GAME_SESSION_EXPIRED', '游戏会话已过期，请重新登录', 401) from None
                    status = 'uncertain'
                except Exception:
                    status = 'uncertain'
                messages = {'applied': '设置已同步到游戏', 'partial': '部分设置已生效，请核对当前生产与工人',
                            'rejected': '游戏未应用该项设置，请核对当前状态', 'uncertain': '应用结果待核实，请在游戏核对后重新登录'}
                results.append(HomeApplyItem(building_uid=uid, status=status, message=messages[status]))
                if status in {'uncertain', 'partial', 'rejected'}:
                    stopped = True
                if status == 'uncertain':
                    session.home_write_uncertain = True
            statuses = {row.status for row in results}
            overall = ('uncertain' if 'uncertain' in statuses else 'partial' if 'partial' in statuses or ('applied' in statuses and 'rejected' in statuses)
                       else 'rejected' if 'rejected' in statuses else 'applied')
            result = HomeApplyResult(request_id=body.request_id, status=overall,
                message={'applied': '家园设置已同步', 'partial': '部分设置已生效，请核对后重新编辑',
                         'rejected': '本次设置未完成', 'uncertain': '应用结果待核实，请在游戏核对后重新登录'}[overall], results=results)
            record.result = result
            self._require(session)
            session.home_last_apply = result
            return await self.state(session)
