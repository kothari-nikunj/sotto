#!/usr/bin/env python3
"""
preferences.py — the EXPLICIT side of Sotto's preference memory.

The user states mutes, tone, VIPs and cadence here. Chat and dashboard use this one writer.
The `explicit` block retains its existing format; legacy inferred fields elsewhere in the file
are preserved but ignored. Draft usage does not change preferences.

`compose_brief` reads these to suppress muted senders / people / sections and to honor tone notes.
The `sotto-feedback` skill writes them via this CLI. Pure stdlib; never raises on read.

Cadence lives here too: `nudge_snooze_until` (a single ISO timestamp, not a list) is
the user's "be quieter" lever — while it is in the future the event funnel (triage_event.py Tier 0)
and the proactive watcher (proactive_scan.py) hold every nudge. It is a *scalar* in the same
explicit block, written through the same path, without changing other preferences.
Legacy CLI stamps use local wall-clock time; admitted owner duration commands store an offset
and millisecond precision so their expiry represents an exact elapsed-time instant.

VIP lives here too: `vip_people` is the user's STATED list of people whose missed calls clear the
quiet-hours bar (triage_event._is_vip checks it before the two heuristics — a top-of-queue
relationship-pulse priority, or a "family" mention in their graph file). It is a plain explicit
list, and saying "Sarah is a VIP"
in chat and toggling VIP in the dashboard are the same write.

CLI:
  preferences.py show
  preferences.py mute-sender <email | @domain | phone>  # newsletters / noisy senders / a number
      # A phone may be written any way ("+12025550171", "+1 202 555 0171", "(202) 555-0171"): it is
      # stored as given and normalized when it is MATCHED, so mutes already in the file keep
      # working without a migration, and the number also matches inside a WhatsApp identifier.
  preferences.py mute-person "<display name>"        # stop flagging them in the brief
  preferences.py mute-section <section>               # e.g. birthdays, screen_time
  preferences.py tone "<short note>"                 # e.g. "keep it terse"
  preferences.py vip "<display name>"                # their missed calls reach you in quiet hours
  preferences.py unmute-sender <v> | unmute-person "<v>" | unmute-section <v> | clear-tone
  preferences.py unvip "<display name>"
  preferences.py snooze-nudges tomorrow | "+2h" | 15:00 | 3pm | 2026-08-08T06:00
  preferences.py remove-tone "keep it terse"          # remove just this note
  preferences.py unsnooze-nudges                     # "back to normal"
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from email.utils import parseaddr
from datetime import datetime, timedelta, timezone

# _shared/lib holds the shared primitives; this module is invoked as a bare script from chat, the
# dashboard, so the path is set up here rather than assumed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jsonstore  # noqa: E402 — THE read-modify-write lock for the volume
# THE tree's identifier normaliser (→ textutil._normalize_identifier, full digits), the same one
# conversation_key threads by. A mute must not have a second opinion about what a number is.
from personal_context import normalized_identifier as _normalized_identifier  # noqa: E402
from tzchain import data_root as _root  # noqa: E402

LISTS = ("mute_senders", "mute_people", "mute_sections", "tone_notes", "vip_people")
# Scalar (single-value) explicit preferences. Same block, same writer, same persistence path.
SCALARS = ("nudge_snooze_until", "nudge_budget")
SNOOZE_FMT = "%Y-%m-%dT%H:%M"    # legacy CLI local wall clock; admitted commands store offset instants
# A snooze lifts when quiet hours do. "Quieter today" used to resolve to a hardcoded 6am while
# SOTTO_QUIET_END defaults to 7 — so the stamp said 6 and nudges actually resumed at 7. Read the
# SAME env var the funnel reads, with the SAME default: the coupling is the variable, not an import
# (event-triage/ isn't importable from here, and triage_event.py imports THIS module).
SNOOZE_MORNING_HOUR_DEFAULT = 7


def _path() -> str:
    return os.path.join(_root(), "preferences.json")


def _load_all() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def empty_explicit() -> dict:
    out = {k: [] for k in LISTS}
    out.update({k: "" for k in SCALARS})
    return out


def load_explicit() -> dict:
    """The user's explicit preferences, always shaped (missing lists default to empty, missing
    scalars to "")."""
    ex = (_load_all().get("explicit") or {})
    out = empty_explicit()
    for k in LISTS:
        v = ex.get(k)
        if isinstance(v, list):
            out[k] = [str(x) for x in v if str(x).strip()]
    for k in SCALARS:
        v = ex.get(k)
        if isinstance(v, str):
            out[k] = v.strip()
    return out


def _norm(kind: str, value: str) -> str:
    value = (value or "").strip()
    if kind == "mute_senders":
        return value.lower()          # emails/domains are case-insensitive
    return value


def _mutate(apply) -> dict:
    """THE way the explicit block changes: read, mutate and write inside ONE lock.

    The mutation has to happen in here. An earlier fix moved only the FILE read inside the lock and
    left callers computing their new block from `load_explicit()` outside it — which made things
    worse, not better: serialising the writes turned a 7% racy overwrite into a 23% deterministic
    one, because the losing writer faithfully wrote back a block it had read before the winner's
    change existed. Measured both times; that is the only reason it was caught.

    `apply` receives the current explicit block and mutates it in place."""
    with jsonstore.transaction(_path(), default={}, mode=0o600, indent=2) as data:
        if not isinstance(data, dict):
            data.clear()
        ex = data.get("explicit")
        if not isinstance(ex, dict):
            ex = empty_explicit()
        for k in LISTS:
            if not isinstance(ex.get(k), list):
                ex[k] = []
        for k in SCALARS:
            if not isinstance(ex.get(k), str):
                ex[k] = ""
        apply(ex)
        ex["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["explicit"] = ex
        return dict(ex)


def _save(explicit: dict) -> None:
    """Replace the whole explicit block. Kept for callers that legitimately own the entire block
    (clear-tone); everything incremental goes through _mutate so it can't clobber a concurrent
    change it never saw."""
    _mutate(lambda ex: (ex.clear(), ex.update(explicit)))


def add(kind: str, value: str) -> dict:
    if kind not in LISTS:
        raise ValueError(f"unknown preference list: {kind}")
    value = _norm(kind, value)

    def _apply(ex):
        if value and value not in ex[kind]:
            ex[kind].append(value)
    return _mutate(_apply)


def remove(kind: str, value: str) -> dict:
    if kind not in LISTS:
        raise ValueError(f"unknown preference list: {kind}")
    value = _norm(kind, value)
    return _mutate(lambda ex: ex.__setitem__(kind, [x for x in ex[kind] if x != value]))


def set_scalar(kind: str, value: str) -> dict:
    """Write one scalar explicit preference (one of `SCALARS`). "" clears it."""
    if kind not in SCALARS:
        raise ValueError(f"unknown preference scalar: {kind}")
    return _mutate(lambda ex: ex.__setitem__(kind, (value or "").strip()))


class OwnerQuietConflictError(ValueError):
    """An admitted event already has a different durable preference receipt."""


# Ordinary scalar owner-command receipts answer recent event retries. One-shot effects also use
# the preference map, but their full receipts fence proposal reuse across later owner events and
# must remain inspectable for the life of this preferences file.
RECEIPT_MAPS = ('owner_preference_receipts', 'owner_quiet_receipts')
RECEIPTS_KEPT = 100


def _settled(record) -> bool:
    """A receipt that already holds its final outcome; anything in flight or unknown is kept."""
    if type(record) is not dict:
        return False
    if 'status' in record:
        return record['status'] in ('committed', 'rejected')
    return any(key in record for key in ('outcome', 'receipt', 'snooze_until'))


def _bound_receipts(path: str, data: dict) -> None:
    """Bound only ordinary scalar receipts this write did not touch, in place.

    Insertion order is first-admission order. A record that differs from the file on disk is being
    written now and stays this write. Other preference receipts may be one-shot effect fences,
    including legacy and status-less cleanup rows, so unknown shapes stay intact."""
    maps = []
    for key in RECEIPT_MAPS:
        receipts = data.get(key)
        if type(receipts) is not dict:
            continue
        if key == 'owner_preference_receipts':
            bounded = [event for event, record in receipts.items()
                       if type(record) is dict and set(record) == {'proof', 'outcome'}]
        else:
            bounded = [event for event, record in receipts.items() if _settled(record)]
        if len(bounded) > RECEIPTS_KEPT:
            maps.append((key, receipts, bounded))
    if not maps:
        return
    try:
        stored = jsonstore.read(path, default={}, strict=True)
    except jsonstore.Unreadable:
        return   # an unreadable prior file proves nothing about what is old; keep everything
    for key, receipts, bounded in maps:
        before = stored.get(key) if type(stored) is dict and type(stored.get(key)) is dict else {}
        excess = len(bounded) - RECEIPTS_KEPT
        for event in bounded:
            if excess <= 0:
                break
            if event not in before or before[event] != receipts[event]:
                continue
            del receipts[event]
            excess -= 1


def _durable_preferences_replace(path: str, data: dict) -> None:
    """Publish the preference and its owner receipt together before acknowledging it."""
    if type(data) is dict and os.path.realpath(path) == os.path.realpath(_path()):
        _bound_receipts(path, data)
    parent = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix='.preferences-', dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_existing_preferences(path: str) -> None:
    """Resolve an uncertain rename before returning an existing receipt."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def admitted_owner_quiet(proof: dict, snooze_until: str, *, inspect: bool = False):
    """Apply or inspect one exact owner quiet command under the canonical preferences lock.

    The receipt sits beside `explicit` in the same JSON file. A retry returns its
    original effect, even if a later preference edit changed the current scalar.
    """
    required = {'action_id', 'event_id', 'payload_hash', 'text_hash', 'identity_digest',
                'command', 'admitted_at_ms', 'provider_thread_id', 'zone'}
    if (type(proof) is not dict or set(proof) != required
            or type(snooze_until) is not str):
        raise ValueError('owner quiet proof is invalid')
    path = _path()
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        if type(data) is not dict:
            raise ValueError('preferences file is invalid')
        receipts = data.get('owner_quiet_receipts', {})
        if type(receipts) is not dict:
            raise ValueError('owner quiet receipts are invalid')
        old = receipts.get(proof['action_id'])
        if proof['action_id'] in receipts:
            if (type(old) is not dict or set(old) != required | {'snooze_until'}
                    or any(old[key] != proof[key] for key in required)
                    or old['snooze_until'] != snooze_until):
                raise OwnerQuietConflictError('owner quiet event conflicts with its receipt')
            _sync_existing_preferences(path)
            return dict(old)
        if inspect:
            return None
        explicit = data.get('explicit', {})
        if type(explicit) is not dict:
            raise ValueError('explicit preferences are invalid')
        explicit['nudge_snooze_until'] = snooze_until
        explicit['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        receipt = {**proof, 'snooze_until': snooze_until}
        receipts[proof['action_id']] = receipt
        data['explicit'] = explicit
        data['owner_quiet_receipts'] = receipts
        _durable_preferences_replace(path, data)
        return dict(receipt)


class OwnerPreferenceRefusalError(ValueError):
    """An explicit owner preference request cannot commit this delta."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _owner_preference_value(value):
    return (type(value) is str and bool(value) and value.strip() == value and len(value) <= 500
            and len(value.encode('utf-16-le', errors='surrogatepass')) // 2 <= 1000
            and not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                        for char in value))


def normalize_owner_quiet_text(text):
    """Only whole-request politeness and a single terminal punctuation mark are optional."""
    if type(text) is not str:
        return None
    text = text.strip()
    question = text.endswith('?')
    if text.endswith(('.', '!', '?')):
        text = text[:-1].rstrip()
    polite = re.sub(r'^(?:please|pls)\s+', '', text, count=1, flags=re.IGNORECASE | re.ASCII)
    polite = re.sub(r',?\s+(?:please|pls)$', '', polite, count=1, flags=re.IGNORECASE | re.ASCII).strip()
    # Only a polite request may end in a question mark; a bare question is not a change request.
    return text + '?' if question and polite == text else polite


def owner_nudge_quiet_args(text):
    """Closed whole-owner phrases; all timestamp math remains in resolve_snooze_spec."""
    if type(text) is not str:
        return None
    text = normalize_owner_quiet_text(text)
    flags = re.IGNORECASE | re.ASCII
    if re.fullmatch(r"quieter today|noisy today|too many notifications|you're being noisy", text, flags):
        return {'operation':'snooze-nudges','spec':'tomorrow'}
    if re.fullmatch(r'resume nudges|back to normal|back to normal nudges|stop quieting nudges', text, flags):
        return {'operation':'unsnooze-nudges'}
    clock = r'(?:today|tomorrow|[0-9]{1,2}(?::[0-9]{2})?(?:am|pm)?|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?)'
    duration = r'(?:[+]?[0-9]+(?:\.[0-9]+)?[hm]|[0-9]+(?:\.[0-9]+)? (?:hours?|minutes?))'
    match = re.fullmatch(r'(?:be )?quiet until ('+clock+r')', text, flags)
    if match is None:
        match = re.fullmatch(r'(?:be )?quiet for ('+duration+r')', text, flags)
    if match is not None and len(match.group(1)) <= 64:
        return {'operation':'snooze-nudges','spec':match.group(1).lower()}
    return None


def owner_preference_args(args):
    """Validate the bounded delta before entering the canonical preference lock."""
    if type(args) is not dict:
        raise ValueError('owner preference arguments invalid')
    op = args.get('operation')
    if op in ('add', 'remove'):
        valid = (set(args) == {'operation', 'field', 'value'} and args['field'] in LISTS
                 and _owner_preference_value(args['value'])
                 and _owner_preference_value(_norm(args['field'], args['value'])))
    elif op == 'snooze-nudges':
        valid = (set(args) == {'operation','spec'} and type(args['spec']) is str
                 and 0 < len(args['spec']) <= 64 and args['spec'] == args['spec'].strip()
                 and not any(ord(c) < 32 or ord(c) >= 127 for c in args['spec']))
    elif op == 'unsnooze-nudges':
        valid = set(args) == {'operation'}
    elif op == 'clear-tone':
        valid = set(args) == {'operation'}
    elif op == 'nudge-budget':
        valid = set(args) == {'operation', 'direction'} and args['direction'] in ('fewer', 'no', 'more')
    else:
        valid = False
    if not valid:
        raise ValueError('owner preference arguments invalid')


def read_owner_preferences():
    """Bounded explicit choices only; no learned preferences or private receipts."""
    with jsonstore.lock(_path()):
        data = jsonstore.read(_path(), default={}, strict=True)
        if type(data) is not dict or type(data.get('explicit', {})) is not dict:
            raise ValueError('preferences file invalid')
        ex = data.get('explicit', {})
        choices, truncated = {field: [] for field in LISTS}, []
        ceiling = configured_nudge_budget()
        budget = {'effective': effective_nudge_budget(ex, ceiling), 'ceiling': ceiling}
        # Match receiver JSON encoding and reserve its envelope, including maximal field names.
        remaining = 512 * 1024 - 512 - len(json.dumps({
            'preferences': choices, 'nudge_budget': budget, 'truncated_fields': list(LISTS)}).encode('utf-8'))
        for field in LISTS:
            raw = ex.get(field, [])
            if type(raw) is not list:
                choices[field] = []
                truncated.append(field)
                continue
            valid = [value for value in raw if _owner_preference_value(value)]
            choices[field] = []
            for value in valid[:100]:
                size = len(json.dumps(value).encode('utf-8')) + 2
                if size > remaining:
                    break
                choices[field].append(value)
                remaining -= size
            if len(choices[field]) != len(raw):
                truncated.append(field)
        return {'preferences': choices, 'nudge_budget': budget, 'truncated_fields': truncated,
                'nudge_snooze_until': (ex.get('nudge_snooze_until') if type(ex.get('nudge_snooze_until')) is str
                    and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?', ex['nudge_snooze_until'])
                    and parse_snooze(ex['nudge_snooze_until']) is not None else '')}


def _owner_preference_outcome_valid(outcome, args):
    if type(outcome) is not dict or outcome.get('operation') != args['operation']:
        return False
    op = args['operation']
    if op in ('add', 'remove'):
        return (set(outcome) == {'operation', 'field', 'value', 'present'}
                and outcome['field'] == args['field'] and outcome['value'] == _norm(args['field'], args['value'])
                and type(outcome['present']) is bool and outcome['present'] == (op == 'add'))
    if op in ('snooze-nudges','unsnooze-nudges'):
        return (set(outcome) == ({'operation','spec','snooze_until'} if op == 'snooze-nudges' else {'operation','snooze_until'})
                and type(outcome.get('snooze_until')) is str
                and (outcome['snooze_until'] == '' if op == 'unsnooze-nudges' else
                     outcome.get('spec') == args['spec'] and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}', outcome['snooze_until']) is not None
                     and parse_snooze(outcome['snooze_until']) is not None))
    if op == 'clear-tone':
        return set(outcome) == {'operation', 'count'} and type(outcome['count']) is int and outcome['count'] == 0
    return (set(outcome) == {'operation', 'direction', 'effective', 'ceiling'}
            and outcome['direction'] == args['direction'] and type(outcome['effective']) is int
            and type(outcome['ceiling']) is int and 0 <= outcome['effective'] <= outcome['ceiling']
            and (args['direction'] != 'no' or outcome['effective'] == 0)
            and (args['direction'] != 'more' or outcome['effective'] == outcome['ceiling']))


def admitted_owner_preference(proof: dict, *, inspect=False, now_local=None, ambiguous_resume=False):
    """Atomically retain a delta and its original outcome in preferences.json."""
    required = {'domain', 'event_id', 'action_id', 'payload_hash', 'text_hash', 'identity_digest',
                'args_hash', 'args', 'admitted_at_ms', 'provider_thread_id'}
    if type(proof) is not dict or set(proof) != required or proof['domain'] != 'sotto.owner-preference-tool.v1':
        raise ValueError('owner preference proof invalid')
    args = proof['args']
    owner_preference_args(args)
    path = _path()
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        if type(data) is not dict:
            raise ValueError('preferences file invalid')
        receipts = data.get('owner_preference_receipts', {})
        if type(receipts) is not dict:
            raise ValueError('owner preference receipts invalid')
        event = proof['event_id']
        if event in receipts:
            old = receipts[event]
            if (type(old) is not dict or set(old) != {'proof', 'outcome'} or old['proof'] != proof):
                raise OwnerPreferenceRefusalError('event_conflict')
            if not _owner_preference_outcome_valid(old['outcome'], args):
                raise ValueError('owner preference receipt invalid')
            _sync_existing_preferences(path)
            return json.loads(json.dumps(old['outcome']))
        if inspect:
            return None
        ex = data.get('explicit', {})
        if type(ex) is not dict:
            raise ValueError('explicit preferences invalid')
        op = args['operation']
        if op in ('add', 'remove'):
            field = args['field']
            values = ex.get(field, [])
            if type(values) is not list:
                raise ValueError('explicit preference list invalid')
            if op == 'remove' and args['value'] not in values:
                raise OwnerPreferenceRefusalError('target_unavailable')
            value = _norm(field, args['value'])
            ex[field] = ([item for item in values if item not in (args['value'], value)] if op == 'remove'
                         else values + ([] if value in values else [value]))
            outcome = {'operation': op, 'field': field, 'value': value, 'present': op == 'add'}
        elif op in ('snooze-nudges','unsnooze-nudges'):
            if ambiguous_resume and (not isinstance(now_local, datetime)
                    or not snooze_active(now_local, ex)
                    or effective_nudge_budget(ex) != configured_nudge_budget()):
                raise OwnerPreferenceRefusalError('target_unavailable')
            try:
                if op == 'snooze-nudges' and not isinstance(now_local, datetime):
                    raise ValueError('quiet admission clock unavailable')
                until = resolve_snooze_spec(args['spec'], now_local) if op == 'snooze-nudges' else ''
            except (ValueError, OverflowError):
                raise OwnerPreferenceRefusalError('target_unavailable') from None
            ex['nudge_snooze_until'] = until
            outcome = {'operation':op, **({'spec':args['spec']} if op == 'snooze-nudges' else {}), 'snooze_until':until}
        elif op == 'clear-tone':
            ex['tone_notes'] = []
            outcome = {'operation': op, 'count': 0}
        else:
            ceiling = configured_nudge_budget()
            current = effective_nudge_budget(ex, ceiling)
            direction = args['direction']
            effective = current // 2 if direction == 'fewer' else 0 if direction == 'no' else ceiling
            ex['nudge_budget'] = str(effective)
            outcome = {'operation': op, 'direction': direction, 'effective': effective, 'ceiling': ceiling}
        ex['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        receipts[event] = {'proof': proof, 'outcome': outcome}
        data['explicit'], data['owner_preference_receipts'] = ex, receipts
        _durable_preferences_replace(path, data)
        return dict(outcome)



def admitted_procedure_offer(proof, offers, master, select, *, inspect=False):
    """One confirmed canonical append; an uncertain separate-file effect is never repeated."""
    path = _path()
    def original(data):
        receipts = data.get('owner_preference_receipts', {})
        if type(receipts) is not dict:
            raise ValueError('owner preference receipts invalid')
        saved = receipts.get(proof['event_id'])
        if saved is not None and (type(saved) is not dict or saved.get('proof') != proof):
            raise OwnerPreferenceRefusalError('event_conflict')
        if saved is not None:
            status = saved.get('status')
            extra = {'outcome'} if status == 'committed' else {'code'} if status == 'rejected' else set()
            if (status not in ('contacting', 'committed', 'rejected')
                    or set(saved) != {'proof', 'status', 'offer'} | extra
                    or type(saved['offer']) is not dict or saved['offer'].get('kind') != 'procedure'
                    or type(saved['offer'].get('offer_id')) is not str
                    or type(saved['offer'].get('detail')) is not str):
                raise ValueError('procedure receipt invalid')
            if status == 'committed' and saved['outcome'] != {
                    'operation': 'accept-procedure-offer', 'offer_id': saved['offer']['offer_id'],
                    'section': 'Procedures', 'rule': saved['offer']['detail'], 'status': 'saved'}:
                raise ValueError('procedure receipt outcome invalid')
            if status == 'rejected' and saved['code'] != 'master_capacity':
                raise ValueError('procedure receipt refusal invalid')
        return receipts, saved
    def reconcile(saved):
        if saved and saved.get('status') == 'committed':
            try:
                offers.clear_offer(expected=saved['offer'])
            except Exception:
                pass  # original durable receipt remains primary; inspect retries exact cleanup
        return saved
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        _, saved = original(data)
        if saved is not None:
            _sync_existing_preferences(path)
    if saved is not None:
        return reconcile(saved)
    if inspect:
        return None
    # Serialize competing decline/accept before consuming the original accepted question.
    with jsonstore.lock(offers._path()):
        with jsonstore.lock(path):
            data = jsonstore.read(path, default={}, strict=True)
            receipts, saved = original(data)
            if saved is None:
                offer = select()
                if any(type(value) is dict and value.get('offer', {}).get('offer_id') == offer['offer_id']
                       and value.get('status') in ('contacting', 'committed') for value in receipts.values()):
                    raise OwnerPreferenceRefusalError('target_unavailable')
                saved = {'proof': proof, 'status': 'contacting', 'offer': offer}
                receipts[proof['event_id']] = saved
                data['owner_preference_receipts'] = receipts
                _durable_preferences_replace(path, data)
                try:
                    master.append_section('Procedures', offer['detail'])
                except master.OverCap:
                    saved = {**saved, 'status': 'rejected', 'code': 'master_capacity'}
                except Exception:
                    return saved  # contact marker may have accompanied a successful markdown write
                else:
                    saved = {**saved, 'status': 'committed', 'outcome': {
                        'operation': 'accept-procedure-offer', 'offer_id': offer['offer_id'],
                        'section': 'Procedures', 'rule': offer['detail'], 'status': 'saved'}}
                receipts[proof['event_id']] = saved
                try:
                    _durable_preferences_replace(path, data)
                except Exception:
                    return {'proof': proof, 'status': 'contacting', 'offer': offer}
    return reconcile(saved)


def procedure_rule_review(rule, master):
    """Closed direct-owner rule; offered-rule and ordinary preference contracts stay unchanged."""
    import hashlib
    if (type(rule) is not str or not rule or rule.strip() != rule or len(rule) > 4000
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF
                   or c in '\u2028\u2029' for c in rule) or master._HEADING.search(rule)):
        raise ValueError('standing rule invalid')
    review = {'operation': 'append-procedure', 'section': 'Procedures', 'rule': rule}
    review['review_hash'] = hashlib.sha256(json.dumps(review, sort_keys=True, ensure_ascii=False,
                                                   separators=(',', ':')).encode()).hexdigest()
    text = 'Add to your standing rules (Procedures):\n\n' + rule + '\n\nSave this rule?'
    if len(text.encode('utf-16-le')) // 2 > 4000:
        raise ValueError('standing rule review too long')
    return review


def owner_priority_cta(name):
    """Fixed original weekly CTA; never an arbitrary priority addition."""
    if (not _owner_preference_value(name) or any(127 <= ord(c) <= 159 or c in '\u2028\u2029' for c in name)):
        raise ValueError('priority name invalid')
    return {'operation': 'prioritize', 'section': 'Priorities', 'name': name,
            'text': 'Staying in touch with ' + name}


def priority_cta_matches(text, name):
    import re
    matched = re.fullmatch(r'prioritize staying in touch with (.+)', text.strip(), flags=re.IGNORECASE | re.ASCII)
    return matched is not None and matched.group(1) == name


def owner_context_review(section, text, master):
    """Explicit owner context is appended only to these two original fixed sections."""
    import hashlib
    if section not in ('About', 'People'):
        raise ValueError('owner context section invalid')
    if (type(text) is not str or not text or text.strip() != text or len(text) > 4000
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF
                   or c in "\u2028\u2029" for c in text) or master._HEADING.search(text)):
        raise ValueError("owner context line invalid")
    review = {'operation': 'append-context', 'section': section, 'text': text}
    review['review_hash'] = hashlib.sha256(json.dumps(review, sort_keys=True, ensure_ascii=False,
                                                     separators=(',', ':')).encode()).hexdigest()
    rendered = f'Add to your standing context ({section}):\n\n{text}\n\nSave this context?'
    if len(rendered.encode('utf-16-le')) // 2 > 4000:
        raise ValueError('owner context review too long')
    return review


def priorities_review(text, master, *, previous_text=None):
    """Only an explicit complete set; the original writer's three-line constraint applies."""
    import hashlib
    if (type(text) is not str or not text or text.strip() != text or len(text) > 4000
            or not 1 <= len(text.split('\n')) <= master.PRIORITY_MAX
            or any(not line.strip() for line in text.split('\n'))
            or any((ord(c) < 32 and c != '\n') or 127 <= ord(c) <= 159
                   or 0xD800 <= ord(c) <= 0xDFFF or c in '\u2028\u2029' for c in text)
            or master._HEADING.search(text)):
        raise ValueError('priorities invalid')
    if previous_text is None:
        with jsonstore.lock(master.path()):
            previous_text = _read_priorities(master)[1]
    if type(previous_text) is not str or len(previous_text) > master.MASTER_CHAR_CAP:
        raise ValueError('priorities review invalid')
    review = {'operation': 'replace-priorities', 'section': 'Priorities',
              'text': text, 'previous_text': previous_text}
    review['review_hash'] = hashlib.sha256(json.dumps(review, sort_keys=True, ensure_ascii=False,
                                                     separators=(',', ':')).encode()).hexdigest()
    rendered = ('Replace your current priorities (complete set):\n\nCurrent:\n' +
                (previous_text or 'None') + '\n\nNew:\n' + text + '\n\nReplace these priorities?')
    if len(rendered.encode('utf-16-le')) // 2 > 4000:
        raise ValueError('priorities review too long')
    return review


def _read_priorities(master):
    try:
        with open(master.path(), encoding='utf-8') as stream:
            text = stream.read()
    except FileNotFoundError:
        text = ''
    except (OSError, UnicodeError):
        raise OwnerPreferenceRefusalError('target_unavailable') from None
    sections = [body.strip('\n') for name, body in master._split(text)[1] if name.lower() == 'priorities']
    if len(sections) > 1:
        raise OwnerPreferenceRefusalError('target_unavailable')
    return text, sections[0] if sections else ''


def admitted_owner_procedure(proof, master, review, proposal, *, inspect=False):
    """Append once through the original writer; interrupted separate-file work stays held."""
    path = _path()
    priorities = review['operation'] == 'replace-priorities'
    context = review['operation'] == 'append-context'
    cta = review['operation'] == 'prioritize'
    tag = 'owner_priority_cta' if cta else 'owner_priorities' if priorities else 'owner_context' if context else 'owner_procedure'
    section = review['section']
    addition = review['text'] if context or cta else review.get('rule')
    outcome = {**review, 'status': 'saved'}
    def original(data):
        records = data.get('owner_preference_receipts', {})
        if type(records) is not dict:
            raise ValueError('owner preference receipts invalid')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if type(saved) is not dict or saved.get('proof') != proof or saved.get('tag') != tag:
                raise OwnerPreferenceRefusalError('event_conflict')
            base = {'tag', 'proof', 'review', 'proposal', 'status'}
            status = saved.get('status')
            extra = {'outcome'} if status == 'committed' else ({'code'} if cta and saved.get('code') == 'priorities_full' else {'code', 'capacity'}) if status == 'rejected' else set()
            if (status not in ('contacting', 'committed', 'rejected') or set(saved) != base | extra
                    or saved['review'] != review or saved['proposal'] != proposal
                    or status == 'committed' and saved['outcome'] != outcome):
                raise ValueError('standing rule receipt invalid')
            if status == 'rejected' and not (cta and saved['code'] == 'priorities_full'):
                capacity = saved['capacity']
                largest = capacity.get('largest_section') if type(capacity) is dict else None
                if (saved['code'] != 'master_capacity' or type(capacity) is not dict
                        or set(capacity) != {'cap', 'largest_section'} or capacity['cap'] != 8000
                        or largest is not None and (type(largest) is not dict
                            or set(largest) != {'name', 'chars', 'name_truncated'}
                            or type(largest['name']) is not str or len(largest['name']) > 200
                            or type(largest['chars']) is not int or largest['chars'] < 0
                            or type(largest['name_truncated']) is not bool)):
                    raise ValueError('standing rule capacity invalid')
        return records, saved
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        records, saved = original(data)
        if saved is not None:
            _sync_existing_preferences(path)
            return saved
        if inspect:
            return None
        if proposal is not None and any(type(value) is dict and value.get('tag') == tag
               and type(value.get('proposal')) is dict
               and value['proposal'].get('action_id') == proposal['action_id']
               for value in records.values()):
            raise OwnerPreferenceRefusalError('target_unavailable')
        with jsonstore.lock(master.path()):
            try:
                with open(master.path(), encoding='utf-8') as stream:
                    before = stream.read()
            except FileNotFoundError:
                before = ''
            except (OSError, UnicodeError):
                raise OwnerPreferenceRefusalError('target_unavailable') from None
            if priorities and _read_priorities(master)[1] != review['previous_text']:
                raise OwnerPreferenceRefusalError('target_changed')
            before_priorities = master.priorities()['priorities'] if cta else None
            saved = {'tag': tag, 'proof': proof, 'review': review,
                     'proposal': proposal, 'status': 'contacting'}
            records[proof['event_id']] = saved
            data['owner_preference_receipts'] = records
            _durable_preferences_replace(path, data)
            try:
                if cta:
                    master.add_priority(addition)
                elif priorities:
                    master.set_section('Priorities', review['text'])
                else:
                    master.append_section(section, addition)
            except master.OverCap as error:
                sizes = sorted(error.sizes.items(), key=lambda pair: (-pair[1], pair[0]))
                largest = None
                if sizes:
                    name, chars = sizes[0]
                    largest = {'name': name[:200], 'chars': chars, 'name_truncated': len(name) > 200}
                saved = {**saved, 'status': 'rejected', 'code': 'master_capacity',
                         'capacity': {'cap': master.MASTER_CHAR_CAP, 'largest_section': largest}}
            except ValueError as error:
                if not cta or str(error) != 'Priorities is full; confirm a replacement set before changing it':
                    return saved
                saved = {**saved, 'status': 'rejected', 'code': 'priorities_full'}
            except Exception:
                return saved
            else:
                try:
                    with open(master.path(), encoding='utf-8') as stream:
                        after = stream.read()
                    pre_before, sections_before = master._split(before)
                    pre_after, sections_after = master._split(after)
                    if cta:
                        unchanged = lambda rows: [(name, body.strip('\n')) for name, body in rows if name.lower() != 'priorities']
                        after_priorities = {row['text'].casefold() for row in master.priorities()['priorities']}
                        if (pre_before.strip() != pre_after.strip() or unchanged(sections_before) != unchanged(sections_after)
                                or addition.casefold() not in after_priorities
                                or any(row['text'].casefold() not in after_priorities for row in before_priorities)):
                            raise ValueError('priority readback unavailable')
                    elif priorities:
                        unchanged = lambda rows: [(name, body.strip('\n')) for name, body in rows if name.lower() != 'priorities']
                        if (pre_before.strip() != pre_after.strip() or unchanged(sections_before) != unchanged(sections_after)
                                or _read_priorities(master)[1] != review['text']):
                            raise ValueError('priorities readback unavailable')
                    else:
                        non_rules = lambda rows: [(name, body.strip('\n')) for name, body in rows if name.lower() != section.lower()]
                        rule_sections = [body.strip('\n') for name, body in sections_after if name.lower() == section.lower()]
                        prior_rules = [body.strip('\n') for name, body in sections_before if name.lower() == section.lower()]
                        expected_rules = [(body + '\n' if body else '') + addition for body in (prior_rules or [''])]
                        if (pre_before.strip() != pre_after.strip() or non_rules(sections_before) != non_rules(sections_after)
                                or rule_sections != expected_rules):
                            raise ValueError('standing rule readback unavailable')
                    _sync_existing_preferences(master.path())
                except Exception:
                    return saved
                saved = {**saved, 'status': 'committed', 'outcome': outcome}
            records[proof['event_id']] = saved
            try:
                _durable_preferences_replace(path, data)
            except Exception:
                return {'tag': tag, 'proof': proof, 'review': review,
                        'proposal': proposal, 'status': 'contacting'}
            return saved


def configured_nudge_budget() -> int:
    """The installation's ceiling. An explicit preference may reduce it, never raise it."""
    try:
        return max(0, int((os.environ.get("SOTTO_NUDGE_BUDGET") or "").strip() or 4))
    except (TypeError, ValueError):
        return 4


def effective_nudge_budget(explicit: dict | None = None, configured: int | None = None) -> int:
    """Current unsolicited-nudge allowance, clamped to the configured installation maximum."""
    ceiling = configured_nudge_budget() if configured is None else max(0, int(configured))
    ex = explicit if isinstance(explicit, dict) else load_explicit()
    raw = str(ex.get("nudge_budget") or "").strip()
    if not raw:
        return ceiling
    try:
        return min(ceiling, max(0, int(raw)))
    except (TypeError, ValueError):
        return ceiling


def change_nudge_budget(direction: str) -> dict:
    """Apply the user's plain volume controls: fewer halves, no sets zero, more restores default."""
    direction = (direction or "").strip().lower()
    if direction not in ("fewer", "no", "more"):
        raise ValueError("nudge-budget takes fewer|no|more")
    ceiling = configured_nudge_budget()

    def _apply(ex):
        current = effective_nudge_budget(ex, ceiling)
        if direction == "fewer":
            value = current if current == 0 else current // 2
        elif direction == "no":
            value = 0
        else:
            value = ceiling
        ex["nudge_budget"] = str(value)

    return _mutate(_apply)


# ── Cadence: the nudge snooze ("quieter today" / "quiet until 3" / "back to normal") ───────────────

def _now_local_best_effort() -> datetime:
    """The user's wall clock (SOTTO_TIMEZONE / settings.json) via the brief's own tz resolution;
    falls back to naive system-local time when _shared/lib isn't importable (bare CLI use)."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
        from timeutil import _now_local, configured_tz  # noqa: PLC0415
        return _now_local(configured_tz() or "+00:00")
    except Exception:  # noqa: BLE001
        return datetime.now()


def _snooze_morning_hour() -> int:
    """The hour a "quieter today" snooze lifts: SOTTO_QUIET_END (the funnel's own quiet-hours knob),
    default 7. Anything unparseable or outside 0..23 falls back to the default."""
    try:
        h = int((os.environ.get("SOTTO_QUIET_END") or "").strip() or SNOOZE_MORNING_HOUR_DEFAULT)
    except ValueError:
        return SNOOZE_MORNING_HOUR_DEFAULT
    return h if 0 <= h <= 23 else SNOOZE_MORNING_HOUR_DEFAULT


def _naive(dt: datetime) -> datetime:
    """Wall-clock view of an instant. The snooze is a LOCAL wall-clock stamp — comparing naive
    both sides is what makes 'quiet until 3' mean 3pm where the user is, whatever tzinfo the
    resolved clock happens to carry."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def parse_snooze(value: str):
    """An `nudge_snooze_until` value → naive local datetime, or None when absent/unparseable.
    A bare date means midnight AT THE START of that date (the moment the snooze lifts)."""
    v = (value or "").strip()
    if not v:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        v += "T00:00:00"
    try:
        return _naive(datetime.fromisoformat(v.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def resolve_snooze_spec(spec: str, now_local: datetime | None = None) -> str:
    """A user-facing snooze spec → the stored ISO local stamp. Deterministic clock math lives here
    (never in the agent's head). Accepts:
      "tomorrow" / "today"        → tomorrow at quiet-end (SOTTO_QUIET_END, default 7am), which is
                                    also what "rest of the day" means — a snooze lifts when quiet
                                    hours do
      "+2h" / "2h" / "90m"        → now + delta        ("quiet for 2 hours")
      "15:00" / "3pm" / "3"       → that time today, or tomorrow if it already passed
      "2026-08-08" / ISO datetime → verbatim
      "" / off / clear / normal   → "" (clears the snooze)
    Raises ValueError on anything else — the caller reports it rather than guessing."""
    s = (spec or "").strip().lower()
    now = _naive(now_local or _now_local_best_effort())
    if s in ("", "off", "clear", "none", "normal", "back to normal"):
        return ""
    if s in ("today", "tomorrow", "rest of the day", "quieter today"):
        nxt = (now + timedelta(days=1)).replace(hour=_snooze_morning_hour(), minute=0,
                                                second=0, microsecond=0)
        return nxt.strftime(SNOOZE_FMT)
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)", s)
    if m:
        n = float(m.group(1))
        delta = timedelta(hours=n) if m.group(2).startswith(("h",)) else timedelta(minutes=n)
        return (now + delta).strftime(SNOOZE_FMT)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif not ampm and hour < 7:
            hour += 12               # bare "3" from someone awake means 3pm, not 3am
        if hour > 23 or minute > 59:
            raise ValueError(f"unparseable time: {spec}")
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.strftime(SNOOZE_FMT)
    parsed = parse_snooze(spec)
    if parsed is not None:
        return parsed.strftime(SNOOZE_FMT)
    raise ValueError(f"unparseable snooze spec: {spec}")


def snooze_active(now_local: datetime | None = None, explicit: dict | None = None) -> bool:
    """True while `nudge_snooze_until` is in the future. Missing/unparseable → False (a broken
    stamp must never silence Sotto forever). Offset stamps compare instants; legacy naive
    stamps retain their local wall-clock comparison."""
    ex = explicit if isinstance(explicit, dict) else load_explicit()
    value = str(ex.get("nudge_snooze_until") or "").strip()
    try:
        until = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = now_local or _now_local_best_effort()
    if until.tzinfo is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=_now_local_best_effort().tzinfo or timezone.utc)
        return now.astimezone(timezone.utc) < until.astimezone(timezone.utc)
    return _naive(now) < until


# A phone-shaped local part, by the same rule textutil._normalize_identifier applies.
_PHONE_LOCAL = re.compile(r"[\d\s\-\+\(\)]+")
# Chat transports that carry a real phone number in front of the '@'. Everything else with an '@'
# is an email (matched as an email), a '@lid' privacy handle, or a '@g.us' room — never a person's
# number, and never something a phone mute may catch.
_PHONE_JID_DOMAINS = ("s.whatsapp.net", "whatsapp.net", "c.us")
PHONE_KEY_MIN_DIGITS = 7                 # shorter than this is a shortcode, not a phone number


def _phone_key(value: str) -> str:
    """The comparison key for a phone number written any way — "+1 202 555 0171", "(202) 555-0171",
    "12025550171", or the phone in front of a WhatsApp JID — and "" for anything that is not one.

    One normaliser for the whole tree: personal_context.normalized_identifier (→
    textutil._normalize_identifier, full digits), so a mute and a conversation agree on what a
    number is. Keys are compared whole, so a mute on +12025550171 never catches ...0170."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    local, sep, domain = v.partition("@")
    if sep and domain not in _PHONE_JID_DOMAINS:
        return ""
    if not _PHONE_LOCAL.fullmatch(local):
        return ""
    key = _normalized_identifier(local)
    return key if len(key) >= PHONE_KEY_MIN_DIGITS else ""


def sender_is_muted(identifier: str, muted: list) -> bool:
    """True if a sender identifier matches a muted sender.

    An email matches exactly, or by an '@domain' / 'domain' suffix rule (so '@news.acme.com' or
    'news.acme.com' mutes the whole sending domain). A phone number matches however either side is
    written — the mute and the identifier are compared through the tree's one identifier normaliser
    (_phone_key), which also reads the number out of a WhatsApp JID. Email matching is unchanged:
    a phone key only ever compares against another phone key."""
    e = (identifier or "").strip().lower()
    if not e:
        return False
    key = _phone_key(e)
    dom = e.split("@", 1)[1] if "@" in e else e
    for m in muted:
        m = (m or "").strip().lower()
        if not m:
            continue
        if key and _phone_key(m) == key:      # a number, in whatever format either side stored it
            return True
        if m.startswith("@"):                 # "@domain" → whole-domain rule
            rule = m[1:]
            if rule and (dom == rule or dom.endswith("." + rule)):
                return True
        elif "@" in m:                        # full address → exact match
            if e == m:
                return True
        else:                                 # bare "domain" → whole-domain rule
            if dom == m or dom.endswith("." + m):
                return True
    return False


def proactively_muted(sender: str, event: dict, explicit: dict) -> bool:
    """THE current-mute predicate for every communication surface: the event funnel's ingress gate,
    the release valve, a tapped promotion and the midday digest all ask this one question, so
    "stop surfacing X" cannot drop yesterday's queued message and still let today's ring through.

    `sender` is triage's resolved display name; event fields retain transport identifiers. Names
    are exact and case-insensitive. Sender rules reuse sender_is_muted, including domain rules and
    phone numbers in any format (and the phone inside a WhatsApp JID).
    """
    explicit = explicit if isinstance(explicit, dict) else {}
    name = (sender or "").strip().lower()
    if name and any(name == str(person or "").strip().lower()
                    for person in (explicit.get("mute_people") or [])):
        return True
    if not isinstance(event, dict):
        return False
    identifiers = []
    for key in ("handle", "contact_jid", "sender_jid", "phone", "email", "address"):
        value = str(event.get(key) or "").strip()
        if value:
            identifiers.append(value)
    from_value = str(event.get("from") or "").strip()
    if from_value:
        identifiers.extend(v for v in (from_value, parseaddr(from_value)[1]) if v)
    return any(sender_is_muted(value, explicit.get("mute_senders") or [])
               for value in identifiers)


_CLI = {
    "mute-sender": ("mute_senders", add), "unmute-sender": ("mute_senders", remove),
    "mute-person": ("mute_people", add), "unmute-person": ("mute_people", remove),
    "mute-section": ("mute_sections", add), "unmute-section": ("mute_sections", remove),
    "tone": ("tone_notes", add), "remove-tone": ("tone_notes", remove),
    "vip": ("vip_people", add), "unvip": ("vip_people", remove),
}


def is_vip(name: str, vip_people: list) -> bool:
    """True when a resolved display name is on the user's stated VIP list (exact, case-insensitive
    — the same standard mute_people uses; nothing fuzzy, so 'Sam' never VIPs 'Samantha')."""
    n = (name or "").strip().lower()
    if not n:
        return False
    return any(n == (v or "").strip().lower() for v in (vip_people or []))


def main():
    if len(sys.argv) < 2:
        print(json.dumps(load_explicit())); return
    cmd = sys.argv[1]
    if cmd == "show":
        print(json.dumps(load_explicit())); return
    if cmd == "clear-tone":
        print(json.dumps(_mutate(lambda ex: ex.__setitem__("tone_notes", [])))); return
    if cmd == "unsnooze-nudges":
        print(json.dumps(set_scalar("nudge_snooze_until", ""))); return
    if cmd == "nudge-budget":
        value = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lower()
        try:
            print(json.dumps(change_nudge_budget(value)))
        except ValueError as e:
            print(json.dumps({"error": str(e)})); sys.exit(2)
        return
    if cmd == "snooze-nudges":
        spec = " ".join(sys.argv[2:]).strip()
        if not spec:
            print(json.dumps({"error": "missing value"})); sys.exit(2)
        try:
            value = resolve_snooze_spec(spec)
        except ValueError as e:
            print(json.dumps({"error": str(e)})); sys.exit(2)
        print(json.dumps(set_scalar("nudge_snooze_until", value))); return
    if cmd not in _CLI:
        print(json.dumps({"error": f"unknown command: {cmd}"})); sys.exit(2)
    kind, fn = _CLI[cmd]
    value = sys.argv[2] if len(sys.argv) > 2 else ""
    if not value.strip():
        print(json.dumps({"error": "missing value"})); sys.exit(2)
    print(json.dumps(fn(kind, value)))


if __name__ == "__main__":
    main()


def admitted_usefulness(proof, feedback, select, guard, *, inspect=False):
    """Original outcomes writer once; a lost separate-file result stays unknown."""
    import hashlib
    args = proof['args']
    retained = {key: value for key, value in proof.items() if key != 'args'}
    path = _path()
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        records = data.setdefault('owner_preference_receipts', {})
        if type(records) is not dict:
            raise ValueError('usefulness receipts invalid')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if type(saved) is not dict or saved.get('tag') != 'usefulness' or saved.get('proof') != retained:
                raise OwnerPreferenceRefusalError('event_conflict')
            locator = saved.get('item_locator')
            if args['excerpt']:
                match = re.fullmatch(r'excerpt:(\d+):(\d+):([a-f0-9]{20})', str(locator))
                valid = bool(match) and int(match[2]) == len(args['excerpt'].strip()) and match[3] == hashlib.sha256(args['excerpt'].strip().encode()).hexdigest()[:20]
            else:
                valid = locator == 'output'
            outcome = {'operation': 'usefulness', 'reference': args['reference'], 'item_locator': locator,
                       'rating': args['rating'], 'status': 'recorded'}
            if (not valid or saved.get('status') not in ('contacting', 'committed')
                    or set(saved) != {'tag', 'proof', 'status', 'item_locator'} | ({'outcome'} if saved['status'] == 'committed' else set())
                    or saved['status'] == 'committed' and saved['outcome'] != outcome):
                raise ValueError('usefulness receipt invalid')
            _sync_existing_preferences(path)
            return saved
        if inspect:
            return None
        locator = select()
        guard()
        saved = {'tag': 'usefulness', 'proof': retained, 'status': 'contacting', 'item_locator': locator}
        records[proof['event_id']] = saved
        _durable_preferences_replace(path, data)
        try:
            guard()
            result = feedback.record(args['reference'], args['rating'], args['excerpt'],
                                     permitted=guard.permitted, archive_version=args['archive_version'])
            if result != {'logged': True, 'outcome': args['rating']} or type(result.get('logged')) is not bool:
                raise ValueError('usefulness recording unconfirmed')
            _sync_existing_preferences(feedback.log_outcome._path())
            guard()
        except Exception:
            return saved
        outcome = {'operation': 'usefulness', 'reference': args['reference'], 'item_locator': locator,
                   'rating': args['rating'], 'status': 'recorded'}
        records[proof['event_id']] = {**saved, 'status': 'committed', 'outcome': outcome}
        try:
            _durable_preferences_replace(path, data)
        except Exception:
            return saved
        return records[proof['event_id']]
