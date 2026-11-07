"""Attended owner reminder tools; original admitted text is never a slash command."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
from functools import partial
import re
import hashlib
import json

import runtime_owner_reminder as reminders

from owner_tool_admission import (_text, action_identity, validate_tool_envelope, UnavailableError)

_identity = partial(action_identity, kind="owner_reminder_tool")
_HEX64 = re.compile(r'[a-f0-9]{64}\Z')
_DUE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})\Z')
_DOMAIN = 'sotto.owner-reminder-tool.v1'


def _validate(body, capability, policy, data_root, activation, *, bearer, root,
              telegram_verifier=None, chat_client=None):
    proof, identity = validate_tool_envelope(body,
        operations=('context', 'list', 'create', 'cancel', 'inspect'),
        kind='owner_reminder_tool', domain=_DOMAIN, bearer=bearer, root=root,
        capability=capability, policy=policy, data_root=data_root)
    args = body['args']
    due = None
    if body['operation'] in ('context', 'list'):
        if args is not None:
            raise ValueError('owner reminder tool context arguments are invalid')
    elif type(args) is dict and args.get('operation') == 'cancel':
        if (set(args) != {'operation', 'reminder_id', 'cancellation'}
                or type(args['reminder_id']) is not str
                or re.fullmatch(r'int_[a-f0-9]{32}', args['reminder_id']) is None):
            raise ValueError('owner reminder cancellation arguments invalid')
        _text(args['cancellation'], bound=16000)
        if args['cancellation'].strip() != args['cancellation']:
            raise ValueError('owner reminder cancellation invalid')
        if body['operation'] not in ('cancel', 'inspect'):
            raise ValueError('owner reminder cancellation operation invalid')
    else:
        conditional = type(args) is dict and args.get('operation') == 'conditional'
        if body['operation'] == 'cancel':
            raise ValueError('owner reminder cancellation arguments invalid')
        if type(args) is not dict or set(args) != ({'operation', 'due', 'action', 'condition', 'anchor_key', 'loop_version', 'loop_identity', 'source_revision'} if conditional else {'due', 'action'}):
            raise ValueError('owner reminder tool arguments are invalid')
        if conditional:
            _text(args['condition'], bound=16000)
            _text(args['anchor_key'], bound=400, whitespace=False)
            if len(args['anchor_key']) > 200 or args['condition'].strip() != args['condition']:
                raise ValueError('conditional reminder arguments invalid')
            for key in ('loop_version', 'loop_identity', 'source_revision'):
                if type(args[key]) is not str or not _HEX64.fullmatch(args[key]):
                    raise ValueError('conditional reminder proof invalid')
        _text(args['action'], bound=1000)
        if len(args['action']) > 500 or args['action'].strip() != args['action']:
            raise ValueError('owner reminder tool action is invalid')
        if type(args['due']) is not str or _DUE.fullmatch(args['due']) is None:
            raise ValueError('owner reminder tool due is invalid')
        try:
            due = datetime.fromisoformat(args['due'].replace('Z', '+00:00'))
        except ValueError as error:
            raise ValueError('owner reminder tool due is invalid') from error
    reminders.validate_route(body['provider_thread_id'], identity, policy, data_root, activation,
                             telegram_verifier=telegram_verifier, chat_client=chat_client)
    return proof, due


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            telegram_verifier=None, chat_client=None):
    """Inspect exact original effects; create at most one reminder per admitted owner event."""
    proof, due = _validate(body, capability, policy, data_root, activation, bearer=bearer, root=root,
                           telegram_verifier=telegram_verifier, chat_client=chat_client)
    if body['operation'] in ('context', 'list'):
        zone = policy['_configured_tz_name']()
        tz = policy['_tz_resolve'](zone)
        if not zone or tz is None or len(zone) > 64:
            raise UnavailableError('owner reminder tool timezone unavailable')
        if body['operation'] == 'list':
            return {'version': 1, 'status': 'listed', 'event_id': body['event_id'],
                    'payload_hash': body['payload_hash'], 'zone': zone, 'items': [
                        {'id': row['id'], 'due': row['due'], 'action': row['action'], 'status': row['status']}
                        for row in reminders._writer().list_admitted(proof['identity_digest'], 50)]}
        return {'version': 1, 'status': 'context', 'event_id': body['event_id'],
                'payload_hash': body['payload_hash'], 'zone': zone,
                'now': datetime.now(timezone.utc).astimezone(tz).isoformat(timespec='milliseconds')}
    conditional = isinstance(body['args'], dict) and body['args'].get('operation') == 'conditional'
    if conditional and body['args']['condition'] not in body['text']:
        return {'version': 1, 'status': 'rejected', 'code': 'owner_span_unavailable'}
    cancel = isinstance(body['args'], dict) and body['args'].get('operation') == 'cancel'
    if cancel and body['args']['cancellation'] not in body['text']:
        return {'version': 1, 'status': 'rejected', 'code': 'owner_span_unavailable'}
    store = reminders._writer()
    # Look up the original effect before any create refusal, including changed retries.
    try:
        row = store.inspect_admitted(proof, cancel=cancel)
    except ValueError:
        return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
    if row is None and body['operation'] == 'inspect':
        return {'version': 1, 'status': 'not_found'}
    if row is None and cancel:
        outcome, row = store.cancel_admitted(body['args']['reminder_id'], proof)
        if outcome != 'committed':
            return {'version': 1, 'status': 'rejected', 'code': outcome}
    if conditional and row is None:
        import runtime_owner_loop
        lock = runtime_owner_loop._load_writer()._load_continuity()._ledger_lock()
    else:
        lock = nullcontext()
    with lock:
        if row is None:
            if due.astimezone(timezone.utc) <= datetime.fromtimestamp(body['admitted_at_ms'] / 1000, timezone.utc):
                return {'version': 1, 'status': 'rejected', 'code': 'due_unavailable'}
            binding = None
            if conditional:
                authority = policy['_source_authority']()
                if authority.get('complete') is not True or authority.get('revision') != body['args']['source_revision']:
                    return {'version': 1, 'status': 'rejected', 'code': 'source_unavailable'}
                granted = set(authority.get('bridge_sources', [])) | set(authority.get('google_sources', [])) | set(authority.get('connector_sources', []))
                binding = {'anchor_key': body['args']['anchor_key'], 'loop_identity': body['args']['loop_identity'], 'sources': []}
                try:
                    state, target, sources = store.conditional_loop(binding, now=datetime.fromtimestamp(body['admitted_at_ms'] / 1000, timezone.utc),
                        permitted=lambda source: source in granted, version=body['args']['loop_version'])
                    import ledger_io
                    if state != 'active' or not ledger_io.is_waiting_on(target.get('action_type')):
                        return {'version': 1, 'status': 'rejected', 'code': 'target_changed'}
                    binding['sources'] = sources
                except PermissionError:
                    return {'version': 1, 'status': 'rejected', 'code': 'source_unavailable'}
                except (ValueError, OSError):
                    return {'version': 1, 'status': 'rejected', 'code': 'target_changed'}
                if policy['_source_authority']() != authority:
                    return {'version': 1, 'status': 'rejected', 'code': 'source_unavailable'}
            outcome, row = store.create_tool_admitted(body['args']['due'], body['args']['action'], proof,
                **({'condition_binding': binding} if conditional else {}))
            if outcome != 'committed':
                return {'version': 1, 'status': 'rejected', 'code': outcome}
    return {**({'operation': 'cancel'} if cancel else {'operation': 'conditional', 'anchor_key': row['anchor_key']} if conditional else {}),
            'version': 1, 'status': 'committed', 'action_id': proof['action_id'],
            'event_id': proof['event_id'], 'payload_hash': proof['payload_hash'],
            'args_hash': proof['args_hash'], 'reminder_id': row['id'],
            'reminder_status': row['status'], 'due': row['due'], 'action': row['action']}


def timer_current(row, identity, thread, *, delivery_notice=False):
    """Saved admitted unconditional action, bound to the independently proved current route."""
    try:
        proof = row['owner_create']
        args = proof['args']
        expected_status = 'canceled' if delivery_notice else 'scheduled'
        if delivery_notice and (row.get('delivery_outcome') not in ('superseded', 'expired', 'failed')
                                or row.get('delivery_notice_outcome') or row.get('owner_cancel')):
            return False
        if (not reminders._writer().owner_timer(row) or row.get('status') != expected_status
                or row.get('anchor_key') or row.get('condition_binding')
                or not re.fullmatch(r'int_[a-f0-9]{32}', row.get('id', ''))
                or set(proof) != {'domain', 'event_id', 'action_id', 'payload_hash', 'text_hash',
                    'identity_digest', 'args_hash', 'args', 'admitted_at_ms', 'provider_thread_id'}
                or type(proof['admitted_at_ms']) is not int
                or not 946684800000 <= proof['admitted_at_ms'] <= 4102444800000
                or proof['domain'] != _DOMAIN or set(args) != {'due', 'action'}
                or proof['provider_thread_id'] != thread
                or proof['identity_digest'] != hashlib.sha256(json.dumps(
                    identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()):
            return False
        for key in ('event_id', 'action_id', 'payload_hash', 'text_hash', 'args_hash'):
            if type(proof[key]) is not str or not _HEX64.fullmatch(proof[key]):
                return False
        action, digest = _identity(proof['event_id'], args)
        due = datetime.fromisoformat(args['due'].replace('Z', '+00:00'))
        saved_due = datetime.fromisoformat(row['due'].replace('Z', '+00:00'))
        return (action == proof['action_id'] and digest == proof['args_hash']
                and due.tzinfo is not None and saved_due == due
                and type(args['action']) is str and 0 < len(args['action']) <= 500
                and args['action'] == row['action']
                and due <= datetime.now(timezone.utc))
    except (KeyError, TypeError, ValueError):
        return False
