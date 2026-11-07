"""Receiver-owned fresh definitions; occurrence state remains in the existing work queue."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from contextlib import closing
from zoneinfo import ZoneInfo

from installed_writers import preferences_for

_preferences = preferences_for(__file__)

DOMAIN = 'sotto.owner-routine-tool.v1'
DESTINATION = 'this Photon conversation'
DESTINATIONS = {'photon': DESTINATION, 'telegram': 'this Telegram conversation'}
YES = re.compile(r'(yes|yes please|yes, please|yep|yup|sure|do it|set it up)[.!]?\Z', re.I)
HEX = re.compile(r'[a-f0-9]{64}\Z')
NAME = re.compile(r'user-[a-z0-9]+(?:-[a-z0-9]+)*\Z')
SYSTEM_PROMPTS = {'Run my morning brief', 'Run my evening brief', 'Run my relationship pulse',
                  'Run my proactive check', 'Run my midday digest'}


class RefusalError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_proposal(args):
    if (type(args) is not dict or set(args) != {'operation', 'name', 'schedule', 'prompt'}
            or args['operation'] != 'propose' or type(args['name']) is not str
            or len(args['name']) > 29 or not NAME.fullmatch(args['name'])):
        raise ValueError('routine proposal invalid')
    prompt = args['prompt']
    if (type(prompt) is not str or not prompt or prompt.strip() != prompt or len(prompt) > 2000
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in prompt)):
        raise ValueError('routine prompt invalid')
    if prompt in SYSTEM_PROMPTS:
        raise RefusalError('unsupported_definition')
    schedule = args['schedule']
    weekday = schedule.get('weekday') if type(schedule) is dict else None
    weekday_set = (type(weekday) is list and 1 <= len(weekday) <= 7
                   and all(type(day) is int and 0 <= day <= 6 for day in weekday)
                   and all(left < right for left, right in zip(weekday, weekday[1:])))
    if (type(schedule) is not dict or set(schedule) != {'hour', 'minute', 'weekday'}
            or (schedule['hour'] is None and schedule['weekday'] is not None)
            or (schedule['hour'] is not None and (type(schedule['hour']) is not int or not 0 <= schedule['hour'] <= 23))
            or type(schedule['minute']) is not int or not 0 <= schedule['minute'] <= 59
            or (weekday not in (None, 'weekdays', 'first_of_month') and not weekday_set
                and (type(weekday) is not int or not 0 <= weekday <= 6))):
        raise ValueError('routine schedule invalid')


def definition(args, zone, *, destination=DESTINATION):
    validate_proposal(args)
    ZoneInfo(zone)
    if destination not in DESTINATIONS.values():
        raise ValueError('routine destination invalid')
    return {key: args[key] for key in ('name', 'schedule', 'prompt')} | {'zone': zone, 'destination': destination}


def validate_definition(value):
    if type(value) is not dict or set(value) != {'name', 'schedule', 'zone', 'prompt', 'destination'}:
        raise ValueError('routine definition invalid')
    if definition({'operation': 'propose', **{key: value[key] for key in ('name', 'schedule', 'prompt')}}, value['zone'], destination=value['destination']) != value:
        raise ValueError('routine definition invalid')


def expression(value):
    schedule = value['schedule']
    if schedule['hour'] is None:
        return f"{schedule['minute']} * * * *"
    if schedule['weekday'] == 'first_of_month':
        return f"{schedule['minute']} {schedule['hour']} 1 * *"
    weekday = ('1-5' if schedule['weekday'] == 'weekdays' else
               ','.join(map(str, schedule['weekday'])) if type(schedule['weekday']) is list else
               schedule['weekday'] if schedule['weekday'] is not None else '*')
    return f"{schedule['minute']} {schedule['hour']} * * {weekday}"


def successor_after_completion(*, expr, completed_at, zone):
    """Fixed wall time: first fold once; skip nonexistent dates, including monthly day1."""
    minute, hour, day, month, weekday = expr.split()
    hourly = hour == '*' and day == month == weekday == '*'
    monthly = day == '1' and month == '*' and weekday == '*'
    weekdays = weekday.split(',')
    valid_set = (1 <= len(weekdays) <= 7 and all(part in ('0', '1', '2', '3', '4', '5', '6') for part in weekdays)
                 and all(int(left) < int(right) for left, right in zip(weekdays, weekdays[1:])))
    if not monthly and (day != '*' or month != '*' or weekday not in ('*', '1-5') and not valid_set):
        raise ValueError('fresh schedule invalid')
    tz = ZoneInfo(zone)
    local = completed_at.astimezone(tz)
    local_date = local.date()
    if hourly:
        # Enumerate local wall hours; do not emit the second fold or normalize a gap.
        base = local.replace(tzinfo=None, minute=0, second=0, microsecond=0)
        for offset in range(72):
            wall = (base + timedelta(hours=offset)).replace(minute=int(minute), tzinfo=tz, fold=0)
            instant = wall.astimezone(timezone.utc)
            if instant.astimezone(tz).replace(tzinfo=None) != wall.replace(tzinfo=None):
                continue
            if instant > completed_at.astimezone(timezone.utc):
                return wall.isoformat()
        raise ValueError('fresh schedule successor unavailable')
    for offset in range(15):
        if monthly:
            month_index = local_date.year * 12 + local_date.month - 1 + offset
            year, month_index = divmod(month_index, 12)
            date = local_date.replace(year=year, month=month_index + 1, day=1)
        else:
            date = local_date + timedelta(days=offset)
        if weekday == '1-5' and date.weekday() >= 5:
            continue
        if weekday not in ('*','1-5') and (date.weekday() + 1) % 7 not in {int(part) for part in weekdays}:
            continue
        wall = datetime(date.year, date.month, date.day, int(hour), int(minute), tzinfo=tz, fold=0)
        instant = wall.astimezone(timezone.utc)
        if instant.astimezone(tz).replace(tzinfo=None) != wall.replace(tzinfo=None):
            continue
        if instant > completed_at.astimezone(timezone.utc):
            return wall.isoformat()
    raise ValueError('fresh schedule successor unavailable')


def _path(root, *, expected_uid=None, create=False):
    directory = Path(root) / 'runtime'
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    meta = directory.lstat()
    uid = os.geteuid() if expected_uid is None else expected_uid
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid or meta.st_mode & 0o022:
        raise ValueError('routine directory invalid')
    path = directory / 'owner-routines.json'
    if path.exists() or path.is_symlink():
        meta = path.lstat()
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != uid or stat.S_IMODE(meta.st_mode) != 0o600
                or meta.st_nlink != 1):
            raise ValueError('routine store invalid')
    return path


def load(root, *, expected_uid=None):
    try:
        path = _path(root, expected_uid=expected_uid)
    except FileNotFoundError:
        return {'version': 1, 'routines': {}, 'receipts': {}, 'consumed': {}}
    if not path.exists():
        return {'version': 1, 'routines': {}, 'receipts': {}, 'consumed': {}}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding='utf-8') as stream:
        doc = json.load(stream)
    if (type(doc) is not dict or set(doc) != {'version', 'routines', 'receipts', 'consumed'}
            or doc['version'] != 1 or any(type(doc[key]) is not dict for key in ('routines', 'receipts', 'consumed'))):
        raise ValueError('routine store invalid')
    for rid, row in doc['routines'].items():
        base = {'routine_id', 'definition', 'definition_hash', 'status', 'binding', 'initial_utc', 'create_proof'}
        if (type(row) is not dict or set(row) not in (base, base | {'cancel_proof'}, base | {'retired'})
                or type(rid) is not str or not HEX.fullmatch(rid) or row['routine_id'] != rid
                or row['status'] not in ('active', 'cancelled', 'retired')
                or (row['status'] == 'retired') != ('retired' in row)
                or 'retired' in row and row['retired'] != {'reason': 'binding_changed'}):
            raise ValueError('routine record invalid')
        binding = row['binding']
        if (type(binding) is not dict or set(binding) != {'identity', 'profile', 'epoch', 'route'}
                or type(binding['epoch']) is not str or re.fullmatch(r'[a-f0-9]{32}', binding['epoch']) is None
                or binding['profile'] not in ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1', 'self-host-telegram-fixed-five-v1')
                or type(binding['identity']) is not dict or type(binding['route']) is not dict
                or datetime.fromisoformat(row['initial_utc'].replace('Z', '+00:00')).tzinfo is None):
            raise ValueError('routine binding invalid')
        validate_definition(row['definition'])
        channel = binding['identity'].get('channel')
        expected_profile_channel = 'telegram' if binding['profile'] == 'self-host-telegram-fixed-five-v1' else 'photon'
        if channel != expected_profile_channel or row['definition']['destination'] != DESTINATIONS[channel]:
            raise ValueError('routine channel binding invalid')
        if row['definition_hash'] != digest(row['definition']):
            raise ValueError('routine definition digest invalid')
    for event, receipt in doc['receipts'].items():
        if type(receipt) is not dict or set(receipt) != {'proof', 'outcome'}:
            raise ValueError('routine receipt invalid')
        proof, outcome = receipt['proof'], receipt['outcome']
        if (proof.get('event_id') != event or proof.get('domain') != DOMAIN
                or type(outcome) is not dict or set(outcome) != ({'operation', 'routine_id', 'definition_hash', 'status','previous_routine_id','previous_definition_hash'} if outcome.get('operation') == 'adjust' else {'operation', 'routine_id', 'definition_hash', 'status'})
                or outcome['operation'] not in ('create', 'cancel','adjust')
                or outcome['status'] != ('active' if outcome['operation'] in ('create','adjust') else 'cancelled')
                or outcome['routine_id'] not in doc['routines']
                or outcome['definition_hash'] != doc['routines'][outcome['routine_id']]['definition_hash']):
            raise ValueError('routine receipt invalid')
        if outcome['operation'] == 'adjust':
            old = doc['routines'].get(outcome['previous_routine_id'])
            args = proof.get('args', {})
            replacement = doc['routines'][outcome['routine_id']]
            if (old is None or old['definition_hash'] != outcome['previous_definition_hash']
                    or args.get('operation') != 'adjust' or args.get('routine_id') != outcome['previous_routine_id']
                    or args.get('previous_definition_hash') != outcome['previous_definition_hash']
                    or args.get('definition') != replacement['definition'] or replacement['create_proof'] != proof
                    or outcome['routine_id'] != digest(['sotto.fresh-routine.v1', proof.get('action_id')])):
                raise ValueError('routine adjustment receipt invalid')
    expected_consumed = {row['create_proof']['args']['proposal']['action_id']: rid
                         for rid, row in doc['routines'].items()}
    if doc['consumed'] != expected_consumed:
        raise ValueError('routine proposal fence invalid')
    return doc


def list_rows(doc):
    rows = sorted(doc['routines'].values(), key=lambda r: (r['status'] != 'active', r['routine_id']))[:100]
    return [{'routine_id': row['routine_id'], **{k: row['definition'][k] for k in ('name', 'schedule', 'zone', 'prompt')},
             'status': row['status']} for row in rows]


def schedule_key(schedule):
    """Canonical run set: a weekday-set alias of daily, weekdays or one weekday is the same schedule."""
    day = schedule['weekday']
    days = (tuple(range(7)) if day is None else tuple(range(1, 6)) if day == 'weekdays'
            else ('monthly',) if day == 'first_of_month' else
            tuple(day) if type(day) is list else (day,))
    return schedule['hour'], schedule['minute'], days


def check_available(doc, value, binding):
    """Only the current owner binding's active routines count toward the cap and conflicts."""
    active = [row for row in doc['routines'].values() if row['status'] == 'active' and row['binding'] == binding]
    if len(active) >= 10:
        raise RefusalError('routine_limit')
    if any(row['definition']['name'] == value['name'] or
           (schedule_key(row['definition']['schedule']), row['definition']['prompt']) ==
           (schedule_key(value['schedule']), value['prompt'])
           for row in active):
        raise RefusalError('routine_conflict')


def adjustment_definition(doc, args, binding):
    if (type(args) is not dict or set(args) != {'operation','routine_id','schedule'}
            or args.get('operation') != 'propose_adjust' or type(args['routine_id']) is not str
            or not HEX.fullmatch(args['routine_id'])):
        raise ValueError('routine adjustment proposal invalid')
    row = doc['routines'].get(args['routine_id'])
    if row is None or row['status'] != 'active' or row['binding'] != binding:
        raise RefusalError('target_unavailable')
    old = row['definition']
    value = {**old,'schedule':args['schedule']}
    validate_definition(value)
    if value == old or schedule_key(value['schedule']) == schedule_key(old['schedule']):
        raise RefusalError('unsupported_definition')
    check_available({'routines':{rid:r for rid,r in doc['routines'].items() if rid != args['routine_id']}}, value, binding)
    return row, value


def preview_adjustment(root, args, binding):
    path = _path(root)
    with _preferences().jsonstore.lock(str(path)):
        return adjustment_definition(load(root), args, binding)


def retire_other_bindings(root, binding):
    """After a proven owner route or identity change, an old-binding routine can never fire again.

    Mark it retired (reason `binding_changed`) so it stops counting, blocking or looking live; its
    receipts and proposal fence stay, so no old approval becomes reusable.
    """
    helpers = _preferences()
    try:
        path = _path(root)
    except FileNotFoundError:
        return 0
    if not path.exists():
        return 0
    with helpers.jsonstore.lock(str(path)):
        doc = load(root)
        stale = [row for row in doc['routines'].values() if row['status'] == 'active' and row['binding'] != binding]
        for row in stale:
            row['status'] = 'retired'
            row['retired'] = {'reason': 'binding_changed'}
        if stale:
            helpers._durable_preferences_replace(str(path), doc)
        return len(stale)


def apply(root, proof, binding, *, inspect=False):
    helpers = _preferences()
    path = _path(root, create=True)
    args = proof['args']
    op = args.get('operation') if type(args) is dict else None
    if op in ('create','adjust'):
        if set(args) != ({'operation', 'routine_id', 'previous_definition_hash', 'definition', 'proposal'} if op == 'adjust' else {'operation', 'definition', 'proposal'}):
            raise ValueError('routine create invalid')
        validate_definition(args['definition'])
        if op == 'adjust' and (type(args['routine_id']) is not str or not HEX.fullmatch(args['routine_id'])
                or type(args['previous_definition_hash']) is not str or not HEX.fullmatch(args['previous_definition_hash'])):
            raise ValueError('routine adjustment invalid')
        proposal = args['proposal']
        if (type(proposal) is not dict or set(proposal) != {'event_id', 'action_id', 'args_hash', 'accepted_dispatch_id', 'source_revision'}
                or any(type(proposal[key]) is not str or not HEX.fullmatch(proposal[key])
                       for key in ('event_id', 'action_id', 'args_hash', 'source_revision'))
                or type(proposal['accepted_dispatch_id']) is not str or not 0 < len(proposal['accepted_dispatch_id']) <= 160):
            raise ValueError('routine proposal proof invalid')
    elif (op != 'cancel' or set(args) != {'operation', 'routine_id'}
          or type(args['routine_id']) is not str or not HEX.fullmatch(args['routine_id'])):
        raise ValueError('routine cancel invalid')
    with helpers.jsonstore.lock(str(path)):
        doc = load(root)
        event = proof['event_id']
        if event in doc['receipts']:
            saved = doc['receipts'][event]
            if saved['proof'] != proof:
                raise RefusalError('event_conflict')
            helpers._sync_existing_preferences(str(path))
            return saved['outcome']
        if inspect:
            return None
        if op in ('create','adjust'):
            if proposal['event_id'] == event or not proof.get('confirmed'):
                raise RefusalError('proposal_unavailable')
            preview_args = ({'operation':'propose_adjust','routine_id':args['routine_id'],'schedule':args['definition']['schedule']}
                            if op == 'adjust' else {'operation': 'propose', **{key: args['definition'][key] for key in ('name', 'schedule', 'prompt')}})
            canonical = json.dumps(preview_args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            expected_action = hashlib.sha256(json.dumps([proposal['event_id'], 'owner_routine_tool', canonical], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            if proposal['args_hash'] != hashlib.sha256(canonical.encode()).hexdigest() or proposal['action_id'] != expected_action:
                raise RefusalError('proposal_unavailable')
            if proposal['action_id'] in doc['consumed']:
                raise RefusalError('proposal_unavailable')
            value = args['definition']
            if op == 'adjust':
                old = doc['routines'].get(args['routine_id'])
                if old is None or old['definition_hash'] != args['previous_definition_hash']:
                    raise RefusalError('target_unavailable')
                old, rebuilt = adjustment_definition(doc, preview_args, binding)
                if rebuilt != value:
                    raise RefusalError('target_unavailable')
            else:
                check_available(doc, value, binding)
            rid = digest(['sotto.fresh-routine.v1', proof['action_id']])
            due = successor_after_completion(expr=expression(value),
                completed_at=datetime.fromtimestamp(proof['admitted_at_ms'] / 1000, timezone.utc), zone=value['zone'])
            row = {'routine_id': rid, 'definition': value, 'definition_hash': digest(value), 'status': 'active',
                   'binding': binding, 'initial_utc': due, 'create_proof': proof}
            if op == 'adjust':
                old['status'] = 'cancelled'
                old['cancel_proof'] = proof
            doc['routines'][rid] = row
            doc['consumed'][proposal['action_id']] = rid
        else:
            rid = args['routine_id']
            row = doc['routines'].get(rid)
            if row is None or row['binding'] != binding:
                raise RefusalError('target_unavailable')
            row['status'] = 'cancelled'
            row['cancel_proof'] = proof
        outcome = {'operation': op, 'routine_id': rid, 'definition_hash': row['definition_hash'], 'status': row['status']}
        if op == 'adjust':
            outcome.update(previous_routine_id=args['routine_id'], previous_definition_hash=args['previous_definition_hash'])
        doc['receipts'][event] = {'proof': proof, 'outcome': outcome}
        helpers._durable_preferences_replace(str(path), doc)
        return outcome


def cursor_binding(row):
    return {'owner_scope': digest(row['binding']['identity']), 'profile_id': row['binding']['profile'],
            'legacy_id': row['routine_id'], 'activation_epoch': row['binding']['epoch'],
            'source_revision': row['definition_hash'], 'owner_route': row['binding']['route']}


def cursors(root):
    path = Path(root) / 'events/work.sqlite3'
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return {row['cursor_hash']: dict(row) for row in db.execute('SELECT * FROM routine_cursors')}
