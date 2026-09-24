"""Delivery-dependent changes, staged by a skill and committed after provider acceptance.

The outbox owns retrying these idempotent effects. Eligibility uses current source/loop/calendar
truth; it never treats a stale or failed calendar read as a cancellation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
FRESH_NUDGE_VALID_SECONDS = 30 * 60
CALENDAR_MIN_FRESH_SECONDS = 600
POST_MEETING_VALID_SECONDS = 30 * 60
# These classes already carry a semantic deadline at admission: missed calls use the event
# funnel's 240-minute envelope, while calendar changes expire at the affected meeting. Applying
# the generic message-freshness deadline here would silently shorten both contracts to 30 minutes.
FRESH_NUDGE_EXEMPT_CLASSES = frozenset({'missed_call', 'calendar_change'})
LOOP_FIELDS = ('anchor_key', 'status', 'summary', 'ask', 'source_refs', 'contact_identifier',
               'canonical_id', 'contact_name', 'group_id', 'source_thread_id', 'source_message_id',
               'created_at', 'snoozed_until', 'resolution_mode', 'action_type', 'channel',
               'meeting_time', 'deadline', 'due')
# The obligation's identity: what makes it THIS debt rather than a restatement of it. Summary,
# ask and deadline are wording (Learn restates them); `source_refs` is evidence (the follow-up merge
# appends to it) — neither belongs here, or a loop that gained wording or evidence between compose
# and delivery would lose its count for exactly the day it moved.
LOOP_IDENTITY_FIELDS = ('anchor_key', 'created_at', 'action_type', 'origin_key', 'source',
                        'source_thread_id', 'source_message_id')


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
        for raw_effect in effects:
            effect = dict(raw_effect)
            if effect.get('kind') in ('loop_surfaced', 'review_candidate_offer'):
                # The outbox/run identity is durable before provider I/O and survives effect
                # retries even on legacy accepted transports that return no provider message ID.
                effect['delivery_id'] = ident
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


def notification_selection(selected, proactive=None):
    """Only rendered candidates earn delivery receipts; omitted candidates retain their state."""
    ident = run_id()
    if not ident:
        return
    with jsonstore.transaction(_path(ident), default={}, strict=True) as doc:
        doc['notification_decision_ids'] = sorted(set(selected))
        # A retry can select a different primary after the original ask closes. Keep the
        # send-time closure gate bound to this attempt's selection, not a previous attempt.
        doc['effects'] = [e for e in doc.get('effects', [])
                         if e.get('kind') != 'eligibility' or not e.get('notification_id')
                         or e['notification_id'] in selected]
        if proactive is not None:
            keys = {n.get('key') for n in proactive}
            anchors = {(n.get('kind'), n.get('anchor_key')) for n in proactive}
            intentions = {n.get('intention_id') for n in proactive}
            doc['effects'] = [e for e in doc.get('effects', [])
                if (e.get('kind') != 'proactive_seen' or e.get('key') in keys)
                and (e.get('kind') not in ('chase', 'handoff') or (e.get('kind'), e.get('anchor_key')) in anchors)
                and (e.get('kind') != 'intention' or e.get('id') in intentions)]


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


def meeting_occurrence(event_id, start):
    """Stable identity for one scheduled occurrence; a reschedule is a new occurrence."""
    event_id, start_at = str(event_id or '').strip(), instant(start)
    return f'{event_id}@{start_at:.3f}' if event_id and start_at is not None else ''


def _loops():
    import ledger_io
    return {row.get('anchor_key'): row for row in ledger_io.load_entries() if row.get('anchor_key')}


def loop_version(row):
    return hashlib.sha256(json.dumps({k: row.get(k) for k in LOOP_FIELDS},
                                    sort_keys=True, default=str).encode()).hexdigest()


def loop_identity(row):
    """Stable obligation identity across Learn restatements; never a fuzzy content match."""
    identity = {key: row.get(key) for key in LOOP_IDENTITY_FIELDS}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


def delivered_surface_count(row):
    """Trusted number of distinct accepted deliveries that actually named this loop."""
    provenance = row.get('delivery_surface') if isinstance(row, dict) else None
    keys = provenance.get('delivery_keys') if isinstance(provenance, dict) else None
    if not isinstance(provenance, dict) or provenance.get('schema') != 1 or not isinstance(keys, list):
        return 0
    try:
        return len({key for key in keys if isinstance(key, str)})
    except (TypeError, ValueError):
        return 0


def source_for_event(event):
    source = event.get('source', '')
    if source == 'proactive':
        source = ('calendar' if event.get('kind') == 'meeting_prep' else
                  'contacts' if event.get('kind') == 'birthday' else event.get('channel', ''))
    return {'email': 'gmail', 'calendar_change': 'calendar', 'meeting_end': 'calendar',
            'phonecalls': 'calls'}.get(source, source)


def request_reference(event):
    """Use the extractor's source message identity, never a sender or thread as an obligation."""
    if event.get('source') not in ('gmail', 'email', 'imessage', 'whatsapp'):
        return None
    source = source_for_event(event)
    if source == 'gmail':
        ident = event.get('source_id') or event.get('id') or event.get('messageId') or event.get('message_id')
    elif source in ('imessage', 'whatsapp'):
        from render_local import message_evidence_id
        ident = message_evidence_id(event, source)
    else:
        ident = None
    return {'source': source, 'id': str(ident)} if ident else None


def request_closed(reference, loops=None):
    """A held original message cannot revive its settled obligations.

    One message can contain several asks. Keep it eligible if any matching obligation is active;
    a new message on the same thread is independent. No fuzzy person/wording joins.
    """
    if not reference:
        return False
    import ledger_io
    matches = []
    for row in (loops if loops is not None else _loops()).values():
        refs = row.get('source_refs') or []
        if isinstance(refs, dict):
            refs = [refs]
        refs = [r for r in refs if isinstance(r, dict)]
        if row.get('source_message_id'):
            refs = [*refs, {'sourceType': row.get('channel'), 'sourceId': row['source_message_id']}]
        if any(source_for_event({'source': r.get('sourceType') or r.get('source_type') or r.get('source')}) == reference['source']
               and str(r.get('sourceId') or r.get('source_id') or r.get('id') or '') == reference['id'] for r in refs):
            matches.append(row)
    return bool(matches) and all(r.get('status') in ledger_io.TERMINAL for r in matches)


def for_bundle(bundle):
    """Capture minimal eligibility facts; exact identity survives formatting and worker retries."""
    effects, deadlines = [], []
    # Re-read immediately before delivery. Zero means every unsolicited nudge, including classes
    # that ordinarily bypass accounting; scheduled briefs/digests never use this bundle helper.
    unsolicited = (bundle.get('_user_requested_delivery') is not True
                   and any('verdict_class' not in row for row in bundle.get('events', [])
                           if isinstance(row, dict)))
    generated = instant(bundle.get('generated_at'))
    if unsolicited:
        effects.append({'kind': 'unsolicited_nudge'})
    loops = None
    for row in bundle.get('events', []):
        event = row.get('event') or row
        delivery_class = str(row.get('class') or '')
        source = source_for_event(event)
        effect = {'kind': 'eligibility', 'source': source}
        deadline = instant(event.get('valid_until'))
        relevance_deadline = instant(row.get('relevance_deadline'))
        # An evidenced future deadline narrows delivery. An already-overdue actionable obligation
        # remains useful; treating its past due time as expiry would discard the held reminder.
        if deadline is None and relevance_deadline is not None and (
                generated is None or relevance_deadline > generated):
            deadline = relevance_deadline
        if (deadline is None and unsolicited and not row.get('deferred_class')
                and delivery_class not in FRESH_NUDGE_EXEMPT_CLASSES):
            observed = instant(event.get('timestamp') or event.get('date'))
            if observed is not None:
                deadline = observed + FRESH_NUDGE_VALID_SECONDS
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
        if effect.get('kind') == 'unsolicited_nudge':
            import preferences
            if preferences.effective_nudge_budget() == 0:
                return False
            continue
        if effect.get('kind') == 'source_permissions':
            if not all(allowed(source) for source in effect.get('sources', [])):
                return False
            continue
        if effect.get('kind') == 'parking_notice':
            loops = _loops() if loops is None else loops
            if not parking_notice_valid(effect, now, rows=loops):
                return False
            continue
        if effect.get('kind') == 'loop_surfaced':
            # Bookkeeping cannot withhold a composed brief. Finalization re-reads the ledger and
            # quietly declines stale/closed effects after the transport accepts the text.
            continue
        if effect.get('kind') == 'review_candidate_offer':
            # Bookkeeping cannot withhold the Friday brief. Once the question is accepted,
            # finalization records its cooldown even if eligibility changed after composition.
            continue
        if effect.get('kind') != 'eligibility':
            continue
        source = effect.get('source')
        if source and not allowed(source):
            return False
        # Only the selected notification carries this reference. Inferring it from every
        # bundled event would let a closed, unselected ask suppress a fresh primary item.
        reference = effect.get('request_reference')
        if reference:
            loops = _loops() if loops is None else loops
            if request_closed(reference, loops):
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


def parking_notice_valid(effect, now=None, *, rows=None):
    """A rendered warning must still describe the exact live, unmuted obligation at send time."""
    now = time.time() if now is None else now
    import ledger_io
    import preferences
    from timeutil import configured_tz, _resolve_tz
    loops = _loops() if rows is None else rows
    row = loops.get(str(effect.get('anchor_key') or ''))
    if (not row or loop_version(row) != effect.get('loop_version')
            or ledger_io.last_touch_day(row) != effect.get('touch')
            or ledger_io.parking_notice_current(row)):
        return False
    zone = _resolve_tz(configured_tz() or '+00:00') or timezone.utc
    tomorrow = (datetime.fromtimestamp(now, timezone.utc).astimezone(zone).date()
                + timedelta(days=1)).isoformat()
    if not ledger_io.park_candidate(row, tomorrow):
        return False
    explicit = preferences.load_explicit()
    muted_sections = {str(value).lower().strip().replace(' ', '_')
                      for value in explicit.get('mute_sections', [])}
    if 'what_moved_today' in muted_sections or 'parking_notice' in muted_sections:
        return False
    name = str(row.get('contact_name') or '').strip().lower()
    if name and any(name == str(value).strip().lower()
                    for value in explicit.get('mute_people', [])):
        return False
    if preferences.sender_is_muted(str(row.get('contact_identifier') or ''),
                                   explicit.get('mute_senders', [])):
        return False
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
        elif kind == 'meeting_prep_delivered':
            occurrence = meeting_occurrence(effect.get('calendar_event_id'), effect.get('calendar_start'))
            if occurrence and effect.get('mode') in ('offer', 'full'):
                date = str(effect.get('date') or '')
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                    from timeutil import configured_tz, _resolve_tz
                    zone = _resolve_tz(configured_tz() or '+00:00') or timezone.utc
                    date = datetime.fromtimestamp(instant(effect['calendar_start']), zone).date().isoformat()
                with proactive_state(date) as state:
                    state['prep_deliveries'] = sorted(set(state.get('prep_deliveries', [])) | {occurrence})
        elif kind == 'intention':
            import schedule_wakeup
            schedule_wakeup.transition(effect['id'], 'fired')
        elif kind == 'pending_offer':
            import pending_offer
            pending_offer.activate_offer(effect['offer'], receipt)
        elif kind == 'parking_notice':
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'morning-brief/scripts'))
            import continuity_resolve
            if not continuity_resolve.acknowledge_parking_notice(effect, receipt['accepted_at']):
                return False
        elif kind == 'loop_surfaced':
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'morning-brief/scripts'))
            import continuity_resolve
            if not continuity_resolve.acknowledge_loop_surfaced(effect, receipt):
                return False
        elif kind == 'review_candidate_offer':
            import review_candidates
            # The question reached the user whether or not the candidate is still eligible by send
            # time, so the cooldown is always recorded: it records delivery and grants no authority.
            # Skipping it let the same question come back the next Friday.
            if not review_candidates.mark_offered(effect, receipt):
                return False
    return True
