"""One automatic first useful look; the receiver/outbox remain the only delivery lane."""
from datetime import datetime, timezone
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import re
import time

from connectors import json_transaction

LABEL = 'onboarding:sotto-welcome-brief'
LEASE_SECONDS = 20 * 60
RETRY_SECONDS = 30 * 60
ATTEMPTS_PER_DAY = 3
_MARKER = re.compile(r'(\d{4}-\d{2}-\d{2})\.(morning|evening)\.delivered\Z')
_RUN_ID = re.compile(r'[0-9a-f]{32}\Z')


def _markers(root):
    markers = []
    for path in (root / 'briefs').glob('*.delivered'):
        match = _MARKER.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        try:
            markers.append((match.group(1), match.group(2), path.read_text().strip(),
                            path.stat().st_mtime))
        except OSError:
            continue
    return markers


def _outbox_rows(root):
    try:
        rows = json.loads((root / 'events/outbox.json').read_text()).get('rows', [])
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError, AttributeError):
        return []


@lru_cache(maxsize=1)
def _tzchain():
    # The same tzchain file receiver/dashboard use; a UTC date can differ from
    # the local date embedded in a brief marker near midnight.
    here = Path(__file__).resolve()
    for candidate in (here.with_name('tzchain.py'),
                      here.parents[2] / 'sotto-chief-of-staff/_shared/lib/tzchain.py'):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location('onboarding_tzchain', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def _accepted_scheduled_brief(root):
    """A marker is a claim; only a matching provider-accepted record proves delivery."""
    markers = _markers(root)
    if not markers:
        return False
    for row in _outbox_rows(root):
        if not isinstance(row, dict) or row.get('status') != 'delivered' \
                or row.get('acceptance') != 'accepted':
            continue
        receipt = row.get('receipt') or {}
        if not isinstance(receipt, dict) or not receipt.get('accepted_at') or not (
                receipt.get('message_id') or receipt.get('message_ids')):
            continue
        payload = row.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        label = payload.get('label') or ''
        for day, kind, owner, _ in markers:
            if row.get('day') != day or not label.endswith(f'sotto-{kind}-brief'):
                continue
            # Applied effects compact payload to its label. The durable outbox
            # id is the run id used by the brief gate and the marker.
            if owner and owner != row.get('id'):
                continue
            return True
    # Older installations may have pruned the outbox row. The receiver's
    # delivery ledger is written only after its channel reports acceptance.
    tzchain = _tzchain()
    zone = (tzchain.resolve(tzchain.configured_tz_name(str(root))) if tzchain else None) or timezone.utc
    try:
        with (root / 'events/delivery.jsonl').open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.get('status') != 'delivered':
                        continue
                    stamp = datetime.fromisoformat(row['ts'].replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        continue
                    accepted_at = stamp.astimezone(timezone.utc).timestamp()
                    label = row.get('label') or ''
                    local_day = datetime.fromtimestamp(accepted_at, zone).date().isoformat()
                    for day, kind, owner, claimed_at in markers:
                        if not label.endswith(f'sotto-{kind}-brief'):
                            continue
                        if day != local_day:
                            continue
                        if owner and row.get('run_id') and owner != row['run_id']:
                            continue
                        if _RUN_ID.fullmatch(owner) and not row.get('run_id'):
                            continue
                        if -60 <= accepted_at - claimed_at <= 6 * 3600:
                            return True
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
    except OSError:
        pass
    return False


def _pending_state(row):
    """A pending send that may already have reached the provider is ambiguous, not queued.

    That is a row marked uncertain, or a gallery the outbox holds for its receipt and will never
    resend; either can stay pending indefinitely, so it must not hold later briefs like a queued one.
    """
    payload = row.get('payload') or {}
    if (row.get('acceptance_uncertain') or row.get('acceptance') == 'unknown'
            or (isinstance(payload, dict) and payload.get('presentation')
                and row.get('acceptance') == 'in_flight')):
        return 'waiting'
    return 'queued'


def _unresolved_scheduled_brief(root):
    """A claimed scheduled send may still land, or may already have landed."""
    # The receiver persists composed work in the outbox before the send seam
    # creates its brief marker. Do not admit welcome through that gap.
    pending = set()
    for row in _outbox_rows(root):
        payload = row.get('payload') if isinstance(row, dict) else None
        label = payload.get('label', '') if isinstance(payload, dict) else ''
        if (label in ('cron:sotto-morning-brief', 'cron:sotto-evening-brief',
                      'brief:sotto-morning-brief', 'brief:sotto-evening-brief')
                and row.get('status') == 'pending'):
            pending.add(_pending_state(row))
    if pending:
        # A genuinely queued brief still holds later scheduled work until it is attempted.
        return 'queued' if 'queued' in pending else 'waiting'
    markers = {(day, kind, owner) for day, kind, owner, _ in _markers(root) if owner}
    for row in _outbox_rows(root):
        if not isinstance(row, dict):
            continue
        payload = row.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        if not any(row.get('day') == day and row.get('id') == owner
                   and (payload.get('label') or '').endswith(f'sotto-{kind}-brief')
                   for day, kind, owner in markers):
            continue
        if row.get('status') == 'pending':
            return _pending_state(row)
        if (row.get('acceptance_uncertain') or row.get('acceptance') in
                ('unknown', 'in_flight', 'accepted')):
            return 'waiting'
    return None


def scheduled_inflight(data):
    """A scheduled send is still queued or has uncertain provider acceptance."""
    return bool(_unresolved_scheduled_brief(Path(data)))


def _adopt_existing(root, now):
    if not _accepted_scheduled_brief(root):
        return False
    with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
        # Any unfinished welcome phase: a failed or abandoned first look must not keep an
        # install that already receives scheduled briefs "fresh", or compose a second first look.
        if state.get('phase') != 'delivered':
            state.update(phase='existing', completed_at=now, acceptance_proven=True)
    return True


_UNCERTAIN = ('unknown', 'in_flight', 'accepted')


def _welcome_ambiguous(rows):
    """A welcome send that may have landed; like a scheduled brief, it never authorizes a replay."""
    return any(isinstance(r, dict) and r.get('status') != 'delivered'
               and (r.get('acceptance_uncertain') or r.get('acceptance') in _UNCERTAIN)
               for r in rows)


def tick(data, ready, start, now=None, *, model_ready=None):
    """Reserve before spawning; resume after crashes, and finish only on a channel receipt."""
    now = time.time() if now is None else now
    root = Path(data)
    try:
        snapshot = json.loads((root / 'config/onboarding.json').read_text())
    except (OSError, ValueError, AttributeError):
        snapshot = {}
    if snapshot.get('phase') == 'delivered':
        return 'delivered'
    if snapshot.get('phase') == 'existing':
        return 'existing'  # historical acceptance evidence may have aged out
    if _adopt_existing(root, now):
        return 'existing'
    if not snapshot.get('phase'):
        if snapshot.get('scheduled_ambiguous'):
            return 'waiting'  # terminal outbox evidence may have been pruned
        unresolved = _unresolved_scheduled_brief(root)
        if unresolved:
            if unresolved == 'waiting':
                with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
                    if not state.get('phase'):
                        state['scheduled_ambiguous'] = True
            return unresolved
    try:
        outbox_rows = json.loads((root / 'events/outbox.json').read_text()).get('rows', [])
    except (OSError, ValueError, AttributeError):
        outbox_rows = []
    welcome_rows = [r for r in outbox_rows if r.get('payload', {}).get('label') == LABEL]
    if not any(r.get('status') in ('delivered', 'pending') for r in welcome_rows) and (
            snapshot.get('welcome_ambiguous') or _welcome_ambiguous(welcome_rows)):
        if not snapshot.get('welcome_ambiguous'):
            with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
                state['welcome_ambiguous'] = True
        return 'waiting'
    if snapshot.get('phase') == 'model_held':
        # A provider-accepted composition wins even if a stale worker later reported a model hold.
        if any(r.get('status') == 'delivered' for r in welcome_rows):
            delivered(data, now)
            return 'delivered'
        if any(r.get('status') == 'pending' for r in welcome_rows):
            return 'queued'
        unresolved = _unresolved_scheduled_brief(root)
        if unresolved:
            return unresolved
        try:
            if model_ready is None or model_ready() is not True:
                return 'model_held'
        except Exception:
            return 'model_held'  # uncertain capability never spends another source/model attempt
    if not (ready() if callable(ready) else ready):
        return 'waiting_for_context'
    if (snapshot.get('phase') == 'composing'
            and max(snapshot.get('lease_until', 0), snapshot.get('retry_at', 0)) > now):
        return 'composing'
    if snapshot.get('phase') == 'queued' and any(r.get('status') == 'pending' for r in welcome_rows):
        return 'queued'
    with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
        if state.get('phase') in ('delivered', 'existing'):
            return state['phase']
        welcome = welcome_rows
        if any(r.get('status') == 'delivered' for r in welcome):
            state.update(phase='delivered', completed_at=now)
            return 'delivered'
        if any(r.get('status') == 'pending' for r in welcome):
            state['phase'] = 'queued'
            return 'queued'
        # Upgrade in place: an established installation keeps its memory and conversation.
        # A concurrent accepted send is picked up on the next tick; the claim
        # alone cannot turn this state terminal while the outbox lock is held.
        if state.get('lease_until', 0) > now or state.get('retry_at', 0) > now:
            return state.get('phase', 'waiting')
        day = int(now // 86400)
        if state.get('attempt_day') != day:
            state.update(attempt_day=day, attempts=0)
        if state.get('attempts', 0) >= ATTEMPTS_PER_DAY:
            return 'retry_later'
        state.update(phase='composing', attempts=state.get('attempts', 0) + 1,
                     lease_until=now + LEASE_SECONDS, retry_at=now + RETRY_SECONDS)
    started = start()  # outside the state lock: completion can call delivered() immediately
    if started is False:
        with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
            if state.get('phase') == 'model_held':
                return 'model_held'
            if state.get('phase') == 'composing' and state.get('lease_until') == now + LEASE_SECONDS:
                state.update(phase='waiting', lease_until=0, retry_at=0,
                             attempts=max(0, state.get('attempts', 1) - 1))
        return 'not_started'
    return 'started'


def delivered(data, now=None):
    with json_transaction(str(Path(data) / 'config/onboarding.json'), default={}) as state:
        state.update(phase='delivered', completed_at=time.time() if now is None else now)


def model_budget_held(data):
    """Park an undelivered welcome until a bounded model capability check admits it."""
    with json_transaction(str(Path(data) / 'config/onboarding.json'), default={}) as state:
        if state.get('phase') not in ('delivered', 'existing'):
            if state.get('phase') == 'composing':
                # Only the reserved welcome attempt failed. A repeated denial or recovery
                # projection sees model_held and cannot refund another attempt.
                state['attempts'] = max(0, state.get('attempts', 0) - 1)
            state.update(phase='model_held', lease_until=0, retry_at=0)


def scheduled_hold(data, now=None):
    """The first look arriving at 6:30 should not immediately be followed by the same day's recap."""
    now = time.time() if now is None else now
    try:
        state = json.loads((Path(data) / 'config/onboarding.json').read_text())
        return ((state.get('phase') in ('composing', 'queued') and state.get('lease_until', 0) > now)
                or (state.get('phase') == 'delivered' and state.get('completed_at', 0) + RETRY_SECONDS > now))
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def status(data, now=None):
    """Read-only setup progress. Composition is never presented as a sent brief."""
    now = time.time() if now is None else now
    root = Path(data)
    try:
        state = json.loads((root / 'config/onboarding.json').read_text())
        if not isinstance(state, dict):
            return 'waiting'
    except (OSError, ValueError):
        state = {}
    try:
        rows = json.loads((root / 'events/outbox.json').read_text()).get('rows', [])
        welcome = [r for r in rows if isinstance(r, dict) and
                   isinstance(r.get('payload'), dict) and r['payload'].get('label') == LABEL]
    except (OSError, ValueError, AttributeError, TypeError):
        welcome = []
    if any(r.get('status') == 'delivered' for r in welcome):
        return 'delivered'
    if any(r.get('status') == 'pending' for r in welcome):
        return 'queued'
    phase = state.get('phase')
    if phase in ('delivered', 'existing'):
        return phase
    if _accepted_scheduled_brief(root):
        return 'existing'
    if state.get('welcome_ambiguous') or _welcome_ambiguous(welcome):
        return 'waiting'  # acceptance unknown: never shown as retrying or sent
    if phase == 'model_held':
        unresolved = _unresolved_scheduled_brief(root)
        if unresolved:
            return unresolved
        return 'model_held'
    if not phase:
        if state.get('scheduled_ambiguous'):
            return 'waiting'
        unresolved = _unresolved_scheduled_brief(root)
        if unresolved:
            return unresolved
    if phase == 'composing':
        try:
            if float(state.get('lease_until', 0)) > now:
                return 'composing'
        except (TypeError, ValueError):
            pass
        return 'retrying'
    if phase == 'queued' or any(r.get('status') == 'failed' for r in welcome):
        return 'retrying'
    return 'waiting'
