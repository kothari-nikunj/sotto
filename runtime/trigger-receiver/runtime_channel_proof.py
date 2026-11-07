"""Crosscheck of current Photon ACK, receipt, expected configuration and zone.

This proves a channel binding only. A disabled native stage does not authorize a
routine epoch, profile, claimed slot, or worker. Never install this function as
receiver._ROUTINE_AUTHORITY_FOR_JOB or treat its result as a send grant.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import runtime_activation


class ChannelProofUnavailableError(ValueError):
    """The current independent proofs do not agree."""


_PHONE = re.compile(r'\A\+[1-9][0-9]{7,14}\Z')


_RETAINED_PROOF = None


def retained_proof_module():
    """Load the ONE Python route transcript (`retained_brief_proof.route_digest`) by package path."""
    global _RETAINED_PROOF
    if _RETAINED_PROOF is None:
        here = Path(__file__).resolve()
        candidates = (here.parent.parent / 'sotto-skills' / '_shared' / 'lib' / 'retained_brief_proof.py',
                      here.parents[2] / 'sotto-chief-of-staff' / '_shared' / 'lib' / 'retained_brief_proof.py')
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        spec = importlib.util.spec_from_file_location('sotto_route_digest', path) if path else None
        if spec is None or spec.loader is None:
            raise ChannelProofUnavailableError('route digest implementation unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RETAINED_PROOF = module
    return _RETAINED_PROOF


def _route_digest(expectation: dict, provider_thread_id: str) -> str:
    """The public Node v1 seven-field transcript, with Node's own input validation."""
    try:
        return retained_proof_module().route_digest(
            'photon', expectation['tenantId'], expectation['projectFingerprint'],
            expectation['owner']['ownerId'], expectation['owner']['accountId'],
            expectation['lineKey'], provider_thread_id)
    except (ValueError, TypeError) as error:
        raise ChannelProofUnavailableError('route identity is invalid') from error


def _startup_module():
    """Load the one canonical renderer/strict projection reader by package path."""
    candidates = (Path(__file__).resolve().parents[2] / 'adapters' / 'sotto' / 'startup_config.py',
                  Path('/app/adapters/sotto/startup_config.py'))
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise ChannelProofUnavailableError('receiver expectation loader unavailable')
    spec = importlib.util.spec_from_file_location('sotto_startup_config_proof', path)
    if spec is None or spec.loader is None:
        raise ChannelProofUnavailableError('receiver expectation loader unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_config(expectation: dict, policy: dict) -> dict:
    """Reconstruct only the credential/identity fields Node authenticates."""
    project = policy.get('PHOTON_PROJECT_ID')
    secret = policy.get('PHOTON_PROJECT_SECRET')
    if not isinstance(project, str) or not isinstance(secret, str):
        raise ChannelProofUnavailableError('receiver Cloud identity unavailable')
    fingerprint = hashlib.sha256(('sotto-photon-project-v1:' + project).encode()).hexdigest()
    if expectation['projectFingerprint'] != fingerprint:
        raise ChannelProofUnavailableError('receiver Cloud project changed')
    if (expectation['owner']['ownerId'] != policy.get('PHOTON_HOME_CHANNEL')
            or expectation['tenantId'] != policy.get('SOTTO_TENANT_ID', '')):
        raise ChannelProofUnavailableError('receiver owner policy differs')
    config = {
        'channel': 'photon', 'owner': expectation['owner'],
        'lineKey': expectation['lineKey'], 'target': 'photon',
        'port': expectation['port'],
        'bearerToken': policy.get('SOTTO_CHAT_RUNTIME_TOKEN'),
        'credentials': {'mode': 'cloud', 'photonProjectId': project,
                        'photonProjectSecret': secret},
    }
    if expectation['tenantId']:
        config['tenantId'] = expectation['tenantId']
    route = expectation['route']
    if route['mode'] == 'fixed':
        config['providerThreadId'] = route['providerThreadId']
    elif route['mode'] == 'first_contact_shared_project':
        config['firstContact'] = {'mode': 'owner_dm_shared_project'}
    else:
        config['firstContact'] = {'mode': 'owner_dm',
                                  'expectedProviderLine': route['expectedProviderLine']}
    return config


def _matching_state(state: object, config: dict, route: dict, policy) -> tuple[str, str, str]:
    """The ChatClient has already checked the HMAC and strict activation schema."""
    if not isinstance(state, dict) or set(state) != {'version', 'configured', 'activation'}:
        raise ChannelProofUnavailableError('activation state unavailable')
    activation = state['activation']
    configured = state['configured']
    if (type(state['version']) is not int or state['version'] != 1
            or not isinstance(activation, dict) or activation.get('status') != 'acknowledged'
            or not isinstance(configured, dict) or not isinstance(configured.get('identity'), dict)):
        raise ChannelProofUnavailableError('activation is not acknowledged')
    identity = configured['identity']
    binding = activation.get('binding')
    if not isinstance(binding, dict):
        raise ChannelProofUnavailableError('activation binding unavailable')
    owner = config.get('owner')
    if (not isinstance(owner, dict)
            or identity.get('owner_id') != owner.get('ownerId')
            or identity.get('account_id') != owner.get('accountId')
            or identity.get('line_key') != config.get('lineKey')):
        raise ChannelProofUnavailableError('receiver and Node owner policy differ')
    thread = binding.get('provider_thread_id')
    if not isinstance(thread, str) or not thread.startswith('imessage:'):
        raise ChannelProofUnavailableError('activation thread unavailable')
    guid, delimiter, line = thread[len('imessage:'):].partition('~')
    if (not delimiter or (line != 'shared' if identity.get('route', {}).get('mode') == 'first_contact_shared_project' else not _PHONE.fullmatch(line)) or
            route != {'platform': 'imessage', 'chat_id': guid, 'thread_id': None}):
        raise ChannelProofUnavailableError('activation route differs from current receipt')
    if identity.get('route', {}).get('mode') == 'fixed':
        if config.get('providerThreadId') != thread:
            raise ChannelProofUnavailableError('fixed route differs from current binding')
    elif identity.get('route', {}).get('mode') == 'first_contact':
        first = config.get('firstContact')
        if not isinstance(first, dict) or first.get('expectedProviderLine') != line:
            raise ChannelProofUnavailableError('first-contact line differs from current binding')
    elif identity.get('route', {}) == {'mode': 'first_contact_shared_project'}:
        if config.get('firstContact') != {'mode': 'owner_dm_shared_project'}:
            raise ChannelProofUnavailableError('shared-project mode differs from current binding')
    else:
        raise ChannelProofUnavailableError('activation mode unavailable')
    event_id = activation.get('event_id')
    if not isinstance(event_id, str) or re.fullmatch(r'[0-9a-f]{64}', event_id) is None:
        raise ChannelProofUnavailableError('activation event unavailable')
    return event_id, guid, line


def check_current_channel(*, chat_client, expected_channel_path: str | Path,
                          receiver_data_root: str | Path, receiver_policy,
                          configured_zone_name, expected_zone: str) -> dict:
    """Check receiver-owned expectation, live Node ACK, current receipt and zone twice.

    The trusted receiver must construct chat_client using its current private
    SOTTO_CHAT_RUNTIME_TOKEN. This function does not read the Node UID's config.
    """
    try:
        startup = _startup_module()
        expectation = startup.read_receiver_channel_expectation(expected_channel_path)
        config = _expected_config(expectation, receiver_policy)
        zone = configured_zone_name()
        if not isinstance(zone, str) or not zone or zone != expected_zone:
            raise ChannelProofUnavailableError('scheduler zone differs from receiver')
        ZoneInfo(zone)
        endpoint = f"http://127.0.0.1:{expectation['port']}"
        if (getattr(chat_client, 'url', None) != endpoint
                or receiver_policy.get('SOTTO_CHAT_RUNTIME_URL') != endpoint
                or not isinstance(receiver_policy.get('SOTTO_CHAT_RUNTIME_TOKEN'), str)):
            raise ChannelProofUnavailableError('runtime endpoint differs from receiver')
        route = runtime_activation.verified_current_photon_route(
            receiver_data_root, receiver_policy)
        state = chat_client.activation_state(config)
        event_id, guid, line = _matching_state(state, config, route, receiver_policy)
        next_expectation = startup.read_receiver_channel_expectation(expected_channel_path)
        next_config = _expected_config(next_expectation, receiver_policy)
        next_route = runtime_activation.verified_current_photon_route(
            receiver_data_root, receiver_policy)
        next_state = chat_client.activation_state(next_config)
        if (expectation != next_expectation or route != next_route or state != next_state
                or configured_zone_name() != zone):
            raise ChannelProofUnavailableError('channel or zone changed during crosscheck')
        return {
            'version': 1, 'status': 'current_channel_proved', 'action_authorized': False,
            'activation_event_id': event_id, 'owner_route': route,
            'provider_thread_id': f'imessage:{guid}~{line}', 'schedule_zone': zone,
            'route_digest': _route_digest(expectation, f'imessage:{guid}~{line}'),
            'tenant_id': expectation['tenantId'],
            'project_fingerprint': expectation['projectFingerprint'],
            'owner_id': expectation['owner']['ownerId'],
            'account_id': expectation['owner']['accountId'],
            'line_key': expectation['lineKey'],
        }
    except (RuntimeError, OSError, ValueError, TypeError, UnicodeError,
            ZoneInfoNotFoundError) as error:
        if isinstance(error, ChannelProofUnavailableError):
            raise
        raise ChannelProofUnavailableError('channel proof unavailable') from error
