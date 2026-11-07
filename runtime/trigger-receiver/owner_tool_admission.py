"""Original-event admission shared by the existing attended owner tools."""
import hashlib
import hmac
import json
import re

import owner_route_authority as authority

UnavailableError = authority.UnavailableError
_HEX64 = re.compile(r'[a-f0-9]{64}\Z')
_CONTROL_LIMIT = 32
_DELETE = 127
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xDFFF
_ADMISSION_EARLIEST_MS = 946684800000
_ADMISSION_LATEST_MS = 4102444800000


def _text(value, *, bound, whitespace=True):
    if (type(value) is not str or not value.strip()
            or len(value.encode('utf-16-le', errors='surrogatepass')) // 2 > bound
            or any((ord(char) < _CONTROL_LIMIT and (not whitespace or char not in '\t\n\r'))
                   or ord(char) == _DELETE or _SURROGATE_START <= ord(char) <= _SURROGATE_END for char in value)):
        raise ValueError('owner reminder tool text is invalid')


def action_identity(event_id, args, *, kind):
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action_id = hashlib.sha256(json.dumps([event_id, kind, canonical], ensure_ascii=False,
                                         separators=(',', ':')).encode()).hexdigest()
    return action_id, hashlib.sha256(canonical.encode()).hexdigest()


def validate_tool_envelope(body, *, operations, kind, domain, bearer, root, capability, policy,  # noqa: PLR0913 -- explicit existing admission dependencies
                            data_root):
    """Shared original-event, capability checks for bounded private tools."""
    if (type(body) is not dict or set(body) != {'version', 'operation', 'identity', 'action_id',
            'event_id', 'payload_hash', 'text_hash', 'text', 'admitted_at_ms', 'provider_thread_id', 'args'}
            or type(body['version']) is not int or body['version'] != 1 or body['operation'] not in operations):
        raise ValueError('owner tool request invalid')
    identity = capability.validate_identity(body['identity'])
    expected = capability.derive(root, identity)
    if type(bearer) is not str or not hmac.compare_digest(expected, bearer):
        raise UnavailableError('owner tool authority unavailable')
    authority.validate_authority(identity, policy, data_root)
    if (policy.get('SOTTO_DEPLOYMENT_MODE') == 'managed' and (identity['channel'] != 'photon'
            or policy.get('SOTTO_RUNTIME_PROFILE') != 'managed-photon-fixed-five-v1')):
        raise UnavailableError('owner tool profile unavailable')
    for key in ('action_id', 'event_id', 'payload_hash', 'text_hash'):
        if type(body[key]) is not str or not _HEX64.fullmatch(body[key]):
            raise ValueError('owner tool binding invalid')
    _text(body['text'], bound=16_000)
    if hashlib.sha256(body['text'].encode()).hexdigest() != body['text_hash']:
        raise ValueError('owner tool original text hash invalid')
    if type(body['admitted_at_ms']) is not int or not _ADMISSION_EARLIEST_MS <= body['admitted_at_ms'] <= _ADMISSION_LATEST_MS:
        raise ValueError('owner tool admission time invalid')
    action, args_hash = action_identity(body['event_id'], body['args'], kind=kind)
    if body['action_id'] != action:
        raise ValueError('owner tool action identity invalid')
    proof = {'domain': domain, 'event_id': body['event_id'], 'action_id': action,
            'payload_hash': body['payload_hash'], 'text_hash': body['text_hash'],
            'identity_digest': hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'args_hash': args_hash, 'args': body['args'],
            'admitted_at_ms': body['admitted_at_ms'], 'provider_thread_id': body['provider_thread_id']}
    return proof, identity


def validate_tool_admission(body, *, operations, kind, domain, bearer, root, capability, policy,  # noqa: PLR0913 -- existing tool API
                            data_root, activation, telegram_verifier=None, chat_client=None):
    proof, identity = validate_tool_envelope(body, operations=operations, kind=kind, domain=domain,
        bearer=bearer, root=root, capability=capability, policy=policy, data_root=data_root)
    authority.validate_route(body['provider_thread_id'], identity, policy, data_root, activation,
                             telegram_verifier=telegram_verifier, chat_client=chat_client)
    return proof
