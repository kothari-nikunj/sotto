"""Delivery-dependent changes, staged by a skill and committed after provider acceptance.

The outbox owns retrying these idempotent effects. Eligibility uses current source/loop/calendar
truth; it never treats a stale or failed calendar read as a cancellation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import jsonstore  # noqa: E402

MAX_PENDING_SECONDS = 4 * 3600
CALENDAR_MIN_FRESH_SECONDS = 600
POST_MEETING_VALID_SECONDS = 30 * 60
LOOP_FIELDS = ('status', 'title', 'action_type', 'deadline', 'due', 'identifier', 'thread_id')


def _root():
    return Path(os.environ.get('SOTTO_DATA', '/data'))


def run_id(value=None):
    value = value if value is not None else os.environ.get('SOTTO_DELIVERY_RUN_ID', '')
    return value if re.fullmatch(r'[a-f0-9]{16,64}', str(value)) else ''


def _path(ident):
    return str(_root() / 'events' / f'delivery-effects-{ident}.json')


def stage(effects, decision_ids=None, run_id=None, result=None):
    """Merge producers in one run; never replace a previously staged offer or chase receipt."""
    ident = globals()['run_id'](run_id)
    if not ident:
        return False
    with jsonstore.transaction(_path(ident), default={}, strict=True) as doc:
        merged = doc.setdefault('effects', [])
        for effect in effects:
            if effect not in merged:
                merged.append(effect)
        doc['decision_ids'] = sorted(set(doc.get('decision_ids', [])) | set(decision_ids or []))
        if result is not None:
            doc['proactive_result'] = result
    return True


def cached_result():
    ident = run_id()
    if not ident:
        return None
    result = jsonstore.read(_path(ident), {}, strict=True).get('proactive_result')
    if result is not None:
        return result
    # A crash can occur after reserving the result but before staging effects. The original
    # decision lives in the same atomic document as its reservation, including across midnight.
    for path in sorted((_root() / 'proactive').glob('????-??-??.json'))[-3:]:
        saved = jsonstore.read(str(path), {}, strict=True).get('runs', {}).get(ident)
        if saved is not None:
            return saved
    return None


def proactive_path(date):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(date)):
        raise ValueError('invalid proactive date')
    return str(_root() / 'proactive' / f'{date}.json')


@contextmanager
def proactive_state(date):
    with jsonstore.transaction(proactive_path(date), default={}, strict=True) as state:
        state.setdefault('nudged', [])
        state.setdefault('pending', {})
        yield state


def instant(value):
    if isinstance(value, (float, int)):
        return float(value)
    from timeutil import parse_observed_time
    parsed = parse_observed_time(value)
    return parsed.timestamp() if parsed else None


def _loops():
    import ledger_io
    return {row.get('anchor_key'): row for row in ledger_io.load_entries() if row.get('anchor_key')}


def loop_version(row):
    return hashlib.sha256(json.dumps({k: row.get(k) for k in LOOP_FIELDS},
                                    sort_keys=True).encode()).hexdigest()


def source_for_event(event):
    source = event.get('source', '')
    if source == 'proactive':
        source = ('calendar' if event.get('kind') == 'meeting_prep' else
                  'contacts' if event.get('kind') == 'birthday' else event.get('channel', ''))
    return {'email': 'gmail', 'calendar_change': 'calendar', 'meeting_end': 'calendar',
            'phonecalls': 'calls'}.get(source, source)


def for_bundle(bundle):
    """Capture minimal eligibility facts; exact identity survives formatting and worker retries."""
    effects, deadlines = [], []
    loops = None
    for row in bundle.get('events', []):
        event = row.get('event') or row
        source = source_for_event(event)
        effect = {'kind': 'eligibility', 'source': source}
        deadline = instant(event.get('valid_until'))
        if deadline is None and event.get('source') == 'meeting_end':
            ended = instant(event.get('end') or event.get('timestamp'))
            deadline = ended + POST_MEETING_VALID_SECONDS if ended else None
        if deadline is None and event.get('source') == 'calendar_change':
            deadline = instant(event.get('start'))
        if deadline is None and event.get('calendar_start'):
            deadline = instant(event['calendar_start'])
        if deadline is not None:
            effect['valid_until'] = deadline
            deadlines.append(deadline)
        if event.get('calendar_event_id'):
            effect.update(calendar_event_id=event['calendar_event_id'],
                          calendar_start=event.get('calendar_start') or event.get('start', ''))
            if event.get('calendar_observed_at'):
                effect['calendar_observed_at'] = event['calendar_observed_at']
        anchor = event.get('anchor_key')
        if anchor:
            loops = _loops() if loops is None else loops
            effect['anchor_key'] = anchor
            effect['loop_version'] = loop_version(loops[anchor]) if anchor in loops else ''
            due = str(loops.get(anchor, {}).get('deadline') or loops.get(anchor, {}).get('due') or '')
            exact_due = instant(due) if 'T' in due or ' ' in due else None
            if exact_due is not None:
                effect['valid_until'] = min(effect.get('valid_until', float('inf')), exact_due)
                deadlines.append(exact_due)
        if event.get('intention_id'):
            effect['intention_id'] = event['intention_id']
        if source in ('imessage', 'whatsapp', 'gmail'):
            effect['event'] = {k: event[k] for k in ('source', 'timestamp', 'date', 'threadId',
                'thread_id', 'chat_guid', 'chat_id', 'group_id', 'handle', 'contact_jid',
                'phone', 'email', 'from', 'to', 'source_id', 'id') if k in event}
            effect['event']['source'] = source
        effects.append(effect)
    return {'effects': effects, 'valid_until': min(deadlines) if deadlines else None}


def valid(effects, now=None):
    now = time.time() if now is None else now
    from source_context import allowed
    loops = None
    conversation = None   # the local snapshot, parsed at most once per call (it is multi-MB)
    for effect in effects:
        if effect.get('kind') == 'source_permissions':
            if not all(allowed(source) for source in effect.get('sources', [])):
                return False
            continue
        if effect.get('kind') != 'eligibility':
            continue
        source = effect.get('source')
        if source and not allowed(source):
            return False
        until = instant(effect.get('valid_until'))
        if until is not None and now >= until:
            return False
        anchor = effect.get('anchor_key')
        if anchor:
            import ledger_io
            loops = _loops() if loops is None else loops
            row = loops.get(anchor)
            if (not row or row.get('status', 'open') not in ledger_io.ACTIVE
                    or (effect.get('loop_version') and loop_version(row) != effect['loop_version'])):
                return False
        if effect.get('intention_id'):
            import schedule_wakeup
            if not any(r.get('id') == effect['intention_id'] and r.get('status') == 'scheduled'
                       for r in schedule_wakeup.current()):
                return False
        event_id = effect.get('calendar_event_id')
        if event_id:
            cache = jsonstore.read(str(_root() / 'cache' / 'calendar_today.json'), {})
            observed = instant(cache.get('generated_at'))
            candidate_observed = instant(effect.get('calendar_observed_at'))
            fresh = max(CALENDAR_MIN_FRESH_SECONDS, 3 * int(cache.get('refresh_secs') or 120))
            if (cache.get('status') == 'ok' and cache.get('complete') is True and observed is not None
                    and cache.get('projection') == 'day'
                    and str(effect.get('calendar_start', ''))[:10] == cache.get('date')
                    and (candidate_observed is None or observed >= candidate_observed)
                    and 0 <= now - observed <= fresh):
                if not any(e.get('id') == event_id
                           and instant(e.get('start')) == instant(effect.get('calendar_start'))
                           for e in cache.get('events', [])):
                    return False
        event = effect.get('event')
        if event:
            from personal_context import conversation_snapshot, current_conversation
            if conversation is None:
                conversation = conversation_snapshot()
            event_time = instant(effect.get('observed_until') or event.get('timestamp') or event.get('date'))
            if event_time and any(m.get('is_from_me') and (instant(m.get('ts')) or 0) > event_time
                                  for m in current_conversation(event,
                                      now=datetime.fromtimestamp(now, timezone.utc),
                                      candidates=conversation)):
                return False  # superseded reminder, not proof the underlying obligation is done
    return True


def finalize(effects, receipt):
    """Apply only our effect kinds. Replays must succeed without another provider send."""
    if not receipt.get('accepted_at'):
        return False
    for effect in effects:
        kind = effect.get('kind')
        if kind == 'proactive_seen':
            with proactive_state(effect['date']) as state:
                state['nudged'] = sorted(set(state['nudged']) | {effect['key']})
                pending = state['pending'].get(effect['key'], {})
                if pending.get('run_id') == effect.get('run_id'):
                    state['pending'].pop(effect['key'], None)
        elif kind == 'intention':
            import schedule_wakeup
            schedule_wakeup.transition(effect['id'], 'fired')
        elif kind == 'pending_offer':
            import pending_offer
            pending_offer.activate_offer(effect['offer'], receipt)
        elif kind == 'retune_offer':
            stamp_retune(effect['date'])
    return True


def stamp_retune(date):
    path = str(_root() / 'proactive' / 'retune_offer.last')
    with jsonstore.lock(path):
        try:
            previous = Path(path).read_text().strip()
        except FileNotFoundError:
            previous = ''
        if previous < date:
            tmp = path + f'.tmp.{os.getpid()}'
            with open(tmp, 'w', encoding='utf-8') as stream:
                stream.write(date)
            os.replace(tmp, path)
