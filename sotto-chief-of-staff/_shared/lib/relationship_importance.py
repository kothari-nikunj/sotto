"""Stable relationship importance and finite engagement hints from dated observations."""
import sys
import hashlib
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from statistics import median

_KNOWLEDGE = str(Path(__file__).parents[1] / 'knowledge')
if _KNOWLEDGE not in sys.path:
    sys.path.insert(0, _KNOWLEDGE)
from knowledge import CONFIDENCE_DECAY_PER_WEEK, PRUNE_STALE_AFTER_DAYS  # noqa: E402

WINDOW_DAYS = 42
VIP_ACTIVE_DAYS, VIP_ACTIVE_WEEKS, VIP_EACH_DIRECTION_DAYS = 6, 3, 2
VVIP_ACTIVE_DAYS, VVIP_ACTIVE_WEEKS, VVIP_EACH_DIRECTION_DAYS = 12, 4, 4
VIP_HELD_MEETINGS, VIP_HELD_WEEKS = 3, 3
VVIP_HELD_MEETINGS, VVIP_HELD_WEEKS = 6, 4
MAX_RELATIONSHIP_MEETING_ATTENDEES = 1
REPLY_SAMPLE_LIMIT = 32
REPLY_MIN_SAMPLES = 2


def _days(values, now):
    today = now.astimezone(timezone.utc).date()
    first = today - timedelta(days=WINDOW_DAYS - 1)
    result = set()
    for value in values or []:
        try:
            day = (value.astimezone(timezone.utc).date() if isinstance(value, datetime)
                   else datetime.strptime(str(value), '%Y-%m-%d').date())
            if first <= day <= today:
                result.add(day)
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def activity_evidence(person, previous, now):
    """Union dated observations, never sum overlapping windows or retain peak volume."""
    previous = previous if isinstance(previous, dict) else {}
    return {key: sorted(day.isoformat() for day in _days(
        list(previous.get(key) or []) + list(person.get(source) or []), now))
        for key, source in (('active_days', 'dates'), ('sent_days', 'from_me'),
                            ('received_days', 'from_them'), ('held_meeting_days', 'held_meetings'))}


def meaningful_relationship(evidence, now=None):
    """Require repeated reciprocal contact; meetings can strengthen but never create the bond."""
    evidence = evidence if isinstance(evidence, dict) else {}
    if now is None:
        def all_days(values):
            result = set()
            for value in values or []:
                parsed = _instant(value)
                if parsed:
                    result.add(parsed.astimezone(timezone.utc).date())
                    continue
                try:
                    result.add(datetime.strptime(str(value), '%Y-%m-%d').date())
                except (TypeError, ValueError):
                    continue
            return result
        sent = all_days(evidence.get('sent_days'))
        received = all_days(evidence.get('received_days'))
    else:
        active = _days(evidence.get('active_days'), now)
        sent = _days(evidence.get('sent_days'), now) & active
        received = _days(evidence.get('received_days'), now) & active
    return min(len(sent), len(received)) >= VIP_EACH_DIRECTION_DAYS


def relationship_meeting_attendees(attendees, user_email=''):
    """Return the sole counterpart in a one-to-one meeting; groups provide no evidence."""
    raw = {str(value).strip().casefold() for value in attendees or [] if str(value).strip()}
    owner = str(user_email or '').strip().casefold()
    counterparts = sorted(value for value in raw if '@' in value and value != owner)
    return counterparts if len(counterparts) == MAX_RELATIONSHIP_MEETING_ATTENDEES else []


def classify(evidence, now, explicit_vip=False):
    evidence = evidence if isinstance(evidence, dict) else {}
    active = _days(evidence.get('active_days'), now)
    sent = _days(evidence.get('sent_days'), now) & active
    received = _days(evidence.get('received_days'), now) & active
    held = _days(evidence.get('held_meeting_days'), now)
    weeks = {day.isocalendar()[:2] for day in active}
    held_weeks = {day.isocalendar()[:2] for day in held}
    tier, reason = 'regular', 'insufficient sustained two-way activity'
    meaningful = meaningful_relationship(evidence, now)
    if explicit_vip:
        tier, reason = 'vip', 'you marked this person VIP'
    elif meaningful and len(held) >= VVIP_HELD_MEETINGS and len(held_weeks) >= VVIP_HELD_WEEKS:
        tier, reason = 'vvip', 'frequent meetings actually held across several weeks'
    elif len(active) >= VVIP_ACTIVE_DAYS and len(weeks) >= VVIP_ACTIVE_WEEKS and min(len(sent), len(received)) >= VVIP_EACH_DIRECTION_DAYS:
        tier, reason = 'vvip', 'frequent two-way activity across several weeks'
    elif meaningful and len(held) >= VIP_HELD_MEETINGS and len(held_weeks) >= VIP_HELD_WEEKS:
        tier, reason = 'vip', 'meetings actually held across several weeks'
    elif len(active) >= VIP_ACTIVE_DAYS and len(weeks) >= VIP_ACTIVE_WEEKS and min(len(sent), len(received)) >= VIP_EACH_DIRECTION_DAYS:
        tier, reason = 'vip', 'regular two-way activity across several weeks'
    return {'tier': tier, 'reason': reason, 'active_days': len(active), 'active_weeks': len(weeks),
            'sent_days': len(sent), 'received_days': len(received), 'held_meetings': len(held),
            'window_days': WINDOW_DAYS}


def _instant(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def engagement_signals(person, previous, now):
    """Derive real alternating replies within one conversation; bursts count once."""
    previous = previous if isinstance(previous, dict) else {}
    cutoff = now - timedelta(days=WINDOW_DAYS - 1)
    conversations = {}
    seen = set()
    for raw in person.get('events') or []:
        at, conversation = _instant(raw.get('at')), str(raw.get('conversation_id') or '')
        native = str(raw.get('native_id') or '')
        if not at or not conversation or not (cutoff <= at <= now):
            continue
        channel = str(raw.get('channel') or '')
        # Channel-scoped either way: the same phone-shaped id on iMessage and WhatsApp at one
        # instant is two events in two conversations, not a duplicate.
        dedupe = (channel, native) if native else (channel, conversation, at, bool(raw.get('from_me')))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        # Native thread identifiers are channel-local. A phone-shaped iMessage handle and a
        # WhatsApp partner id can normalize to the same text; pairing across them would invent a
        # reply (and, after two such pairs, owner reply-speed evidence) where no thread contains one.
        conversations.setdefault((str(raw.get('channel') or ''), conversation), []).append(
            (at, bool(raw.get('from_me')), str(raw.get('channel') or ''), native))

    observed = []
    for (conversation_channel, conversation), events in conversations.items():
        events.sort()
        runs = []
        for at, from_me, channel, native in events:
            if runs and runs[-1][2] == from_me:
                first_at, first_id, _direction, _last_at, _last_id, run_channel = runs[-1]
                runs[-1] = (first_at, first_id, from_me, at, native, run_channel)
            else:
                runs.append((at, native, from_me, at, native, channel))
        for start_event, finish_event in zip(runs, runs[1:]):
            _start_first, _start_first_id, _starter, start, _start_last_id, channel = start_event
            finish, finish_id, finisher, _finish_last, _finish_last_id, _finish_channel = finish_event
            # A response is identified by the reply itself. Later overlapping pages can reveal an
            # earlier message in the incoming burst; including that start would count one reply
            # twice under two ids.
            raw_id = "|".join((conversation_channel, conversation, channel,
                               finish_id or finish.isoformat(), "owner" if finisher else "counterpart"))
            observed.append({'id': hashlib.sha256(raw_id.encode()).hexdigest()[:24],
                'conversation_key': hashlib.sha256(
                    f"{conversation_channel}|{conversation}".encode()).hexdigest()[:24],
                'direction': 'owner' if finisher else 'counterpart',
                'started_at': start.isoformat(), 'replied_at': finish.isoformat(),
                'lag_days': round((finish - start).total_seconds() / 86400.0, 4)})
    result = dict(previous)
    samples = []
    for sample in list(previous.get('reply_samples') or []) + observed:
        if not isinstance(sample, dict) or not sample.get('id'):
            continue
        replied = _instant(sample.get('replied_at'))
        started = _instant(sample.get('started_at'))
        try:
            lag = float(sample.get('lag_days'))
        except (TypeError, ValueError):
            lag = -1
        if (sample.get('direction') in {'owner', 'counterpart'} and replied and started
                and 0 <= lag <= PRUNE_STALE_AFTER_DAYS
                and timedelta(0) <= now - replied <= timedelta(days=PRUNE_STALE_AFTER_DAYS)):
            duplicate = None
            for index, saved in enumerate(samples):
                saved_reply, saved_start = _instant(saved.get('replied_at')), _instant(saved.get('started_at'))
                # One reply at one instant is one sample — within a conversation. Two channels
                # can carry two replies at the same second; legacy samples with no key still
                # collapse on the instant alone.
                same_conversation = (not sample.get('conversation_key') or not saved.get('conversation_key')
                                     or sample.get('conversation_key') == saved.get('conversation_key'))
                exact_reply = (saved.get('direction') == sample.get('direction')
                               and saved_reply == replied and same_conversation)
                same_turn = (sample.get('conversation_key')
                             and sample.get('conversation_key') == saved.get('conversation_key')
                             and saved.get('direction') == sample.get('direction')
                             and saved_start and saved_reply
                             and max(saved_start, started) <= min(saved_reply, replied))
                if exact_reply or same_turn:
                    duplicate = index
                    break
            if duplicate is None:
                samples.append(sample)
            else:
                samples[duplicate] = sample
    retained = sorted(samples, key=lambda sample: str(sample.get('replied_at') or ''))[-REPLY_SAMPLE_LIMIT:]
    if retained:
        result['reply_samples'] = retained
    else:
        result.pop('reply_samples', None)
    owner_replies = [sample for sample in retained if sample.get('direction') == 'owner']
    counterpart_replies = [sample for sample in retained if sample.get('direction') == 'counterpart']
    # Below the floor nothing learned survives: the documented two-sample rule is the only guard
    # against learning from one data point, and a value carried over from a fuller past would
    # outlive the evidence that earned it.
    if len(owner_replies) < REPLY_MIN_SAMPLES:
        for key in ('owner_reply_at', 'owner_reply_lag_days', 'attention_boost', 'attention_boost_expires'):
            result.pop(key, None)
    if len(counterpart_replies) < REPLY_MIN_SAMPLES:
        for key in ('counterpart_reply_lag_days', 'counterpart_reply_lag_expires', 'last_owner_message_at'):
            result.pop(key, None)
    if len(owner_replies) >= REPLY_MIN_SAMPLES:
        latest = max(_instant(sample['replied_at']) for sample in owner_replies)
        owner_lag = max(0.0, median(float(sample['lag_days']) for sample in owner_replies))
        result['owner_reply_at'] = latest.isoformat()
        result['owner_reply_lag_days'] = round(owner_lag, 2)
        result['attention_boost'] = round(1.0 / (1.0 + owner_lag), 3)
        result['attention_boost_expires'] = (latest + timedelta(days=PRUNE_STALE_AFTER_DAYS)).isoformat()
    if len(counterpart_replies) >= REPLY_MIN_SAMPLES:
        latest_sample = max(counterpart_replies, key=lambda sample: str(sample['replied_at']))
        latest = _instant(latest_sample['replied_at'])
        # Whole days are explainable and bounded; a learned hint may delay, never accelerate.
        result['counterpart_reply_lag_days'] = max(1, min(7, int(round(median(
            float(sample['lag_days']) for sample in counterpart_replies)))))
        result['counterpart_reply_lag_expires'] = (latest + timedelta(days=PRUNE_STALE_AFTER_DAYS)).isoformat()
        result['last_owner_message_at'] = str(latest_sample['started_at'])
    for key, expiry_key in (('attention_boost', 'attention_boost_expires'),
                            ('counterpart_reply_lag_days', 'counterpart_reply_lag_expires')):
        expiry = _instant(result.get(expiry_key))
        if not expiry or expiry < now:
            result.pop(key, None)
            result.pop(expiry_key, None)
            if key == 'attention_boost':
                result.pop('owner_reply_at', None)
                result.pop('owner_reply_lag_days', None)
            else:
                result.pop('last_owner_message_at', None)
    return result


def attention_tiebreak(engagement, now):
    """A recent owner reply is a small tie-break only; missing observations never subtract."""
    engagement = engagement if isinstance(engagement, dict) else {}
    expiry = _instant(engagement.get('attention_boost_expires'))
    observed = _instant(engagement.get('owner_reply_at'))
    if not engagement.get('attention_boost') or not expiry or expiry < now or not observed:
        return 0.0
    weeks = max((now - observed).total_seconds(), 0) / (7 * 86400)
    confidence = max(0.0, 1.0 - weeks * CONFIDENCE_DECAY_PER_WEEK)
    return round(float(engagement['attention_boost']) * confidence, 3)


def learned_chase_after(default_at, engagement, now=None, obligation_at=None):
    """Return a deadline-less chase hint which can only delay the caller's default."""
    engagement = engagement if isinstance(engagement, dict) else {}
    default = _instant(default_at)
    obligation = _instant(obligation_at)
    ref = _instant(now) or datetime.now(timezone.utc)
    expiry = _instant(engagement.get('counterpart_reply_lag_expires'))
    sent = _instant(engagement.get('last_owner_message_at'))
    try:
        days = int(engagement.get('counterpart_reply_lag_days'))
    except (TypeError, ValueError):
        days = 0
    if not default or not expiry or expiry < ref or not sent or days <= 0:
        return default_at
    observed = expiry - timedelta(days=PRUNE_STALE_AFTER_DAYS)
    weeks = max((ref - observed).total_seconds(), 0) / (7 * 86400)
    confidence = max(0.0, 1.0 - weeks * CONFIDENCE_DECAY_PER_WEEK)
    learned_days = ceil(min(days, 7) * confidence)
    # Historical samples teach a duration, not the due date of the historical message. Apply that
    # duration to this obligation's own observation/chase/promise anchor; otherwise a new debt
    # created weeks later always collapses back to its ordinary default.
    anchor = obligation or sent
    return max(default, anchor + timedelta(days=learned_days))


def engagement_for_contact(contact, history):
    """Resolve persisted engagement without letting an ambiguous display name borrow signals."""
    history = history if isinstance(history, dict) else {}
    cid = str(contact.get('canonical_id') or '')
    if cid:
        record = history.get(cid, {})
        return record.get('engagement', {}) if isinstance(record, dict) else {}
    name = str(contact.get('name') or contact.get('display_name') or '').strip().casefold()
    matches = [row for key, row in history.items() if isinstance(row, dict)
               and str(row.get('name') or key).strip().casefold() == name]
    return matches[0].get('engagement', {}) if len(matches) == 1 else {}


def for_contact(contact, history, vip_people, now, aliases=None):
    """Prefer canonical identity; ambiguous display names never borrow someone's rank."""
    name = str(contact.get('name') or '').strip().casefold()
    cid = str(contact.get('canonical_id') or '')
    history = history if isinstance(history, dict) else {}
    if cid:
        record = history.get(cid, {})
    else:
        matches = [record for key, record in history.items() if isinstance(record, dict)
                   and str(record.get('name') or key).strip().casefold() == name]
        if len(matches) > 1:
            return classify({}, now)
        record = matches[0] if matches else {}
    contact_names = {name} | {str(v).strip().casefold() for v in aliases or [] if str(v).strip()}
    explicit = bool(contact_names & {str(v).strip().casefold() for v in vip_people or []})
    return classify(record.get('importance_evidence', {}), now, explicit_vip=explicit)


def main():
    import argparse
    import json
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Explain relationship importance from saved activity')
    parser.add_argument('--person')
    args = parser.parse_args()
    root = Path(os.environ.get('SOTTO_DATA', '/data'))
    def read(path):
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    history = read(root / 'knowledge/relationship_state.json').get('history', {})
    vips = read(root / 'preferences.json').get('explicit', {}).get('vip_people', [])
    now = datetime.now(timezone.utc)
    names = [args.person] if args.person else sorted({str(h.get('name') or key) for key, h in history.items()} | set(vips))
    people = [{'name': name, **for_contact({'name': name}, history, vips, now)} for name in names]
    print(json.dumps({'people': people if args.person else [p for p in people if p['tier'] in {'vip', 'vvip'}]}))


if __name__ == '__main__':
    main()
