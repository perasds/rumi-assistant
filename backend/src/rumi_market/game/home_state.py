"""Session-owned Home projection using individually verified hot64 consumers.

3998 and 2971 are login batches, completed by 3001 and 2913 respectively.
Only explicit public fields are retained; names, opaque bytes and archives are
never stored. Unknown change branches invalidate their corresponding baseline.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import math
import logging

from .envelope import ResponseEnvelope
from .home_codec import HomeProtoCodec
from .proto import BUSINESS_MESSAGE_LIMIT


MAX_COMBINE_DEPTH = 4
MAX_COMBINE_UNITS = 512
MAX_ARCHITECTURES = 4096
MAX_LUMIS = 16384
MAX_CHANGES = 4096
MAX_OCCUPANTS = 4096
MAX_PROJECTION_ROWS = 65536
_DISPATCH_COMMANDS = frozenset({10083, 10089, 10091, 10093})
_COMMANDS = frozenset({2016, 2913, 2971, 2972, 3001, 3003, 3004, 3005, 3235, 3998}) | _DISPATCH_COMMANDS
_ROSTER_COMMANDS = frozenset({2913, 2971, 2972, 3235})
_ARCHITECTURE_REPLACEMENTS = frozenset({4, 5, 9, 10, 11, 12})
_LOGGER = logging.getLogger(__name__)
_WORK_OLD = ("lumi_uid", "lumi_id", "lumi_work_energy", "lumi_work_speed", "recipe_id",
             "recipe_intend_times", "recipe_complete_times", "recipe_update_time", "recipe_work_amount",
             "recipe_state", "lumi_work_speed_source")
_RECIPE = ("recipe_id", "recipe_begin_time", "recipe_need_seconds", "recipe_lumi_uid", "recipe_state",
           "work_speed", "last_update_time", "recipe_lumi_id", "lumi_work_energy", "record_prosperity",
           "record_lumi_level", "record_lumi_break_lv", "recipe_real_run_seconds", "lumi_accelerate_seconds")
_TRAVEL = ("slot_id", "lumi_uid", "lumi_id", "map_id", "begin_time", "end_time", "is_mind_wandering")
_INT64_TYPES = frozenset({3, 4, 6, 16, 18})


def _value(message, name):
    if not message.HasField(name):
        return None
    value = getattr(message, name)
    return str(value) if message.DESCRIPTOR.fields_by_name[name].type in _INT64_TYPES else value


def _fields(message, names):
    return {name: _value(message, name) for name in names}


def _uid(message, name="lumi_uid"):
    if not message.HasField(name) or getattr(message, name) <= 0:
        raise ValueError("GAME_HOME_STATE_INVALID")
    return getattr(message, name)


def _bounded(rows, limit):
    if len(rows) > limit:
        raise ValueError("GAME_HOME_STATE_INVALID")
    return rows


def _dispatch_rows(info):
    result = {}
    for row in _bounded(info.dispatchStores, MAX_OCCUPANTS).values():
        if row.lumiUid > 0:
            if row.id <= 0 or row.id in result:
                raise ValueError("GAME_HOME_STATE_INVALID")
            result[row.id] = str(row.lumiUid)
    return result


def _store_items(rows):
    result = []
    for row in _bounded(rows, MAX_CHANGES):
        # The native CmdItemShow getter uses CountDefaultValue=0 when absent.
        if not row.HasField('item_id') or row.item_id <= 0 or row.count < 0:
            raise ValueError('GAME_HOME_STATE_INVALID')
        result.append({'item_id': str(row.item_id), 'count': row.count})
    return result


def _architecture(message, farms=None):
    uid = _uid(message, "architecture_uid")
    dispatch = _dispatch_rows(message.actFarm) if message.HasField("actFarm") else {}
    if farms is not None:
        farms.pop(uid, None)
        if message.HasField("actFarm") and message.actFarm.id > 0:
            farms[uid] = (message.actFarm.id, dispatch)
    dorm = (_fields(row, ("lumi_uid", "lumi_id", "put_time"))
            for row in _bounded(message.dorm.lumi_list, MAX_OCCUPANTS))
    travel = (_fields(row, _TRAVEL)
              for row in _bounded(message.home_travel.slot_list, MAX_OCCUPANTS))
    return {"uid": uid, "id": _value(message, "architecture_id"),
            "level": _value(message, "architecture_level"), "is_hide": _value(message, "is_hide"),
            "belong_island_id": _value(message, "belong_island_id"),
            "level_up_complete_time": _value(message, "level_up_complete_time"),
            "is_need_cut_ribbon": _value(message, "is_need_cut_ribbon"),
            "work_old": _fields(message.work_old, _WORK_OLD) if message.HasField("work_old") else None,
            "store_items": _store_items(message.store_info.base_rewards) if message.HasField('store_info') else None,
            "store_extra_items": _store_items(message.store_info.extra_rewards) if message.HasField('store_info') else None,
            "cur_recipe": _fields(message.cur_recipe, _RECIPE) if message.HasField("cur_recipe") else None,
            "dorm": list(dorm) if message.HasField("dorm") else None,
            "travel": list(travel) if message.HasField("home_travel") else None,
            "has_farm_data": message.HasField("actFarm"),
            "dispatch_uids": sorted(set(dispatch.values())),
            "interaction_uids": [str(uid) for uid in _bounded(message.interaction.lumiUid, MAX_OCCUPANTS) if uid > 0]}


def _architecture_size(row):
    return 1 + sum(len(row[name] or []) for name in ("dorm", "travel", "dispatch_uids", "interaction_uids", "store_items", "store_extra_items"))


def _architecture_rows(messages, farms):
    result, count = [], 0
    for message in _bounded(messages, MAX_ARCHITECTURES):
        row = _architecture(message, farms)
        count += _architecture_size(row) + len(farms.get(row["uid"], (None, {}))[1])
        if count > MAX_PROJECTION_ROWS:
            raise ValueError("GAME_HOME_STATE_INVALID")
        result.append(row)
    return result


def _worker(message):
    return {"uid": str(_uid(message)), "id": _value(message, "lumi_id"),
            "work_aptitude": _value(message.init_aptitude, "aptitude_work") if message.HasField("init_aptitude") else None,
            "raw_state": _value(message, "lumi_state")}


class HomeState:
    def __init__(self, verified_version: bool):
        if type(verified_version) is not bool:
            raise ValueError("GAME_HOME_STATE_INVALID")
        self._verified = verified_version
        self._unavailable_reason = None if verified_version else 'HOME_VERSION_UNVERIFIED'
        self._closed = self._valid = self._roster_ready = False
        self._sequence = 0
        self._observed_at = None
        self._architectures, self._home_lumis, self._workers, self._energy = {}, {}, {}, {}
        self._pending_architectures = self._pending_workers = None
        self._architecture_farms = {}
        self._pending_farms = None
        self._dispatch_ready = False
        self._dispatch_farm_id = None
        self._dispatch = {}
        self._changed = asyncio.Event()
        self._observing_command = self._observing_change_type = None
        self._codec = None
        if verified_version:
            try:
                self._codec = HomeProtoCodec()
            except Exception:
                self._verified = False
                self._unavailable_reason = 'HOME_SCHEMA_UNAVAILABLE'

    @property
    def sequence(self) -> int:
        return self._sequence

    def _signal(self, *, observed=True):
        if observed:
            self._check_capacity()
        self._sequence += 1
        self._observed_at = datetime.now(timezone.utc).isoformat() if observed else None
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _check_capacity(self):
        count = sum(_architecture_size(row) for row in self._architectures.values())
        count += sum(_architecture_size(row) for row in self._pending_architectures or [])
        count += sum(len(entries) for _, entries in self._architecture_farms.values())
        count += sum(len(entries) for _, entries in (self._pending_farms or {}).values())
        count += sum(len(rows) for rows in (self._home_lumis, self._energy, self._workers,
                                          self._pending_workers or {}, self._dispatch))
        if count > MAX_PROJECTION_ROWS:
            raise ValueError("GAME_HOME_STATE_INVALID")

    def _invalidate_home(self):
        self._valid = False
        if self._verified and not self._closed:
            self._unavailable_reason = 'HOME_STATE_INVALID'
        self._architectures.clear()
        self._home_lumis.clear()
        self._energy.clear()
        self._pending_architectures = None
        self._pending_farms = None
        self._architecture_farms.clear()
        self._invalidate_dispatch()

    def _invalidate_dispatch(self):
        self._dispatch_ready = False
        self._dispatch_farm_id = None
        self._dispatch.clear()
        for row in self._architectures.values():
            row["dispatch_uids"] = []

    def _sync_dispatch(self):
        uids = sorted(set(self._dispatch.values()))
        for uid, (farm_id, _) in self._architecture_farms.items():
            if farm_id == self._dispatch_farm_id and uid in self._architectures:
                self._architectures[uid]["dispatch_uids"] = list(uids)

    def _dispatch_baseline(self, farm_id, entries):
        self._dispatch_farm_id, self._dispatch = farm_id, dict(entries)
        self._dispatch_ready = True
        self._sync_dispatch()

    def _invalidate_roster(self):
        self._roster_ready = False
        self._workers.clear()
        self._pending_workers = None

    def invalidate(self):
        self._invalidate_home()
        self._invalidate_roster()
        self._signal(observed=False)

    def close(self):
        self._closed = True
        self.invalidate()
        self._codec = None

    def snapshot(self) -> dict:
        occupancy = {}
        for architecture in self._architectures.values():
            def add(uid, kind, **extra):
                if uid is not None and int(uid) > 0:
                    occupancy.setdefault(str(uid), []).append({"kind": kind, "architecture_uid": architecture["uid"], **extra})
            if architecture["work_old"] is not None:
                add(architecture["work_old"]["lumi_uid"], "work_old")
            if architecture["cur_recipe"] is not None:
                add(architecture["cur_recipe"]["recipe_lumi_uid"], "recipe")
            for row in architecture["dorm"] or []:
                add(row["lumi_uid"], "dorm")
            for row in architecture["travel"] or []:
                add(row["lumi_uid"], "travel", slot_id=row["slot_id"])
            for uid in architecture["interaction_uids"]:
                add(uid, "interaction")
            for uid in architecture["dispatch_uids"]:
                add(uid, "dispatch")
        workers = [{**row, "work_energy": self._energy.get(int(uid)), "occupancy": occupancy.get(uid, [])}
                   for uid, row in self._workers.items()] if self._roster_ready else []
        dispatch_uids = sorted(set(self._dispatch.values())) if self._dispatch_ready else []
        for row in workers:
            if row["uid"] in dispatch_uids and not any(item["kind"] == "dispatch" for item in row["occupancy"]):
                row["occupancy"].append({"kind": "dispatch", "architecture_uid": None})
        return deepcopy({"valid": self._valid and not self._closed, "sequence": self._sequence,
                         "unavailable_reason": self._unavailable_reason,
                         "observed_at": self._observed_at,
                         "architectures": list(self._architectures.values()),
                         "home_lumis": list(self._home_lumis.values()), "workers": workers,
                         "roster_ready": self._roster_ready and not self._closed,
                         "dispatch_ready": self._dispatch_ready and not self._closed,
                         "dispatch_lumi_uids": dispatch_uids})

    def observe(self, response: ResponseEnvelope):
        if self._closed or not self._verified:
            return
        if not isinstance(response, ResponseEnvelope):
            self.invalidate()
            return
        if response.command not in _COMMANDS or response.command == 3004:
            return  # The verified 3004 source handler contains only nop/ret.
        self._observing_command, self._observing_change_type = response.command, None
        try:
            if response.remaining != 0 or response.code != 0:
                raise ValueError("GAME_HOME_STATE_INVALID")
            self._observe(response.command, response.data, depth=0, units=[0])
        except Exception:
            _LOGGER.warning('Home notification rejected: command=%s architecture_change_type=%s',
                            self._observing_command, self._observing_change_type)
            if response.command in _DISPATCH_COMMANDS:
                self._invalidate_dispatch()
            elif response.command in _ROSTER_COMMANDS:
                self._invalidate_roster()
            elif response.command != 2016:
                self._invalidate_home()
            else:
                self._invalidate_home()
                self._invalidate_roster()
            self._signal(observed=False)

    def _observe(self, command, data, *, depth, units):
        if command not in _COMMANDS or command == 3004:
            return
        self._observing_command, self._observing_change_type = command, None
        if depth > MAX_COMBINE_DEPTH or len(data) > BUSINESS_MESSAGE_LIMIT:
            raise ValueError("GAME_HOME_STATE_INVALID")
        message = self._codec.decode(command, data)
        if command == 2016:
            units[0] += len(message.unit_lst)
            if units[0] > MAX_COMBINE_UNITS:
                raise ValueError("GAME_HOME_STATE_INVALID")
            for unit in message.unit_lst:
                if not unit.HasField("id") or unit.id <= 0:
                    raise ValueError("GAME_HOME_STATE_INVALID")
                self._observe(unit.id, unit.msg, depth=depth + 1, units=units)
            return
        if command == 3998:
            if self._pending_architectures is None:
                self._pending_architectures = []
                self._pending_farms = {}
            rows = _architecture_rows(message.architecture_list, self._pending_farms)
            if len(self._pending_architectures) + len(rows) > MAX_ARCHITECTURES:
                raise ValueError("GAME_HOME_STATE_INVALID")
            self._pending_architectures.extend(rows)
            self._check_capacity()
            return
        if command == 3001:
            farms = self._pending_farms if self._pending_architectures is not None else {}
            rows = (self._pending_architectures if self._pending_architectures is not None else
                    _architecture_rows(message.architecture_list, farms))
            architectures = {row["uid"]: row for row in rows}
            if len(architectures) != len(rows):
                raise ValueError("GAME_HOME_STATE_INVALID")
            lumis = {str(_uid(row)): {"uid": str(row.lumi_uid), "id": _value(row, "lumi_id")}
                     for row in _bounded(message.home_lumi_list, MAX_LUMIS)}
            energy = {_uid(row): _value(row, "work_energy") for row in _bounded(message.lumi_list, MAX_LUMIS)}
            if len(lumis) != len(message.home_lumi_list) or len(energy) != len(message.lumi_list):
                raise ValueError("GAME_HOME_STATE_INVALID")
            self._architectures, self._home_lumis, self._energy = architectures, lumis, energy
            self._pending_architectures = None
            self._pending_farms = None
            if self._architecture_farms and not farms:
                self._invalidate_dispatch()
            self._architecture_farms = farms
            for farm_id, entries in farms.values():
                self._dispatch_baseline(farm_id, entries)
            self._valid = True
            self._unavailable_reason = None
        elif command == 3003:
            rows = _bounded(message.change_list, MAX_CHANGES)
            for change in rows:
                self._observing_change_type = change.change_type if change.HasField('change_type') else None
                if (not change.HasField("change_type")
                        or change.change_type not in {1, 2} | _ARCHITECTURE_REPLACEMENTS
                        or not change.HasField("architecture_info")):
                    raise ValueError("GAME_HOME_STATE_INVALID")
                row = change.architecture_info
                uid = _uid(row, "architecture_uid")
                if not self._valid:
                    continue
                if change.change_type == 1:
                    if uid in self._architectures or len(self._architectures) >= MAX_ARCHITECTURES:
                        raise ValueError("GAME_HOME_STATE_INVALID")
                    self._architectures[uid] = _architecture(row, self._architecture_farms)
                elif change.change_type == 2:
                    self._architectures.pop(uid, None)
                    if self._architecture_farms.pop(uid, None) is not None:
                        self._invalidate_dispatch()
                elif uid in self._architectures:
                    was_farm = uid in self._architecture_farms
                    self._architectures[uid] = _architecture(row, self._architecture_farms)
                    if was_farm and uid not in self._architecture_farms:
                        self._invalidate_dispatch()
                if uid in self._architecture_farms:
                    self._dispatch_baseline(*self._architecture_farms[uid])
            if not self._valid or not rows:
                return
        elif command == 3005:
            for change in _bounded(message.change_list, MAX_CHANGES):
                if not change.HasField("lumi_energy"):
                    raise ValueError("GAME_HOME_STATE_INVALID")
                uid = _uid(change.lumi_energy)
                if self._valid:
                    if uid not in self._energy and len(self._energy) >= MAX_LUMIS:
                        raise ValueError("GAME_HOME_STATE_INVALID")
                    self._energy[uid] = _value(change.lumi_energy, "work_energy")
            if not self._valid or not message.change_list:
                return
        elif command == 2971:
            if self._pending_workers is None:
                self._pending_workers = {}
            self._roster_ready = False
            for message_row in _bounded(message.lumi_list, MAX_LUMIS):
                row = _worker(message_row)
                self._pending_workers[row["uid"]] = row
            if len(self._pending_workers) > MAX_LUMIS:
                raise ValueError("GAME_HOME_STATE_INVALID")
            self._check_capacity()
            return
        elif command == 2913:
            if self._pending_workers is None:
                return
            self._workers, self._pending_workers = self._pending_workers, None
            self._roster_ready = True
        elif command == 2972:
            for change in _bounded(message.change_list, MAX_CHANGES):
                if not change.HasField("change_type") or change.change_type not in (1, 2, 3):
                    raise ValueError("GAME_HOME_STATE_INVALID")
                pet = self._codec.pet_message(change.data)
                uid = str(_uid(pet))
                if not self._roster_ready:
                    continue
                if change.change_type == 1 and uid not in self._workers:
                    if len(self._workers) >= MAX_LUMIS:
                        raise ValueError("GAME_HOME_STATE_INVALID")
                    self._workers[uid] = _worker(pet)
                elif change.change_type == 2:
                    self._workers.pop(uid, None)
                elif change.change_type == 3 and uid in self._workers:
                    self._workers[uid] = _worker(pet)
            if not self._roster_ready or not message.change_list:
                return
        elif command == 3235:
            for change in _bounded(message.change_list, MAX_CHANGES):
                uid = str(_uid(change))
                if not change.HasField("lumi_state"):
                    raise ValueError("GAME_HOME_STATE_INVALID")
                if self._roster_ready and uid in self._workers:
                    self._workers[uid]["raw_state"] = _value(change, "lumi_state")
            if not self._roster_ready or not message.change_list:
                return
        elif command == 10093:
            if not message.HasField("info"):
                raise ValueError("GAME_HOME_STATE_INVALID")
            if message.info.id <= 0:
                return  # The verified Player.Init ignores inactive farm ids.
            self._dispatch_baseline(message.info.id, _dispatch_rows(message.info))
        elif command in (10083, 10091):
            if command == 10083:
                if not message.HasField("stealInfo"):
                    raise ValueError("GAME_HOME_STATE_INVALID")
                rows = [message.stealInfo]
            else:
                rows = _bounded(message.infos, MAX_CHANGES)
            for row in rows:
                if row.id <= 0:
                    raise ValueError("GAME_HOME_STATE_INVALID")
                if self._dispatch_ready:
                    self._dispatch.pop(row.id, None)
                    if row.lumiUid > 0:
                        self._dispatch[row.id] = str(row.lumiUid)
                    if len(self._dispatch) > MAX_OCCUPANTS:
                        raise ValueError("GAME_HOME_STATE_INVALID")
            if not self._dispatch_ready:
                return
            self._sync_dispatch()
        elif command == 10089:
            if not self._dispatch_ready:
                return
            self._dispatch.pop(message.id, None)
            self._sync_dispatch()
        self._signal()

    async def wait_for_update(self, after_sequence: int, timeout: float = 5) -> bool:
        if (type(after_sequence) is not int or after_sequence < 0 or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ValueError("GAME_HOME_STATE_INVALID")
        deadline = asyncio.get_running_loop().time() + timeout
        while self._valid and not self._closed:
            if self._sequence > after_sequence:
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                return False
        return False
