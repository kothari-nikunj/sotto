"""Bounded source refresh for carried-over obligations, never a loop-state writer.

Recent inbox sampling is not a completion check. Read a few exact threads/known
counterparts for the existing brief model; no name searches, new model pass, or
persistent message cache. An incomplete read is context, never proof of absence.
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from itertools import zip_longest
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
    from textutil import _normalize_identifier
    cards = [[str(v) for v in [*(c.get('emails') or []), *(c.get('phones') or [])]]
             for c in local.get('contacts') or [] if isinstance(c, dict)]
    for identifier, sources in identifiers.items():
        key = _normalize_identifier(identifier)
        matches = [card for card in cards if key and key in {_normalize_identifier(v) for v in card}]
        if len(matches) == 1:
            for value in matches[0]:
                if sum(_normalize_identifier(value) in {_normalize_identifier(v) for v in card}
                       for card in cards) == 1:
                    add(value, sources | {'contacts'})
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
        # Older Gmail obligations used the native thread ID as their anchor but did not
        # persist source_thread_id.  The exact anchor is source evidence; a name is not.
        if not tid and _CHANNEL_SOURCES.get(channel, channel) == 'gmail':
            anchor = str(row.get('anchor_key') or '')
            if re.fullmatch(r'thread:[a-zA-Z0-9_-]+', anchor):
                tid = anchor.removeprefix('thread:')
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
    def lookup(identifier, canonical_id=None):
        paths = owners.get(kg.normalize_identifier(identifier), set())
        if len(paths) != 1:
            return ()
        path = next(iter(paths))
        if canonical_id and Path(path).stem != canonical_id:
            return ()
        return [i for i in people[path] if owners.get(kg.normalize_identifier(i)) == {path}]
    return lookup


def _imessage_plan(rows, *, now, allowed, aliases):
    """Existing source obligations choose exact, uniquely verified direct-message handles."""
    from ledger_provenance import source_loop_sources
    from textutil import _normalize_identifier
    from timeutil import _parse_ts
    day = _local_now(now).date().isoformat()
    slot = day + str(_local_now(now).hour // 12)
    found = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get('status', 'open') not in ('open', 'waiting', 'failed', 'blocked')
                or row.get('resolution_mode') == 'explicit' or row.get('group_id')
                or str(row.get('snoozed_until') or '')[:10] > day):
            continue
        try:
            origins = source_loop_sources(row)
        except (ValueError, TypeError, KeyError):
            continue
        if not origins or not all(allowed(source) for source in origins | {'imessage'}):
            continue
        created = str(row.get('created_at') or '')
        # Match the resolver: a date-only original has an unknown time, hence an end-of-day cutoff.
        cutoff = _parse_ts(created if len(created) > 10 else created + 'T23:59:59.999999')
        if cutoff is None:
            continue
        cutoff = (cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=_local_now(now).tzinfo)).timestamp()
        if cutoff >= now.timestamp():
            continue
        primary = str(row.get('contact_identifier') or '')
        try:
            identifiers = [primary, *aliases(primary, row.get('canonical_id'))] if primary else []
        except (ValueError, TypeError, OSError):
            identifiers = [primary] if primary else []
        handles = {}
        for value in identifiers:
            value = str(value).strip()
            key = _normalize_identifier(value)
            # The native SQL matches the raw address. A normalized WhatsApp JID or formatted
            # display phone is not an iMessage endpoint; prefer an observed E.164 alias.
            if key and (re.fullmatch(r'\+?[0-9]{7,15}', value) or _mail_address(value)):
                if key not in handles or (value.startswith('+') and not handles[key].startswith('+')):
                    handles[key] = value
        if not handles:
            continue
        # Prefer the verified phone for cross-channel refresh. Multiple phones rotate half-daily.
        options = sorted(handles, key=lambda key: ('@' in key, key))
        phones = [key for key in options if '@' not in key]
        options = phones or options
        offset = int(hashlib.sha256((slot + str(row.get('anchor_key'))).encode()).hexdigest()[:8], 16)
        key = options[offset % len(options)]
        due = str(row.get('deadline') or '')[:10]
        rank = (0 if due and due <= day else 1,
                hashlib.sha256((slot + str(row.get('anchor_key'))).encode()).hexdigest())
        prior = found.get(key)
        if prior is None:
            found[key] = (rank, handles[key], frozenset(origins | {'imessage'}), cutoff)
        else:
            # Sibling asks share one read and retain every source that selected it.
            found[key] = (min(rank, prior[0]), prior[1], prior[2] | origins, min(prior[3], cutoff))
    return [('imessage', value, sources, cutoff) for _, value, sources, cutoff in sorted(found.values())]


def refresh(rows, local, service, normalize, **kwargs):
    """Backwards-compatible Gmail-only refresh using the same bounded gather."""
    context, receipt = refresh_context(rows, local, service, normalize, **kwargs)
    return context['emails'], receipt


def refresh_context(rows, local, service, normalize, *, allowed, now=None, aliases=None, clock=time.monotonic,
                    known_messages=(), known_threads=None, classify_error=_rate_limited_only,
                    read_messages=None):
    from gmail_read import message_fields
    from textutil import _normalize_identifier
    from timeutil import _parse_ts
    from render_local import message_evidence_id
    now = now or dt.datetime.now(dt.timezone.utc)
    receipt = {'status': 'skipped', 'targets': 0, 'checked': 0, 'requests': 0,
               'messages': 0, 'reused_threads': 0, 'complete': False}
    known = {str(m.get('id')): m for m in known_messages
             if isinstance(m, dict) and m.get('id') and m.get('_body_complete') is True}
    if not allowed('gmail'):
        receipt['status'] = 'disabled'
        return {'emails': [], 'imessage': []}, receipt
    aliases = aliases if aliases is not None else graph_aliases()
    candidates = _plan(rows, local, now=now, allowed=allowed, graph_aliases=aliases)
    text_candidates = (_imessage_plan(rows, now=now, allowed=allowed, aliases=aliases)
                       if read_messages is not None and allowed('imessage') else [])
    # Interleave source lanes within the existing total target budget, rather than adding
    # another per-task allowance. Neither lane permanently occupies every slot.
    candidates = [candidate for pair in zip_longest(candidates, text_candidates)
                  for candidate in pair if candidate is not None]
    receipt['targets'] = len(candidates)
    candidates = candidates[:MAX_TARGETS]
    # Only the sources that chose a searched target gate this read, never every ledger row's.
    dependencies = {'gmail'}.union(*(candidate[2] for candidate in candidates))
    receipt['sources'] = sorted(dependencies)
    consent_valid = lambda: all(allowed(source) for source in dependencies)
    if not candidates:
        receipt['status'] = 'not_applicable'
        return {'emails': [], 'imessage': []}, receipt
    receipt['status'] = 'partial'  # A bounded sample cannot establish absence of completion.
    deadline = clock() + READ_BUDGET_SECONDS
    messages, seen_messages, seen_threads, size = [], set(), set(), 0
    texts, seen_texts = [], set()
    def capacity():
        return (consent_valid() and clock() < deadline and receipt['requests'] < MAX_REQUESTS
                and len(messages) < MAX_MESSAGES and size < MAX_CONTEXT_CHARS)
    def execute(call):
        receipt['requests'] += 1
        return call.execute()
    def text_error(code):
        receipt['imessage_error'] = code
        receipt.setdefault('error', code)  # Gmail quota failure must retain lane-wide priority.

    def read_text(value):
        remaining = deadline - clock()
        if remaining <= 0 or not consent_valid():
            return None, clock()
        payload = read_messages(value, timeout=remaining)
        return payload, clock()

    with ExitStack() as stack:
        message_reads = []
        if text_candidates:
            pool = stack.enter_context(ThreadPoolExecutor(max_workers=MAX_TARGETS))
            for candidate in candidates:
                if candidate[0] == 'imessage' and capacity():
                    _, value, sources, cutoff = candidate
                    receipt['requests'] += 1
                    message_reads.append((value, sources, cutoff, pool.submit(read_text, value)))
        for candidate in candidates:
            kind, value, sources = candidate[:3]
            if kind == 'imessage':
                continue
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
                    thread_messages, thread_ids, thread_size = [], set(), 0
                    for raw in _evidence(thread.get('messages')):
                        if not consent_valid():
                            break
                        if raw.get('id') in seen_messages or raw.get('id') in thread_ids:
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
                        room = MAX_BODY_CHARS
                        row['body'] = body[:room]
                        row['_loop_context'] = True
                        row['_loop_context_sources'] = sorted(sources)
                        row['_body_truncated'] = len(body) > room
                        row['_body_complete'] = not row['_body_truncated']
                        thread_size += len(row['body'])
                        thread_messages.append(row)
                        thread_ids.add(row.get('id'))
                    # Admit the bounded thread selection atomically: a full output allowance
                    # must not leave only an older prefix while hiding its newest reply.
                    if (len(messages) + len(thread_messages) > MAX_MESSAGES
                            or size + thread_size > MAX_CONTEXT_CHARS):
                        receipt.setdefault('omitted_messages', {'gmail': 0, 'imessage': 0})['gmail'] += len(thread_messages)
                        continue
                    messages.extend(thread_messages)
                    seen_messages.update(thread_ids)
                    size += thread_size
                receipt['checked'] += int(checked or not tids)
            except Exception as error:
                # A permission 403 is a failed read, not quota; only the lane's classifier can tell.
                receipt['error'] = 'rate_limited' if classify_error(error) == 'rate_limited' else 'read_failed'
                # Do not fan out after a provider/transport failure. Preserve earlier successful reads.
                break
        # Exact-handle calls run together while Gmail gathers, under the same deadline and caps.
        # Results are consumed deterministically; a slow/failing read adds no authority or cache.
        for value, sources, cutoff, future in message_reads:
            try:
                payload, received_at = future.result()
                if received_at > deadline:
                    text_error('read_timeout')
                    continue
                if (not isinstance(payload, dict) or payload.get('error') or payload.get('status') in
                        ('disabled', 'unavailable', 'degraded') or not isinstance(payload.get('messages'), list)
                        or _normalize_identifier(str(payload.get('identifier') or '')) != _normalize_identifier(value)):
                    text_error('read_failed')
                    continue
                receipt['checked'] += 1
                for raw in payload['messages']:
                    if not consent_valid():
                        break
                    if (not isinstance(raw, dict) or raw.get('is_group_chat') or raw.get('group_id')
                            or type(raw.get('is_from_me')) is not bool
                            or _normalize_identifier(str(raw.get('handle') or '')) != _normalize_identifier(value)):
                        continue
                    observed = _parse_ts(str(raw.get('timestamp') or ''))
                    if observed is None:
                        continue
                    instant = (observed if observed.tzinfo else observed.replace(tzinfo=_local_now(now).tzinfo)).timestamp()
                    if not cutoff < instant <= now.timestamp():
                        continue
                    mid = message_evidence_id(raw, 'imessage')
                    if not mid or mid in seen_texts:
                        continue
                    row = dict(raw)
                    body = str(row.get('text') or '')
                    if not body or len(body) > MAX_BODY_CHARS:
                        # Keep exact native evidence IDs (including content-derived IDs), not a
                        # truncated new message that could hide a later qualifier or change its ID.
                        continue
                    # Both selected source lanes may contribute within the existing caps.
                    # Only evict trailing Gmail evidence when a valid text needs the room,
                    # never spend the text reads then silently give all output slots to Gmail.
                    while (len(messages) + len(texts) >= MAX_MESSAGES or size + len(body) > MAX_CONTEXT_CHARS) and len(messages) > len(texts):
                        # Each thread is chronological. Evict it whole rather than dropping its
                        # newest reply while retaining an older ask that could look unanswered.
                        thread = messages[-1].get('threadId')
                        while messages and messages[-1].get('threadId') == thread:
                            size -= len(messages.pop().get('body') or '')
                            receipt.setdefault('omitted_messages', {'gmail': 0, 'imessage': 0})['gmail'] += 1
                    if len(messages) + len(texts) >= MAX_MESSAGES or size + len(body) > MAX_CONTEXT_CHARS:
                        receipt.setdefault('omitted_messages', {'gmail': 0, 'imessage': 0})['imessage'] += 1
                        continue
                    row['_loop_context'] = True
                    row['_loop_context_sources'] = sorted(sources)
                    size += len(body)
                    texts.append(row)
                    seen_texts.add(mid)
            except Exception:
                text_error('read_failed')
    if not consent_valid():
        receipt.update(status='disabled', messages=0)
        return {'emails': [], 'imessage': []}, receipt
    receipt['messages'] = len(messages) + len(texts)
    if read_messages is not None:
        receipt['imessage_scope'] = ('At most the newest 24 native rows per selected exact handle within '
            'the native 90-day limit; only rows after an original ask and no later than this pass. '
            'Oversize texts are omitted intact. This is not a complete history search.')
        receipt['message_counts'] = {'gmail': len(messages), 'imessage': len(texts)}
    return {'emails': messages, 'imessage': texts}, receipt


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
