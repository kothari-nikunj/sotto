"""Private explicit-command writer bridge; never exposed as an MCP/model tool."""
import hashlib
import hmac
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_HEX64 = re.compile(r'[a-f0-9]{64}\Z')
_TEXT_MAX = 500


class UnavailableError(Exception):
    pass


def _load_writer():
    here = Path(__file__).resolve()
    for path in (here.parents[2] / 'sotto-chief-of-staff' / '_shared' / 'knowledge' / 'knowledge_edit.py',
                 Path('/app/sotto-skills/_shared/knowledge/knowledge_edit.py')):
        if path.is_file():
            spec = importlib.util.spec_from_file_location('sotto_owner_loop_writer', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise UnavailableError('writer unavailable')


def _validate(body, capability, root, policy, data_root, activation,
              telegram_verifier=None, chat_client=None):
    if type(body) is not dict:
        raise ValueError('owner command request is invalid')
    closing = body.get('operation') in ('close_prepare', 'close_write', 'close_inspect')
    fields = {'version', 'operation', 'identity', 'action_id', 'event_id', 'payload_hash',
              'text_hash', 'text', 'admitted_at_ms', 'zone', 'provider_thread_id'}
    if closing:
        fields |= {'anchor_key', 'expected_row_digest'}
    if set(body) != fields:
        raise ValueError('owner command request is invalid')
    if (type(body['version']) is not int or body['version'] != 1 or body['operation'] not in
            ('prepare', 'write', 'inspect', 'close_prepare', 'close_write', 'close_inspect')):
        raise ValueError('owner command request is invalid')
    identity = capability.validate_identity(body['identity'])
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
    if (not (policy.get('SOTTO_DEPLOYMENT_MODE') == 'managed'
                    or selfhost_photon or selfhost_telegram)
            or identity['tenant_id'] != policy.get('SOTTO_TENANT_ID')):
        raise UnavailableError('owner command authority unavailable')
    if identity['channel'] == 'telegram':
        token = policy.get('TELEGRAM_BOT_TOKEN', '')
        if (not selfhost_telegram or policy.get('SOTTO_CRON_DELIVER') != 'telegram'
                or identity['owner_id'] != policy.get('TELEGRAM_HOME_CHANNEL')
                or identity['owner_id'] != policy.get('TELEGRAM_ALLOWED_USERS')
                or not isinstance(token, str)
                or identity['bot_fingerprint'] != hashlib.sha256(
                    ('sotto-telegram-bot-v1:' + token).encode()).hexdigest()):
            raise UnavailableError('owner command authority unavailable')
    elif (policy.get('SOTTO_CRON_DELIVER') != 'photon'
          or identity['owner_id'] != policy.get('PHOTON_HOME_CHANNEL')
          or identity['owner_id'] != policy.get('PHOTON_ALLOWED_USERS')
          or identity['project_fingerprint'] != hashlib.sha256(
              ('sotto-photon-project-v1:' + policy.get('PHOTON_PROJECT_ID', '')).encode()).hexdigest()):
        raise UnavailableError('owner command authority unavailable')
    for name in ('action_id', 'event_id', 'payload_hash', 'text_hash'):
        if type(body[name]) is not str or _HEX64.fullmatch(body[name]) is None:
            raise ValueError('owner command request is invalid')
    event_id = body['event_id']
    domain = 'sotto.owner-loop-close-command.v1' if closing else 'sotto.owner-loop-command.v1'
    action_id = hashlib.sha256(json.dumps([domain, event_id], separators=(',', ':')).encode()).hexdigest()
    text = body['text']
    if (body['action_id'] != action_id or type(text) is not str or not text
            or len(text) > _TEXT_MAX or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                                          for char in text)
            or hashlib.sha256(text.encode('utf-8')).hexdigest() != body['text_hash']):
        raise ValueError('owner command request is invalid')
    if closing:
        anchor = body['anchor_key']
        expected = body['expected_row_digest']
        if (type(anchor) is not str or not anchor or anchor.strip() != anchor or len(anchor) > 256
                or text != '/loop close ' + anchor
                or (expected is not None and (type(expected) is not str or _HEX64.fullmatch(expected) is None))
                or ((body['operation'] == 'close_prepare') != (expected is None))):
            raise ValueError('owner command request is invalid')
    elif text.strip() != text:
        raise ValueError('owner command request is invalid')
    timestamp = body['admitted_at_ms']
    if type(timestamp) is not int or not 946684800000 <= timestamp <= 4102444800000:
        raise ValueError('owner command request is invalid')
    zone = policy['_configured_tz_name']()
    tz = policy['_tz_resolve'](zone)
    if not zone or tz is None or len(zone) > 64:
        raise UnavailableError('owner timezone unavailable')
    if body['operation'] in ('prepare', 'close_prepare'):
        if body['zone'] is not None:
            raise ValueError('owner command request is invalid')
    elif body['zone'] != zone:
        raise UnavailableError('owner timezone changed')
    thread = body['provider_thread_id']
    if identity['channel'] == 'telegram':
        if (type(thread) is not str or thread != f"telegram:{identity['owner_id']}"
                or identity['route_mode'] != 'fixed' or identity['route_id'] != thread
                or telegram_verifier is None or chat_client is None):
            raise UnavailableError('owner route unavailable')
        try:
            proof = telegram_verifier.check_current_channel(
                chat_client=chat_client,
                expected_channel_path=Path(data_root) / 'runtime/expected-channel.json',
                receiver_data_root=data_root, receiver_policy=policy,
                configured_zone_name=policy['_configured_tz_name'], expected_zone=zone)
        except Exception as error:
            raise UnavailableError('owner route unavailable') from error
        if (proof.get('status') != 'current_channel_proved'
                or proof.get('action_authorized') is not False
                or proof.get('provider_thread_id') != thread
                or proof.get('bot_fingerprint') != identity['bot_fingerprint']
                or proof.get('tenant_id') != identity['tenant_id']
                or proof.get('owner_id') != identity['owner_id']
                or proof.get('account_id') != identity['account_id']
                or proof.get('line_key') != identity['line_key']):
            raise UnavailableError('owner route changed')
        return zone, datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(tz)
    if identity['route_mode'] == 'first_contact_shared_project':
        import runtime_owner_reminder
        try:
            runtime_owner_reminder.validate_route(thread, identity, policy, data_root, activation,
                                                  chat_client=chat_client)
        except runtime_owner_reminder.UnavailableError as error:
            raise UnavailableError('owner route unavailable') from error
        return zone, datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(tz)
    if type(thread) is not str or not thread.startswith('imessage:') or len(thread) > 512:
        raise ValueError('owner command request is invalid')
    guid, separator, line = thread[len('imessage:'):].partition('~')
    if (not separator or not guid or not line or '~' in line
            or line != policy.get('SOTTO_IMESSAGE_NUMBER')):
        raise UnavailableError('owner route unavailable')
    route = activation.verified_current_photon_route(data_root, policy)
    if route['chat_id'] != guid:
        raise UnavailableError('owner route changed')
    if identity['route_mode'] == 'fixed' and identity['route_id'] != thread:
        raise UnavailableError('owner route changed')
    if identity['route_mode'] == 'first_contact' and identity['route_id'] != line:
        raise UnavailableError('owner route changed')
    return zone, datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(tz)


def perform(body, *, bearer, root, policy, data_root, capability, activation,
            telegram_verifier=None, chat_client=None):
    """The caller supplies the authenticated loopback bearer separately from JSON."""
    if type(body) is not dict:
        raise ValueError('owner command request is invalid')
    expected = capability.derive(root, capability.validate_identity(body.get('identity')))
    if type(bearer) is not str or not hmac.compare_digest(expected, bearer):
        raise UnavailableError('owner command authority unavailable')
    # _validate accepts only the exact serialized request shape.
    zone, when = _validate(body, capability, root, policy, data_root, activation,
                           telegram_verifier=telegram_verifier, chat_client=chat_client)
    if body['operation'] in ('close_prepare', 'close_write', 'close_inspect'):
        writer = _load_writer()
        anchor = body['anchor_key']
        request = {'command': body['text'], 'owner_event_id': body['event_id'], 'now': when}
        try:
            if body['operation'] == 'close_prepare':
                prepared = writer.op_loop_close_prepare_admitted_owner_anchor(anchor, **request)
                return {'version': 1, 'status': 'prepared', 'zone': zone,
                        'expected_row_digest': prepared['expected_row_digest']}
            if body['operation'] == 'close_write':
                result = writer.op_loop_close_admitted_owner_anchor(
                    anchor, expected_row_digest=body['expected_row_digest'], **request)
            else:
                result = writer.op_loop_close_inspect_admitted_owner_anchor(
                    anchor, expected_row_digest=body['expected_row_digest'], **request)
            return {'version': 1, 'status': 'committed', 'anchor_key': result['anchor_key'],
                    'post_digest': result['post_digest']}
        except writer.OwnerLoopClosePreEffectRefusalError as error:
            if body['operation'] == 'close_inspect':
                raise UnavailableError('owner close receipt unavailable') from error
            return {'version': 1, 'status': 'rejected', 'code': error.code}
    if body['operation'] == 'prepare':
        return {'version': 1, 'status': 'prepared', 'zone': zone}
    writer = _load_writer()
    if body['operation'] == 'write':
        try:
            result = writer.op_loop_add_admitted_owner_text(
                body['text'], owner_text_span=body['text'], owner_event_id=body['event_id'], now=when)
        except writer.OwnerLoopPreEffectRefusalError as error:
            return {'version': 1, 'status': 'rejected', 'code': error.code}
        return {'version': 1, 'status': 'committed', 'anchor_key': result['anchor_key']}
    cr = writer._load_continuity()
    with cr._ledger_lock():
        protected_path = Path(writer.__file__).resolve().parents[1] / 'lib' / 'protected_ledger_io.py'
        spec = importlib.util.spec_from_file_location('sotto_owner_loop_protected', protected_path)
        protected = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(protected)
        rows = protected.load_protected_entries(cr._data_root(), strict=True)
        matches = [row for row in rows if row.get('owner_event_id') == body['event_id']]
        if len(matches) != 1:
            raise UnavailableError('owner row unavailable')
        row = matches[0]
        kind, sources = writer.classify_ledger_sources(row)
        if kind != 'owner' or sources or row.get('summary') != body['text'] or not isinstance(row.get('anchor_key'), str):
            raise UnavailableError('owner row unavailable')
        cr._fsync_ledger_directory_tree()
        return {'version': 1, 'status': 'committed', 'anchor_key': row['anchor_key']}
