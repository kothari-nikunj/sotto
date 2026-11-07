import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import managed
import pytest


def state(root, name, **fields):
    (root / 'config').mkdir(exist_ok=True)
    (root / 'config' / name).write_text(json.dumps({'tenant_id': 'test', **fields}))


def test_activation_and_source_consent_are_independent(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', '+15551234567')
    assert 'Text Sotto' in managed.brief_hold(tmp_path)
    state(tmp_path, 'photon-activation.json', activated=True, owner='+15551234567')
    assert 'context source' in managed.brief_hold(tmp_path)
    state(tmp_path, 'managed-capabilities.json', sources={'calendar': {'connected': True, 'consented': False}})
    assert managed.brief_hold(tmp_path)
    state(tmp_path, 'managed-capabilities.json', sources={'calendar': {'connected': True, 'consented': True}})
    assert managed.brief_hold(tmp_path) is None
    # Capability is independent of current event count / whether the Mac is awake.
    monkeypatch.setenv('SOTTO_TENANT_ID', 'another')
    assert managed.brief_hold(tmp_path)


def test_self_host_does_not_require_managed_state(tmp_path, monkeypatch):
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    assert managed.brief_hold(tmp_path) is None


def test_tool_result_consent_comes_from_pending_server_request(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    state(tmp_path, 'managed-capabilities.json', sources={
        'imessage': {'consented': False, 'connected': False},
        'whatsapp': {'consented': True, 'connected': True},
        'contacts': {'consented': False, 'connected': False}})
    call = lambda name, arguments: {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
    assert managed.validate_tool_request(tmp_path, call('read_history', {'source': 'whatsapp'}))
    assert not managed.validate_tool_request(tmp_path, call('read_history', {'source': 'imessage'}))
    # An unscoped read_local is how every skill calls it (`read_local(since_hours=24)`), so the
    # pre-execution gate lets it through and the RESULT is filtered field by field below.
    assert managed.validate_tool_request(tmp_path, call('read_local', {}))
    assert not managed.validate_tool_request(tmp_path, call('read_local', {'sources': ['whatsapp', 'imessage']}))
    assert not managed.validate_tool_request(tmp_path, call('send_message', {'channel': 'imessage'}))
    assert not managed.validate_tool_request(tmp_path, call('get_messages', {'source': 'whatsapp'}))
    assert not managed.validate_tool_request(tmp_path, call('get_contacts', {}))
    assert managed.validate_tool_request(tmp_path, call('health', {}))

    wrap = lambda value: {'structuredContent': value, 'isError': False}
    # Empty sources means "all" to old Bridge builds. Accept it only after inspecting every
    # returned field against current server-side consent.
    assert managed.validate_tool_response(
        tmp_path, call('read_local', {}), wrap({'whatsapp': [{'text': 'allowed'}]}))
    assert not managed.validate_tool_response(
        tmp_path, call('read_local', {'sources': ['whatsapp']}),
        wrap({'whatsapp': [{'text': 'allowed'}], 'contacts': [{'name': 'blocked'}]}))
    assert managed.validate_tool_response(
        tmp_path, call('read_history', {'source': 'whatsapp'}),
        wrap({'source': 'whatsapp', 'rows': [{'text': 'allowed'}], 'complete': True}))
    assert not managed.validate_tool_response(
        tmp_path, call('read_history', {'source': 'whatsapp'}),
        wrap({'source': 'imessage', 'rows': [{'text': 'blocked'}], 'complete': True}))
    assert not managed.validate_tool_response(
        tmp_path, call('future_content_tool', {}), wrap({'private': 'content'}))
    assert not managed.validate_tool_response(
        tmp_path, call('read_local', {'sources': [None]}), wrap({}))


def test_google_consent_opens_only_granted_sources_and_preserves_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    state(tmp_path, 'managed-capabilities.json', sources={'imessage': {'consented': True, 'connected': True}})
    managed.record_google_consent(tmp_path, ['https://www.googleapis.com/auth/calendar.readonly'])
    sources = managed.read_state(tmp_path, 'managed-capabilities.json')['sources']
    assert sources['calendar'] == {'consented': True, 'connected': True}
    assert sources['gmail'] == {'consented': False, 'connected': False}
    assert sources['imessage']['connected'] is True


def test_google_contacts_authority_is_separate_from_mac_contacts(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    state(tmp_path, 'managed-capabilities.json', sources={'contacts': {'connected': True, 'consented': True}})
    managed.record_google_consent(tmp_path, [])
    before = managed.source_authority_state(tmp_path)
    assert 'contacts' in before['allowed_sources']
    assert before['google_sources'] == []
    managed.record_google_consent(tmp_path, ['https://www.googleapis.com/auth/contacts.readonly'])
    connected = managed.source_authority_state(tmp_path)
    assert connected['google_sources'] == ['google_contacts']
    assert 'contacts' in connected['allowed_sources']
    assert connected['revision'] != before['revision']
    managed.record_google_consent(tmp_path, [])
    after = managed.source_authority_state(tmp_path)
    assert after['google_sources'] == []
    assert 'contacts' in after['allowed_sources']
    assert after['revision'] != connected['revision']


@pytest.mark.parametrize('source, revoke', [
    ('imessage', lambda root: managed.record_bridge_consent(root, [], [])),
    ('gmail', lambda root: managed.record_google_consent(root, [])),
    ('granola', lambda root: managed.record_connector_consent(root, 'granola', False)),
])
def test_managed_source_revocation_erases_finished_legacy_diagnostics(
        tmp_path, monkeypatch, source, revoke):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    state(tmp_path, 'managed-capabilities.json', sources={
        source: {'consented': True, 'connected': True}})
    path = tmp_path / 'events/learning-diagnostics/brief-old.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'schema': 1, 'rejected': [
        {'evidence_snippets': ['private from ' + source]}]}))
    revoke(tmp_path)
    assert not path.exists()


def test_connector_consent_lifecycle_is_tenant_bound_and_self_host_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    state(tmp_path, 'managed-capabilities.json', sources={
        'calendar': {'consented': True, 'connected': True}})
    managed.record_connector_consent(tmp_path, 'granola', True)
    sources = managed.read_state(tmp_path, 'managed-capabilities.json')['sources']
    assert sources['granola'] == {'consented': True, 'connected': True}
    assert sources['calendar']['connected'] is True
    managed.record_connector_consent(tmp_path, 'granola', False)
    assert managed.read_state(tmp_path, 'managed-capabilities.json')['sources']['granola'] == {
        'consented': False, 'connected': False}

    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE')
    before = (tmp_path / 'config/managed-capabilities.json').read_text()
    managed.record_connector_consent(tmp_path, 'granola', True)
    assert (tmp_path / 'config/managed-capabilities.json').read_text() == before


def test_connector_reconciliation_repairs_only_missing_same_tenant_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    linked = [{'service': 'granola', 'connected': True}]
    state(tmp_path, 'managed-capabilities.json', sources={})
    managed.reconcile_connector_capabilities(tmp_path, linked)
    assert managed.read_state(tmp_path, 'managed-capabilities.json')['sources']['granola'] == {
        'consented': True, 'connected': True}

    # Explicit disconnect is authoritative even while a stale token is still visible at boot.
    managed.record_connector_consent(tmp_path, 'granola', False)
    managed.reconcile_connector_capabilities(tmp_path, linked)
    assert managed.read_state(tmp_path, 'managed-capabilities.json')['sources']['granola'] == {
        'consented': False, 'connected': False}

    # A token on a capability file bound to another tenant cannot grant this tenant access.
    state(tmp_path, 'managed-capabilities.json', sources={}, tenant_id='other')
    managed.reconcile_connector_capabilities(tmp_path, linked)
    assert json.loads((tmp_path / 'config/managed-capabilities.json').read_text())['tenant_id'] == 'other'


@pytest.mark.parametrize('body', ['', '[]', '{"tenant_id":"test","sources":null}'])
def test_connector_reconciliation_keeps_malformed_or_missing_state_closed(
        tmp_path, monkeypatch, body):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    (tmp_path / 'config').mkdir()
    path = tmp_path / 'config/managed-capabilities.json'
    if body:
        path.write_text(body)
    managed.reconcile_connector_capabilities(
        tmp_path, [{'service': 'granola', 'connected': True}])
    if body:
        assert path.read_text() == body
    else:
        assert not path.exists()


def test_connector_consent_rejects_unknown_sources(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    with pytest.raises(ValueError, match='Invalid managed connector'):
        managed.record_connector_consent(tmp_path, 'future-service', True)


def test_photon_policy_rejects_groups_even_from_owner():
    path = Path(__file__).resolve().parents[2] / 'adapters/sotto/photon_owner.py'
    spec = importlib.util.spec_from_file_location('managed_photon', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.permits(SimpleNamespace(chat_type='dm', user_id='owner'), 'owner')
    assert not module.permits(SimpleNamespace(chat_type='group', user_id='owner'), 'owner')
    assert not module.permits(SimpleNamespace(chat_type='dm', user_id='stranger'), 'owner')
    assert not module.permits(None, 'owner')


def test_bridge_consent_holds_until_actual_read_and_survives_sleep(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    managed.record_bridge_consent(tmp_path, ['imessage'], [])
    assert not managed.has_sources(tmp_path)
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    assert managed.has_sources(tmp_path)
    managed.record_bridge_consent(tmp_path, ['imessage'], [])
    assert managed.has_sources(tmp_path)
    managed.record_bridge_consent(tmp_path, [], [])
    assert not managed.has_sources(tmp_path)


def test_ingress_rejects_disabled_content_before_persistence(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    managed.validate_local_ingress(tmp_path, {'local_data': {'imessage': [{'text': 'allowed'}],
        'whatsapp': [], 'contacts': [], 'screen_time': {'total_minutes': 0, 'top_apps': []}}})
    with pytest.raises(ValueError):
        managed.validate_local_ingress(tmp_path, {'local_data': {'whatsapp': [{'text': 'blocked'}]}})
    with pytest.raises(ValueError):
        managed.validate_local_ingress(tmp_path, {'events': [{'source': 'contacts'}]}, events=True)
    managed.record_bridge_consent(tmp_path, [], [])
    with pytest.raises(ValueError):
        managed.validate_local_ingress(tmp_path, {'events': [{'source': 'imessage'}]}, events=True)


def test_all_bridge_sources_require_consent_and_use_wire_field_names(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    local = {field: ([{'fixture': True}] if field != 'contacts_total' else 1)
             for fields in managed.BRIDGE_SOURCE_FIELDS.values() for field in fields}
    for source, fields in managed.BRIDGE_SOURCE_FIELDS.items():
        managed.record_bridge_consent(tmp_path, [source], [])
        assert not managed.has_sources(tmp_path)
        managed.validate_local_ingress(tmp_path, {'local_data': {k: local[k] for k in fields}})
        managed.record_bridge_consent(tmp_path, [], [])
        with pytest.raises(ValueError):
            managed.validate_local_ingress(tmp_path, {'local_data': {k: local[k] for k in fields}})
    managed.record_bridge_consent(tmp_path, list(managed.BRIDGE_SOURCE_FIELDS), ['contacts'])
    assert not managed.has_sources(tmp_path)  # identity metadata alone cannot personalize a brief
    managed.validate_local_ingress(tmp_path, {'local_data': local})
    managed.validate_local_ingress(tmp_path, {'events': [{'source': 'calls'}]}, events=True)
    with pytest.raises(ValueError):
        managed.record_bridge_consent(tmp_path, ['unknown'], [])


def test_full_google_grants_preserve_mac_contacts_consent(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    state(tmp_path, 'managed-capabilities.json', sources={'contacts': {'consented': True, 'connected': True}})
    managed.record_google_consent(tmp_path, ['https://www.googleapis.com/auth/' + s for s in ('gmail.modify', 'calendar', 'contacts')])
    sources = managed.read_state(tmp_path, 'managed-capabilities.json')['sources']
    assert all(sources[s]['connected'] for s in ('gmail', 'calendar', 'google_contacts', 'contacts'))
    managed.record_google_consent(tmp_path, [])
    sources = managed.read_state(tmp_path, 'managed-capabilities.json')['sources']
    assert not sources['google_contacts']['consented']
    assert sources['contacts']['consented']


def test_google_address_book_alone_does_not_start_scheduled_briefs(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    managed.record_google_consent(tmp_path, ['https://www.googleapis.com/auth/contacts'])
    assert not managed.has_sources(tmp_path)


def _gated_relay(tmp_path, monkeypatch, mode):
    """A Relay wired the way receiver.py wires it, with a recording forward in place of a Mac."""
    if mode == 'managed':
        monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
        monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    else:
        monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    spec = importlib.util.spec_from_file_location(
        'relay_gate_' + mode, Path(__file__).resolve().parent / 'relay.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    relay = module.Relay()
    relay._touch()                     # the Bridge is connected and polling
    forwarded = []

    def forward(request, timeout):
        forwarded.append(request)
        listing = {'tools': [{'name': name} for name in
                             ('health', 'read_local', 'get_messages', 'send_message')]}
        return {'jsonrpc': '2.0', 'id': request.get('id'),
                'result': listing if request.get('method') == 'tools/list'
                else {'content': [{'type': 'text', 'text': '{}'}], 'isError': False}}

    relay._forward = forward
    relay.validate_request = lambda request: managed.validate_tool_request(tmp_path, request)
    relay.filter_tools = lambda result: managed.filter_tool_list(tmp_path, result)
    return relay, forwarded


def _tool_call(name, arguments, rid=1):
    return {'jsonrpc': '2.0', 'id': rid, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments}}


def test_managed_refuses_a_send_before_the_bridge_executes_it(tmp_path, monkeypatch):
    # The gate that mattered was post-execution: the Mac sent the message and the relay merely
    # discarded the receipt. Denial must happen before the call is handed to a connected Bridge.
    relay, forwarded = _gated_relay(tmp_path, monkeypatch, 'managed')
    state(tmp_path, 'managed-capabilities.json',
          sources={'imessage': {'consented': True, 'connected': True}})
    response = relay.mcp_call(_tool_call('send_message', {'channel': 'imessage', 'to': '+15551234567',
                                                          'body': 'hi'}, rid=4), timeout=1)
    assert forwarded == []                                  # nothing ever reached the Mac
    assert response['id'] == 4 and 'result' not in response
    assert response['error']['code'] == -32002
    assert 'not permitted' in response['error']['message'] and 'send_message' in response['error']['message']
    # A consented read is untouched by the gate.
    allowed = relay.mcp_call(_tool_call('get_messages', {'identifier': '+15551234567'}, rid=5), timeout=1)
    assert allowed['result']['isError'] is False
    assert [r['params']['name'] for r in forwarded] == ['get_messages']
    # ...and an unconsented one is refused without a forward too.
    assert relay.mcp_call(_tool_call('get_contacts', {}, rid=6), timeout=1)['error']['code'] == -32002
    assert len(forwarded) == 1


def test_self_host_forwards_every_tool_call_as_before(tmp_path, monkeypatch):
    relay, forwarded = _gated_relay(tmp_path, monkeypatch, 'self-host')
    for name in ('send_message', 'read_local', 'get_contacts'):
        assert 'result' in relay.mcp_call(_tool_call(name, {}), timeout=1)
    assert [r['params']['name'] for r in forwarded] == ['send_message', 'read_local', 'get_contacts']
    listed = relay.mcp_call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    assert 'send_message' in [t['name'] for t in listed['result']['tools']]


def test_managed_tools_list_does_not_advertise_a_denied_tool(tmp_path, monkeypatch):
    relay, _forwarded = _gated_relay(tmp_path, monkeypatch, 'managed')
    state(tmp_path, 'managed-capabilities.json',
          sources={'imessage': {'consented': True, 'connected': True}})
    live = [t['name'] for t in relay.mcp_call({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']]
    assert live == ['health', 'read_local', 'get_messages']
    relay._last_poll = float('-inf')                        # Mac asleep: same filter on the cache
    cached = [t['name'] for t in relay.mcp_call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']]
    assert cached == live


def test_request_and_response_gates_cannot_disagree(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    state(tmp_path, 'managed-capabilities.json',
          sources={'whatsapp': {'consented': True, 'connected': True}})
    wrap = lambda value: {'structuredContent': value, 'isError': False}
    for name, arguments in (('send_message', {'channel': 'imessage', 'to': 'x', 'body': 'y'}),
                            ('future_write_tool', {}), ('get_contacts', {}),
                            ('read_history', {'source': 'imessage'}),
                            ('read_local', {'sources': ['imessage']})):
        request = {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
        assert managed.validate_tool_request(tmp_path, request) is False, name
        assert managed.validate_tool_response(tmp_path, request, wrap({'ok': True})) is False, name
    for name, arguments in (('health', {}), ('read_history', {'source': 'whatsapp'}),
                            ('read_local', {'sources': ['whatsapp']})):
        request = {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
        assert managed.validate_tool_request(tmp_path, request) is True, name


def test_managed_send_requires_separate_opt_in_and_a_bound_operation(tmp_path, monkeypatch):
    relay, forwarded = _gated_relay(tmp_path, monkeypatch, 'managed')
    args = {'channel': 'imessage', 'to': '+15551234567', 'body': 'On my way',
            'request_id': 'request-1234567890'}
    managed.record_bridge_consent(tmp_path, [], [], allow_send=True)
    # Sending is independent from reading contacts or message history.
    assert 'result' in relay.mcp_call(_tool_call('send_message', args))
    assert len(forwarded) == 1
    listing = relay.mcp_call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    assert 'send_message' in [tool['name'] for tool in listing['result']['tools']]
    for field, value in (('request_id', ''), ('to', 'ramp_81@rbm.goog'),
                         ('to', '81'), ('channel', 'whatsapp'), ('body', ' ')):
        assert not managed.validate_tool_request(tmp_path, _tool_call('send_message', {**args, field: value}))
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'], allow_send=False)
    assert 'error' in relay.mcp_call(_tool_call('send_message', args))
    # A legacy app's missing field must not preserve an earlier send grant.
    managed.record_bridge_consent(tmp_path, [], [], allow_send=True)
    managed.record_bridge_consent(tmp_path, [], [])
    assert not managed.validate_tool_request(tmp_path, _tool_call('send_message', args))
    with pytest.raises(ValueError):
        managed.record_bridge_consent(tmp_path, [], [], allow_send='true')


def test_send_permission_is_tenant_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    managed.record_bridge_consent(tmp_path, [], [], allow_send=True)
    monkeypatch.setenv('SOTTO_TENANT_ID', 'another')
    listing = managed.filter_tool_list(tmp_path, {'tools': [{'name': 'send_message'}]})
    assert listing == {'tools': []}


def test_note_body_requires_notes_consent_at_listing_request_and_response(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    call = {'method': 'tools/call', 'params': {'name': 'get_note', 'arguments': {'id': 7}}}
    result = {'structuredContent': {'body': 'private note'}, 'isError': False}
    listing = {'tools': [{'name': 'get_note'}, {'name': 'health'}]}
    state(tmp_path, 'managed-capabilities.json', sources={'apple_notes': {'consented': True, 'connected': True}})
    assert managed.validate_tool_request(tmp_path, call)
    assert managed.validate_tool_response(tmp_path, call, result)
    assert managed.filter_tool_list(tmp_path, listing) == listing
    state(tmp_path, 'managed-capabilities.json', sources={'apple_notes': {'consented': False, 'connected': True}})
    assert not managed.validate_tool_request(tmp_path, call)
    assert not managed.validate_tool_response(tmp_path, call, result)
    assert managed.filter_tool_list(tmp_path, listing)['tools'] == [{'name': 'health'}]


def test_local_search_scope_and_response_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    state(tmp_path, 'managed-capabilities.json', sources={'imessage': {'consented': True}})

    def call(sources):
        return {'method': 'tools/call', 'params': {'name': 'search_local', 'arguments': {'sources': sources}}}

    for sources in [None, [], ['contacts'], ['imessage', 'whatsapp'], [None]]:
        assert not managed.validate_tool_request(tmp_path, call(sources))
    request = call(['imessage'])
    assert managed.validate_tool_request(tmp_path, request)
    listing = {'tools': [{'name': 'search_local'}, {'name': 'health'}]}
    assert managed.filter_tool_list(tmp_path, listing) == listing
    payload = {'status': 'ok', 'results': [{'source': 'imessage', 'source_id': '7', 'excerpt': 'October', 'author': 'me',
                                            'matched_fields': ['body']}],
               'coverage': {'imessage': {'status': 'ok', 'complete': True, 'indexed': 1, 'truncated': 0,
                                         'stopped_early': False, 'newest_at': '2026-11-05T14:51:24Z'}}}
    assert managed.validate_tool_response(tmp_path, request, {'structuredContent': payload})
    legacy = {'status': 'ok', 'results': [{'source': 'imessage', 'timestamp': '2026-11-01T08:30:00Z'},
                                       {'source': 'imessage', 'timestamp': '2026-11-01T09:30:00Z'}],
              'coverage': {'imessage': {'status': 'ok', 'complete': True, 'indexed': 2}}}
    assert managed.validate_tool_response(tmp_path, request, {'structuredContent': legacy})
    for invalid in [
        {**payload, 'results': [{'source': 'whatsapp', 'excerpt': 'blocked'}]},
        {**payload, 'coverage': {'whatsapp': {'status': 'ok'}}},
        {**payload, 'secret': 'blocked'},
        {**payload, 'results': [{'source': ['imessage'], 'excerpt': 'blocked'}]},
        {**payload, 'results': [{'source': 'imessage', 'secret': 'blocked'}]},
    ]:
        assert not managed.validate_tool_response(tmp_path, request, {'structuredContent': invalid})
    state(tmp_path, 'managed-capabilities.json', sources={'imessage': {'consented': False}})
    assert not managed.validate_tool_response(tmp_path, request, {'structuredContent': payload})
    assert managed.filter_tool_list(tmp_path, listing) == {'tools': [{'name': 'health'}]}


def test_snapshot_coverage_obeys_source_consent(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    detail = {'rows_scanned': 1000, 'scan_observed': True, 'limit_reached': True,
              'content_partial': True, 'bounded': True, 'status': 'partial'}
    managed.validate_local_ingress(tmp_path, {'local_data': {'source_coverage': {'imessage': detail}}})
    for source in ('contacts', 'deferred_unread_whatsapp'):
        with pytest.raises(ValueError):
            managed.validate_local_ingress(tmp_path, {'local_data': {'source_coverage': {source: detail}}})
    with pytest.raises(ValueError):
        managed.validate_local_ingress(tmp_path, {'local_data': {'source_coverage': {'imessage': {'body': 'not metadata'}}}})


def test_additive_native_metadata_keeps_existing_source_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    managed.record_bridge_consent(tmp_path, ['imessage', 'calls'], ['imessage', 'calls'])
    local = {'imessage': [{'text': '[attachment — contents not read]', 'content_partial': True,
                           'attachments': [{'id': 'a1', 'name': 'report.pdf', 'mime_type': 'application/pdf', 'size_bytes': 42}]}],
             'calls': [{'phone': '+15555550101', 'record_id': '1', 'service_provider': 'FaceTime',
                        'timestamp': '2026-10-15 01:00:00', 'timestamp_utc': '2026-10-15T05:00:00Z'}]}
    request = {'method': 'tools/call', 'params': {'name': 'read_local', 'arguments': {}}}
    assert managed.validate_tool_response(tmp_path, request, {'structuredContent': local})
    # Legacy rows are still accepted, and new nested metadata needs no extra top-level capability.
    assert managed.validate_tool_response(tmp_path, request, {'structuredContent': {'imessage': [{'text': 'hello'}]}})
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    assert not managed.validate_tool_response(tmp_path, request, {'structuredContent': local})
    managed.record_bridge_consent(tmp_path, [], [])
    assert not managed.validate_tool_response(tmp_path, request, {'structuredContent': {'imessage': local['imessage']}})


@pytest.mark.parametrize('detail', [
    {'status': 'private source text'}, {'rows_scanned': -1}, {'rows_scanned': True},
    {'scan_observed': 'true'}, {'limit_reached': 1}, {'observed_at': 'private source text'},
])
def test_snapshot_coverage_rejects_untyped_metadata(tmp_path, monkeypatch, detail):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    with pytest.raises(ValueError):
        managed.validate_local_ingress(tmp_path, {'local_data': {'source_coverage': {'imessage': detail}}})


def test_rich_snapshot_coverage_is_typed_and_still_consent_gated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    detail = {'status': 'empty', 'requested_window': {'since': '2026-10-20T00:00:00Z', 'until': '2026-10-21T00:00:00Z'},
              'effective_window': {'since': None, 'until': None, 'basis': 'sent'},
              'rows_returned': 0, 'query_row_caps': [100], 'omitted_rows': None,
              'last_successful_read_at': '2026-10-21T00:00:00Z', 'failure_category': None,
              'components': {'deferred_unread': {'status': 'failed', 'failure_category': 'query'}},
              'attachments': {'metadata': 'available', 'body': 'not_read'}}
    managed.validate_local_ingress(tmp_path, {'local_data': {'source_coverage': {'imessage': detail}}})
    managed.validate_local_ingress(tmp_path, {'local_data': {'source_coverage': {'imessage': {
        **detail, 'effective_window': {'since': None, 'until': None, 'basis': 'due_or_created'}}}}})
    for changed in [
        {'rows_returned': True}, {'rows_returned': 2 ** 53}, {'omitted_rows': -1},
        {'query_row_caps': [1, 1]}, {'query_row_caps': list(range(33))},
        {'effective_window': {'since': None, 'until': None, 'basis': 'private body'}},
        {'effective_window': {'since': None, 'until': None, 'basis': 'due_or_modified'}},
        {'components': {'profiles': {'status': 'ok', 'failure_category': None}}},
        {'components': {'private name': {'status': 'ok', 'failure_category': None}}},
        {'components': {'deferred_unread': {'status': 'failed', 'failure_category': 'private body'}}},
        {'attachments': {'metadata': 'available', 'body': 'read'}},
        {'requested_window': {'since': '2026-10-21T00:00:00Z', 'until': '2026-10-20T00:00:00Z'}},
    ]:
        with pytest.raises(ValueError):
            managed.validate_local_ingress(tmp_path, {'local_data': {'source_coverage': {'imessage': {**detail, **changed}}}})
    with pytest.raises(ValueError):
        managed.validate_local_ingress(tmp_path, {'local_data': {'source_coverage': {'whatsapp': detail}}})
