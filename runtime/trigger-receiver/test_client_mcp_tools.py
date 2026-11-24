"""Dedicated client tools for calls, reminders, browser history and Contacts reuse one reader."""
import pytest
from types import SimpleNamespace

import client_mcp as mcp
import client_mcp_backend as backend
from test_client_mcp_contacts import BOOK, _receiver


def test_calls_read_permitted_call_sources_with_names_and_name_the_rest():
    local = {'calls': [{'phone': '+14155550101', 'timestamp': '2026-11-09 10:00:00'}], 'source_status': {'calls': 'ok'}}
    rec, calls = _receiver({('calls',): local, ('contacts',): BOOK}, ['calls', 'contacts'])
    result = backend.read(rec, 'sotto_calls', {'since_hours': 48})
    assert calls[0] == {'name': 'read_local', 'arguments': {'since_hours': 48, 'sources': ['calls']}}
    assert result['calls'][0]['phone_name'] == 'Dana Reyes'
    assert result['not_permitted'] == ['whatsapp_calls']
    assert result['client_page']['complete'] is True


@pytest.mark.parametrize('name,sources', [('sotto_reminders', ('reminders',)),
                                          ('sotto_browser_history', ('chrome', 'safari'))])
def test_wrappers_use_the_mac_context_reader(name, sources):
    payload = {'source_status': {source: 'ok' for source in sources}}
    rec, calls = _receiver({sources: payload}, sources)
    result = backend.read(rec, name, {})
    assert calls[0]['name'] == 'read_local' and tuple(calls[0]['arguments']['sources']) == sources
    assert result['not_permitted'] == [] and 'client_page' in result


def test_a_wrapper_with_no_permitted_source_is_unavailable():
    rec, calls = _receiver({}, ['imessage'])
    with pytest.raises(RuntimeError, match='source unavailable'):
        backend.read(rec, 'sotto_browser_history', {})
    assert calls == []


def test_browser_service_filter_returns_only_the_service_on_permitted_browsers():
    local = {'source_status': {'chrome': 'ok', 'safari': 'ok'},
             'chrome_history': [{'domain': 'lu.ma'}, {'domain': 'events.luma.com'},
                                {'domain': 'notluma.com'}, {'domain': 'luma.com.example.test'}],
             'safari_history': [{'domain': 'luma.com'}], 'search_queries': ['unrelated query']}
    rec, calls = _receiver({('chrome',): local}, ['chrome'])
    result = backend.read(rec, 'sotto_browser_history', {'service': 'luma'})
    assert calls[0]['arguments'] == {'since_hours': 24, 'sources': ['chrome']}
    assert result['chrome_history'] == [{'domain': 'lu.ma'}, {'domain': 'events.luma.com'}]
    assert 'search_queries' not in result and 'safari_history' not in result
    assert result['not_permitted'] == ['safari'] and result['service'] == 'luma'
    assert 'bounded domain summaries' in result['service_coverage']


def test_browser_service_filter_is_typed_before_calling_the_reader():
    response = mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'sotto_browser_history', 'arguments': {'service': 'luma.com.evil.test'}}},
        {'tools': ['sotto_browser_history']}, lambda *_: pytest.fail('invalid service reached the reader'))
    assert response['result']['isError'] is True
    error = response['result']['structuredContent']
    assert error['reason'] == 'invalid_arguments' and 'service' in error['detail']


def test_browser_service_cursor_cannot_be_reused_for_another_service():
    local = {'source_status': {'chrome': 'ok'}, 'chrome_history': [
        {'domain': 'lu.ma', 'top_titles': ['a' * 20_000]},
        {'domain': 'luma.com', 'top_titles': ['b' * 20_000]},
        {'domain': 'docsend.com'}]}
    rec, _ = _receiver({('chrome',): local}, ['chrome'])
    first = backend.read(rec, 'sotto_browser_history', {'service': 'luma'})
    cursor = first['client_page']['next_cursor']
    assert cursor and [row['domain'] for row in first['chrome_history']] == ['lu.ma']
    next_page = backend.read(rec, 'sotto_browser_history', {'service': 'luma', 'cursor': cursor})
    assert [row['domain'] for row in next_page['chrome_history']] == ['luma.com']
    changed = backend.read(rec, 'sotto_browser_history', {'service': 'docsend', 'cursor': cursor})
    assert changed['status'] == 'changed' and 'chrome_history' not in changed


def test_contacts_search_mac_and_google_and_say_which_side_is_unavailable():
    rec, _ = _receiver({('contacts',): BOOK}, ['contacts'])
    result = backend.read(rec, 'sotto_contacts', {'query': 'dana'})
    assert [row['name'] for row in result['mac_contacts']['contacts']] == ['Dana Reyes']
    assert result['google_contacts'] == {'status': 'unavailable'}
    rec, _ = _receiver({}, [])
    google = {'source': 'google_contacts', 'items': [{'name': 'Dana Reyes'}]}
    rec._runtime_source_authority = lambda **_: {'complete': True, 'revision': 'c' * 64, 'bridge_sources': [],
                                                 'google_sources': ['google_contacts'], 'connector_sources': []}
    rec._sotto_adapter = lambda _: SimpleNamespace(read_owner=lambda operation, arguments: google)
    result = backend.read(rec, 'sotto_contacts', {'query': 'dana'})
    assert result == {'mac_contacts': {'status': 'unavailable'}, 'google_contacts': google}


def test_no_contacts_source_at_all_is_unavailable_not_empty():
    rec, _ = _receiver({}, ['imessage'])
    with pytest.raises(RuntimeError, match='source unavailable'):
        backend.read(rec, 'sotto_contacts', {'query': 'dana'})


def test_new_tools_are_read_only_listed_as_source_readers_and_validate_cursors():
    for name in ('sotto_calls', 'sotto_reminders', 'sotto_browser_history', 'sotto_contacts'):
        assert mcp.TOOLS[name]['annotations']['readOnlyHint'] is True
    assert 'sotto_calls' in mcp.SOURCE_READERS['whatsapp_calls']
    assert 'sotto_browser_history' in mcp.SOURCE_READERS['safari']
    assert 'sotto_contacts' in mcp.SOURCE_READERS['contacts'] and 'sotto_contacts' in mcp.SOURCE_READERS['google_contacts']
    assert mcp.SOURCE_READERS['screen_time'] == ('sotto_mac_context',)
    response = mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'sotto_calls', 'arguments': {'cursor': 'not-a-cursor'}}}, {'tools': ['sotto_calls']}, lambda *_: {})
    assert response['result']['structuredContent']['reason'] == 'invalid_arguments'
    assert 'cursor' in response['result']['structuredContent']['detail']


def test_one_contacts_side_failing_keeps_the_other():
    google = {'source': 'google_contacts', 'items': [{'name': 'Dana Reyes'}]}
    rec, _ = _receiver({}, ['contacts'])
    rec._runtime_source_authority = lambda **_: {'complete': True, 'revision': 'c' * 64, 'bridge_sources': ['contacts'],
                                                 'google_sources': ['google_contacts'], 'connector_sources': []}
    rec.RELAY = SimpleNamespace(mcp_call=lambda *_, **__: None)  # the Mac is asleep
    rec._sotto_adapter = lambda _: SimpleNamespace(read_owner=lambda operation, arguments: google)
    assert backend.read(rec, 'sotto_contacts', {'query': 'dana'}) == {
        'mac_contacts': {'status': 'unavailable'}, 'google_contacts': google}
