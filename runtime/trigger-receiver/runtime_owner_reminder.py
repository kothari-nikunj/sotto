"""Receiver-only one-time reminder commands from admitted owner events.

The private owner-loop capability authenticates the same Photon owner and
route. Reminder action IDs and saved effects have their own domain; no model tool
or legacy intention CLI call can assert this provenance.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import re

_HEX64 = re.compile(r'[a-f0-9]{64}\Z')
_CREATE = re.compile(
    r'/reminder at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})) (.+)\Z')
_CANCEL = re.compile(r'/reminder cancel (int_[a-f0-9]{32})\Z')
_DOMAIN = 'sotto.owner-reminder-command.v1'


from owner_route_authority import UnavailableError, validate_authority, validate_route


class PreEffectRefusalError(Exception):
    """A parsed owner request that cannot create an effect."""


def _writer():
    here = Path(__file__).resolve()
    for path in (here.parents[2] / 'sotto-chief-of-staff' / '_shared' / 'scripts' / 'schedule_wakeup.py',
                 Path('/app/sotto-skills/_shared/scripts/schedule_wakeup.py')):
        if path.is_file():
            spec = importlib.util.spec_from_file_location('sotto_owner_reminder_schedule', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise UnavailableError('reminder store unavailable')


def _command(text):
    if (type(text) is not str or not text or len(text) > 700 or text.strip() != text
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in text)):
        raise ValueError('owner reminder command is invalid')
    if text == '/reminder list':
        return 'list', None, None
    match = _CREATE.fullmatch(text)
    if match:
        due, action = match.groups()
        if (not action or len(action) > 500 or action.strip() != action
                or '  ' in text[:len(text) - len(action)]):
            raise ValueError('owner reminder command is invalid')
        try:
            instant = datetime.fromisoformat(due.replace('Z', '+00:00'))
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError('offset is required')
        except ValueError as error:
            raise PreEffectRefusalError('due_unavailable') from error
        return 'create', due, action
    match = _CANCEL.fullmatch(text)
    if match:
        return 'cancel', match.group(1), None
    raise ValueError('owner reminder command is invalid')


def _validate(body, capability, policy, data_root, activation,
              telegram_verifier=None, chat_client=None):
    if (type(body) is not dict
            or set(body) != {'version', 'operation', 'identity', 'action_id', 'event_id',
                             'payload_hash', 'text_hash', 'text', 'admitted_at_ms',
                             'provider_thread_id'}
            or type(body['version']) is not int or body['version'] != 1
            or body['operation'] not in ('create', 'cancel', 'inspect', 'list')):
        raise ValueError('owner reminder request is invalid')
    identity = capability.validate_identity(body['identity'])
    validate_authority(identity, policy, data_root)
    for name in ('action_id', 'event_id', 'payload_hash', 'text_hash'):
        if type(body[name]) is not str or _HEX64.fullmatch(body[name]) is None:
            raise ValueError('owner reminder request is invalid')
    event_id = body['event_id']
    action_id = hashlib.sha256(json.dumps([_DOMAIN, event_id], separators=(',', ':')).encode()).hexdigest()
    if body['action_id'] != action_id:
        raise ValueError('owner reminder request is invalid')
    command, target, action = _command(body['text'])
    if ((body['operation'] != command and body['operation'] != 'inspect')
            or (body['operation'] == 'inspect' and command == 'list')
            or hashlib.sha256(body['text'].encode()).hexdigest() != body['text_hash']):
        raise ValueError('owner reminder request is invalid')
    admitted = body['admitted_at_ms']
    if type(admitted) is not int or not 946684800000 <= admitted <= 4102444800000:
        raise ValueError('owner reminder request is invalid')
    if command == 'create':
        if datetime.fromisoformat(target.replace('Z', '+00:00')).astimezone(timezone.utc) <= datetime.fromtimestamp(
                admitted / 1000, timezone.utc):
            raise PreEffectRefusalError('due_unavailable')
    validate_route(body['provider_thread_id'], identity, policy, data_root, activation,
                   telegram_verifier=telegram_verifier, chat_client=chat_client)
    identity_digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    proof = {'event_id': event_id, 'action_id': action_id, 'payload_hash': body['payload_hash'],
             'text_hash': body['text_hash'], 'identity_digest': identity_digest, 'command': body['text']}
    return command, target, action, proof


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            telegram_verifier=None, chat_client=None):
    """Return an exact saved-effect receipt; inspection never mutates the log."""
    if type(body) is not dict:
        raise ValueError('owner reminder request is invalid')
    expected = capability.derive(root, capability.validate_identity(body.get('identity')))
    if type(bearer) is not str or not hmac.compare_digest(expected, bearer):
        raise UnavailableError('owner reminder authority unavailable')
    try:
        command, target, action, proof = _validate(
            body, capability, policy, data_root, activation,
            telegram_verifier=telegram_verifier, chat_client=chat_client)
    except PreEffectRefusalError:
        return {'version': 1, 'status': 'rejected', 'code': 'due_unavailable'}
    store = _writer()
    if command == 'list':
        return {'version': 1, 'status': 'listed', 'items': [
            {'id': row['id'], 'due': row['due'], 'action': row['action'], 'status': row['status']}
            for row in store.list_admitted(proof['identity_digest'], 50)]}
    if body['operation'] == 'inspect':
        try:
            row = store.inspect_admitted(proof, cancel=command == 'cancel')
        except ValueError:
            return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        if row is None:
            return {'version': 1, 'status': 'not_found'}
        outcome = 'committed'
    elif command == 'create':
        outcome, row = store.create_admitted(target, action, proof)
    else:
        outcome, row = store.cancel_admitted(target, proof)
    if outcome != 'committed':
        return {'version': 1, 'status': 'rejected', 'code': outcome}
    return {'version': 1, 'status': 'committed', 'operation': command,
            'action_id': proof['action_id'], 'event_id': proof['event_id'],
            'payload_hash': proof['payload_hash'], 'reminder_id': row['id'],
            'reminder_status': row['status']}
