"""Offline private activation-state contract; no channel, credentials, or server."""
import copy
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path
import sys
import urllib.error

import pytest

SPEC = importlib.util.spec_from_file_location('sotto_activation_state_client', Path(__file__).with_name('client.py'))
client = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = client
SPEC.loader.exec_module(client)
TOKEN = 'synthetic-private-bearer-000000000000000'
OWNER = '+15555550123'
LINE = '+15555550999'
GUID = 'iMessage;-;+15555550123'
PROJECT = 'synthetic-project'


class Opener:
    def __init__(self, raw):
        self.raw = raw
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.raw, Exception):
            raise self.raw
        return io.BytesIO(self.raw if isinstance(self.raw, bytes) else json.dumps(self.raw).encode())


def config(*, first_contact=False):
    value = {'channel': 'photon', 'owner': {'accountId': 'fixture-account', 'ownerId': OWNER},
             'lineKey': 'fixture-line', 'target': 'photon', 'bearerToken': TOKEN,
             'credentials': {'mode': 'cloud', 'photonProjectId': PROJECT,
                             'photonProjectSecret': 'synthetic-secret'}}
    if first_contact:
        value['firstContact'] = {'mode': 'owner_dm', 'expectedProviderLine': LINE}
    else:
        value['providerThreadId'] = f'imessage:{GUID}~{LINE}'
    return value


def response(expected, status, *, changes=None):
    identity, fields = client._expected_configured_channel(expected)
    configured = {'version': 1, 'identity': identity,
                  'credential_proof': hmac.new(TOKEN.encode(), client._identity_transcript(fields),
                                               hashlib.sha256).hexdigest()}
    mode = identity['route']['mode']
    activation = {'status': status, 'mode': mode}
    if status != 'absent':
        activation.update(binding={
            'tenant_id': '',
            'project_fingerprint': hashlib.sha256(('sotto-photon-project-v1:' + PROJECT).encode()).hexdigest(),
            'owner_id': OWNER, 'account_id': 'fixture-account', 'line_key': 'fixture-line',
            'provider_thread_id': f'imessage:{GUID}~{LINE}'},
            event_id='a' * 64, admitted_at=1_780_000_000_000)
        if status == 'acknowledged':
            activation['acknowledged_at'] = 1_780_000_000_001
    if changes:
        changes(configured, activation)
    return {'version': 1, 'configured': configured, 'activation': activation}


def read(expected, value):
    opener = Opener(value)
    service = client.ChatClient('http://127.0.0.1:8789', TOKEN, timeout=3, opener=opener)
    result = service.activation_state(expected)
    request, timeout = opener.requests[0]
    assert request.full_url == 'http://127.0.0.1:8789/v1/activation-state'
    assert json.loads(request.data) == {}
    assert request.get_header('Authorization') == f'Bearer {TOKEN}'
    assert timeout == 3
    return result


@pytest.mark.parametrize('first_contact', [False, True])
@pytest.mark.parametrize('status', ['absent', 'pending', 'acknowledged'])
def test_exact_configured_identity_and_durable_status_are_distinct(first_contact, status):
    expected = config(first_contact=first_contact)
    result = read(expected, response(expected, status))
    assert result['activation']['status'] == status
    assert result['activation']['mode'] == ('first_contact' if first_contact else 'fixed')
    assert 'binding' not in result['activation'] if status == 'absent' else 'binding' in result['activation']


@pytest.mark.parametrize('change', [
    lambda configured, _: configured.update(credential_proof='0' * 64),
    lambda configured, _: configured['identity'].update(owner_id='+15555550998'),
    lambda _, activation: activation.update(mode='first_contact'),
    lambda _, activation: activation['binding'].update(project_fingerprint='0' * 64),
    lambda _, activation: activation['binding'].update(owner_id='+15555550998'),
    lambda _, activation: activation['binding'].update(provider_thread_id='imessage:other;-;guid~+15555550999'),
    lambda _, activation: activation.update(admitted_at=True),
    lambda _, activation: activation.update(acknowledged_at=1),
    lambda _, activation: activation.update(extra='unexpected'),
])
def test_mismatch_or_ambiguous_ack_fails_closed(change):
    expected = config()
    with pytest.raises(RuntimeError, match='activation state unavailable or mismatched'):
        read(expected, response(expected, 'pending', changes=change))


def test_first_contact_requires_exact_line_and_real_binding():
    expected = config(first_contact=True)
    with pytest.raises(RuntimeError):
        read(expected, response(expected, 'acknowledged', changes=lambda _, activation:
             activation['binding'].update(provider_thread_id=f'imessage:{GUID}~+15555550000')))


@pytest.mark.parametrize('raw', [
    b'{"version":1,"version":1,"configured":{},"activation":{}}',
    b'{"version":NaN,"configured":{},"activation":{}}',
    b'{' + b' ' * 9000 + b'}',
])
def test_duplicate_nonfinite_or_oversize_response_fails_closed(raw):
    with pytest.raises(RuntimeError, match='activation state unavailable or mismatched'):
        read(config(), raw)


def test_http_refusal_and_wrong_local_bearer_fail_closed():
    error = urllib.error.HTTPError('http://127.0.0.1:8789/v1/activation-state', 501,
                                   'unsupported', {}, None)
    with pytest.raises(RuntimeError):
        read(config(), error)
    expected = config()
    expected['bearerToken'] = 'different-bearer-000000000000000000000'
    with pytest.raises(RuntimeError):
        read(expected, response(config(), 'acknowledged'))


def test_runtime_adapter_forwards_explicit_config_with_bounded_timeout(monkeypatch):
    api_spec = importlib.util.spec_from_file_location(
        'sotto_activation_state_api', Path(__file__).with_name('runtime_api.py'))
    api = importlib.util.module_from_spec(api_spec)
    api_spec.loader.exec_module(api)
    expected = config()
    seen = []
    class FakeClient:
        def activation_state(self, supplied):
            seen.append(supplied)
            return {'version': 1, 'configured': {}, 'activation': {'status': 'absent'}}
    monkeypatch.setattr(api, '_client', lambda timeout=60: seen.append(timeout) or FakeClient())
    assert api.activation_state(expected)['activation']['status'] == 'absent'
    assert seen == [3, expected]


def test_shared_project_ack_requires_exact_shared_thread_and_bound_mode():
    expected = config(first_contact=True)
    expected['firstContact'] = {'mode': 'owner_dm_shared_project'}
    value = response(expected, 'acknowledged')
    value['activation']['binding']['provider_thread_id'] = f'imessage:{GUID}~shared'
    assert read(expected, value)['activation']['mode'] == 'first_contact_shared_project'
    identity, fields = client._expected_configured_channel(expected)
    assert identity['route'] == {'mode': 'first_contact_shared_project'}
    assert fields[7] == 'shared_project'
    for field, replacement in [('provider_thread_id', f'imessage:{GUID}~{LINE}'),
                               ('owner_id', '+15555550000'), ('project_fingerprint', 'b' * 64)]:
        changed = copy.deepcopy(value)
        changed['activation']['binding'][field] = replacement
        with pytest.raises(RuntimeError):
            read(expected, changed)
    changed = copy.deepcopy(value)
    changed['activation']['mode'] = 'first_contact'
    with pytest.raises(RuntimeError):
        read(expected, changed)
