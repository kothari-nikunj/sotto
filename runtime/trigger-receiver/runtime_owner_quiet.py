"""Private admitted owner quiet commands; no model or MCP write surface."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
from pathlib import Path
from installed_writers import preferences_for, UnavailableError

_HEX64 = re.compile(r'[a-f0-9]{64}\Z')
_FOR = re.compile(r'/quiet for ([1-9][0-9]*)([hm])\Z')
_DOMAIN = 'sotto.owner-quiet-command.v1'


_preferences = preferences_for(__file__)


def _command(text):
    if text == '/quiet off':
        return 'off'
    match = _FOR.fullmatch(text) if type(text) is str else None
    if match is None:
        raise ValueError('owner quiet command is invalid')
    amount = int(match.group(1))
    if amount > (24 if match.group(2) == 'h' else 1440):
        raise ValueError('owner quiet duration is invalid')
    return match.group(1) + match.group(2)


def _validate(body, capability, policy, data_root, activation,
              telegram_verifier=None, chat_client=None):
    if (type(body) is not dict
            or set(body) != {'version', 'operation', 'identity', 'action_id', 'event_id',
                             'payload_hash', 'text_hash', 'text', 'admitted_at_ms',
                             'provider_thread_id', 'zone'}
            or type(body['version']) is not int or body['version'] != 1
            or body['operation'] not in ('prepare', 'write', 'inspect')):
        raise ValueError('owner quiet request is invalid')
    identity = capability.validate_identity(body['identity'])
    managed_photon = (identity['channel'] == 'photon'
                      and policy.get('SOTTO_DEPLOYMENT_MODE') == 'managed'
                      and policy.get('SOTTO_RUNTIME_PROFILE') == 'managed-photon-fixed-five-v1')
    selfhost_photon = (identity['channel'] == 'photon'
                       and policy.get('SOTTO_DEPLOYMENT_MODE') == 'self_host'
                       and policy.get('SOTTO_RUNTIME_PROFILE') == 'self-host-photon-fixed-five-v1'
                       and bool(policy.get('GOOGLE_AI_API_KEY'))
                       and 'SOTTO_MODEL_PROXY_URL' not in policy
                       and 'SOTTO_MODEL_PROXY_TOKEN' not in policy)
    selfhost_telegram = (identity['channel'] == 'telegram'
                         and policy.get('SOTTO_DEPLOYMENT_MODE') == 'self_host'
                         and policy.get('SOTTO_RUNTIME_PROFILE') == 'self-host-telegram-fixed-five-v1'
                         and bool(policy.get('GOOGLE_AI_API_KEY'))
                         and 'SOTTO_MODEL_PROXY_URL' not in policy
                         and 'SOTTO_MODEL_PROXY_TOKEN' not in policy)
    if (not (managed_photon or selfhost_photon or selfhost_telegram)
            or identity['tenant_id'] != policy.get('SOTTO_TENANT_ID')
            or str(data_root) != policy.get('SOTTO_DATA')):
        raise UnavailableError('owner quiet authority unavailable')
    if identity['channel'] == 'telegram':
        token = policy.get('TELEGRAM_BOT_TOKEN', '')
        if (not selfhost_telegram or policy.get('SOTTO_CRON_DELIVER') != 'telegram'
                or identity['owner_id'] != policy.get('TELEGRAM_HOME_CHANNEL')
                or identity['owner_id'] != policy.get('TELEGRAM_ALLOWED_USERS')
                or not isinstance(token, str)
                or identity['bot_fingerprint'] != hashlib.sha256(
                    ('sotto-telegram-bot-v1:' + token).encode()).hexdigest()):
            raise UnavailableError('owner quiet authority unavailable')
    elif (policy.get('SOTTO_CRON_DELIVER') != 'photon'
          or identity['owner_id'] != policy.get('PHOTON_HOME_CHANNEL')
          or identity['owner_id'] != policy.get('PHOTON_ALLOWED_USERS')
          or identity['project_fingerprint'] != hashlib.sha256(
              ('sotto-photon-project-v1:' + policy.get('PHOTON_PROJECT_ID', '')).encode()).hexdigest()):
        raise UnavailableError('owner quiet authority unavailable')
    for name in ('action_id', 'event_id', 'payload_hash', 'text_hash'):
        if type(body[name]) is not str or _HEX64.fullmatch(body[name]) is None:
            raise ValueError('owner quiet request is invalid')
    event_id = body['event_id']
    action_id = hashlib.sha256(json.dumps([_DOMAIN, event_id], separators=(',', ':')).encode()).hexdigest()
    text = body['text']
    command = _command(text)
    if (body['action_id'] != action_id or len(text) > 500
            or hashlib.sha256(text.encode()).hexdigest() != body['text_hash']):
        raise ValueError('owner quiet request is invalid')
    admitted = body['admitted_at_ms']
    if type(admitted) is not int or not 946684800000 <= admitted <= 4102444800000:
        raise ValueError('owner quiet request is invalid')
    zone = policy['_configured_tz_name']()
    tz = policy['_tz_resolve'](zone)
    if not zone or tz is None or len(zone) > 64:
        raise UnavailableError('owner quiet timezone unavailable')
    if body['operation'] == 'prepare':
        if body['zone'] is not None:
            raise ValueError('owner quiet request is invalid')
    elif body['zone'] != zone:
        raise UnavailableError('owner quiet timezone changed')
    thread = body['provider_thread_id']
    if identity['channel'] == 'telegram':
        if (type(thread) is not str or thread != f"telegram:{identity['owner_id']}"
                or identity['route_mode'] != 'fixed' or identity['route_id'] != thread
                or telegram_verifier is None or chat_client is None):
            raise UnavailableError('owner quiet route unavailable')
        try:
            proof = telegram_verifier.check_current_channel(
                chat_client=chat_client,
                expected_channel_path=Path(data_root) / 'runtime/expected-channel.json',
                receiver_data_root=data_root, receiver_policy=policy,
                configured_zone_name=policy['_configured_tz_name'], expected_zone=zone)
        except Exception as error:
            raise UnavailableError('owner quiet route unavailable') from error
        if (proof.get('status') != 'current_channel_proved'
                or proof.get('action_authorized') is not False
                or proof.get('provider_thread_id') != thread
                or proof.get('bot_fingerprint') != identity['bot_fingerprint']
                or proof.get('tenant_id') != identity['tenant_id']
                or proof.get('owner_id') != identity['owner_id']
                or proof.get('account_id') != identity['account_id']
                or proof.get('line_key') != identity['line_key']):
            raise UnavailableError('owner quiet route changed')
    elif identity['route_mode'] == 'first_contact_shared_project':
        import runtime_owner_reminder
        try:
            runtime_owner_reminder.validate_route(thread, identity, policy, data_root, activation,
                                                  chat_client=chat_client)
        except runtime_owner_reminder.UnavailableError as error:
            raise UnavailableError('owner quiet route unavailable') from error
    else:
        if type(thread) is not str or not thread.startswith('imessage:') or len(thread) > 512:
            raise ValueError('owner quiet request is invalid')
        guid, separator, line = thread[len('imessage:'):].partition('~')
        if (not separator or not guid or not line or '~' in line
                or line != policy.get('SOTTO_IMESSAGE_NUMBER')):
            raise UnavailableError('owner quiet route unavailable')
        route = activation.verified_current_photon_route(data_root, policy)
        if (route['chat_id'] != guid
                or identity['route_mode'] == 'fixed' and identity['route_id'] != thread
                or identity['route_mode'] == 'first_contact' and identity['route_id'] != line):
            raise UnavailableError('owner quiet route changed')
    identity_digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    proof = {'action_id': action_id, 'event_id': event_id, 'payload_hash': body['payload_hash'],
             'text_hash': body['text_hash'], 'identity_digest': identity_digest,
             'command': text, 'admitted_at_ms': admitted, 'provider_thread_id': thread,
             'zone': zone}
    when = (datetime.fromtimestamp(admitted // 1000, timezone.utc)
            + timedelta(milliseconds=admitted % 1000)).astimezone(tz)
    return zone, command, when, proof


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            telegram_verifier=None, chat_client=None):
    """Return the original committed effect on retries; inspection never reapplies it."""
    if type(body) is not dict:
        raise ValueError('owner quiet request is invalid')
    expected = capability.derive(root, capability.validate_identity(body.get('identity')))
    if type(bearer) is not str or not hmac.compare_digest(expected, bearer):
        raise UnavailableError('owner quiet authority unavailable')
    zone, command, when, proof = _validate(
        body, capability, policy, data_root, activation,
        telegram_verifier=telegram_verifier, chat_client=chat_client)
    if body['operation'] == 'prepare':
        return {'version': 1, 'status': 'prepared', 'zone': zone}
    writer = _preferences()
    snooze_until = ''
    if command != 'off':
        duration = timedelta(**{'hours' if command[-1] == 'h' else 'minutes': int(command[:-1])})
        snooze_until = (when.astimezone(timezone.utc) + duration).astimezone(
            when.tzinfo).isoformat(timespec='milliseconds')
    try:
        receipt = writer.admitted_owner_quiet(proof, snooze_until,
                                              inspect=body['operation'] == 'inspect')
    except writer.OwnerQuietConflictError:
        return {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
    if receipt is None:
        return {'version': 1, 'status': 'not_found'}
    return {'version': 1, 'status': 'committed', 'action_id': receipt['action_id'],
            'event_id': receipt['event_id'], 'payload_hash': receipt['payload_hash'],
            'zone': receipt['zone'], 'snooze_until': receipt['snooze_until']}
