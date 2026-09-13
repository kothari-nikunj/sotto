"""One automatic first useful look; the receiver/outbox remain the only delivery lane."""
import json
from pathlib import Path
import time

from connectors import json_transaction

LABEL = 'onboarding:sotto-welcome-brief'
LEASE_SECONDS = 20 * 60
RETRY_SECONDS = 30 * 60
ATTEMPTS_PER_DAY = 3


def tick(data, ready, start, now=None):
    """Reserve before spawning; resume after crashes, and finish only on a channel receipt."""
    now = time.time() if now is None else now
    root = Path(data)
    try:
        snapshot = json.loads((root / 'config/onboarding.json').read_text())
    except (OSError, ValueError, AttributeError):
        snapshot = {}
    if snapshot.get('phase') in ('delivered', 'existing'):
        return snapshot['phase']
    if not snapshot.get('phase') and any(
            list((root / 'briefs').glob('*.' + kind + '.delivered')) for kind in ('morning', 'evening')):
        with json_transaction(str(root / 'config/onboarding.json'), default={}) as state:
            state.update(phase='existing', completed_at=now)
        return 'existing'
    if not (ready() if callable(ready) else ready):
        return 'waiting_for_context'
    if (snapshot.get('phase') == 'composing'
            and max(snapshot.get('lease_until', 0), snapshot.get('retry_at', 0)) > now):
        return 'composing'
    try:
        outbox_rows = json.loads((root / 'events/outbox.json').read_text()).get('rows', [])
    except (OSError, ValueError, AttributeError):
        outbox_rows = []
    welcome_rows = [r for r in outbox_rows if r.get('payload', {}).get('label') == LABEL]
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
        if not state.get('phase') and any(
                list((root / 'briefs').glob('*.' + kind + '.delivered')) for kind in ('morning', 'evening')):
            state.update(phase='existing', completed_at=now)
            return 'existing'
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
            if state.get('phase') == 'composing' and state.get('lease_until') == now + LEASE_SECONDS:
                state.update(phase='waiting', lease_until=0, retry_at=0,
                             attempts=max(0, state.get('attempts', 1) - 1))
        return 'not_started'
    return 'started'


def delivered(data, now=None):
    with json_transaction(str(Path(data) / 'config/onboarding.json'), default={}) as state:
        state.update(phase='delivered', completed_at=time.time() if now is None else now)


def scheduled_hold(data, now=None):
    """The first look arriving at 6:30 should not immediately be followed by the same day's recap."""
    now = time.time() if now is None else now
    try:
        state = json.loads((Path(data) / 'config/onboarding.json').read_text())
        return ((state.get('phase') in ('composing', 'queued') and state.get('lease_until', 0) > now)
                or (state.get('phase') == 'delivered' and state.get('completed_at', 0) + RETRY_SECONDS > now))
    except (OSError, ValueError, AttributeError, TypeError):
        return False
