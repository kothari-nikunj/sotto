"""Bounded source refresh for carried-over obligations, never a loop-state writer.

Recent inbox sampling is not a completion check. Read a few exact threads/known
counterparts for the existing brief model; no name searches, new model pass, or
persistent message cache. An incomplete read is context, never proof of absence.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
from pathlib import Path

MAX_TARGETS = 6
MAX_REQUESTS = 10
MAX_MESSAGES = 24
MAX_BODY_CHARS = 2000
MAX_CONTEXT_CHARS = 48000
READ_BUDGET_SECONDS = 20
_EMAIL = re.compile(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+\Z")
# Messaging handles shaped like addresses (WhatsApp JIDs) can never receive or send mail.
_MESSAGING_DOMAINS = ('whatsapp.net', 'g.us', 'c.us', 'lid', 'broadcast', 'newsletter')
# Unsent drafts (including Sotto's own), trash and spam are not evidence of what anyone said.
_NOT_EVIDENCE = frozenset({'DRAFT', 'TRASH', 'SPAM'})
# Ledger channel spellings that name a consent source differently; others are already source IDs.
_CHANNEL_SOURCES = {'email': 'gmail', 'sms': 'imessage', 'phone': 'calls', 'call': 'calls',
                    'tel': 'calls', 'whatsapp_call': 'whatsapp_calls'}


def _mail_address(value):
    domain = value.rpartition('@')[2].casefold()
    return bool(_EMAIL.fullmatch(value)) and not any(
        domain == d or domain.endswith('.' + d) for d in _MESSAGING_DOMAINS)


def _row_sources(row):
    """The consent sources that observed this obligation: its channel and any named origin."""
    from source_context import CONTEXT_SOURCES
    values = (str(row.get('channel') or '').lower(), str(row.get('source') or '').lower())
    return {'gmail'} | {s for s in (_CHANNEL_SOURCES.get(v, v) for v in values) if s in CONTEXT_SOURCES}


def _local_now(now):
    """The ledger's day: the same configured zone `ledger_io.load_active` uses, never UTC."""
    from timeutil import _resolve_tz, _tz_offset_minutes, configured_tz
    zone_name = configured_tz() or '+00:00'
    zone = _resolve_tz(zone_name)
    return now.astimezone(zone) if zone else now + dt.timedelta(minutes=_tz_offset_minutes(zone_name))


def _evidence(messages):
    """The newest six messages that are mail someone sent or received."""
    return sorted((m for m in messages or [] if not _NOT_EVIDENCE & set(m.get('labelIds') or [])),
                  key=lambda m: int(m.get('internalDate') or 0))[-6:]


def _rate_limited_only(error):
    """Fallback classifier; callers pass the Gmail lane's own so a 403 is judged by its reason."""
    return 'rate_limited' if getattr(getattr(error, 'resp', None), 'status', None) == 429 else 'read_failed'


def _emails(row, local, graph_aliases):
    """Expand only observed identifiers; a display name is never a lookup key.

    Returns each mail address with the sources that supplied it (a roster, Apple Contacts).
    """
    identifier = str(row.get('contact_identifier') or '')
    identifiers = {identifier: set()} if identifier else {}
    group = str(row.get('group_id') or '')
    if group:
        identifiers = {}
        for field in ('imessage', 'whatsapp'):
            for message in local.get(field) or []:
                if str(message.get('chat_guid') or message.get('group_id') or '') == group:
                    for member in message.get('group_participants') or []:
                        identifiers.setdefault(str(member), set()).add(field)
    found = {}
    def add(value, sources):
        if _mail_address(value):
            found.setdefault(value.casefold(), set(sources))
    for identifier, sources in identifiers.items():
        add(identifier, sources)
        for alias in graph_aliases(identifier):
            add(str(alias), sources)
    # Contacts only adds a dependency for an address no grant-free path already supplied.
    for identifier, sources in identifiers.items():
        for contact in local.get('contacts') or []:
            values = [*(contact.get('emails') or []), *(contact.get('phones') or [])]
            if identifier.casefold() in {str(v).casefold() for v in values}:
                for value in values:
                    add(str(value), sources | {'contacts'})
    return found


def _plan(rows, local, *, now, allowed, graph_aliases):
    """Targets with the sources that chose each; a revoked source never steers a read."""
    local_now = _local_now(now)
    slot = local_now.strftime('%Y-%m-%d') + str(local_now.hour // 12)
    day = local_now.strftime('%Y-%m-%d')
    def rank(row):
        due = str(row.get('deadline') or '')[:10]
        return (0 if due and due <= day else 1,
                hashlib.sha256((slot + str(row.get('anchor_key'))).encode()).hexdigest())
    candidates, seen = [], set()
    eligible = [r for r in rows if r.get('status', 'open') in ('open', 'waiting', 'failed', 'blocked')
                and str(r.get('snoozed_until') or '')[:10] <= day
                and r.get('resolution_mode') != 'explicit']
    for row in sorted(eligible, key=rank):
        sources = _row_sources(row)
        if not all(allowed(source) for source in sources):
            continue
        channel = str(row.get('channel') or '').lower()
        tid = str(row.get('source_thread_id') or '')
        if tid and _CHANNEL_SOURCES.get(channel, channel) == 'gmail':
            options = [('thread', tid, sources)]
        else:
            addresses = _emails(row, local, graph_aliases)
            options = [('email', e, sources | addresses[e]) for e in sorted(addresses)]
            # Advance one address each half-day, with a stable per-task starting offset.
            # Rotating obligations alone would always query the alphabetically first member.
            if options:
                offset = int(hashlib.sha256(str(row.get('anchor_key')).encode()).hexdigest()[:8], 16)
                offset = (offset + local_now.toordinal() * 2 + local_now.hour // 12) % len(options)
                options = options[offset:] + options[:offset]
        # One counterpart per obligation in this round; multiple addresses are not extra slots.
        for kind, value, dependencies in options:
            if (kind, value) not in seen:
                candidates.append((kind, value, frozenset(dependencies)))
                seen.add((kind, value))
                break
    return candidates


def targets(rows, local, *, now, allowed, graph_aliases=lambda _: ()):
    """Rotate a bounded sample, prioritizing due work without permanently starving peers."""
    return [(kind, value) for kind, value, _ in
            _plan(rows, local, now=now, allowed=allowed, graph_aliases=graph_aliases)]


def graph_aliases():
    """Use the graph's verified identifiers, refusing ambiguous ownership; never name matching."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'knowledge'))
    import knowledge as kg
    people, owners = {}, {}
    for path in Path(kg.people_dir()).glob('*.md'):
        try:
            person = kg.parse_person_file(path.read_text())
        except (OSError, ValueError):
            continue
        identifiers = {str(i) for i in person.identifiers if isinstance(i, str)}
        people[str(path)] = identifiers
        for identifier in identifiers:
            owners.setdefault(kg.normalize_identifier(identifier), set()).add(str(path))
    def lookup(identifier):
        paths = owners.get(kg.normalize_identifier(identifier), set())
        if len(paths) != 1:
            return ()
        path = next(iter(paths))
        return [i for i in people[path] if owners.get(kg.normalize_identifier(i)) == {path}]
    return lookup


def refresh(rows, local, service, normalize, *, allowed, now=None, aliases=None, clock=time.monotonic,
            known_messages=(), known_threads=None, classify_error=_rate_limited_only):
    from gmail_read import message_fields
    now = now or dt.datetime.now(dt.timezone.utc)
    receipt = {'status': 'skipped', 'targets': 0, 'checked': 0, 'requests': 0,
               'messages': 0, 'reused_threads': 0, 'complete': False}
    known = {str(m.get('id')): m for m in known_messages
             if isinstance(m, dict) and m.get('id') and m.get('_body_complete') is True}
    if not allowed('gmail'):
        receipt['status'] = 'disabled'
        return [], receipt
    candidates = _plan(rows, local, now=now, allowed=allowed,
                       graph_aliases=aliases if aliases is not None else graph_aliases())
    receipt['targets'] = len(candidates)
    candidates = candidates[:MAX_TARGETS]
    # Only the sources that chose a searched target gate this read, never every ledger row's.
    dependencies = {'gmail'}.union(*(sources for _, _, sources in candidates))
    receipt['sources'] = sorted(dependencies)
    consent_valid = lambda: all(allowed(source) for source in dependencies)
    if not candidates:
        receipt['status'] = 'not_applicable'
        return [], receipt
    receipt['status'] = 'partial'  # A bounded sample cannot establish absence of completion.
    deadline = clock() + READ_BUDGET_SECONDS
    messages, seen_messages, seen_threads, size = [], set(), set(), 0
    def capacity():
        return (consent_valid() and clock() < deadline and receipt['requests'] < MAX_REQUESTS
                and len(messages) < MAX_MESSAGES and size < MAX_CONTEXT_CHARS)
    def execute(call):
        receipt['requests'] += 1
        return call.execute()
    for kind, value, sources in candidates:
        if not capacity():
            break
        try:
            tids = [value]
            if kind == 'email':
                response = execute(service.users().messages().list(userId='me',
                    q=f'{{from:"{value}" to:"{value}" cc:"{value}"}} newer_than:30d -in:trash -in:spam',
                    maxResults=2))
                tids = list(dict.fromkeys(m.get('threadId') for m in response.get('messages', [])
                                         if m.get('threadId')))
            checked = False
            for tid in tids:
                if tid in seen_threads:
                    checked = True
                    continue
                if not capacity():
                    break
                seen_threads.add(tid)
                thread = (known_threads or {}).get(tid) or {}
                inventory = _evidence(thread.get('messages'))
                # Reuse only when the current metadata inventory proves every selected message
                # has a full native body already gathered. One observed row is not a whole thread.
                reuse = bool(inventory) and all(str(m.get('id')) in known for m in inventory)
                if reuse:
                    receipt['reused_threads'] += 1
                else:
                    thread = execute(service.users().threads().get(userId='me', id=tid, format='full'))
                checked = True
                for raw in _evidence(thread.get('messages')):
                    if not consent_valid():
                        break
                    if len(messages) >= MAX_MESSAGES or size >= MAX_CONTEXT_CHARS:
                        break
                    if raw.get('id') in seen_messages:
                        continue
                    if int(raw.get('internalDate') or 0) > int(now.timestamp() * 1000):
                        continue
                    try:
                        if reuse:
                            row = dict(known[str(raw['id'])])
                        else:
                            fields = message_fields(raw)
                            row = normalize(fields, fields)
                    except (RuntimeError, TypeError, ValueError):
                        continue
                    body = str(row.get('body') or '')
                    room = min(MAX_BODY_CHARS, MAX_CONTEXT_CHARS - size)
                    row['body'] = body[:room]
                    row['_loop_context'] = True
                    row['_loop_context_sources'] = sorted(sources)
                    row['_body_truncated'] = len(body) > room
                    row['_body_complete'] = not row['_body_truncated']
                    size += len(row['body'])
                    messages.append(row)
                    seen_messages.add(row.get('id'))
            receipt['checked'] += int(checked or not tids)
        except Exception as error:
            # A permission 403 is a failed read, not quota; only the lane's classifier can tell.
            receipt['error'] = 'rate_limited' if classify_error(error) == 'rate_limited' else 'read_failed'
            # Do not fan out after a provider/transport failure. Preserve earlier successful reads.
            break
    if not consent_valid():
        receipt.update(status='disabled', messages=0)
        return [], receipt
    receipt['messages'] = len(messages)
    return messages, receipt


def merge_context(existing, refreshed):
    """Merge exact message IDs without relabeling inbound messages as sent.

    An inbox row keeps its own consent: only the loop excerpt carries the steering sources, so
    revoking one drops the excerpt, never the ordinary row.
    """
    out = [dict(row) for row in existing]
    by_id = {str(row.get('id')): row for row in out if row.get('id')}
    for row in refreshed:
        mid = str(row.get('id') or '')
        if not mid:
            continue
        sources = set(row.get('_loop_context_sources') or ['gmail'])
        prior = by_id.get(mid)
        if prior is None:
            value = dict(row)
            out.append(value)
            by_id[mid] = value
            continue
        if sources == {'gmail'} and row.get('body') and len(row['body']) > len(prior.get('body') or ''):
            # Only a Gmail-only read may enrich the inbox body; another source's read stays gated.
            prior.update(body=row['body'], _body_truncated=row.get('_body_truncated', False))
        # A duplicate already in the inbox still needs its targeted-evidence presentation.
        # Keep only a bounded excerpt here; do not expand a 30k ordinary body into this lane.
        prior['_loop_context'] = True
        prior['_loop_context_body'] = str(row.get('body') or '')[:MAX_BODY_CHARS]
        prior['_loop_context_body_sources'] = sorted(set(prior.get('_loop_context_body_sources') or [])
                                                     | sources)
    return out
