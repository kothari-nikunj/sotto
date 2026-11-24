"""Bounded observed context and explicit examples, shared by every relevance/draft consumer."""
from __future__ import annotations

import hashlib
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

import pack_path

# graph_display_name reads knowledge/ and conversation_filter reads scripts/; a fresh composer
# process must find both, or a graph-name mute silently stops matching.
pack_path.ensure()

FEEDBACK_DAYS = 42
FEEDBACK_LIMIT = 8
FEEDBACK_EXCERPT_CHARS = 320


def accepted_outputs(*, permitted=None):
    """Resolve the newest verifiable accepted edition while its sources remain authorized."""
    from accepted_briefs import bindings, permitted_sources
    from message_targets import delivery_body
    from source_context import allowed

    root = Path(os.environ.get('SOTTO_DATA', '/data'))
    selected = {}
    for reference, artifact, run, _, contacted in bindings(root, delivery_body=delivery_body, exact=True):
        order = (contacted, run)
        if reference not in selected or order > selected[reference][0]:
            selected[reference] = order, artifact
    outputs = {}
    for reference, (order, artifact) in selected.items():
        if not permitted_sources(artifact, lambda source: allowed(source)
                                 and (permitted is None or permitted(source))):
            continue
        text = delivery_body(artifact['brief_text'])
        version = hashlib.sha256(json.dumps(
            [reference, order[1], text, artifact['_source_permissions']],
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        outputs[reference] = {'text': text, 'archive_version': version}
    return outputs


def item_locator(text, excerpt):
    """Locate one rated item without persisting its private prose."""
    if not excerpt:
        return 'output'
    # Lookahead, not a plain search: re.finditer skips overlapping hits, so "aa" would look unique
    # in "aaa" and the locator would point at one of two equally valid items.
    starts = [match.start() for match in re.finditer('(?=' + re.escape(excerpt) + ')', text)]
    if not starts:
        raise ValueError('excerpt must appear verbatim in the referenced output')
    if len(starts) != 1:
        raise ValueError('excerpt must identify exactly one item in the referenced output')
    start = starts[0]
    digest = hashlib.sha256(excerpt.encode()).hexdigest()[:20]
    return f'excerpt:{start}:{len(excerpt)}:{digest}'


def _resolve_item(text, locator):
    if not text or not str(locator).startswith('excerpt:'):
        return None
    try:
        _, start, length, want = str(locator).split(':', 3)
        start, length = int(start), int(length)
    except (ValueError, TypeError):
        return None
    if length < 1 or length > FEEDBACK_EXCERPT_CHARS:
        return None
    candidate = text[start:start + length]
    return candidate if hashlib.sha256(candidate.encode()).hexdigest()[:20] == want else None


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


def _recent_ratings(root, now):
    for row in rows(root / 'outcomes.jsonl'):
        if (row.get('source') not in ('user_feedback', 'notification_feedback')
                or row.get('outcome') not in ('useful', 'not_useful')
                or not isinstance(row.get('reference'), str) or not isinstance(row.get('archive_version'), str)):
            continue
        try:
            stamp = datetime.fromisoformat(str(row['ts']).replace('Z', '+00:00'))
        except (KeyError, ValueError, TypeError):
            continue
        if now - timedelta(days=FEEDBACK_DAYS) <= (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)) <= now:
            yield row


def _notification_originals(root, ratings, gmail_ids, permitted):
    rated = [row for row in ratings if row['source'] == 'notification_feedback' and isinstance(row.get('source_ref'), str)]
    if not rated or not gmail_ids:
        return {}
    from source_context import allowed
    if not allowed('gmail') or (permitted is not None and not permitted('gmail')):
        return {}
    ids = {hashlib.sha256(f'gmail:{ident}'.encode()).hexdigest(): ident for ident in gmail_ids if isinstance(ident, str) and ident}
    references = {row['reference'] for row in rated if row['source_ref'] in ids}
    if not references:
        return {}
    try:
        scope = json.loads(os.environ.get('SOTTO_BRIEF_FEEDBACK_PROOF', 'null'))
    except ValueError:
        return {}
    if not isinstance(scope, dict) or not scope:
        return {}
    from protected_brief_detail import notification_bindings
    return {ref: (binding, ids[binding['source_ref']]) for ref, binding in notification_bindings(root, scope, references).items()
            if binding and binding['source_ref'] in ids}


def _brief_example(row, outputs):
    output = outputs.get(row['reference'])
    locator = row.get('item_locator')
    if not output or output['archive_version'] != row['archive_version'] or not isinstance(locator, str):
        return None
    example = {'outcome': row['outcome'], 'reference': row['reference'][:200], 'item_locator': locator}
    if locator != 'output':
        item = _resolve_item(output['text'], locator)
        if item is None:
            return None
        example['item'] = item
    return example


def feedback(now=None, *, permitted=None, gmail_ids=()):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    root = Path(os.environ.get('SOTTO_DATA', '/data'))
    ratings = list(_recent_ratings(root, now))
    outputs = accepted_outputs(permitted=permitted) if any(r['source'] == 'user_feedback' for r in ratings) else {}
    notifications = _notification_originals(root, ratings, gmail_ids, permitted)
    examples = {}
    for row in ratings:
        if row['source'] == 'notification_feedback':
            binding, ident = notifications.get(row['reference'], ({}, None))
            if not binding or any(binding[key] != row.get(key) for key in ('archive_version', 'source_ref', 'decision_id')):
                continue
            example = {'outcome': row['outcome'], 'previous_notification_about_gmail_message_id': ident}
        else:
            example = _brief_example(row, outputs)
            if example is None:
                continue
        key = row['reference'], row['archive_version'], example.get('item_locator')
        examples.pop(key, None)
        examples[key] = example
    return list(examples.values())[-FEEDBACK_LIMIT:]


def render_feedback(now=None, *, permitted=None, gmail_ids=()):
    examples = feedback(now, permitted=permitted, gmail_ids=gmail_ids)
    if not examples:
        return ''
    return ('\nUSER FEEDBACK ON SPECIFIC PAST OUTPUTS\n'
            'Use these explicit ratings only as narrow evidence about the referenced output. '
            'A previous notification rating applies to the nudge about that exact Gmail message. '
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


def resolve_sender(event, lookup):
    """Use the shared contact resolvers for a transport event, including email aliases."""
    from render_local import resolve_imessage_name, resolve_whatsapp_name, resolve_call_name
    from textutil import _s, _sender_addr, _extract_sender_name
    source = event_source(event)
    if source == 'gmail':
        envelope = _s(event.get('from'))
        ident = _sender_addr(envelope)
        return (lookup.get(ident) or _extract_sender_name(envelope) or ident or 'Unknown'), ident
    if source == 'whatsapp':
        ident = _s(event.get('sender_jid')) or _s(event.get('contact_jid'))
        return resolve_whatsapp_name(ident, _s(event.get('partner_name')), lookup), ident
    if source == 'calls':
        ident = _s(event.get('phone'))
        return resolve_call_name(ident, lookup) or '', ident
    ident = _s(event.get('handle'))
    return resolve_imessage_name(ident, lookup), ident


def graph_display_name(ident):
    """Best-effort exact-identifier lookup; a display name never joins people."""
    if not ident:
        return ''
    try:
        import knowledge
        from textutil import _looks_like_phone_number
        path = knowledge.find_person_file(identifier=ident)
        name = knowledge.parse_person_file(Path(path).read_text()).name if path else ''
        return name if name and name != 'Unknown' and not _looks_like_phone_number(name) else ''
    except Exception:  # noqa: BLE001
        return ''


def conversation_filter(explicit, lookup=None, *, graph_name=None):
    """One batch's current mute check, shared by triage, composition and delivery."""
    from email.utils import getaddresses
    import preferences
    from render_local import build_contact_lookup
    from source_context import allowed
    if lookup is None:
        lookup = {}
        if explicit.get('mute_people') and allowed('contacts'):
            try:
                path = Path(os.environ.get('SOTTO_DATA', '/data')) / 'knowledge/last_local_snapshot.json'
                lookup = build_contact_lookup(json.loads(path.read_text()).get('local', {}).get('contacts', []))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
    names = {}
    def include(event):
        if not isinstance(event, dict):
            return False
        try:
            for name in ('', event.get('resolved_name'), event.get('sender_name')):
                if preferences.proactively_muted(name, event, explicit):
                    return False
            if not explicit.get('mute_people'):
                return True
            if event_source(event) == 'gmail':
                addresses = getaddresses([str(event.get('from') or '')])
                if len(addresses) == 1 and preferences.proactively_muted(addresses[0][0], event, explicit):
                    return False
            name, ident = resolve_sender(event, lookup)
            if preferences.proactively_muted(name, event, explicit):
                return False
            if ident and normalized_identifier(ident) not in lookup:
                if ident not in names:
                    names[ident] = (graph_name or graph_display_name)(ident)
                return not preferences.proactively_muted(names[ident], event, explicit)
            return True
        except Exception:  # noqa: BLE001 — malformed preferences/identity hold context and delivery
            return False
    return include


def notification_sender(item):
    """Minimal selected-sender proof for current mute checks; never retain message prose."""
    event = item.get('event') or {}
    result = {key: event[key] for key in ('source', 'from', 'handle', 'contact_jid', 'sender_jid',
              'phone', 'email', 'address', 'partner_name', 'resolved_name', 'sender_name') if key in event}
    result.setdefault('source', item.get('channel', ''))
    if item.get('person'):
        result['resolved_name'] = item['person']
    if item.get('identifier'):
        result.setdefault('handle', item['identifier'])
        if event_source(result) == 'gmail':
            result.setdefault('from', item['identifier'])
    return result


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


def commitment_calendar(calendar, item):
    """Only a uniquely linked invitation can confirm the scheduling outcome."""
    thread = item.get('thread_id')
    if item.get('channel') not in ('email', 'gmail') or not thread or not calendar:
        return None
    events = [event for event in calendar.get('events') or [] if isinstance(event, dict)
              and (event.get('threadId') or event.get('thread_id')) == thread
              and event.get('start') and event.get('status') != 'cancelled' and event.get('my_response') != 'declined']
    return {'events': events} if len(events) == 1 else None


def direct_conversation_event(source, identifier, snapshot, include):
    """Locate one exact direct chat; never choose among several chats or use display names."""
    field = {'imessage': 'handle', 'whatsapp': 'contact_jid'}.get(source)
    peer = normalized_identifier(identifier)
    if not field or not peer:
        return {}
    matches = {conversation_key(event): event for event, _, _ in snapshot
               if event_source(event) == source and not (event.get('is_group_chat') or event.get('group_id'))
               and normalized_identifier(event.get(field)) == peer and include(event)}
    return next(iter(matches.values())) if len(matches) == 1 else {}


def _conversation_text(event):
    return '\n'.join(str(event.get(k) or '') for k in ('subject', 'text', 'body', 'snippet')
                     if event.get(k))


def conversation_message(event, *, timestamp='', prior_class='', text_limit=CONVERSATION_TEXT_CHARS,
                         preserve_ending=False):
    from timeutil import parse_observed_time, source_observed_time
    observed = source_observed_time(event) or parse_observed_time(timestamp)
    text = _conversation_text(event)
    original_length = len(text)
    clipped = original_length > text_limit
    if clipped:
        if preserve_ending:
            marker = '\n[message middle omitted]\n'
            head = (text_limit - len(marker)) // 2
            text = text[:head] + marker + text[-(text_limit - len(marker) - head):]
        else:
            text = text[:text_limit]  # keep digest and meeting-prep's existing 400-char preview
    return {'source': event_source(event), 'ts': observed.isoformat() if observed else '',
            'thread': conversation_key(event), 'is_from_me': bool(event.get('is_from_me')) or prior_class == 'signal',
            'text': text, 'prior_class': prior_class, 'original_length': original_length,
            'text_clipped': clipped}


def conversation_snapshot(events=None, *, include=None):
    """Read retained context once, plus copies of incoming rows, with optional sender filtering."""
    from source_context import project_local
    incoming = None if events is None else [dict(event) for event in events if isinstance(event, dict)]
    if incoming == []:
        return []
    threads = None if incoming is None else {conversation_key(event) for event in incoming}
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
    candidates.extend((event, '', '') for event in incoming or [])
    return [row for row in candidates if isinstance(row[0], dict)
            and (threads is None or conversation_key(row[0]) in threads)
            and (include is None or include(row[0]))]


def current_conversation(event, now=None, candidates=None, *, days=CONVERSATION_DAYS, limit=CONVERSATION_MESSAGES):
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
        message = conversation_message(candidate, timestamp=stamp, prior_class=cls,
                                       text_limit=CONVERSATION_TOTAL_CHARS, preserve_ending=True)
        parsed = _parse_ts(message['ts'])
        if parsed is None:
            if candidate != event:
                continue
            parsed = now
        parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        if not now - timedelta(days=days) <= parsed <= now:
            continue
        # Deduplicate using the full observed text: two distinct endings must not collapse merely
        # because their bounded display excerpts happen to match.
        key = (message['ts'], message['is_from_me'], hashlib.sha256(
            _conversation_text(candidate).encode()).digest())
        selected[key] = (parsed, message)
    ordered = sorted(selected.values(), key=lambda pair: pair[0])
    messages = [m for _, m in ordered[-limit:]]
    while sum(len(m['text']) for m in messages) > CONVERSATION_TOTAL_CHARS and len(messages) > 1:
        messages.pop(0)
    if messages and len(ordered) > len(messages):
        messages[0]['older_observed_messages_omitted'] = len(ordered) - len(messages)
    return messages


TOPIC_RECORD_CHARS = 800
TOPIC_CHARS = 2400
TOPIC_RECORDS = 6


def _participant_records(*, local=None, gmail=None, calendar=None, loops=None):
    """One permission/identity projection for both the cohort and its retrieval topics."""
    from email.utils import getaddresses
    from source_context import allowed, project_local
    local = project_local(local or {})
    # Explicit active work is first: stale file mtimes and chat volume cannot crowd it out.
    for item in (loops or []):
        if not isinstance(item, dict) or item.get('status') in ('closed', 'resolved', 'done', 'cancelled', 'expired'):
            continue
        if item.get('group_id'):
            continue
        yield [item.get(f) for f in ('canonical_id', 'contact_identifier', 'identifier')], \
            str(item.get('summary') or item.get('ask') or '')
    if allowed('calendar'):
        for event in calendar or []:
            if isinstance(event, dict):
                yield [p.get('email') if isinstance(p, dict) else p for p in event.get('attendees') or []], \
                    ' '.join(str(event.get(k) or '') for k in ('summary', 'title', 'description'))
    for source, fields in (('imessage', ('handle', 'canonical_id')), ('whatsapp', ('contact_jid', 'canonical_id'))):
        for message in reversed(local.get(source) or []):
            if not isinstance(message, dict) or message.get('is_group_chat'):
                continue
            yield [message.get(f) for f in fields], \
                ' '.join(str(message.get(k) or '') for k in ('subject', 'text', 'body'))
    if allowed('gmail'):
        for message in gmail or []:
            if isinstance(message, dict):
                yield [a for f in ('from', 'to', 'cc') if message.get(f)
                       for _, a in getaddresses([str(message[f])])], \
                    ' '.join(str(message.get(k) or '') for k in ('subject', 'snippet', 'body'))


def participant_identifiers(**inputs):
    """Select current participants by addresses/canonical IDs, never by fuzzy name or time."""
    identifiers = []
    for values, _ in _participant_records(**inputs):
        for value in values:
            value = normalized_identifier(value)
            if value and value not in identifiers and len(identifiers) < PARTICIPANT_LIMIT:
                identifiers.append(value)
    return set(identifiers)


def participant_topics(*, cohort=None, **inputs):
    """Bounded lexical retrieval hints, never instructions or a new identity join."""
    from itertools import chain
    cohort = participant_identifiers(**inputs) if cohort is None else cohort
    topics = {}
    # Work decides WHO packs, but current conversations decide WHAT to recall. Several long
    # standing obligations must not use every topic slot before today's message is considered.
    records = chain(_participant_records(local=inputs.get('local'), gmail=inputs.get('gmail')),
                    _participant_records(calendar=inputs.get('calendar')),
                    _participant_records(loops=inputs.get('loops')))
    for values, text in records:
        text = ' '.join(text.split())[:TOPIC_RECORD_CHARS]
        if not text:
            continue
        for value in values:
            value = normalized_identifier(value)
            if not value or value not in cohort:
                continue
            parts = topics.setdefault(value, [])
            if text not in parts and len(parts) < TOPIC_RECORDS:
                parts.append(text)
    return {key: ' '.join(parts)[:TOPIC_CHARS] for key, parts in topics.items()}


def topic_for_identifiers(topics, identifiers):
    """Share the existing topic budget across exact aliases; a busy phone cannot hide an email."""
    parts = list(dict.fromkeys(topics[key] for key in sorted({normalized_identifier(i) for i in identifiers})
                              if topics.get(key)))
    if not parts:
        return ''
    share = max(0, (TOPIC_CHARS - len(parts) + 1) // len(parts))
    return ' '.join(part[:share] for part in parts)[:TOPIC_CHARS]


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Relevant prior context and explicit output feedback')
    parser.add_argument('--participant', action='append', default=None,
                        help='exact canonical ID, email or phone; repeat for a task cohort')
    parser.add_argument('--feedback-only', action='store_true')
    args = parser.parse_args()
    print(render_feedback())
