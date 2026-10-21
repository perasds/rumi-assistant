"""Pure conservative reservations for queues owned by one provider session.

The service owns the book's lifetime. Only a native completed-batch count plus
observed bag debits AND receipts may release a batch. Prepayment never releases
the costs of future batches. Invalid evidence freezes the last safe reservation.
"""
from collections import defaultdict
from dataclasses import dataclass, field, replace


def _count(value, minimum=0):
    return type(value) is int and minimum <= value


def _identity(value, *, zero=False):
    if type(value) is int:
        value = str(value)
    if (type(value) is not str or not value.isascii() or not value.isdecimal()
            or len(value) > 20 or value != str(int(value))
            or int(value) < (0 if zero else 1)):
        raise ValueError('invalid queue identity')
    return value


def _vector(rows):
    if type(rows) is dict:
        rows = [{'item_id': item, 'count': count} for item, count in rows.items()]
    if type(rows) is not list:
        raise ValueError('invalid queue quantities')
    result = defaultdict(int)
    for row in rows:
        if type(row) is not dict or not _count(row.get('count'), 1):
            raise ValueError('invalid queue quantities')
        item = _identity(row.get('item_id'))
        if type(row['item_id']) is not str:
            raise ValueError('item identity must be a string')
        result[item] += row['count']
    return dict(result)


def _totals(values):
    if type(values) is not dict or any(type(item) is not str or not item
            or not _count(count) for item, count in values.items()):
        raise ValueError('invalid inventory counters')
    return values.copy()


def _state(raw):
    inventory, home = raw['inventory'], raw['home']
    if (inventory.get('valid') is not True or home.get('valid') is not True
            or not _count(inventory.get('sequence'))
            or not _count(inventory.get('movement_epoch'))
            or not _count(home.get('sequence'))
            or type(home.get('architectures')) is not list):
        raise ValueError('incomplete queue evidence')
    buildings = {}
    for row in home['architectures']:
        uid = _identity(row['uid'])
        if uid in buildings:
            raise ValueError('duplicate building')
        buildings[uid] = row
    return {'inventory_sequence': inventory['sequence'], 'home_sequence': home['sequence'],
        'epoch': inventory['movement_epoch'], 'spent': _totals(inventory['spent_totals']),
        'received': _totals(inventory['received_totals']), 'buildings': buildings}


def _work(row, *, finite=True):
    if (row.get('cur_recipe') is not None or row.get('has_farm_data')
            or finite and row.get('quantity_mode') == 'continuous' or row.get('is_hide')
            or row.get('is_need_cut_ribbon')
            or row.get('level_up_complete_time') not in (None, 0, '0')
            or not _count(row.get('level'), 1)):
        raise ValueError('unsupported queue configuration')
    work = row['work_old']
    for key in ('recipe_id', 'recipe_intend_times', 'recipe_complete_times', 'recipe_state'):
        if not _count(work.get(key)):
            raise ValueError('unknown native queue state')
    if work['recipe_state'] not in (0, 1):
        raise ValueError('unknown native queue state')
    worker = _identity(work.get('lumi_uid'))
    if worker == '2147483647':
        raise ValueError('unknown queue worker')
    return work, worker


def _external_queue(row):
    """Copy source identity and counters without recording ticking progress."""
    work = row.get('work_old') or {}
    cur = row.get('cur_recipe')
    if type(work) is not dict or (cur is not None and type(cur) is not dict):
        raise ValueError('unknown external queue')
    cur_identity = None if cur is None else (cur.get('recipe_id'), cur.get('recipe_lumi_uid'))
    identity = (row.get('id'), row.get('level'), work.get('lumi_uid'),
        cur_identity, bool(row.get('has_farm_data')))
    return identity, work.get('recipe_id'), work.get('recipe_intend_times'), work.get('recipe_complete_times')


@dataclass
class Queue:
    uid: str
    building_id: str
    recipe_id: str
    times: int
    costs: dict[str, int]
    products: dict[str, int]
    owner_id: str
    level: int
    initial_worker: str
    baseline_inventory_sequence: int
    baseline_epoch: int
    baseline_spent: dict[str, int]
    baseline_received: dict[str, int]
    baseline_order_spent: dict[str, int]
    baseline_order_received: dict[str, int]
    baseline_home_sequence: int
    baseline_completed: int = 0
    settled_batches: int = 0
    current_completed: int = 0
    recipe_alive: bool = True
    external_spent: dict[str, int] = field(default_factory=dict)
    external_received: dict[str, int] = field(default_factory=dict)
    external_queues: dict[str, tuple] = field(default_factory=dict)

    @property
    def finished(self):
        return self.settled_batches == self.times


class QueueBook:
    def __init__(self):
        self.active: dict[str, Queue] = {}
        self._retired: dict[str, int] = {}
        self._order_spent, self._order_received = {}, {}
        self._confirmed_idle = {}
        self._last = None
        self._valid = True

    @property
    def frozen_reserves(self):
        return self._retired.copy()

    def is_confirmed_idle(self, raw, building_uid):
        """Match a confirmed cancellation, including retained native counters."""
        if not self._valid:
            return False
        try:
            uid = _identity(building_uid)
            home = raw['home']
            if home.get('valid') is not True:
                return False
            matches = [row for row in home['architectures'] if _identity(row['uid']) == uid]
            return (len(matches) == 1 and self._confirmed_idle.get(uid) == _external_queue(matches[0])
                and matches[0].get('work_old', {}).get('recipe_id') == 0)
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def _check_state(self, state, previous=None):
        old = self._last if previous is None else previous
        if old is None:
            return
        if (state['epoch'] != old['epoch']
                or state['inventory_sequence'] < old['inventory_sequence']
                or state['home_sequence'] < old['home_sequence']):
            raise ValueError('queue evidence reset')
        for name in ('spent', 'received'):
            if any(state[name].get(item, 0) < count for item, count in old[name].items()):
                raise ValueError('inventory counter regressed')
            if (state['inventory_sequence'] == old['inventory_sequence']
                    and state[name] != old[name]):
                raise ValueError('counter changed without notification')

    def _external_progress(self, queue, state, owned_uids):
        previous = {uid: row for uid, row in queue.external_queues.items() if uid not in owned_uids}
        current = {uid: _external_queue(row) for uid, row in state['buildings'].items()
                   if uid not in owned_uids}
        if previous.keys() != current.keys():
            raise ValueError('external queue sources changed')
        for uid, old in previous.items():
            new = current[uid]
            if old == new:
                continue
            if (old[0] != new[0] or (self._last is not None
                    and state['home_sequence'] == self._last['home_sequence'])):
                raise ValueError('external queue identity changed')
            _, recipe, intended, completed = old
            _, next_recipe, next_intended, next_completed = new
            if not all(_count(value) for value in (*old[1:], *new[1:])):
                raise ValueError('unknown external queue progress')
            # A cur_recipe branch has its own clock; only its identity matters.
            if old[0][3] is not None and next_recipe == recipe and next_intended == intended:
                continue
            if recipe == 0:
                if next_recipe == 0 and (new[2:] == old[2:] or next_intended == next_completed == 0):
                    continue
                raise ValueError('external idle queue was edited')
            retained = (next_intended == intended and completed <= next_completed
                        and (intended == 0 or next_completed <= intended))
            stopped = next_recipe == 0 and (retained or next_intended == next_completed == 0)
            if not (stopped or recipe != 0 and next_recipe == recipe and retained):
                raise ValueError('external queue was started or edited')
        return current

    def _progress(self, queue, state, *, cancelling=False, owned_uids=None):
        owned = set(self.active) | {queue.uid} if owned_uids is None else owned_uids
        external = self._external_progress(queue, state, owned)
        row = state['buildings'][queue.uid]
        work, worker = _work(row)
        completed, recipe_id = work['recipe_complete_times'], str(work['recipe_id'])
        cleared = cancelling and work['recipe_intend_times'] == 0 and completed == 0
        if (_identity(row['id']) != queue.building_id or row['level'] != queue.level
                or worker != queue.initial_worker
                or (not cleared and (work['recipe_intend_times'] != queue.times
                    or not queue.current_completed <= completed <= queue.times))
                or recipe_id not in (queue.recipe_id, '0')
                or (not queue.recipe_alive and recipe_id != '0')
                or (recipe_id == '0' and completed != queue.times and not cancelling)):
            raise ValueError('native queue configuration changed')
        if cancelling and (recipe_id != '0' or state['home_sequence'] <= queue.baseline_home_sequence):
            raise ValueError('cancellation is not confirmed')
        if (self._last is not None and state['home_sequence'] == self._last['home_sequence']
                and (completed != queue.current_completed or (recipe_id != '0') != queue.recipe_alive)):
            raise ValueError('queue changed without notification')
        if cleared:
            # Native cancellation may erase both counters. Preserve only batches
            # already settled before that notification; infer neither refunds nor
            # completed work from its new inventory movements.
            return queue.settled_batches, queue.current_completed, False, external
        confirmed = min(queue.times, completed - queue.baseline_completed)
        if state['inventory_sequence'] <= queue.baseline_inventory_sequence:
            confirmed = 0
        for name, amounts, baseline, offsets, baseline_offsets, external_budget in (
                ('spent', queue.costs, queue.baseline_spent, self._order_spent,
                    queue.baseline_order_spent, queue.external_spent),
                ('received', queue.products, queue.baseline_received,
                    self._order_received, queue.baseline_order_received, queue.external_received)):
            for item, count in amounts.items():
                change = state[name].get(item, 0) - baseline.get(item, 0)
                change -= offsets.get(item, 0) - baseline_offsets.get(item, 0)
                change -= external_budget.get(item, 0)
                confirmed = min(confirmed, max(0, change) // count)
        return max(queue.settled_batches, confirmed), completed, recipe_id != '0', external

    def add(self, before_raw, after_raw, proposal, recipe, *, owner_id,
            external_spent=None, external_received=None):
        """Register a queue with worst-case external movement budgets, or deny it.

        External budgets are optional ``dict[item_id, nonnegative int]``. The
        caller supplies the full possible external movements after this baseline;
        they are never credited to this queue, even before notifications arrive.
        """
        if not self._valid:
            return False
        try:
            operation = proposal['operation']
            uid = _identity(operation['building_uid'])
            if uid in self.active and not self.active[uid].finished:
                return False
            times = operation['recipe_times']
            costs, products = _vector(recipe['materials']), _vector(recipe['products'])
            if (operation.get('action') != 'produce' or not _count(times, 1)
                    or type(owner_id) is not str or not owner_id or not costs or not products
                    or recipe.get('quantity_mode') == 'continuous'
                    or _identity(operation['building_id']) != _identity(recipe['building_id'])
                    or _identity(operation['recipe_id']) != _identity(recipe['recipe_id'])
                    or _vector(proposal['costs']) != {item: count * times for item, count in costs.items()}
                    or self.blocked_items().intersection(costs.keys() | products.keys())):
                return False
            before, after = _state(before_raw), _state(after_raw)
            self._check_state(before)
            self._check_state(after, before)
            old_row, row = before['buildings'][uid], after['buildings'][uid]
            old_work, worker = _work(old_row)
            if (old_work['recipe_id'] != 0 or old_work['recipe_state'] != 0
                    or (old_work['recipe_complete_times'] not in (0, old_work['recipe_intend_times'])
                        and not self.is_confirmed_idle(before_raw, uid))
                    or row['level'] not in recipe['available_levels']
                    or _identity(old_row['id']) != _identity(operation['building_id'])
                    or old_row['level'] != row['level']
                    or after['home_sequence'] <= before['home_sequence']):
                return False
            queue = Queue(uid, _identity(operation['building_id']), _identity(operation['recipe_id']),
                times, costs, products, owner_id, row['level'], worker,
                before['inventory_sequence'], before['epoch'], before['spent'], before['received'],
                self._order_spent.copy(), self._order_received.copy(), before['home_sequence'],
                external_spent=_totals({} if external_spent is None else external_spent),
                external_received=_totals({} if external_received is None else external_received))
            owned = set(self.active) | {uid}
            queue.external_queues = {key: _external_queue(value)
                for key, value in before['buildings'].items() if key not in owned}
            changes = {key: self._progress(entry, after, owned_uids=owned)
                for key, entry in self.active.items() if key != uid}
            changes[uid] = self._progress(queue, after, owned_uids=owned)
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        self.active[uid] = queue
        self._apply(after, changes)
        return True

    def note_unreserved_start(self, before_raw, after_raw, operation, recipe):
        """Record one confirmed no-material start without granting ownership.

        Its outputs must be disjoint from every unfinished owned queue. All
        other sources and owned queues are validated before any state is changed.
        """
        if not self._valid:
            return False
        try:
            uid, times = _identity(operation['building_uid']), operation['recipe_times']
            products = _vector(recipe['products'])
            if (operation.get('action') != 'produce' or not _count(times)
                    or _vector(recipe['materials']) or not products
                    or self.blocked_items().intersection(products)
                    or _identity(operation['building_id']) != _identity(recipe['building_id'])
                    or _identity(operation['recipe_id']) != _identity(recipe['recipe_id'])
                    or uid in self.active and not self.active[uid].finished):
                return False
            before, after = _state(before_raw), _state(after_raw)
            self._check_state(before)
            self._check_state(after, before)
            old_row, row = before['buildings'][uid], after['buildings'][uid]
            old_work, worker = _work(old_row, finite=False)
            work, next_worker = _work(row, finite=False)
            if (old_work['recipe_id'] != 0 or old_work['recipe_state'] != 0
                    or str(work['recipe_id']) != operation['recipe_id']
                    or work['recipe_intend_times'] != times
                    or times > 0 and work['recipe_complete_times'] > times
                    or _identity(old_row['id']) != _identity(operation['building_id'])
                    or row['id'] != old_row['id'] or row['level'] != old_row['level']
                    or row['level'] not in recipe['available_levels'] or next_worker != worker
                    or after['home_sequence'] <= before['home_sequence']):
                return False
            for mode in (old_row.get('quantity_mode'), row.get('quantity_mode'), recipe.get('quantity_mode')):
                if mode == 'continuous' and times != 0 or mode == 'batch' and times == 0:
                    return False
            owned, changes = set(self.active) | {uid}, {}
            for key, entry in self.active.items():
                progress = self._progress(entry, before)
                if key == uid:
                    continue
                staged = replace(entry, settled_batches=progress[0], current_completed=progress[1],
                    recipe_alive=progress[2], external_queues=progress[3])
                progress = self._progress(staged, after, owned_uids=owned)
                progress[3][uid] = _external_queue(row)
                changes[key] = progress
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        self.active.pop(uid, None)
        self._apply(after, changes)
        return True

    def _apply(self, state, changes):
        for uid, progress in changes.items():
            entry = self.active[uid]
            (entry.settled_batches, entry.current_completed,
                entry.recipe_alive, entry.external_queues) = progress
        for uid, fingerprint in list(self._confirmed_idle.items()):
            try:
                unchanged = _external_queue(state['buildings'][uid]) == fingerprint
            except (KeyError, TypeError, ValueError, AttributeError):
                unchanged = False
            if not unchanged:
                del self._confirmed_idle[uid]
        self._last = state

    def refresh(self, raw):
        """Advance all queues atomically; invalidation retains safe reservations."""
        if not self._valid:
            return False
        try:
            state = _state(raw)
            self._check_state(state)
            changes = {uid: self._progress(entry, state) for uid, entry in self.active.items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            self._valid = False
            return False
        self._apply(state, changes)
        return True

    def reserves(self, raw):
        self.refresh(raw)
        result = defaultdict(int, self._retired)
        for queue in self.active.values():
            for item, count in queue.costs.items():
                result[item] += count * (queue.times - queue.settled_batches)
        return {item: count for item, count in result.items() if count}

    def blocked_items(self):
        return {item for queue in self.active.values() if not queue.finished
                for item in queue.costs.keys() | queue.products.keys()}

    def retire(self, building_uid, raw):
        """After confirmed native cancellation, freeze the unproven remainder."""
        if not self._valid:
            return False
        try:
            uid = _identity(building_uid)
            queue = self.active[uid]
            state = _state(raw)
            self._check_state(state)
            changes = {key: self._progress(entry, state, cancelling=key == uid)
                       for key, entry in self.active.items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        self._apply(state, changes)
        for item, count in queue.costs.items():
            self._retired[item] = self._retired.get(item, 0) + count * (queue.times - queue.settled_batches)
        del self.active[uid]
        self._confirmed_idle[uid] = _external_queue(state['buildings'][uid])
        for entry in self.active.values():
            entry.external_queues[uid] = _external_queue(state['buildings'][uid])
        return True

    def note_order(self, costs, rewards):
        """Exclude known native hand-in movements, even before notify arrives."""
        spent, received = _vector(costs), _vector(rewards)
        for total, amounts in ((self._order_spent, spent), (self._order_received, received)):
            for item, count in amounts.items():
                total[item] = total.get(item, 0) + count
