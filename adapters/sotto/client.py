"""Credential-explicit loopback client for the experimental Sotto conversation runtime.

This module does not select the production runtime or discover owner credentials.
Its caller supplies the private loopback bearer; no Keychain, proxy, redirect or
provider fallback is consulted. Provider acceptance never means device display.
"""
import base64
import errno
import hashlib
import hmac
import json
import re
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class CallResult:
    ok: bool
    acceptance: str
    value: dict


class ModelAdmissionBusyError(RuntimeError):
    """The local runtime proved that its proxy refused an unbilled model attempt."""


_IDENTITY_DOMAIN = b'sotto-configured-channel-identity-v1\x00'
_HEX64 = re.compile(r'[a-f0-9]{64}\Z')


def _identity_text(value, *, empty=False, limit=512):
    if (not isinstance(value, str) or (not value and not empty) or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in value)):
        raise ValueError('configured channel identity is invalid')
    try:
        encoded = value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError('configured channel identity is invalid') from error
    if len(encoded) > 4096:
        raise ValueError('configured channel identity is invalid')
    return value


def _identity_transcript(fields):
    if not isinstance(fields, (list, tuple)) or len(fields) > 32:
        raise ValueError('configured channel identity is invalid')
    chunks = [_IDENTITY_DOMAIN, struct.pack('>I', len(fields))]
    for value in fields:
        encoded = _identity_text(value, empty=True, limit=4096).encode('utf-8')
        chunks.extend((struct.pack('>I', len(encoded)), encoded))
    return b''.join(chunks)


def _expected_configured_channel(config):
    """Exact identity fields from an explicitly supplied, already rendered Node config."""
    if not isinstance(config, dict) or config.get('channel') not in ('telegram', 'photon'):
        raise ValueError('configured channel identity is invalid')
    channel = config['channel']
    owner = config.get('owner')
    if not isinstance(owner, dict) or set(owner) != {'accountId', 'ownerId'}:
        raise ValueError('configured channel identity is invalid')
    identity = {
        'channel': channel,
        'tenant_id': _identity_text(config.get('tenantId', ''), empty='tenantId' not in config),
        'account_id': _identity_text(owner['accountId']),
        'owner_id': _identity_text(owner['ownerId']),
        'line_key': _identity_text(config.get('lineKey')),
        'target': _identity_text(config.get('target')),
    }
    first = config.get('firstContact')
    fixed = config.get('providerThreadId')
    if 'firstContact' in config and first is None:
        raise ValueError('configured channel identity is invalid')
    if (first is None) == (fixed is None):
        raise ValueError('configured channel identity is invalid')
    if first is not None:
        if channel != 'photon':
            raise ValueError('configured channel identity is invalid')
        if first == {'mode': 'owner_dm_shared_project'}:
            route = {'mode': 'first_contact_shared_project'}
        else:
            if (not isinstance(first, dict) or set(first) != {'mode', 'expectedProviderLine'}
                    or first['mode'] != 'owner_dm'):
                raise ValueError('configured channel identity is invalid')
            route = {'mode': 'first_contact',
                     'expected_provider_line': _identity_text(first['expectedProviderLine'])}
    else:
        route = {'mode': 'fixed', 'provider_thread_id': _identity_text(fixed)}
    identity['route'] = route
    credentials = config.get('credentials')
    if not isinstance(credentials, dict):
        raise TypeError('configured channel identity is invalid')
    if channel == 'telegram':
        if set(credentials) != {'telegramToken', 'telegramLinkFile'}:
            raise ValueError('configured channel identity is invalid')
        link = credentials['telegramLinkFile']
        if not isinstance(link, dict) or set(link) != {'bot_token', 'allowed_user'}:
            raise ValueError('configured channel identity is invalid')
        allowed = link['allowed_user']
        if type(allowed) is int:
            if not 0 < allowed <= 9007199254740991:
                raise ValueError('configured channel identity is invalid')
            allowed = str(allowed)
        elif not isinstance(allowed, str):
            raise ValueError('configured channel identity is invalid')
        credentials_fields = ['telegram', _identity_text(credentials['telegramToken'], limit=4096),
                              _identity_text(link['bot_token'], limit=4096), _identity_text(allowed)]
    elif credentials.get('mode') == 'cloud':
        if set(credentials) != {'mode', 'photonProjectId', 'photonProjectSecret'}:
            raise ValueError('configured channel identity is invalid')
        credentials_fields = ['cloud', _identity_text(credentials['photonProjectId'], limit=4096),
                              _identity_text(credentials['photonProjectSecret'], limit=4096)]
    elif credentials.get('mode') == 'self_host':
        if route['mode'] == 'first_contact_shared_project':
            raise ValueError('shared-project identity requires Cloud credentials')
        if set(credentials) != {'mode', 'photonServerUrl', 'photonApiKey'}:
            raise ValueError('configured channel identity is invalid')
        credentials_fields = ['self_host', _identity_text(credentials['photonServerUrl'], limit=4096),
                              _identity_text(credentials['photonApiKey'], limit=4096)]
    else:
        raise ValueError('configured channel identity is invalid')
    fields = [identity['channel'], identity['tenant_id'], identity['account_id'],
              identity['owner_id'], identity['line_key'], identity['target'], route['mode'],
              'shared_project' if route['mode'] == 'first_contact_shared_project' else
              route.get('provider_thread_id', route.get('expected_provider_line')),
              *credentials_fields]
    return identity, fields


def _validate_configured_channel_response(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'identity', 'credential_proof'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['credential_proof'], str)
            or _HEX64.fullmatch(value['credential_proof']) is None):
        raise ValueError('configured channel identity response is invalid')
    identity = value['identity']
    if (not isinstance(identity, dict)
            or set(identity) != {'channel', 'tenant_id', 'account_id', 'owner_id', 'line_key', 'target', 'route'}
            or identity['channel'] not in ('telegram', 'photon')):
        raise ValueError('configured channel identity response is invalid')
    for name in ('tenant_id', 'account_id', 'owner_id', 'line_key', 'target'):
        _identity_text(identity[name], empty=name == 'tenant_id')
    route = identity['route']
    if not isinstance(route, dict):
        raise TypeError('configured channel identity response is invalid')
    if route.get('mode') == 'fixed':
        if set(route) != {'mode', 'provider_thread_id'}:
            raise ValueError('configured channel identity response is invalid')
        _identity_text(route['provider_thread_id'])
    elif route.get('mode') == 'first_contact':
        if identity['channel'] != 'photon' or set(route) != {'mode', 'expected_provider_line'}:
            raise ValueError('configured channel identity response is invalid')
        _identity_text(route['expected_provider_line'])
    elif route == {'mode': 'first_contact_shared_project'} and identity['channel'] == 'photon':
        pass
    else:
        raise ValueError('configured channel identity response is invalid')
    return identity, value['credential_proof']


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate chat response field')
        value[key] = item
    return value


def _reject_nonfinite(_value):
    raise ValueError('nonfinite chat response')


def _activation_text(value, *, empty=False):
    if (not isinstance(value, str) or (not value and not empty) or
            len(value.encode('utf-16-le')) // 2 > 512 or
            any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                for char in value)):
        raise ValueError('invalid activation state')
    return value


def _activation_binding(value, configured, expected_project):
    fields = {'tenant_id', 'project_fingerprint', 'owner_id', 'account_id',
              'line_key', 'provider_thread_id'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid activation state')
    for key in fields - {'project_fingerprint'}:
        _activation_text(value[key], empty=key == 'tenant_id')
    if (not isinstance(value['project_fingerprint'], str) or
            _HEX64.fullmatch(value['project_fingerprint']) is None or
            not hmac.compare_digest(value['project_fingerprint'], expected_project) or
            any(value[key] != configured[key] for key in
                ('tenant_id', 'owner_id', 'account_id', 'line_key'))):
        raise ValueError('activation state owner mismatch')
    thread = value['provider_thread_id']
    route = configured['route']
    if not thread.startswith('imessage:'):
        raise ValueError('activation state destination mismatch')
    guid, separator, line = thread[len('imessage:'):].partition('~')
    if (not guid or ';-;' not in guid or len(guid) > 400 or
            (separator and (not line or '~' in line))):
        raise ValueError('activation state destination mismatch')
    if route['mode'] == 'fixed':
        if thread != route['provider_thread_id']:
            raise ValueError('activation state destination mismatch')
    else:
        if not separator or line != ('shared' if route['mode'] == 'first_contact_shared_project'
                                     else route['expected_provider_line']):
            raise ValueError('activation state destination mismatch')
    return value


def _activation_admission(value, configured, expected_project):
    if not isinstance(value, dict) or value.get('mode') != configured['route']['mode']:
        raise ValueError('invalid activation state')
    status = value.get('status')
    if status == 'absent':
        if set(value) != {'status', 'mode'}:
            raise ValueError('invalid activation state')
        return value
    fields = {'status', 'mode', 'binding', 'event_id', 'admitted_at'}
    if status == 'acknowledged':
        fields.add('acknowledged_at')
    elif status != 'pending':
        raise ValueError('invalid activation state')
    if set(value) != fields:
        raise ValueError('invalid activation state')
    _activation_binding(value['binding'], configured, expected_project)
    if (not isinstance(value['event_id'], str) or _HEX64.fullmatch(value['event_id']) is None or
            type(value['admitted_at']) is not int or not 0 <= value['admitted_at'] <= 2**53 - 1):
        raise ValueError('invalid activation state')
    if status == 'acknowledged' and (type(value['acknowledged_at']) is not int or
                                     not value['admitted_at'] <= value['acknowledged_at'] <= 2**53 - 1):
        raise ValueError('invalid activation state')
    return value


def _clean_receipt(value):
    if not isinstance(value, dict):
        return {'acceptance': 'unknown'}
    state = value.get('acceptance')
    if state == 'in_flight' or state not in ('accepted', 'unknown', 'rejected', 'not_attempted'):
        state = 'unknown'
    ids = value.get('message_ids', value.get('provider_message_ids', []))
    ids = [item for item in ids[:8] if isinstance(item, str) and 0 < len(item) <= 512] if isinstance(ids, list) else []
    identifier = value.get('message_id') or (ids[0] if ids else None)
    if state == 'accepted' and (not isinstance(identifier, str) or not 0 < len(identifier) <= 512):
        state = 'unknown'
    receipt = {'acceptance': state}
    if state == 'accepted':
        receipt['message_id'] = identifier
        receipt['message_ids'] = ids or [identifier]
        route_digest = value.get('route_digest')
        payload_hash = value.get('payload_hash')
        if (type(value.get('route_proof_version')) is int
                and value['route_proof_version'] == 1
                and isinstance(route_digest, str)
                and re.fullmatch(r'[a-f0-9]{64}', route_digest)
                and isinstance(payload_hash, str)
                and re.fullmatch(r'[a-f0-9]{64}', payload_hash)):
            receipt['route_proof_version'] = 1
            receipt['route_digest'] = route_digest
            receipt['payload_hash'] = payload_hash
    return receipt


class ChatClient:
    """The receiver owns policy and retry decisions; repeated IDs stay repeated IDs."""

    def __init__(self, url, token, *, timeout=60, opener=None):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('chat service must use an explicit IPv4 loopback port')
        if not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError('private chat bearer required')
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
            raise ValueError('invalid timeout')
        self.url = url.rstrip('/')
        self._token = token
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __repr__(self):
        return f'ChatClient(url={self.url!r}, token=<redacted>)'

    def call(self, path, payload=None, *, response_limit=1_048_576, strict_json=False):
        if path not in ('send', 'send-gallery', 'send-gallery-bytes', 'receipt', 'run', 'sessions', 'retention', 'health',
                        'activation-evidence', 'activation-ack', 'configured-identity',
                        'activation-state'):
            raise ValueError('unsupported chat operation')
        if type(response_limit) is not int or not 0 < response_limit <= 1_048_576:
            raise ValueError('invalid chat response bound')
        request = urllib.request.Request(
            f'{self.url}/v1/{path}',
            data=None if path == 'health' else json.dumps(payload).encode(),
            headers={'Authorization': f'Bearer {self._token}', 'Content-Type': 'application/json'},
            method='GET' if path == 'health' else 'POST',
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(response_limit + 1)
                if len(raw) > response_limit:
                    return CallResult(False, 'unknown', {})
                value = (json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
                         if strict_json else json.loads(raw))
                if not isinstance(value, dict):
                    return CallResult(False, 'unknown', {})
                return CallResult(True, 'accepted', value)
        except urllib.error.HTTPError as error:
            if (path == 'run' and error.code == 429
                    and error.headers.get('X-Sotto-Admission') == 'busy'):
                raise ModelAdmissionBusyError('local model admission busy') from None
            # These statuses prove validation/authentication refused service execution.
            # Never follow a redirect with the private bearer or mistake a 5xx for no send.
            state = 'not_attempted' if error.code in (400, 401, 403, 404, 405, 413, 415) else 'unknown'
            return CallResult(False, state, {})
        except urllib.error.URLError as error:
            reason = error.reason
            # A refused TCP connection cannot have reached the provider; a lost response can.
            state = 'not_attempted' if isinstance(reason, ConnectionRefusedError) or getattr(reason, 'errno', None) == errno.ECONNREFUSED else 'unknown'
            return CallResult(False, state, {})
        except (OSError, TimeoutError, ValueError):
            return CallResult(False, 'unknown', {})

    def send_receipt(self, body, target, dispatch_id, *, attempt=1, source_revision=None):
        request = {'dispatch_id': dispatch_id, 'target': target, 'text': body, 'attempt': attempt}
        if source_revision is not None:
            if not isinstance(source_revision, str) or re.fullmatch(r'[a-f0-9]{64}', source_revision) is None:
                raise ValueError('invalid delivery source revision')
            request['source_revision'] = source_revision
        result = self.call('send', request)
        receipt = _clean_receipt(result.value) if result.ok else {'acceptance': result.acceptance}
        receipt['target'] = target
        accepted = receipt['acceptance'] == 'accepted'
        return accepted, '' if accepted else 'chat transport acceptance unconfirmed', receipt

    def send_gallery(self, paths, summary, target, dispatch_id):
        result = self.call('send-gallery', {'dispatch_id': dispatch_id, 'target': target, 'paths': paths, 'summary': summary})
        receipt = _clean_receipt(result.value) if result.ok else {'acceptance': result.acceptance}
        accepted = receipt['acceptance'] == 'accepted'
        return accepted, '' if accepted else 'gallery acceptance unconfirmed', receipt

    def send_gallery_bytes(self, images, summary, target, dispatch_id):
        """Transfer exact receiver-read bytes; no Node access to receiver private paths."""
        if (not isinstance(images, list) or len(images) != 4 or
                any(not isinstance(image, dict) or set(image) != {'name', 'bytes'} or
                    image['name'] != f'{index:02d}.png' or
                    not isinstance(image['bytes'], bytes) or
                    not 0 < len(image['bytes']) <= 5_000_000
                    for index, image in enumerate(images, 1))):
            return False, 'gallery files unavailable', {'acceptance': 'not_attempted'}
        payload = {'dispatch_id': dispatch_id, 'target': target, 'summary': summary,
                   'images': [{'name': image['name'], 'base64': base64.b64encode(
                       image['bytes']).decode('ascii')} for image in images]}
        result = self.call('send-gallery-bytes', payload)
        receipt = _clean_receipt(result.value) if result.ok else {'acceptance': result.acceptance}
        accepted = receipt['acceptance'] == 'accepted'
        return accepted, '' if accepted else 'gallery acceptance unconfirmed', receipt

    def receipt(self, dispatch_id):
        result = self.call('receipt', {'dispatch_id': dispatch_id})
        if not result.ok or result.value.get('found') is not True:
            return None
        return _clean_receipt(result.value.get('receipt'))

    def run(self, prompt, run_id, *, skill=None, answer_only=False):
        payload = {'prompt': prompt, 'env': {'SOTTO_UNATTENDED': '1', 'SOTTO_DELIVERY_RUN_ID': run_id}}
        if answer_only:
            payload['answer_only'] = True
        if skill is not None:
            payload['skill'] = skill
        result = self.call('run', payload)
        if not result.ok or not isinstance(result.value.get('text'), str):
            raise RuntimeError('chat unattended run failed')
        return result.value

    def sessions(self, action, *, before=None):
        if action not in {'list', 'rotate', 'purge_before'}:
            raise ValueError('Unsupported chat session operation')
        payload = {'action': action}
        if action == 'purge_before':
            if not isinstance(before, str) or not before:
                raise ValueError('Session purge cutoff is required')
            payload['before'] = before
        elif before is not None:
            raise ValueError('Only session purge accepts a cutoff')
        result = self.call('sessions', payload)
        if not result.ok:
            raise RuntimeError('chat session operation failed')
        return result.value

    def activation_evidence(self):
        result = self.call('activation-evidence', {})
        if (not result.ok or set(result.value) != {'version', 'pending'}
                or type(result.value['version']) is not int or result.value['version'] != 1
                or (result.value['pending'] is not None and not isinstance(result.value['pending'], dict))):
            raise RuntimeError('chat activation evidence unavailable')
        return result.value['pending']

    def activation_ack(self, event_id):
        if not isinstance(event_id, str) or re.fullmatch(r'[a-f0-9]{64}', event_id) is None:
            raise ValueError('invalid activation event identity')
        result = self.call('activation-ack', {'event_id': event_id})
        if (not result.ok or set(result.value) != {'acknowledged'}
                or type(result.value['acknowledged']) is not bool):
            raise RuntimeError('chat activation acknowledgement unavailable')
        return result.value['acknowledged']

    def verify_configured_identity(self, expected_config):
        """Verify loaded channel identity/credential against an explicit rendered config.

        This proves only local configuration agreement. It says nothing about provider
        acceptance, activation ACK, model policy, source grants, or full startup parity.
        """
        try:
            result = self.call('configured-identity', {}, response_limit=8192)
            if not result.ok:
                raise ValueError('configured channel identity unavailable')
            self._verify_configured_payload(expected_config, result.value)
            return True
        except (ValueError, TypeError, UnicodeError) as error:
            raise RuntimeError('configured channel identity unavailable or mismatched') from error

    def _verify_configured_payload(self, expected_config, value):
        if not isinstance(expected_config, dict):
            raise TypeError('configured channel identity is invalid')
        expected_bearer = _identity_text(expected_config.get('bearerToken'), limit=4096)
        if (len(expected_bearer) < 32 or any(char.isspace() for char in expected_bearer)
                or not hmac.compare_digest(expected_bearer.encode('utf-8'), self._token.encode('utf-8'))):
            raise ValueError('configured channel identity is invalid')
        identity, fields = _expected_configured_channel(expected_config)
        observed, proof = _validate_configured_channel_response(value)
        expected_proof = hmac.new(self._token.encode('utf-8'), _identity_transcript(fields),
                                  hashlib.sha256).hexdigest()
        if observed != identity or not hmac.compare_digest(proof, expected_proof):
            raise ValueError('configured channel identity mismatch')
        return identity

    def activation_state(self, expected_config):
        """Read one authenticated configured identity plus durable admission state.

        `absent` and `pending` grant no activation. Even `acknowledged` proves only
        Node's journal/ACK facts; the receiver must independently verify its
        current owner policy and sole Photon activation receipt before use.
        """
        try:
            result = self.call('activation-state', {}, response_limit=8192, strict_json=True)
            if (not result.ok or set(result.value) != {'version', 'configured', 'activation'} or
                    type(result.value['version']) is not int or result.value['version'] != 1):
                raise ValueError('activation state unavailable')
            configured = self._verify_configured_payload(expected_config, result.value['configured'])
            if configured['channel'] != 'photon' or configured['target'] != 'photon':
                raise ValueError('activation state requires Photon')
            credentials = expected_config.get('credentials')
            if not isinstance(credentials, dict) or credentials.get('mode') != 'cloud':
                raise ValueError('activation state requires Cloud project identity')
            project = _identity_text(credentials.get('photonProjectId'), limit=4096)
            fingerprint = hashlib.sha256(('sotto-photon-project-v1:' + project).encode()).hexdigest()
            _activation_admission(result.value['activation'], configured, fingerprint)
            return result.value
        except (ValueError, TypeError, UnicodeError, KeyError) as error:
            raise RuntimeError('chat activation state unavailable or mismatched') from error
