"""Bounded observed context and explicit examples, shared by every relevance/draft consumer."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

FEEDBACK_DAYS = 42
FEEDBACK_LIMIT = 8


def rows(path, limit=2000):
    try:
        with open(path, encoding='utf-8') as handle:
            lines = deque(handle, maxlen=limit)
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result.append(value)
        except ValueError:
            continue
    return result


def feedback(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    recent = {}
    for row in rows(Path(os.environ.get('SOTTO_DATA', '/data')) / 'outcomes.jsonl'):
        if row.get('source') != 'user_feedback' or row.get('outcome') not in ('useful', 'not_useful'):
            continue
        try:
            stamp = datetime.fromisoformat(str(row['ts']).replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if not now - timedelta(days=FEEDBACK_DAYS) <= stamp <= now:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        key = row.get('reference')
        recent.pop(key, None)
        recent[key] = {k: str(row.get(k) or '')[:200] for k in ('outcome', 'reference')}
    return list(recent.values())[-FEEDBACK_LIMIT:]


def render_feedback(now=None):
    examples = feedback(now)
    if not examples:
        return ''
    return ('\nUSER FEEDBACK ON SPECIFIC PAST OUTPUTS\n'
            'Use these explicit ratings only as narrow evidence about the referenced output. '
            'An item rating is not a sender mute, a permanent rule, or permission to act. '
            'Do not infer dislike from missing feedback or from an unanswered message. '
            'User-stated rules and corrections take precedence over inferred patterns. '
            + json.dumps(examples, ensure_ascii=False) + '\nEND USER FEEDBACK\n')


# Source/thread identity is shared by live review and catch-up; display names never join threads.
CONVERSATION_DAYS = 7
CONVERSATION_MESSAGES = 12
CONVERSATION_TEXT_CHARS = 400
CONVERSATION_TOTAL_CHARS = 4_800
PARTICIPANT_LIMIT = 80


def normalized_identifier(value):
    from textutil import _normalize_identifier
    value = str(value or '')
    return '' if value.lower().endswith('@lid') else (_normalize_identifier(value) or '')


def event_source(event):
    source = str(event.get('source') or '').lower()
    return 'gmail' if source in ('email', 'gmail') else source


def conversation_key(event):
    """Stable source/thread key; a missing identity stays independent, never name-joined."""
    import hashlib
    from email.utils import getaddresses
    source = event_source(event)
    thread = next((str(event[k]).strip() for k in ('threadId', 'thread_id', 'chat_guid', 'chat_id', 'group_id')
                   if event.get(k)), '')
    if thread:
        return source + ':thread:' + thread
    identifier = next((event[k] for k in ('handle', 'contact_jid', 'phone', 'email') if event.get(k)), '')
    if not identifier:
        identifier = event.get('to' if event.get('is_from_me') else 'from') or ''
    if source == 'gmail':
        addresses = getaddresses([str(identifier)])
        identifier = addresses[0][1] if len(addresses) == 1 else ''
    identifier = normalized_identifier(identifier)
    if identifier and not event.get('is_group_chat'):
        return source + ':person:' + identifier
    stable = event.get('source_id') or event.get('rowid') or event.get('id')
    if stable is None:
        stable = hashlib.sha256(json.dumps(event, sort_keys=True, default=str).encode()).hexdigest()[:20]
    return source + ':event:' + str(stable)


def conversation_message(event, *, timestamp='', prior_class=''):
    from timeutil import parse_observed_time
    observed = parse_observed_time(event.get('timestamp') or event.get('date')) or parse_observed_time(timestamp)
    text = '\n'.join(str(event.get(k) or '') for k in ('subject', 'text', 'body', 'snippet') if event.get(k))
    return {'source': event_source(event), 'ts': observed.isoformat() if observed else '',
            'thread': conversation_key(event), 'is_from_me': bool(event.get('is_from_me')) or prior_class == 'signal',
            'text': text[:CONVERSATION_TEXT_CHARS], 'prior_class': prior_class}


def conversation_snapshot():
    """Read shared queue/snapshot sources once for an ingress batch."""
    from source_context import project_local
    root = Path(os.environ.get('SOTTO_DATA', '/data'))
    candidates = [(r.get('event') or {}, r.get('ts', ''), r.get('verdict_class', ''))
                  for r in rows(root / 'events/queue.jsonl')]
    try:
        snapshot = json.loads((root / 'knowledge/last_local_snapshot.json').read_text())
        local = project_local(snapshot.get('local') or {})
        for source in ('imessage', 'whatsapp', 'gmail'):
            candidates.extend(({**r, 'source': source}, '', '') for r in local.get(source, [])[-400:]
                              if isinstance(r, dict))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return candidates


def current_conversation(event, now=None, candidates=None):
    """Bounded observed thread context, including replies; no model calls or guessed joins."""
    from source_context import allowed
    from timeutil import _parse_ts
    source = event_source(event)
    if not allowed(source):
        return []
    now = now or datetime.now(timezone.utc)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    candidates = list(candidates if candidates is not None else conversation_snapshot())
    candidates.append((event, '', ''))
    selected = {}
    for candidate, stamp, cls in candidates:
        if not isinstance(candidate, dict) or conversation_key(candidate) != conversation_key(event):
            continue
        message = conversation_message(candidate, timestamp=stamp, prior_class=cls)
        parsed = _parse_ts(message['ts'])
        if parsed is None:
            if candidate != event:
                continue
            parsed = now
        parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        if not now - timedelta(days=CONVERSATION_DAYS) <= parsed <= now:
            continue
        key = (message['ts'], message['is_from_me'], message['text'])
        selected[key] = (parsed, message)
    messages = [m for _, m in sorted(selected.values(), key=lambda pair: pair[0])[-CONVERSATION_MESSAGES:]]
    while sum(len(m['text']) for m in messages) > CONVERSATION_TOTAL_CHARS and len(messages) > 1:
        messages.pop(0)
    return messages


def participant_identifiers(*, local=None, gmail=None, calendar=None, loops=None):
    """Select current participants by addresses/canonical IDs, never by fuzzy name or time."""
    from email.utils import getaddresses
    from source_context import allowed, project_local
    identifiers = []
    def add(value):
        value = normalized_identifier(value)
        if value and value not in identifiers and len(identifiers) < PARTICIPANT_LIMIT:
            identifiers.append(value)
    local = project_local(local or {})
    # Explicit active work is first: stale file mtimes and chat volume cannot crowd it out.
    for item in (loops or []):
        if not isinstance(item, dict) or item.get('status') in ('closed', 'resolved', 'done', 'cancelled', 'expired'):
            continue
        if item.get('group_id'):
            continue
        for field in ('canonical_id', 'contact_identifier', 'identifier'):
            add(item.get(field))
    if allowed('calendar'):
        for event in calendar or []:
            for person in event.get('attendees') or []:
                add(person.get('email') if isinstance(person, dict) else person)
    for source, fields in (('imessage', ('handle', 'canonical_id')), ('whatsapp', ('contact_jid', 'canonical_id'))):
        for message in reversed(local.get(source) or []):
            if not isinstance(message, dict) or message.get('is_group_chat'):
                continue
            for field in fields:
                add(message.get(field))
    if allowed('gmail'):
        for message in gmail or []:
            for field in ('from', 'to', 'cc'):
                for _, address in getaddresses([str(message.get(field) or '')]):
                    add(address)
    return set(identifiers)


if __name__ == '__main__':
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'knowledge'))
    parser = argparse.ArgumentParser(description='Relevant prior context and explicit output feedback')
    parser.add_argument('--participant', action='append', default=None,
                        help='exact canonical ID, email or phone; repeat for a task cohort')
    parser.add_argument('--feedback-only', action='store_true')
    args = parser.parse_args()
    print(render_feedback())
