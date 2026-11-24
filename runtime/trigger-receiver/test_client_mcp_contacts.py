"""Client MCP reads name known numbers and look up compact Contacts cards (Nov 2026)."""
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import client_mcp as mcp
import client_mcp_backend as backend
import client_mcp_contacts as contacts

LIB = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff' / '_shared' / 'lib'
sys.path.insert(0, str(LIB))
import render_local  # noqa: E402
import textutil  # noqa: E402

_spec = importlib.util.spec_from_file_location('mcp_contacts_catalog', LIB / 'source_catalog.py')
CATALOG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CATALOG)
REVISION = 'c' * 64
CONTACTS = [
    {'name': 'Dana Reyes', 'phones': ['(415) 555-0101'], 'emails': ['dana@example.test'],
     'notes': '<sn>id:1/friendof:2</sn>\nMet at the 2009 hackathon. ' + 'x' * 400},
    {'name': 'Shared Line A', 'phones': ['+1 415 555 0199'], 'emails': []},
    {'name': 'Shared Line B', 'phones': ['4155550199'], 'emails': []},
]


def _receiver(payloads, bridge):
    before = {'complete': True, 'revision': REVISION, 'bridge_sources': list(bridge),
              'google_sources': [], 'connector_sources': []}
    calls = []

    def call(request, **_):
        calls.append(request['params'])
        return {'result': {'structuredContent': payloads[tuple(request['params']['arguments'].get('sources') or ())
                                                        if request['params']['name'] == 'read_local' else request['params']['name']]}}
    libs = {'source_catalog': CATALOG, 'render_local': render_local, 'textutil': textutil}
    rec = SimpleNamespace(_runtime_source_authority=lambda **_: before, RELAY=SimpleNamespace(mcp_call=call, bridge_connected=lambda: True),
                          _source_context=lambda: SimpleNamespace(unwrap_tool_result=lambda r: r['structuredContent']),
                          _load_shared_lib=libs.get)
    return rec, calls


BOOK = {'contacts': CONTACTS, 'source_status': {'contacts': 'ok'}}


def test_history_rows_name_a_number_only_its_one_card_owns():
    history = {'source': 'imessage', 'status': 'ok', 'complete': True, 'rows': [
        {'handle': '+14155550101', 'text': 'hi', 'is_from_me': False},
        {'handle': '+14155550199', 'text': 'shared', 'is_from_me': False},
        {'handle': '+16505550100', 'text': 'stranger', 'is_from_me': False}]}
    rec, calls = _receiver({'read_history': history, ('contacts',): BOOK}, ['imessage', 'contacts'])
    rows = backend.read(rec, 'sotto_message_history', {'source': 'imessage', 'since': 1, 'until': 2})['rows']
    assert rows[0]['handle_name'] == 'Dana Reyes'
    assert 'handle_name' not in rows[1] and 'handle_name' not in rows[2]
    assert {'name': 'read_local', 'arguments': {'since_hours': contacts.BOOK_HOURS, 'sources': ['contacts']}} in calls


def test_names_need_contacts_permission():
    history = {'source': 'imessage', 'status': 'ok', 'rows': [{'handle': '+14155550101', 'is_from_me': False}]}
    rec, calls = _receiver({'read_history': history, ('contacts',): BOOK}, ['imessage'])
    rows = backend.read(rec, 'sotto_message_history', {'source': 'imessage', 'since': 1, 'until': 2})['rows']
    assert 'handle_name' not in rows[0] and len(calls) == 1


def test_calls_and_search_results_carry_names():
    local = {'calls': [{'phone': '+14155550101', 'timestamp': '2026-11-09 10:00:00'}],
             'source_status': {'calls': 'ok'}}
    search = {'status': 'ok', 'coverage': {}, 'results': [{'source': 'imessage', 'title': '+14155550101',
                                                           'author': '+14155550101', 'body': 'x'}]}
    rec, _ = _receiver({('calls',): local, 'search_local': search, ('contacts',): BOOK}, ['calls', 'imessage', 'contacts'])
    assert backend.read(rec, 'sotto_mac_context', {'sources': ['calls']})['calls'][0]['phone_name'] == 'Dana Reyes'
    hit = backend.read(rec, 'sotto_search_local', {'query': 'x', 'sources': ['imessage'], 'since': 1, 'until': 2})['results'][0]
    assert hit['author_name'] == 'Dana Reyes'
    assert 'title_name' not in hit  # titles are free text, never an identity


def test_contacts_are_compact_and_a_query_finds_one_card_by_name_or_number():
    rec, calls = _receiver({('contacts',): BOOK}, ['contacts'])
    every = backend.read(rec, 'sotto_mac_context', {'sources': ['contacts']})['contacts']
    dana = next(row for row in every if row['name'] == 'Dana Reyes')
    assert '<sn>' not in dana['notes'] and len(dana['notes']) <= contacts.NOTE_CHARS
    for query in ('dana', '415-555-0101', 'dana@example'):
        found = backend.read(rec, 'sotto_mac_context', {'sources': ['contacts'], 'query': query})
        assert [row['name'] for row in found['contacts']] == ['Dana Reyes']
        assert found['contacts_query']['matched'] == 1
    assert calls[-1]['arguments']['since_hours'] == contacts.BOOK_HOURS  # a lookup reads the whole address book


def test_a_contacts_query_is_only_for_the_contacts_source():
    response = mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'sotto_mac_context', 'arguments': {'sources': ['calls'], 'query': 'dana'}}},
        {'tools': ['sotto_mac_context']}, lambda *_: {})
    assert response['result']['structuredContent']['reason'] == 'invalid_arguments'
    assert 'query' in response['result']['structuredContent']['detail']


def test_a_busy_server_waits_briefly_for_a_slot_instead_of_refusing(monkeypatch):
    monkeypatch.setattr(mcp, '_ACTIVE', threading.BoundedSemaphore(1))
    mcp._ACTIVE.acquire()
    threading.Timer(0.2, mcp._ACTIVE.release).start()
    started = time.monotonic()
    assert mcp.slot(wait=2) is True
    assert time.monotonic() - started >= 0.15  # noqa: PLR2004 - the 0.2 s release timer
    mcp._ACTIVE.release()


def test_a_still_busy_server_says_when_to_retry(monkeypatch):
    monkeypatch.setattr(mcp, '_ACTIVE', threading.BoundedSemaphore(1))
    mcp._ACTIVE.acquire()
    try:
        assert mcp.slot(wait=0.05) is False
    finally:
        mcp._ACTIVE.release()


@pytest.mark.parametrize('field', ['jid', 'sender_jid'])
def test_whatsapp_privacy_ids_never_get_a_name(field):
    local = {'whatsapp_calls': [{field: '14155550101@lid'}], 'source_status': {'whatsapp_calls': 'ok'}}
    rec, _ = _receiver({('whatsapp_calls',): local, ('contacts',): BOOK}, ['whatsapp_calls', 'contacts'])
    row = backend.read(rec, 'sotto_mac_context', {'sources': ['whatsapp_calls']})['whatsapp_calls'][0]
    assert field + '_name' not in row


def test_person_results_say_learning_detail_once_and_name_client_readers():
    learning = {'checkpoint': 'present', 'review_scope': 'partial', 'latest_attempt': {'status': 'succeeded', 'at': 't'},
                'observation_counts': {'rows': 3}, 'requested_window': {'since': 'a', 'until': 'b'}}
    saved = {'person_knowledge': {}, 'candidates': {'items': [{'name': 'Avery Q', 'canonical_id': 'c_1'}]},
             'coverage': {'graph': 'no_match', 'learning_scope': 'Source-wide ...',
                          'identity_resolution': {'status': 'name_only', 'next_action': 'search_contacts',
                                                  'saved_identity_discovery': {'reader': 'knowledge-query', 'argv': []}},
                          'historical_sources': [
                              {'source': 'imessage', 'status': 'ok', 'reader': 'read_history', 'learning': learning},
                              {'source': 'gmail', 'status': 'connected', 'reader': 'gmail_search', 'learning': learning},
                              {'source': 'granola', 'status': 'disabled', 'reader': 'gather_granola'}]}}
    rec, _ = _receiver({}, [])
    rec._find_sotto_script = lambda *parts: '/synthetic'
    rec._spawn_env = lambda _: {}
    rec.RUNTIME_KNOWLEDGE_READ = SimpleNamespace(perform=lambda *_, **__: saved)
    coverage = backend.read(rec, 'sotto_person', {'person': 'Avery'})['coverage']
    assert 'learning_scope' not in coverage
    assert 'saved_identity_discovery' not in coverage['identity_resolution']
    rows = {row['source']: row for row in coverage['historical_sources']}
    assert rows['imessage']['reader'] == 'sotto_message_history'
    assert rows['gmail']['reader'] == 'sotto_email_search'
    assert rows['granola']['reader'] == 'sotto_granola_history'
    assert rows['gmail']['learning'] == {'latest_attempt': 'succeeded', 'review_scope': 'partial',
                                         'detail': 'sotto_source_status.history_learning'}


def test_a_reminder_titled_with_a_number_gets_no_name():
    local = {'reminders': [{'title': '4155550101'}], 'source_status': {'reminders': 'ok'}}
    rec, _ = _receiver({('reminders',): local, ('contacts',): BOOK}, ['reminders', 'contacts'])
    assert 'title_name' not in backend.read(rec, 'sotto_reminders', {})['reminders'][0]


def test_combined_contacts_withholds_result_after_permission_changes():
    rec, _ = _receiver({('contacts',): BOOK}, ['contacts'])
    before = {**rec._runtime_source_authority(), 'google_sources': ['google_contacts']}
    after = {**before, 'bridge_sources': [], 'revision': 'd' * 64}
    snapshots = iter([before, before, before])
    rec._runtime_source_authority = lambda **_: next(snapshots, after)
    rec._sotto_adapter = lambda _: SimpleNamespace(
        read_owner=lambda *_: {'source': 'google_contacts', 'items': []})

    with pytest.raises(RuntimeError, match='authority changed'):
        backend.read(rec, 'sotto_contacts', {'query': 'Dana'})


@pytest.mark.parametrize('bad', [
    {'source_status': ['wrong-shape']}, {'source_coverage': ['wrong-shape']},
    {'source_coverage': {'contacts': None}}, {'source_coverage': None},
    {'contacts': [CONTACTS[0], 'bad row']},
    {'contacts': [CONTACTS[0], {'name': 'Second owner', 'phones': '4155550101'}]},
])
def test_malformed_book_never_blocks_history_or_supplies_a_name(bad):
    history = {'source': 'imessage', 'status': 'ok', 'rows': [{'handle': '+14155550101'}]}
    rec, _ = _receiver({'read_history': history, ('contacts',): {**BOOK, **bad}}, ['imessage', 'contacts'])
    row = backend.read(rec, 'sotto_message_history', {'source': 'imessage', 'since': 1, 'until': 2})['rows'][0]
    assert row == {'handle': '+14155550101'}
