"""Existing private HTTP endpoints preserve their distinct transport contracts."""
import io
from http import HTTPStatus
import json
from types import SimpleNamespace

import pytest

from test_receiver import rec
from test_runtime_owner_loop_close import IDENTITY, ROOT

ENDPOINTS = (
    ('owner-quiet-intent', 'RUNTIME_OWNER_QUIET', 8192, 2048),
    ('owner-loop-intent', 'RUNTIME_OWNER_LOOP', 8192, 2048),
    ('owner-reminder-intent', 'RUNTIME_OWNER_REMINDER', 8192, 512 * 1024),
    ('owner-reminder-tool', 'RUNTIME_OWNER_REMINDER_TOOL', 65536, 512 * 1024),
    ('owner-loop-tool', 'RUNTIME_OWNER_LOOP_TOOL', 65536, 512 * 1024),
    ('owner-preference-tool', 'RUNTIME_OWNER_PREFERENCE_TOOL', 65536, 512 * 1024),
    ('owner-routine-tool', 'RUNTIME_OWNER_ROUTINE_TOOL', 65536, 512 * 1024),
    ('owner-gmail-draft-tool', 'RUNTIME_OWNER_GMAIL_DRAFT_TOOL', 65536, 512 * 1024),
    ('owner-calendar-tool', 'RUNTIME_OWNER_CALENDAR_TOOL', 65536, 512 * 1024),
    ('owner-contacts-tool', 'RUNTIME_OWNER_CONTACTS_TOOL', 262144, 256 * 1024),
    ('owner-bridge-send-tool', 'RUNTIME_OWNER_BRIDGE_SEND_TOOL', 65536, 512 * 1024),
    ('owner-brief-request', 'RUNTIME_OWNER_BRIEF_REQUEST', 65536, 512 * 1024),
    ('owner-skill-request', 'RUNTIME_OWNER_SKILL_REQUEST', 65536, 512 * 1024),
    ('owner-meeting-prep-request', 'RUNTIME_OWNER_MEETING_PREP_REQUEST', 65536, 512 * 1024),
    ('owner-offer-reply', 'RUNTIME_OWNER_OFFER_REPLY', 65536, 512 * 1024),
    ('owner-web-read', 'RUNTIME_OWNER_WEB_READ', 65536, 512 * 1024),
)


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MCP_TOKEN', ROOT)
    monkeypatch.setattr(rec, '_pending_offers', lambda: [])
    monkeypatch.setattr(rec.RUNTIME_OWNER_LOOP, '_load_writer', lambda: None)
    state = SimpleNamespace(calls=[], result={'status': 'recorded'})

    def post(path, *, size=None, auth=True, peer='127.0.0.1', body=None):
        value = body or {'identity': IDENTITY, 'operation': 'read', 'args': {}}
        raw = body if isinstance(body, bytes) else json.dumps(value).encode()
        handler = rec.Handler.__new__(rec.Handler)
        handler.path = '/runtime/' + path
        handler.client_address = (peer, 1234)
        token = rec.OWNER_LOOP_CAPABILITY.derive(ROOT, IDENTITY) if auth else 'untrusted'
        handler.headers = {'Content-Length': str(len(raw) if size is None else size),
                           'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
        handler.rfile = io.BytesIO(raw)
        handler._send = lambda status, payload: (status, payload)
        return handler.do_POST()

    state.post = post
    return state


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_each_existing_endpoint_enforces_exact_auth_path_and_byte_contract(http, monkeypatch, endpoint):
    path, module_name, input_bound, output_bound = endpoint
    def perform(body, **kwargs):
        http.calls.append((body, kwargs))
        return http.result
    monkeypatch.setattr(getattr(rec, module_name), 'perform', perform)
    assert http.post(path) == (200, {'status': 'recorded'})
    assert len(http.calls) == 1
    assert http.post(path, auth=False)[0] == HTTPStatus.UNAUTHORIZED
    assert http.post(path, peer='203.0.113.1')[0] == HTTPStatus.UNAUTHORIZED
    assert http.post(path + '?extra=1')[0] == HTTPStatus.NOT_FOUND
    assert http.post(path, size=input_bound + 1)[0] == HTTPStatus.BAD_REQUEST
    assert http.post(path, body=b'{"identity":{},"identity":{}}')[0] == HTTPStatus.BAD_REQUEST
    assert len(http.calls) == 1
    http.result = {'status': 'recorded', 'padding': 'x' * output_bound}
    assert http.post(path)[0] == HTTPStatus.SERVICE_UNAVAILABLE
    assert [call[0]['operation'] for call in http.calls] == ['read', 'read']


def test_usefulness_larger_response_is_only_for_the_named_preference_read(http, monkeypatch):
    http.result = {'status': 'usefulness_list', 'padding': 'x' * (600 * 1024)}
    monkeypatch.setattr(rec.RUNTIME_OWNER_PREFERENCE_TOOL, 'perform', lambda *args, **kwargs: http.result)
    body = {'identity': IDENTITY, 'operation': 'read', 'args': {'operation': 'usefulness-list'}}
    assert http.post('owner-preference-tool', body=body)[0] == HTTPStatus.OK
    assert http.post('owner-preference-tool', body={**body, 'operation': 'inspect'})[0] == HTTPStatus.SERVICE_UNAVAILABLE
    assert http.post('owner-preference-tool', body={**body, 'args': {}})[0] == HTTPStatus.SERVICE_UNAVAILABLE
    http.result['padding'] = 'x' * (1024 * 1024)
    assert http.post('owner-preference-tool', body=body)[0] == HTTPStatus.SERVICE_UNAVAILABLE


@pytest.mark.parametrize('error,category', [(OSError('private'), 'io'), (KeyError('private'), 'type'),
    (ImportError('private'), 'import'), (RuntimeError('private'), 'other')])
def test_cleanup_failure_keeps_only_closed_categories(http, monkeypatch, error, category):
    def failed(*args, **kwargs):
        raise error
    monkeypatch.setattr(rec.RUNTIME_OWNER_LOOP_TOOL, 'perform', failed)
    body = {'identity': IDENTITY, 'operation': 'apply', 'args': {'operation': 'cleanup'}}
    assert http.post('owner-loop-tool', body=body) == (503, {
        'cleanup_stage': 'receiver_perform', 'cleanup_category': category, 'cleanup_http_status': 503})
    assert http.post('owner-loop-tool', body={**body, 'operation': 'inspect'}) == (503, {
        'error': 'owner reminder unavailable'})
