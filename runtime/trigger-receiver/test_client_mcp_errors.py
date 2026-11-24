"""A failed client read says why, from a closed set, without private content (audit H5/R2)."""
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError

import client_mcp as mcp
import managed
import client_mcp_backend as backend
from client_mcp_catalog import READ_FAILURES, ReadUnavailableError

REVISION = 'a' * 64
NOW = 1793660000
INVALID_PARAMS = -32602
INVALID_REQUEST = -32600
_SPEC = importlib.util.spec_from_file_location(
    'client_mcp_errors_google_api', Path(__file__).parents[2] / 'adapters/sotto/google_api.py')
google_api = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(google_api)


@pytest.fixture
def grant(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp.time, 'time', lambda: NOW)
    return mcp.create_grant(str(tmp_path), 'fixture-client', list(mcp.TOOLS), 1)[1]


def call(grant, backend_fn, name='sotto_email_search', arguments=None):
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': name, 'arguments': arguments or {'query': 'Cedar'}}}
    return mcp.dispatch(request, grant, backend_fn)


def test_the_reason_set_is_closed():
    assert {reason: retry for reason, (retry, _) in READ_FAILURES.items()} == {
        'bridge_offline': True, 'timeout': True, 'busy': True, 'rate_limited': True, 'provider_unavailable': True,
        'source_not_permitted': False, 'invalid_window': False, 'invalid_arguments': False,
        'not_found': False, 'reauth_required': False, 'too_large': False, 'internal': False}
    assert all(action.endswith('.') and action.count('. ') == 0 for _, action in READ_FAILURES.values())
    with pytest.raises(ValueError):
        ReadUnavailableError('PRIVATE reason')


def test_message_history_future_until_is_an_invalid_window_before_the_bridge(grant):
    result = call(grant, lambda *_: pytest.fail('invalid window reached the Bridge'), 'sotto_message_history',
                  {'source': 'imessage', 'since': NOW - 86400, 'until': NOW + 3600})['result']
    assert result['isError'] is True and 'until' in result['content'][0]['text']
    assert result['structuredContent']['reason'] == 'invalid_window'
    assert result['structuredContent']['retryable'] is False


def test_schema_failure_is_a_tool_error_the_model_can_correct(grant):
    result = call(grant, lambda *_: pytest.fail('invalid arguments reached a reader'), 'sotto_email_search',
                  {'query': 'PRIVATE', 'max': 40})
    assert 'error' not in result
    value = result['result']
    assert value['isError'] is True
    assert value['structuredContent'] == {'status': 'unavailable', 'reason': 'invalid_arguments', 'retryable': False,
                                          'detail': 'max: use an integer from 1 to 20'}
    assert 'Correct the arguments' in value['content'][0]['text'] and 'PRIVATE' not in json.dumps(value)


def test_unknown_tool_and_malformed_requests_stay_protocol_errors(grant):
    assert call(grant, lambda *_: pytest.fail('unknown tool read'), 'sotto_send', {})['error']['code'] == INVALID_PARAMS
    assert mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': []},
                        grant, None)['error']['code'] == INVALID_PARAMS
    assert mcp.dispatch({'jsonrpc': '1.0', 'id': 1, 'method': 'tools/call'}, grant, None)['error']['code'] == INVALID_REQUEST


class _Failing:
    """A Google session whose every GET fails one way; the body would be private provider text."""
    def __init__(self, status=None, raises=None):
        self.status, self.raises = status, raises

    def get(self, *_args, **_kwargs):
        if self.raises:
            raise self.raises
        return SimpleNamespace(status_code=self.status, headers={}, close=lambda: None,
                               iter_content=lambda **_: iter([b'PRIVATE provider body']))

    def close(self):
        pass


def _google(factory):
    def read(name, arguments):
        operation = {'sotto_calendar': 'calendar_list', 'sotto_email_get': 'gmail_get'}[name]
        return google_api.read_owner(operation, arguments, factory)
    return read


CALENDAR_BACKWARDS = ('sotto_calendar', {'start': '2026-11-03T00:00:00Z', 'end': '2026-11-02T00:00:00Z'})
CALENDAR = ('sotto_calendar', {'start': '2026-11-02T00:00:00Z', 'end': '2026-11-03T00:00:00Z'})
EMAIL = ('sotto_email_get', {'message_id': '18c0ffee'})


def _missing_credential():
    raise FileNotFoundError('PRIVATE /token/path')


@pytest.mark.parametrize('read,factory,reason,retryable', [
    (CALENDAR_BACKWARDS, lambda: _Failing(raises=AssertionError('PRIVATE window reached Google')), 'invalid_arguments', False),
    (EMAIL, lambda: _Failing(raises=RefreshError('PRIVATE invalid_grant')), 'reauth_required', False),
    (EMAIL, lambda: _Failing(raises=RefreshError('PRIVATE endpoint outage', retryable=True)),
     'provider_unavailable', True),
    (EMAIL, lambda: _Failing(status=401), 'reauth_required', False),
    (CALENDAR, _missing_credential, 'reauth_required', False),
    (EMAIL, lambda: _Failing(status=400), 'invalid_arguments', False),
    (EMAIL, lambda: _Failing(status=404), 'not_found', False),
    (EMAIL, lambda: _Failing(status=429), 'rate_limited', True),
    (EMAIL, lambda: _Failing(status=500), 'internal', False),
    (EMAIL, lambda: _Failing(raises=OSError('PRIVATE socket')), 'internal', False),
])
def test_google_failures_name_an_actionable_reason(grant, read, factory, reason, retryable):
    # calendar_list reports a failed calendar as partial coverage, so provider statuses use an exact get.
    result = call(grant, _google(factory), *read)['result']
    assert result['isError'] is True
    assert result['structuredContent'] == {'status': 'unavailable', 'reason': reason, 'retryable': retryable}
    assert READ_FAILURES[reason][1] in result['content'][0]['text']
    assert 'PRIVATE' not in json.dumps(result) and 'token' not in json.dumps(result)


@pytest.mark.parametrize('valid_home,reason', [(False, 'internal'), (True, 'reauth_required')])
def test_google_credential_root_and_missing_token_are_distinct(grant, monkeypatch, tmp_path, valid_home, reason):
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path) if valid_home else 'relative')
    result = call(grant, _google(google_api._owner_gmail_session), *EMAIL)['result']
    assert result['structuredContent'] == {'status': 'unavailable', 'reason': reason, 'retryable': False}
    assert ('reconnect' in result['content'][0]['text']) is valid_home


def _staged(stage):
    error = RuntimeError('PRIVATE Google body')
    error.sotto_read_stage = stage
    return error


@pytest.mark.parametrize('failure,reason,retryable', [
    (ReadUnavailableError('timeout', 'PRIVATE subject line'), 'timeout', True),
    (ReadUnavailableError('bridge_offline'), 'bridge_offline', True),
    (subprocess.TimeoutExpired(['knowledge_query.py', 'PRIVATE'], 10), 'timeout', True),
    (RuntimeError('PRIVATE provider body'), 'internal', False),
    (_staged('provider_rate_limited'), 'rate_limited', True),
    (_staged('provider_deadline'), 'timeout', True),
])
def test_backend_failures_map_to_a_reason_without_private_text(grant, failure, reason, retryable):
    def fail(*_):
        raise failure
    result = call(grant, fail)['result']
    assert result['isError'] is True
    assert result['structuredContent'] == {'status': 'unavailable', 'reason': reason, 'retryable': retryable}
    assert reason in result['content'][0]['text'] and 'PRIVATE' not in json.dumps(result)
    assert 'do not treat it as empty' in result['content'][0]['text']


def test_an_oversized_result_is_too_large(grant):
    result = call(grant, lambda *_: {'items': ['x' * (mcp.MAX_RESULT + 1)]})['result']
    assert result['structuredContent']['reason'] == 'too_large'


def _receiver(response, *, bridge=('imessage',), connected=True):
    authority = {'complete': True, 'revision': REVISION, 'bridge_sources': list(bridge)}
    return SimpleNamespace(_runtime_source_authority=lambda **_: authority,
                           RELAY=SimpleNamespace(mcp_call=lambda *_a, **_k: response,
                                                 bridge_connected=lambda: connected),
                           _source_context=lambda: SimpleNamespace(unwrap_tool_result=lambda v: v))


@pytest.mark.parametrize('response,bridge,connected,reason', [
    ({'jsonrpc': '2.0', 'id': 'x', 'error': {'code': -32001, 'message': 'Bridge timed out'}}, ['imessage'], True, 'timeout'),
    ({'result': {'structuredContent': {'error_code': 'bridge_offline'}, 'isError': True}}, ['imessage'], True, 'bridge_offline'),
    (None, [], False, 'bridge_offline'),
    (None, [], True, 'source_not_permitted'),
])
def test_backend_names_the_bridge_failure(response, bridge, connected, reason):
    with pytest.raises(ReadUnavailableError) as raised:
        backend.read(_receiver(response, bridge=bridge, connected=connected), 'sotto_message_history',
                     {'source': 'imessage', 'since': 1, 'until': 2})
    assert raised.value.reason == reason


class _SerialBridge:
    """A connected Bridge whose health probe queues behind another read and times out."""
    def __init__(self, health):
        self.health, self.busy = health, False

    def bridge_connected(self):
        return True

    def mcp_call(self, request, timeout):
        if self.busy:
            return {'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32001, 'message': 'Bridge timed out'}}
        return {'result': {'structuredContent': self.health, 'isError': False}}


@pytest.fixture
def live(tmp_path, monkeypatch):
    from test_source_authority import _health, _setup, managed, rec
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv('SOTTO_HOME', raising=False)
    managed.record_bridge_consent(tmp_path, ['imessage', 'contacts'], ['imessage', 'contacts'])
    monkeypatch.setattr(rec, '_BRIDGE_PROBE_CACHE', {})
    monkeypatch.setattr(rec, '_BRIDGE_CONSENT_SEEN', {})
    relay = _SerialBridge(_health(('imessage', 'contacts')))
    monkeypatch.setattr(rec, 'RELAY', relay)
    return rec, relay


def test_a_queued_health_probe_is_a_timeout_not_a_permission_failure(live):
    rec, relay = live
    healthy = rec._runtime_source_authority()
    rec._BRIDGE_PROBE_CACHE.clear()
    relay.busy = True
    queued = rec._runtime_source_authority()
    assert queued.keys() == healthy.keys() and queued['revision'] == healthy['revision']
    assert queued['bridge_sources'] == []
    for name, arguments in (('sotto_message_history', {'source': 'imessage', 'since': 1, 'until': 2}),
                            ('sotto_contacts', {'query': 'Dana'})):
        with pytest.raises(ReadUnavailableError) as raised:
            backend.read(rec, name, arguments)
        assert raised.value.reason == 'timeout'


@pytest.mark.parametrize('connected,allowed,reason', [
    (False, ('imessage', 'contacts'), 'bridge_offline'),
    (True, ('imessage',), 'source_not_permitted'),
])
def test_contacts_names_an_offline_mac_or_a_disabled_source(live, connected, allowed, reason):
    from test_source_authority import _health
    rec, relay = live
    relay.health = _health(allowed)
    relay.bridge_connected = lambda: connected
    with pytest.raises(ReadUnavailableError) as raised:
        backend.read(rec, 'sotto_contacts', {'query': 'Dana'})
    assert raised.value.reason == reason


def test_message_history_keeps_the_bridge_unavailable_reason():
    page = {'source': 'imessage', 'status': 'unavailable', 'reason': 'permission_denied',
            'rows': [], 'complete': False}
    result = backend.read(_receiver({'result': page}), 'sotto_message_history',
                          {'source': 'imessage', 'since': 1, 'until': 2})
    assert result['status'] == 'unavailable' and result['reason'] == 'permission_denied'


def test_history_unavailable_reason_reaches_the_receiver(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'managed-capabilities.json').write_text(json.dumps(
        {'tenant_id': 'test', 'sources': {'whatsapp': {'consented': True, 'connected': True}}}))
    request = {'method': 'tools/call', 'params': {'name': 'read_history', 'arguments': {'source': 'whatsapp'}}}
    payload = {'source': 'whatsapp', 'status': 'unavailable', 'reason': 'read_failed',
               'rows': [], 'complete': False}
    assert managed.validate_tool_response(tmp_path, request, {'structuredContent': payload})
    for leaked in ('open /Users/alice/Library/Group Containers/ChatStorage.sqlite failed', 'WhatsApp history unavailable',
                   ['read_failed'], None):
        assert not managed.validate_tool_response(tmp_path, request, {'structuredContent': {**payload, 'reason': leaked}})


@pytest.mark.parametrize(('health', 'is_error', 'reason'), [
    ({'connected': True}, False, 'internal'),
    ({}, True, 'internal'),
    ({'connected': False}, False, 'bridge_offline'),
])
def test_missing_health_authority_does_not_claim_source_is_disabled(live, health, is_error, reason):
    rec, relay = live
    relay.mcp_call = lambda request, timeout: {'result': {'structuredContent': health, 'isError': is_error}}
    for tool, arguments in (('sotto_message_history', {'source': 'imessage', 'since': 1, 'until': 2}),
                            ('sotto_contacts', {'query': 'Dana'})):
        with pytest.raises(ReadUnavailableError) as raised:
            backend.read(rec, tool, arguments)
        assert raised.value.reason == reason
