"""External source adapters use existing readers with scoped, current authority."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import client_mcp_backend as backend

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
_spec = importlib.util.spec_from_file_location('mcp_source_catalog_fixture', PACK / '_shared/lib/source_catalog.py')
CATALOG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CATALOG)
REVISION = 'a' * 64


def receiver(payload, *, bridge=(), google=(), connectors=(), after=None):
    before = {'complete': True, 'revision': REVISION, 'bridge_sources': list(bridge),
              'google_sources': list(google), 'connector_sources': list(connectors)}
    states = iter([before, after if after is not None else before])
    calls = []
    rec = SimpleNamespace(_runtime_source_authority=lambda **_: next(states),
        RELAY=SimpleNamespace(mcp_call=lambda request, **_: calls.append(request) or
            {'result': {'structuredContent': payload}}),
        _source_context=lambda: SimpleNamespace(unwrap_tool_result=lambda result: result['structuredContent']),
        _load_shared_lib=lambda name: CATALOG if name == 'source_catalog' else None,
        _sotto_adapter=lambda name: SimpleNamespace(read_owner=lambda operation, arguments:
            calls.append((operation, arguments)) or payload))
    return rec, calls, before


@pytest.mark.parametrize('source', CATALOG.SOURCE_FIELDS)
def test_mac_context_routes_every_existing_source_and_projects_scope(source):
    receipt = {'status': 'partial', 'effective_window': {'since': None, 'until': None, 'basis': 'inventory'},
               'rows_returned': 1, 'omitted_rows': None, 'query_row_caps': [20]}
    payload = {'generated_at': '2026-11-01T12:00:00Z', 'window_hours': 24,
               **{field: [{'private': owner}] for owner, fields in CATALOG.SOURCE_FIELDS.items() for field in fields},
               'source_status': {key: 'partial' for key in CATALOG.SOURCE_FIELDS},
               'source_coverage': {key: receipt for key in CATALOG.SOURCE_FIELDS}}
    rec, calls, _ = receiver(payload, bridge=CATALOG.SOURCE_FIELDS)
    result = backend.read(rec, 'sotto_mac_context', {'sources': [source]})
    assert calls[0]['params'] == {'name': 'read_local', 'arguments': {'since_hours': 24, 'sources': [source]}}
    assert set(result) == {'generated_at', 'window_hours', 'source_status', 'source_coverage',
                           'client_page', *CATALOG.SOURCE_FIELDS[source]}
    assert result['source_coverage'] == {source: receipt}
    assert result['source_status'] == {source: 'partial'}


def test_mac_context_keeps_companion_coverage_and_explicit_window():
    payload = {'imessage': [], 'deferred_unread_imessage': [],
               'source_coverage': {'imessage': {'status': 'empty'}, 'deferred_unread_imessage': {'status': 'failed'}},
               'source_status': {'imessage': 'partial', 'deferred_unread_imessage': 'degraded'}}
    rec, calls, _ = receiver(payload, bridge=['imessage'])
    result = backend.read(rec, 'sotto_mac_context', {'sources': ['imessage'], 'since_hours': 8760})
    assert result.pop('client_page')['complete'] is True
    assert result == payload
    assert calls[0]['params']['arguments']['since_hours'] == 8760


@pytest.mark.parametrize('name,args,source', [
    ('sotto_mac_context', {'sources': ['chrome']}, 'chrome'),
    ('sotto_note_get', {'id': 42}, 'apple_notes'),
])
def test_local_reads_check_source_before_and_after(name, args, source):
    rec, calls, _ = receiver({}, bridge=[])
    with pytest.raises(RuntimeError, match='source unavailable'):
        backend.read(rec, name, args)
    assert calls == []
    for patch in ({'bridge_sources': []}, {'revision': 'b' * 64}, {'complete': False}):
        before = {'complete': True, 'revision': REVISION, 'bridge_sources': [source],
                  'google_sources': [], 'connector_sources': []}
        payload = {'source_status': {source: 'ok'}} if name == 'sotto_mac_context' else {'status': 'ok'}
        rec, calls, _ = receiver(payload, bridge=[source], after={**before, **patch})
        with pytest.raises(RuntimeError):
            backend.read(rec, name, args)
        assert len(calls) == 1


def test_failed_mac_reads_carry_a_fix_hint_for_their_failure():
    coverage = {'chrome': {'status': 'failed', 'failure_category': 'locked'},
                'calls': {'status': 'ok'}, 'safari': {'status': 'failed'}}
    payload = {'chrome_history': [], 'calls': [], 'safari_history': [],
               'source_status': {'chrome': 'degraded', 'calls': 'ok', 'safari': 'needs_fda'},
               'source_coverage': coverage}
    result = _mac_read(payload, {'sources': ['chrome', 'calls', 'safari']})
    assert result['source_hints'] == {'chrome': CATALOG.FAILURE_HINTS['locked'],
                                      'safari': CATALOG.FAILURE_HINTS['permission_denied']}
    clean = _mac_read({'calls': [], 'source_status': {'calls': 'ok'}}, {'sources': ['calls']})
    assert 'source_hints' not in clean


def test_unconfigured_document_search_says_how_to_configure_it():
    payload = {'status': 'ok', 'results': [], 'coverage': {
        'recent_files': {'status': 'not_configured', 'complete': False},
        'apple_notes': {'status': 'ok', 'complete': True}}}
    rec, _, _ = receiver(payload, bridge=['recent_files', 'apple_notes'])
    result = backend.read(rec, 'sotto_search_local', {'query': 'x', 'sources': ['recent_files', 'apple_notes'],
                                                     'since': 1, 'until': 2})
    assert result['source_hints'] == {'recent_files': CATALOG.SEARCH_HINTS['not_configured']}


def test_exact_note_uses_existing_reader():
    payload = {'status': 'ok', 'id': 42, 'body': 'Synthetic note'}
    rec, calls, _ = receiver(payload, bridge=['apple_notes'])
    result = backend.read(rec, 'sotto_note_get', {'id': 42})
    assert result.pop('client_page')['complete'] is True
    assert result == payload
    assert calls[0]['params']['name'] == 'get_note'
    assert calls[0]['params']['arguments'] == {'id': 42}


@pytest.mark.parametrize('coverage', [[], {'chrome': {'status': 'ok', 'private': 'not metadata'}}])
def test_mac_context_withholds_malformed_coverage(coverage):
    rec, _, _ = receiver({'chrome_history': [], 'source_status': {'chrome': 'ok'},
                          'source_coverage': coverage}, bridge=['chrome'])
    with pytest.raises(RuntimeError):
        backend.read(rec, 'sotto_mac_context', {'sources': ['chrome']})


def test_old_bridge_missing_coverage_is_not_fabricated():
    rec, _, _ = receiver({'chrome_history': [], 'source_status': {'chrome': 'ok'}}, bridge=['chrome'])
    assert 'source_coverage' not in backend.read(rec, 'sotto_mac_context', {'sources': ['chrome']})


@pytest.mark.parametrize('name,args,operation,expected', [
    ('sotto_contacts_search', {'query': 'Cedar'}, 'contacts_search', {'query': 'Cedar', 'max': 10}),
    ('sotto_contacts_get', {'resource_name': 'people/c123'}, 'contacts_get', {'resource_name': 'people/c123'}),
])
def test_google_contacts_use_google_consent_and_canonical_reader(name, args, operation, expected):
    result = {'source': 'google_contacts', 'items': []}
    rec, calls, _ = receiver(result, google=['google_contacts'])
    assert backend.read(rec, name, args) == result
    assert calls == [(operation, expected)]
    rec, calls, _ = receiver(result, bridge=['contacts'])
    with pytest.raises(RuntimeError):
        backend.read(rec, name, args)
    assert not calls
    before = {'complete': True, 'revision': REVISION, 'bridge_sources': [],
              'google_sources': [], 'connector_sources': []}
    rec, calls, _ = receiver(result, google=['google_contacts'], after=before)
    with pytest.raises(RuntimeError):
        backend.read(rec, name, args)
    assert calls == [(operation, expected)]


def test_granola_reuses_canonical_guarded_read_without_extra_authority_probe():
    rec, calls, before = receiver({}, connectors=['granola'])
    rec._find_sotto_script = lambda *parts: '/synthetic/' + '/'.join(parts)
    rec._spawn_env = lambda _: {'SOTTO_DATA': '/synthetic/data'}
    def perform(body, **kwargs):
        calls.append((body, kwargs))
        assert kwargs['initial_authority'] == before
        assert kwargs['authority']()['connector_sources'] == ['granola']
        return {'source': 'granola', 'meetings': [], 'complete': False}
    rec.RUNTIME_KNOWLEDGE_READ = SimpleNamespace(perform=perform)
    assert backend.read(rec, 'sotto_granola_history', {'meeting_id': 'm1'})['complete'] is False
    body, kwargs = calls[0]
    assert body == {'operation': 'granola_history', 'arguments': {'argv': ['--days', '7', '--meeting-id', 'm1']},
                    'source_revision': REVISION}
    assert kwargs['script'] == '/synthetic/_shared/scripts/granola_history.py'


@pytest.mark.parametrize('payload', [{}, {'error': 'private error'},
    {'chrome_history': []}, {'source_status': {'chrome': 'made_up'}},
    {'error_code': 'failed', 'source_status': {'chrome': 'ok'}}])
def test_mac_context_does_not_turn_failed_or_unobserved_reads_into_empty(payload):
    rec, _, _ = receiver(payload, bridge=['chrome'])
    with pytest.raises(RuntimeError):
        backend.read(rec, 'sotto_mac_context', {'sources': ['chrome']})


def _mac_read(payload, arguments):
    rec, _, _ = receiver(payload, bridge=arguments['sources'])
    return backend.read(rec, 'sotto_mac_context', arguments)


def test_large_contacts_page_through_external_dispatch_without_missing_or_duplicate_rows():
    import json
    import client_mcp as mcp
    contacts = [{'name': f'Synthetic Contact {i:04d}', 'phones': [f'+1555000{i:04d}'],
                 'emails': [f'contact{i:04d}@example.test']} for i in range(2102)]
    coverage = {'contacts': {'status': 'ok', 'rows_returned': len(contacts)}}
    payload = {'contacts': contacts, 'contacts_total': len(contacts),
               'source_status': {'contacts': 'ok'}, 'source_coverage': coverage}
    assert len(json.dumps(payload).encode()) > mcp.MAX_RESULT
    args = {'sources': ['contacts'], 'since_hours': 168}
    seen = []
    while True:
        # The native Contacts reader does not promise row order across reads.
        payload['contacts'] = list(reversed(payload['contacts']))
        payload['generated_at'] = str(len(seen))
        result = _mac_read(payload, args)
        response = mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'sotto_mac_context', 'arguments': args}},
            {'tools': ['sotto_mac_context']}, lambda *_: result)
        assert response['result']['isError'] is False
        assert len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) <= 32768
        assert result['source_coverage'] == coverage
        assert result['contacts_total'] == len(contacts)
        seen.extend(result['contacts'])
        page = result['client_page']
        assert page['returned_rows'] == len(result['contacts']) and page['total_rows'] == len(contacts)
        if page['complete']:
            assert page['next_cursor'] is None
            break
        assert page['returned_rows'] > 0
        args['cursor'] = page['next_cursor']
    assert sorted(seen, key=lambda row: row['name']) == contacts


def test_mac_pages_keep_field_and_time_order_and_reject_changed_snapshot():
    calls = [{'timestamp': str(200 - index), 'phone': 'x' * 600} for index in range(100)]
    chats = [{'text': 'y' * 600, 'timestamp': str(300 - index)} for index in range(100)]
    payload = {'calls': calls, 'whatsapp_calls': chats,
               'source_status': {'calls': 'ok', 'whatsapp_calls': 'partial'}}
    args = {'sources': ['calls', 'whatsapp_calls']}
    result = _mac_read(payload, args)
    assert result['calls'] == calls[:len(result['calls'])]
    assert result['whatsapp_calls'] == []
    cursor = result['client_page']['next_cursor']
    seen_calls, seen_chats = result['calls'][:], []
    follow = cursor
    while follow:
        page = _mac_read(payload, {**args, 'cursor': follow})
        seen_calls.extend(page['calls'])
        seen_chats.extend(page['whatsapp_calls'])
        follow = page['client_page']['next_cursor']
    assert seen_calls == calls and seen_chats == chats
    payload['calls'] = [{'timestamp': 'new', 'phone': 'new'}, *calls]
    changed = _mac_read(payload, {**args, 'cursor': cursor})
    assert changed['status'] == 'changed' and 'calls' not in changed
    assert changed['client_page']['complete'] is False


def test_contacts_inventory_pages_after_time_ranked_sources():
    contacts = [{'name': f'Synthetic Contact {i:03d}', 'notes': 'n' * 200} for i in range(300)]
    payload = {'calls': [{'timestamp_utc': '2026-11-05T14:51:31Z', 'phone': '+15550000001'}],
               'contacts': contacts, 'contacts_total': 300,
               'reminders': [{'title': 'Synthetic reminder'}],
               'whatsapp_calls': [{'timestamp': '2026-11-05 06:51:31'}],
               'source_status': {'calls': 'ok', 'contacts': 'ok', 'reminders': 'ok', 'whatsapp_calls': 'ok'}}
    args = {'sources': ['calls', 'contacts', 'reminders', 'whatsapp_calls'], 'since_hours': 24}
    first = _mac_read(payload, args)
    assert first['calls'] and first['reminders'] and first['whatsapp_calls']
    assert first['client_page']['next_cursor'], 'fixture must need a second page'


def test_mac_page_rejects_cursor_from_old_contacts_first_order():
    contacts = [{'name': 'A'}]
    calls = [{'name': 'B'}]
    sources = ['calls', 'contacts']
    # Old receivers hashed payload order and returned the contact at offset zero.
    legacy = {'sources': sources, 'since_hours': 24, 'data': [('contacts', contacts), ('calls', calls)]}
    version = backend.hashlib.sha256(backend._encoded(legacy)).hexdigest()
    page = backend._local_page({'contacts': contacts, 'calls': calls},
                               {'sources': sources, 'cursor': version + ':1'})
    assert page['status'] == 'changed'
    assert 'contacts' not in page and 'calls' not in page


def test_busy_history_window_requires_restart_without_mixing_pages():
    rows = [{'title': str(index), 'url': 'https://example.com/' + 'x' * 600}
            for index in range(100)]
    payload = {'chrome_history': rows, 'source_status': {'chrome': 'ok'}}
    args = {'sources': ['chrome'], 'since_hours': 24}
    first = _mac_read(payload, args)
    cursor = first['client_page']['next_cursor']
    assert cursor
    payload['chrome_history'] = [{'title': 'new', 'url': 'https://example.com/new'}, *rows[:-1]]
    changed = _mac_read(payload, {**args, 'cursor': cursor})
    assert changed['status'] == 'changed' and 'chrome_history' not in changed
    assert changed['client_page']['complete'] is False
    restarted = _mac_read(payload, {**args, 'cursor': 'first'})
    assert restarted['chrome_history'][0]['title'] == 'new'


@pytest.mark.parametrize('cursor', ['bad', 'a' * 64 + ':999999999', 'a' * 64 + ':0'])
def test_mac_invalid_cursor_requires_restart(cursor):
    payload = {'contacts': [], 'contacts_total': 0, 'source_status': {'contacts': 'ok'}}
    result = _mac_read(payload, {'sources': ['contacts'], 'cursor': cursor})
    assert result['status'] == 'changed'
    assert result['client_page']['next_cursor'] is None


def test_mac_oversized_individual_row_fails_without_silent_loss():
    payload = {'contacts': [{'name': 'Synthetic', 'notes': 'x' * 40000}],
               'contacts_total': 1, 'source_status': {'contacts': 'ok'}}
    with pytest.raises(RuntimeError, match='page byte limit'):
        _mac_read(payload, {'sources': ['contacts']})


def test_quote_heavy_note_pages_preserve_reader_partial_and_reassemble_original():
    import json
    import client_mcp as mcp
    body = '"' * 100000
    payload = {'id': 1, 'status': 'degraded', 'title': 'Synthetic', 'body': body,
               'partial': True, 'format': 'markdown', 'coverage': 'Original reader limits'}
    assert len(json.dumps(payload).encode()) > mcp.MAX_RESULT
    args, parts = {'id': 1}, []
    while True:
        rec, calls, _ = receiver(payload, bridge=['apple_notes'])
        result = backend.read(rec, 'sotto_note_get', args)
        assert calls[0]['params']['arguments'] == {'id': 1}
        response = mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'sotto_note_get', 'arguments': args}},
            {'tools': ['sotto_note_get']}, lambda *_: result)
        assert response['result']['isError'] is False
        assert result['partial'] is True and result['status'] == 'degraded'
        assert result['coverage'] == payload['coverage']
        assert result['client_page']['total_chars'] == len(body)
        assert len(result['body']) <= 3000
        parts.append(result['body'])
        if result['client_page']['complete']:
            break
        args['cursor'] = result['client_page']['next_cursor']
    assert ''.join(parts) == body


def test_note_changed_content_restarts_without_returning_different_note_tail():
    payload = {'status': 'ok', 'id': 1, 'body': 'a' * 6000, 'partial': False}
    rec, _, _ = receiver(payload, bridge=['apple_notes'])
    first = backend.read(rec, 'sotto_note_get', {'id': 1})
    rec, _, _ = receiver({**payload, 'body': 'b' * 6000}, bridge=['apple_notes'])
    changed = backend.read(rec, 'sotto_note_get', {'id': 1, 'cursor': first['client_page']['next_cursor']})
    assert changed['status'] == 'changed' and 'body' not in changed


def test_exact_note_refuses_a_different_note_identifier():
    rec, _, _ = receiver({'status': 'ok', 'id': 43, 'body': 'Wrong original note'}, bridge=['apple_notes'])
    with pytest.raises(RuntimeError):
        backend.read(rec, 'sotto_note_get', {'id': 42})


def test_native_note_paging_negotiates_cursor_and_does_not_repage():
    page = {'status': 'ok', 'id': 42, 'body': 'x' * 3000,
            'client_page': {'offset': 3000, 'returned_chars': 3000, 'total_chars': 9000,
                            'complete': False, 'next_cursor': REVISION + ':6000',
                            'byte_limit': backend.PAGE_BYTES}, 'note_page_version': 1}
    rec, calls, _ = receiver(page, bridge=['apple_notes'])
    result = backend.read(rec, 'sotto_note_get', {'id': 42, 'cursor': REVISION + ':3000'})
    assert calls[0]['params']['_meta'] == {'sotto/accepts': ['note_page_v1'],
                                         'sotto/note_cursor': REVISION + ':3000'}
    assert result['body'] == page['body']
    assert result['client_page'] == page['client_page']
    assert 'note_page_version' not in result


@pytest.mark.parametrize('patch', [
    {'body': 'x' * 3001}, {'id': 43}, {'note_page_version': True},
    {'status': 'wrong'}, {'partial': 'true'}, {'title': []}, {'note_page_version': None},
    {'client_page': {}}, {'title': 'x' * 40000},
    {'client_page': {'offset': 0, 'returned_chars': 1, 'total_chars': 2,
                     'complete': False, 'next_cursor': REVISION + ':0', 'byte_limit': 32768}},
])
def test_native_note_page_rejects_malformed_or_oversized_payload(patch):
    page = {'status': 'ok', 'id': 42, 'body': 'x', 'note_page_version': 1,
            'client_page': {'offset': 0, 'returned_chars': 1, 'total_chars': 1,
                            'complete': True, 'next_cursor': None, 'byte_limit': backend.PAGE_BYTES}}
    rec, _, _ = receiver({**page, **patch}, bridge=['apple_notes'])
    with pytest.raises(RuntimeError, match='Bridge note page invalid'):
        backend.read(rec, 'sotto_note_get', {'id': 42})
