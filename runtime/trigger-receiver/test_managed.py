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
    assert not managed.validate_tool_request(tmp_path, call('read_local', {}))
    assert not managed.validate_tool_request(tmp_path, call('read_local', {'sources': ['whatsapp', 'imessage']}))
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


def test_photon_policy_rejects_groups_even_from_owner():
    path = Path(__file__).resolve().parents[2] / 'adapters/hermes/sotto_photon/__init__.py'
    spec = importlib.util.spec_from_file_location('managed_photon', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.permits(SimpleNamespace(chat_type='dm', user_id='owner'), 'owner')
    assert not module.permits(SimpleNamespace(chat_type='group', user_id='owner'), 'owner')
    assert not module.permits(SimpleNamespace(chat_type='dm', user_id='stranger'), 'owner')
    assert not module.permits(None, 'owner')


@pytest.mark.parametrize('mode', ['managed', 'self-host'])
def test_photon_formats_chat_and_standalone_at_registered_boundary(monkeypatch, mode):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    import asyncio
    import sys
    import types

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('managed_format_fixture', root / 'adapters/hermes/sotto_photon/__init__.py',
                                                 submodule_search_locations=[])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    fmt_spec = importlib.util.spec_from_file_location(spec.name + '.chatfmt', root / 'sotto-chief-of-staff/_shared/lib/chatfmt.py')
    fmt = importlib.util.module_from_spec(fmt_spec)
    fmt_spec.loader.exec_module(fmt)
    monkeypatch.setitem(sys.modules, fmt_spec.name, fmt)
    upstream = types.ModuleType('plugins.platforms.photon.adapter')
    deliveries = []
    fail_delivery = False

    async def deliver(self, chat_id, content, reply_to=None, metadata=None):
        deliveries.append(content)
        return SimpleNamespace(success=not fail_delivery)

    upstream.PhotonAdapter = type('Adapter', (), {'send': deliver})
    upstream.SendResult = SimpleNamespace
    gateway = types.ModuleType('gateway.run')
    gateway._PROVIDER_ERROR_REPLIES = ()
    monkeypatch.setitem(sys.modules, 'gateway.run', gateway)
    sent = []

    async def standalone(config, chat_id, message, **kwargs):
        sent.append(message)
        assert not upstream._markdown_enabled()
        return {'success': True, 'message_id': 'provider-fixture'}

    upstream._standalone_send = standalone
    # Match the actual upstream keyword; an unrecognized platform_prompt must fail.
    def register_platform(*, adapter_factory, standalone_sender_fn, allow_update_command, platform_hint):
        return locals()

    class Context:
        def register_platform(self, **kwargs):
            self.registration = register_platform(**kwargs)

    upstream.register = lambda ctx: ctx.register_platform(adapter_factory=None, standalone_sender_fn=None,
                                                          allow_update_command=True, platform_hint='Markdown rendered')
    for name in ('plugins', 'plugins.platforms', 'plugins.platforms.photon'):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, 'plugins.platforms.photon.adapter', upstream)
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'owner')
    ctx = Context()
    module.register(ctx)
    registration = ctx.registration
    adapter = registration['adapter_factory']()
    raw = '### Reply\n> **Hello**\n[Read](https://example.com)'
    expected = 'Reply\nHello\nRead: https://example.com'
    assert adapter.format_message(adapter.format_message(raw)) == expected
    result = asyncio.run(registration['standalone_sender_fn'](None, 'owner', raw))
    assert result['message_id'] == 'provider-fixture' and sent == [expected]
    assert registration['allow_update_command'] is (mode != 'managed')
    assert 'plain-text' in registration['platform_hint']
    rejected = asyncio.run(registration['standalone_sender_fn'](None, 'stranger', raw))
    if mode == 'self-host':
        assert rejected['message_id'] == 'provider-fixture' and len(sent) == 2
        return  # tenant budget/owner restrictions apply only to managed mode
    assert 'error' in rejected and len(sent) == 1
    module.install_budget_error_reply()
    assert len(gateway._PROVIDER_ERROR_REPLIES) == 1
    pattern, reply = gateway._PROVIDER_ERROR_REPLIES[0]
    assert pattern.search('API error: sotto_budget_exhausted') and reply == module.BUDGET_NOTICE
    assert not pattern.search('429: real upstream rate limit')

    async def exercise_notices():
        nonlocal fail_delivery
        fail_delivery = True
        assert not (await adapter.send('owner', module.BUDGET_NOTICE)).success
        fail_delivery = False
        results = await asyncio.gather(*(adapter.send('owner', module.BUDGET_NOTICE) for _ in range(4)))
        assert all(r.success for r in results)
        assert deliveries == [module.BUDGET_NOTICE, module.BUDGET_NOTICE]
        assert results[-1].raw_response['suppressed'] == 'duplicate_budget_notice'
        await adapter.send('owner', 'Normal reply after recovery')
        await adapter.send('owner', module.BUDGET_NOTICE)
        assert deliveries[-2:] == ['Normal reply after recovery', module.BUDGET_NOTICE]

    asyncio.run(exercise_notices())


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
