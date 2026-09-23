"""Prove extraction, prompt wiring and permission changes, without a paid model call."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import compose_brief as cb
import source_context as sc
from source_catalog import SOURCE_FIELDS, SOURCE_LABELS, LOCAL_SNAPSHOT_TTL_HOURS


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)


def stamp(hours=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def source_status(source):
    return next(r for r in sc.source_health()['sources'] if r['source'] == source)


def test_partial_reads_keep_valid_evidence_usable_in_the_actual_prompt(monkeypatch):
    monkeypatch.setenv('X_BEARER_TOKEN', 'fixture')
    inputs = {'type': 'morning', 'local': {
        'chrome_history': [{'domain': 'partial-profile.example', 'visit_count': 3}],
        'source_status': {'chrome': 'degraded'}}, 'google': {},
        'x_context': {'attendees': [{'handle': 'fixture', 'recent_posts': [{'text': 'Valid X fixture'}]}],
                      'warnings': ['Bookmark access unavailable'],
                      'request_status': {'succeeded': True, 'status': 'degraded'}}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert 'partial-profile.example' in prompt and 'Valid X fixture' in prompt
    assert 'Partial coverage: Chrome History, X (attendee context)' in prompt
    assert 'Unavailable on this device: Chrome' not in prompt
    assert 'Unavailable on this device: X' not in prompt
    assert 'Use the returned evidence' in prompt


def test_x_health_distinguishes_configuration_success_and_failure(monkeypatch, tmp_path):
    for key in ('X_BEARER_TOKEN', 'X_USER_ACCESS_TOKEN', 'X_OWNER_USER_ID', 'SOTTO_X_STUB'):
        monkeypatch.delenv(key, raising=False)
    assert source_status('x')['status'] == 'unconfigured'
    monkeypatch.setenv('X_BEARER_TOKEN', 'fixture')
    assert source_status('x')['status'] == 'unverified'
    sc.record_x_status({'attendees': [{'private': 'never save me'}], 'request_status': {
        'configured': True, 'succeeded': True, 'status': 'ok', 'error_codes': []}})
    success = source_status('x')['last_success_at']
    assert success and source_status('x')['status'] == 'ok'
    sc.record_x_status({'request_status': {'configured': True, 'succeeded': False,
        'status': 'degraded', 'error_codes': ['rate_limited', 'private provider body']}})
    sc.record_x_status({'request_status': {'configured': True, 'succeeded': False,
        'status': 'unverified', 'error_codes': []}})
    assert source_status('x')['status'] == 'degraded'
    assert source_status('x')['error_codes'] == ['rate_limited']
    assert source_status('x')['last_success_at'] == success
    saved = (tmp_path / 'config/source-state.json').read_text()
    assert 'never save me' not in saved and 'private provider body' not in saved
    monkeypatch.delenv('X_BEARER_TOKEN')
    assert source_status('x')['status'] == 'unconfigured'


@pytest.mark.parametrize('mode', ['self-host', 'managed'])
def test_cached_x_and_bookmarks_follow_current_credentials_through_delivery(monkeypatch, mode):
    import delivery_effects
    from render_local import _format_x_context
    monkeypatch.delenv('SOTTO_X_STUB', raising=False)
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    monkeypatch.setenv('X_BEARER_TOKEN', 'fixture-public')
    monkeypatch.setenv('X_USER_ACCESS_TOKEN', 'fixture-user')
    monkeypatch.setenv('X_OWNER_USER_ID', '1')
    payload = {'attendees': [{'handle': 'fixture', 'recent_posts': [{'text': 'Public post fixture'}],
                             'bookmarks': [{'text': 'Private bookmark fixture'}]}]}
    before = sc.permission_fingerprint()
    sources = sc.used_sources(x_context=payload)
    effects = [{'kind': 'source_permissions', 'sources': sources}]
    assert sources == ['x', 'x_bookmarks'] and delivery_effects.valid(effects)
    assert 'Private bookmark fixture' in _format_x_context({'x_context': payload})
    monkeypatch.delenv('X_USER_ACCESS_TOKEN')
    assert before != sc.permission_fingerprint()
    assert not delivery_effects.valid(effects)
    text = _format_x_context({'x_context': payload})
    assert 'Public post fixture' in text and 'Private bookmark fixture' not in text
    monkeypatch.delenv('X_BEARER_TOKEN')
    assert _format_x_context({'x_context': payload}) == ''
    assert payload['attendees'][0]['bookmarks']  # no mutation of the saved evidence


@pytest.mark.parametrize('source,fields', SOURCE_FIELDS.items())
def test_successful_empty_extraction_is_observed_for_every_local_source(source, fields):
    payload = {field: [] for field in fields}
    if source == 'contacts':
        payload['contacts_total'] = 0
    if source == 'screen_time':
        payload['screen_time'] = {'top_apps': [], 'total_minutes': 0}
    sc.record_bridge_status({**payload, 'generated_at': stamp(), 'source_status': {source: 'ok'}})
    assert source_status(source)['status'] == 'empty'
    assert set(source_status(source)['field_counts']) == set(fields)


def test_probe_cannot_erase_failed_extraction_or_freshen_old_activity():
    sc.record_bridge_status({'generated_at': stamp(3), 'source_status': {'chrome': 'ok'},
                             'chrome_history': [], 'search_queries': []})
    sc.record_bridge_status({'generated_at': stamp(2), 'source_status': {'chrome': 'unavailable'},
                             'chrome_history': [], 'search_queries': []})
    sc.record_bridge_status({'last_seen': stamp(), 'sources': {'chrome': 'ok'}})
    assert source_status('chrome')['status'] == 'unavailable'
    sc.record_bridge_status({'generated_at': stamp(), 'source_status': {'chrome': 'ok'},
                             'chrome_history': [], 'search_queries': []})
    assert source_status('chrome')['status'] == 'empty'


@pytest.mark.parametrize('read_status,expected', [('ok', 'ok'), ('degraded', 'degraded')])
def test_health_during_wake_read_does_not_discard_extraction(read_status, expected):
    # read_local.generated_at is stamped before gathering. The reverse connection can answer
    # health while the independent wake-push reader is still collecting its payload.
    started, probed = stamp(1), stamp(0.5)
    sc.record_bridge_status({'last_seen': probed, 'sources': {'chrome': 'ok'}})
    sc.record_bridge_status({'generated_at': started, 'source_status': {'chrome': read_status},
                            'chrome_history': [{'domain': 'fixture.example'}], 'search_queries': []})
    result = source_status('chrome')
    assert result['status'] == expected
    assert result['observed_at'] == started
    assert result['field_counts']['chrome_history'] == 1


def test_delayed_read_cannot_replace_newer_read_or_access_failure(tmp_path):
    read = {'source_status': {'chrome': 'ok'}, 'chrome_history': [], 'search_queries': []}
    newest = stamp(1)
    sc.record_bridge_status({**read, 'generated_at': newest})
    sc.record_bridge_status({'last_seen': stamp(0.5), 'sources': {'chrome': 'needs_fda'}})
    sc.record_bridge_status({**read, 'generated_at': stamp(2),
                            'chrome_history': [{'domain': 'older.example'}]})
    result = source_status('chrome')
    assert result['status'] == 'needs_fda'
    assert result['observed_at'] == newest
    assert result['field_counts']['chrome_history'] == 0


def test_delayed_read_after_disable_cannot_restore_counts_or_consent(tmp_path):
    read = {'source_status': {'chrome': 'ok'}, 'chrome_history': [], 'search_queries': []}
    original = stamp(3)
    sc.record_bridge_status({**read, 'generated_at': original})
    sc.record_bridge_status({'last_seen': stamp(1), 'sources': {'chrome': 'disabled'}})
    sc.record_bridge_status({**read, 'generated_at': stamp(2),
                            'chrome_history': [{'domain': 'revoked.example'}]})
    assert not sc.allowed('chrome')
    assert source_status('chrome')['status'] == 'disabled'
    row = json.loads((tmp_path / 'config/source-state.json').read_text())['sources']['chrome']
    assert row['read']['observed_at'] == original
    assert row['read']['field_counts']['chrome_history'] == 0


@pytest.mark.parametrize('delayed', [False, True])
def test_disabled_read_cannot_make_reenabled_access_look_disabled(delayed):
    read = {'generated_at': stamp(2), 'source_status': {'chrome': 'disabled'},
            'chrome_history': [], 'search_queries': []}
    health = {'last_seen': stamp(1), 'sources': {'chrome': 'ok'}}
    for payload in (health, read) if delayed else (read, health):
        sc.record_bridge_status(payload)
    assert sc.allowed('chrome')
    assert source_status('chrome')['status'] == 'unverified'


def test_old_read_is_stale_even_while_healthy_probe_keeps_arriving():
    sc.record_bridge_status({'generated_at': stamp(LOCAL_SNAPSHOT_TTL_HOURS + 1),
                             'source_status': {'recent_files': 'ok'},
                             'recent_files': [{'filename': 'private-example.pdf'}]})
    sc.record_bridge_status({'sources': {'recent_files': 'ok'}, 'last_seen': stamp()})
    assert source_status('recent_files')['status'] == 'stale'
    assert 'private-example' not in json.dumps(sc.source_health())


def test_healthy_probe_without_read_is_unverified_not_empty():
    sc.record_bridge_status({'sources': {'recent_files': 'ok'}})
    assert source_status('recent_files')['status'] == 'unverified'


def test_new_access_failure_is_visible_without_erasing_last_read():
    sc.record_bridge_status({'generated_at': stamp(1), 'source_status': {'chrome': 'ok'},
                             'chrome_history': [], 'search_queries': []})
    before = source_status('chrome')['observed_at']
    sc.record_bridge_status({'sources': {'chrome': 'needs_fda'}})
    assert source_status('chrome')['status'] == 'needs_fda'
    assert source_status('chrome')['observed_at'] == before


def test_malformed_or_missing_payload_does_not_claim_healthy_empty():
    sc.record_bridge_status({'source_status': {'recent_files': 'ok'}, 'recent_files': 'invalid'})
    assert source_status('recent_files')['status'] == 'partial'
    sc.record_bridge_status({'source_status': {'chrome': 'ok'}})
    assert source_status('chrome')['status'] == 'partial'
    sc.record_bridge_status({'source_status': {'chrome': 'ok'}, 'chrome_history': []})
    assert source_status('chrome')['status'] == 'partial'
    sc.record_bridge_status({'source_status': {'screen_time': {'unexpected': True}}})
    assert source_status('screen_time')['status'] == 'unverified'
    sc.record_bridge_status({'source_status': {'screen_time': 'ok'}, 'screen_time': []})
    assert source_status('screen_time')['status'] == 'partial'


def test_clock_ahead_cannot_block_a_later_disable(tmp_path):
    sc.record_bridge_status({'generated_at': stamp(-365 * 24), 'source_status': {'chrome': 'ok'},
                             'chrome_history': [], 'search_queries': []})
    sc.record_bridge_status({'sources': {'chrome': 'disabled'}})
    assert not sc.allowed('chrome')
    # Heal a future epoch left by the previous version as well.
    path = tmp_path / 'config/source-state.json'
    state = json.loads(path.read_text())
    state['sources']['chrome'].update(status='ok', observed_epoch=32503680000)
    path.write_text(json.dumps(state))
    sc.record_bridge_status({'sources': {'chrome': 'disabled'}})
    assert not sc.allowed('chrome')


def test_corrected_clock_cannot_block_a_timestamped_disable_or_let_a_straddling_read_restore_it(monkeypatch, tmp_path):
    arrival = [datetime(2026, 9, 22, 12, tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return arrival[0]
    monkeypatch.setattr(sc, 'datetime', Clock)
    ahead = timedelta(minutes=5)
    read = {'source_status': {'chrome': 'ok'}, 'chrome_history': [{'domain': 'must-not-leak.example'}],
            'search_queries': []}
    # A read while the Mac runs five minutes ahead pins the ordering watermark in the future.
    sc.record_bridge_status({**read, 'generated_at': (arrival[0] + ahead).isoformat()})
    # A second read starts, still stamped ahead; then NTP corrects the clock and the user disables
    # Chrome. The probe carries the true time, which every stored stamp already exceeds.
    straddling = {**read, 'generated_at': (arrival[0] + ahead + timedelta(seconds=10)).isoformat()}
    arrival[0] += timedelta(seconds=20)
    sc.record_bridge_status({'last_seen': arrival[0].isoformat(), 'sources': {'chrome': 'disabled'}})
    assert not sc.allowed('chrome')
    # The straddling read completes after the toggle with its future stamp: it cannot restore access.
    arrival[0] += timedelta(seconds=5)
    sc.record_bridge_status(straddling)
    assert not sc.allowed('chrome')
    assert 'chrome_history' not in sc.project_local(straddling)
    row = json.loads((tmp_path / 'config/source-state.json').read_text())['sources']['chrome']
    assert row['status'] == 'disabled' and row['read']['field_counts']['chrome_history'] == 1
    # Only the user's later re-enable, reported by a probe under the corrected clock, restores it.
    arrival[0] += timedelta(seconds=5)
    sc.record_bridge_status({'last_seen': arrival[0].isoformat(), 'sources': {'chrome': 'ok'}})
    assert sc.allowed('chrome')


def test_stale_pre_upgrade_receipt_cannot_be_restored_by_a_read_in_flight(tmp_path):
    # Rows from the previous version carry only a server-capped observed_epoch. A read that was
    # running when the disable landed, stamped by a Mac running ahead, still cannot restore access.
    path = tmp_path / 'config/source-state.json'
    path.parent.mkdir()
    now = datetime.now(timezone.utc)
    path.write_text(json.dumps({'sources': {'chrome': {
        'status': 'disabled', 'observed_at': now.isoformat(), 'observed_epoch': now.timestamp()}}}))
    sc.record_bridge_status({'generated_at': (now + timedelta(minutes=5)).isoformat(),
                             'source_status': {'chrome': 'ok'}, 'chrome_history': [], 'search_queries': []})
    assert not sc.allowed('chrome')


@pytest.mark.parametrize('raw', [
    '{broken', '{"sources": []}', '{"sources": {"chrome": "broken"}}', '[]', 'null',
    '{"sources": {"chrome": {"status": null}}}', '{"sources": {"chrome": {"status": []}}}',
    '{"sources": {"chrome": {}}}', '{"sources": {"chrome": {"status": "bogus"}}}',
])
@pytest.mark.parametrize('writer', ['bridge', 'x'])
def test_metadata_writers_cannot_erase_an_unreadable_consent_receipt(tmp_path, raw, writer):
    path = tmp_path / 'config/source-state.json'
    path.parent.mkdir()
    path.write_text(raw)
    assert not sc.allowed('chrome')
    # Refused, never repaired, and never fatal: the brief path calls both writers unguarded.
    if writer == 'bridge':
        sc.record_bridge_status({'source_status': {'recent_files': 'ok'}, 'recent_files': []})
    else:
        sc.record_x_status({'request_status': {'status': 'ok', 'succeeded': True}})
    assert path.read_text() == raw
    assert not sc.allowed('chrome')
    assert sc.source_health() == {'status': 'unavailable', 'sources': []}


def test_unreadable_consent_receipt_still_composes_a_brief_without_local_sources(tmp_path):
    path = tmp_path / 'config/source-state.json'
    path.parent.mkdir()
    path.write_text('{"sources": {"chrome": {"status": "bogus"}}}')
    local = {'generated_at': stamp(), 'source_status': {'chrome': 'ok', 'imessage': 'ok'},
             'chrome_history': [{'domain': 'must-not-leak.example', 'visit_count': 3}], 'search_queries': [],
             'imessage': [{'text': 'must not leak either', 'handle': '+15550001111'}]}
    saved = cb._save_local_snapshot(local)  # the writer inside must not raise
    assert saved.get('_consent_unreadable') is True
    assert 'chrome_history' not in saved and 'imessage' not in saved
    prompt = cb.build_prompt(cb._load_prompt(), {'type': 'morning', 'local': saved, 'google': {}})
    assert 'Local Sources Withheld' in prompt
    assert 'must-not-leak.example' not in prompt and 'must not leak either' not in prompt
    assert path.read_text() == '{"sources": {"chrome": {"status": "bogus"}}}'
    # Once repaired, the marker does not linger on the cached snapshot.
    path.unlink()
    assert '_consent_unreadable' not in cb._local_fallback({})


def test_new_source_with_an_unusable_stamp_writes_no_empty_row(tmp_path):
    sc.record_bridge_status({'generated_at': stamp(), 'sources': {'chrome': 'ok'}})
    sc.record_bridge_status({'generated_at': '1969-12-31T00:00:00+00:00', 'sources': {'recent_files': 'ok'}})
    state = json.loads((tmp_path / 'config/source-state.json').read_text())
    assert 'recent_files' not in state['sources']
    assert sc.allowed('chrome') and sc.allowed('recent_files')


def test_disable_wins_timestamp_ties_and_only_a_newer_probe_can_restore_access():
    t = stamp(2)
    read = {'source_status': {'chrome': 'ok'}, 'chrome_history': [], 'search_queries': []}
    sc.record_bridge_status({'generated_at': t, 'sources': {'chrome': 'disabled'}})
    sc.record_bridge_status({**read, 'generated_at': t})
    assert not sc.allowed('chrome')
    sc.record_bridge_status({'last_seen': t, 'sources': {'chrome': 'ok'}})
    assert not sc.allowed('chrome')
    newer = stamp(1)
    # A completed read may have started before the toggle, so it never restores consent.
    sc.record_bridge_status({**read, 'generated_at': newer})
    assert not sc.allowed('chrome')
    sc.record_bridge_status({'last_seen': newer, 'sources': {'chrome': 'ok'}})
    assert sc.allowed('chrome')
    sc.record_bridge_status({'generated_at': newer, 'sources': {'chrome': 'disabled'}})
    assert not sc.allowed('chrome')


def test_revocation_hides_previous_activity_counts_and_does_not_get_undone(tmp_path):
    sc.record_bridge_status({'generated_at': stamp(1), 'source_status': {'chrome': 'ok'},
                             'chrome_history': [{'domain': 'private.example'}], 'search_queries': []})
    sc.record_bridge_status({'generated_at': stamp(), 'sources': {'chrome': 'disabled'}})
    sc.record_bridge_status({'generated_at': stamp(2), 'source_status': {'chrome': 'ok'},
                             'chrome_history': [{'domain': 'private.example'}], 'search_queries': []})
    assert source_status('chrome') == {'source': 'chrome', 'label': SOURCE_LABELS['chrome'], 'status': 'disabled'}
    saved = (tmp_path / 'config/source-state.json').read_text()
    assert 'private.example' not in saved


CONTEXT = [
    ('imessage', {'imessage': [{'handle': '+15550001001', 'text': 'Message fixture: please review the proposal before our meeting next week.',
                               'is_from_me': False, 'is_read': False, 'date': stamp(), 'timestamp': stamp()}],
                  'contacts': [{'name': 'Alex Fixture', 'phones': ['+15550001001'], 'emails': []}]}, ['Message fixture:']),
    ('whatsapp', {'whatsapp': [{'contact_jid': '15550001001@s.whatsapp.net', 'partner_name': 'Alex Fixture',
                               'text': 'WhatsApp fixture: please review the proposal before our meeting next week.',
                               'is_from_me': False, 'is_read': False, 'timestamp': stamp()}],
                  'contacts': [{'name': 'Alex Fixture', 'phones': ['+15550001001'], 'emails': []}]}, ['WhatsApp fixture:']),
    ('calls', {'calls': [{'phone': '+15550001001', 'timestamp': stamp(), 'is_outgoing': False,
                         'is_answered': False, 'call_type': 'phone'}],
               'contacts': [{'name': 'Missed Call Fixture', 'phones': ['+15550001001'], 'emails': []}]}, ['Missed Call Fixture']),
    ('whatsapp_calls', {'whatsapp_calls': [{'jid': '15550001001@s.whatsapp.net', 'timestamp': stamp(),
                                           'is_outgoing': False, 'is_missed': True}],
                        'contacts': [{'name': 'WhatsApp Call Fixture', 'phones': ['+15550001001'], 'emails': []}]}, ['WhatsApp Call Fixture']),
    ('contacts', {'contacts': [{'name': 'Contact Fixture', 'phones': [], 'emails': [],
                               'notes': 'Contact fixture context from our introduction.'}], 'contacts_total': 1},
     ['Contact fixture context']),
    ('chrome', {'chrome_history': [{'domain': 'chrome-fixture.example', 'visit_count': 3,
                                   'top_titles': ['Chrome fixture title']}],
                'search_queries': ['chrome fixture query']}, ['chrome-fixture.example', 'chrome fixture query']),
    ('safari', {'safari_history': [{'domain': 'safari-fixture.example', 'visit_count': 3,
                                  'top_titles': ['Safari fixture title']}],
                'safari_search_queries': ['safari fixture query']}, ['safari-fixture.example', 'safari fixture query']),
    ('recent_files', {'recent_files': [{'filename': 'source-wiring-fixture.pdf', 'status': 'opened',
                                      'source_url': 'https://file-fixture.example/deck'}]},
     ['source-wiring-fixture.pdf', 'file-fixture.example/deck']),
    ('screen_time', {'screen_time': {'top_apps': [{'app_name': 'Source fixture editor', 'minutes': 35}],
                                     'total_minutes': 35}}, ['Source fixture editor']),
    ('apple_notes', {'apple_notes': [{'title': 'Source fixture note', 'folder': 'Work',
                                     'modified_date': stamp(), 'snippet': 'Source fixture note body'}]},
     ['Source fixture note', 'Source fixture note body']),
    ('reminders', {'reminders': [{'title': 'Source fixture reminder', 'is_completed': False,
                                 'due_date': stamp(), 'list_name': 'Work'}]}, ['Source fixture reminder']),
]


@pytest.mark.parametrize('source,payload,markers', CONTEXT)
@pytest.mark.parametrize('mode', ['self-host', 'managed'])
def test_bridge_read_reaches_real_brief_prompt_and_revocation_removes_it(
        tmp_path, monkeypatch, source, payload, markers, mode):
    import urllib.request
    monkeypatch.setenv('BRIDGE_TOKEN', 'synthetic-token')
    local = {**payload, 'generated_at': stamp(), 'source_status': {source: 'ok'}}
    for field in SOURCE_FIELDS[source]:
        local.setdefault(field, [])
    class Reply:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return json.dumps({'result': {'content': [{'type': 'text', 'text': json.dumps(local)}]}}).encode()
    def bridge_read(request, **_):
        assert json.loads(request.data)['params']['name'] == 'read_local'
        return Reply()
    monkeypatch.setattr(urllib.request, 'urlopen', bridge_read)
    if mode == 'managed':
        monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
        monkeypatch.setenv('SOTTO_TENANT_ID', 'synthetic')
        caps = {'tenant_id': 'synthetic', 'sources': {source: {'consented': True, 'connected': True}}}
        if 'contacts' in payload:
            caps['sources']['contacts'] = {'consented': True, 'connected': True}
        path = tmp_path / 'config/managed-capabilities.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(caps))
    pulled = sc.read_local(24)
    assert source_status(source)['status'] == 'ok'
    _, template = cb._split_prompt(cb._load_prompt())
    inputs = {'type': 'morning', 'local': pulled, 'google': {}}
    rendered = cb.build_prompt(template, inputs)
    assert all(marker in rendered for marker in markers)
    assert source in sc.used_sources(local=pulled)
    import delivery_effects
    manifest = [{'kind': 'source_permissions', 'sources': sc.used_sources(local=pulled)}]
    assert delivery_effects.valid(manifest)
    # Simulate revocation after gather, before compose/delivery of the already held input.
    if mode == 'managed':
        caps['sources'][source]['consented'] = False
        path.write_text(json.dumps(caps))
    else:
        sc.record_bridge_status({'sources': {source: 'disabled'}})
    revoked = cb.build_prompt(template, inputs)
    assert all(marker not in revoked for marker in markers)
    assert source not in sc.used_sources(local=pulled)
    assert source_status(source)['status'] == 'disabled'
    assert not delivery_effects.valid(manifest)


def test_diagnostic_receipts_contain_no_source_content(tmp_path):
    sc.record_bridge_status({'source_status': {'recent_files': 'ok'},
        'recent_files': [{'filename': 'do-not-store.pdf', 'path': '/Users/private/Documents/do-not-store.pdf'}]})
    value = json.loads(Path(tmp_path, 'config/source-state.json').read_text())
    assert value['sources']['recent_files']['read']['field_counts'] == {'recent_files': 1}
    assert 'do-not-store' not in json.dumps(value)


def test_failed_file_metadata_cannot_tell_the_model_a_document_is_unread():
    from render_local import _format_file_matches, _format_recent_files
    rendered = _format_recent_files({'recent_files': [{'filename': 'fixture.pdf', 'status': 'unknown'}]})
    assert 'open status unknown' in rendered
    assert 'unopened' not in rendered
    matched = _format_file_matches([{'filename': 'fixture.pdf', 'event': 'Fixture meeting',
                                     'status': 'unknown', 'keywords': []}])
    assert 'open status unknown' in matched
    assert 'unread' not in matched


@pytest.mark.parametrize('skew_minutes', [-5, 0, 5])
def test_bridge_order_survives_clock_skew_and_reverse_arrival(monkeypatch, tmp_path, skew_minutes):
    arrival = [datetime(2026, 9, 22, 12, tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return arrival[0]
    monkeypatch.setattr(sc, 'datetime', Clock)
    started = arrival[0] + timedelta(minutes=skew_minutes)
    read = {'generated_at': started.isoformat(), 'source_status': {'chrome': 'ok'},
            'chrome_history': [{'domain': 'must-not-leak.example'}], 'search_queries': []}
    arrival[0] += timedelta(seconds=2)
    sc.record_bridge_status({'last_seen': (started + timedelta(seconds=1)).isoformat(),
                            'sources': {'chrome': 'disabled'}})
    arrival[0] += timedelta(seconds=1)
    sc.record_bridge_status(read)
    assert not sc.allowed('chrome')
    assert 'chrome_history' not in sc.project_local(read)
    row = json.loads((tmp_path / 'config/source-state.json').read_text())['sources']['chrome']
    assert row['status'] == 'disabled' and not row.get('read')


def test_future_clock_read_cannot_overwrite_newer_read_counts(monkeypatch):
    arrival = [datetime(2026, 9, 22, 12, tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return arrival[0]
    monkeypatch.setattr(sc, 'datetime', Clock)
    newer = arrival[0] + timedelta(minutes=5)
    payload = {'source_status': {'chrome': 'ok'}, 'search_queries': []}
    sc.record_bridge_status({**payload, 'generated_at': newer.isoformat(), 'chrome_history': []})
    arrival[0] += timedelta(seconds=5)
    sc.record_bridge_status({**payload, 'generated_at': (newer-timedelta(seconds=1)).isoformat(),
                            'chrome_history': [{'domain': 'stale.example'}]})
    result = source_status('chrome')
    assert result['status'] == 'empty'
    assert result['field_counts']['chrome_history'] == 0
    assert result['age_seconds'] == 5


def test_first_snapshot_discloses_each_partial_unavailable_and_disabled_source():
    local = {'generated_at': stamp(), 'source_status': {
        'chrome': 'degraded', 'imessage': 'needs_fda', 'whatsapp': 'disabled'},
        'chrome_history': [{'domain': 'first-partial.example', 'visit_count': 3}],
        'search_queries': [], 'imessage': [], 'deferred_unread_imessage': [],
        'whatsapp': [], 'deferred_unread_whatsapp': []}
    saved = cb._save_local_snapshot(local)
    prompt = cb.build_prompt(cb._load_prompt(), {'type': 'morning', 'local': saved, 'google': {}})
    assert 'Partial coverage: Chrome History' in prompt
    assert 'Unavailable on this device: iMessage' in prompt
    assert 'Disabled by user: WhatsApp' in prompt
    assert 'first-partial.example' in prompt


@pytest.mark.parametrize('source', ['imessage', 'whatsapp'])
def test_deferred_reader_failure_is_partial_parent_coverage(source):
    payload = {'generated_at': stamp(), 'source_status': {
        source: 'ok', 'deferred_unread_'+source: 'needs_fda'},
        source: [], 'deferred_unread_'+source: []}
    sc.record_bridge_status(payload)
    assert source_status(source)['status'] == 'partial'
    saved = cb._save_local_snapshot(payload)
    local = cb._normalize_local({'local': saved})
    assert local['_source_availability'][source] == 'partial'
    assert 'deferred_unread_'+source not in local['source_status']


@pytest.mark.parametrize('source', ['chrome', 'whatsapp', 'whatsapp_calls'])
def test_absent_optional_source_stays_quiet_but_loss_of_available_source_warns(source):
    absent = {'source_status': {source: 'unavailable'},
              **{field: [] for field in SOURCE_FIELDS[source]}}
    sc.record_bridge_status(absent)
    assert source_status(source)['status'] == 'not_present'
    sc.record_bridge_status({**absent, 'source_status': {source: 'ok'}})
    sc.record_bridge_status(absent)
    assert source_status(source)['status'] == 'unavailable'


def test_absence_does_not_hide_first_run_core_failure_or_claim_failed_access_was_available():
    sc.record_bridge_status({'sources': {'imessage': 'unavailable', 'whatsapp': 'needs_fda'}})
    assert source_status('imessage')['status'] == 'unavailable'
    assert source_status('whatsapp')['status'] == 'needs_fda'
    sc.record_bridge_status({'sources': {'whatsapp': 'unavailable'}})
    assert source_status('whatsapp')['status'] == 'not_present'


def test_optional_source_with_partial_data_is_known_available():
    sc.record_bridge_status({'source_status': {'chrome': 'partial'},
                             'chrome_history': [{'domain': 'fixture.example'}], 'search_queries': []})
    sc.record_bridge_status({'sources': {'chrome': 'unavailable'}})
    assert source_status('chrome')['status'] == 'unavailable'


def requested_read(requested, mac_stamp, domains=()):
    return {'_bridge_request_started_at': requested, 'generated_at': mac_stamp,
            'source_status': {'chrome': 'ok'}, 'search_queries': [],
            'chrome_history': [{'domain': domain, 'visit_count': 3} for domain in domains]}


def test_server_request_order_survives_mac_clock_correction_and_delayed_read(tmp_path):
    old = requested_read(stamp(3), stamp(-2), ['old.example'])
    new = requested_read(stamp(1), stamp(1), ['new.example', 'second.example'])
    cb._save_local_snapshot(old)
    cb._save_local_snapshot(new)
    sc.record_bridge_status({'_bridge_request_started_at': stamp(0.5),
                            'last_seen': stamp(0.5), 'sources': {'chrome': 'needs_fda'}})
    # Replaying an older result must neither clear the current failure nor undo the new data.
    cb._save_local_snapshot(old)
    health = source_status('chrome')
    assert health['status'] == 'needs_fda'
    assert health['field_counts']['chrome_history'] == 2
    snapshot = json.loads((tmp_path / 'knowledge/last_local_snapshot.json').read_text())
    assert snapshot['local']['chrome_history'] == new['chrome_history']
    assert snapshot['local']['_bridge_request_started_at'] == new['_bridge_request_started_at']


def test_first_server_ordered_read_replaces_legacy_future_watermark(tmp_path):
    cb._save_local_snapshot({'generated_at': stamp(-2), 'source_status': {'chrome': 'ok'},
                            'chrome_history': [{'domain': 'legacy.example'}], 'search_queries': []})
    new = requested_read(stamp(0.1), stamp(0.1), ['current.example'])
    saved = cb._save_local_snapshot(new)
    assert saved['chrome_history'] == new['chrome_history']
    sc.record_bridge_status({'_bridge_request_started_at': stamp(), 'last_seen': stamp(),
                            'sources': {'chrome': 'unavailable'}})
    assert source_status('chrome')['status'] == 'unavailable'


def test_server_order_never_allows_a_read_or_old_probe_to_restore_consent():
    sc.record_bridge_status(requested_read(stamp(3), stamp(-3), ['old.example']))
    disabled_request = stamp(2)
    sc.record_bridge_status({'_bridge_request_started_at': disabled_request,
                            'last_seen': stamp(2), 'sources': {'chrome': 'disabled'}})
    sc.record_bridge_status(requested_read(stamp(1), stamp(-4), ['must-not-leak.example']))
    sc.record_bridge_status({'_bridge_request_started_at': disabled_request,
                            'last_seen': stamp(-5), 'sources': {'chrome': 'ok'}})
    assert not sc.allowed('chrome')
    sc.record_bridge_status({'_bridge_request_started_at': stamp(), 'last_seen': stamp(),
                            'sources': {'chrome': 'ok'}})
    assert sc.allowed('chrome')


def test_first_server_probe_restores_a_future_dated_legacy_disable_after_clock_correction():
    sc.record_bridge_status({'last_seen': stamp(-3), 'sources': {'chrome': 'disabled'}})
    assert not sc.allowed('chrome')
    sc.record_bridge_status({'_bridge_request_started_at': stamp(), 'last_seen': stamp(1),
                            'sources': {'chrome': 'ok'}})
    assert sc.allowed('chrome')


def test_probe_started_before_legacy_disable_cannot_restore_it_after_clock_correction():
    started_before_disable = stamp(0.5)
    sc.record_bridge_status({'last_seen': stamp(-3), 'sources': {'chrome': 'disabled'}})
    sc.record_bridge_status({'_bridge_request_started_at': started_before_disable,
                            'last_seen': stamp(1), 'sources': {'chrome': 'ok'}})
    assert not sc.allowed('chrome')


def test_behind_clock_legacy_observation_is_not_mistaken_for_server_arrival():
    sc.record_bridge_status({'last_seen': stamp(1), 'sources': {'chrome': 'disabled'}})
    sc.record_bridge_status({'_bridge_request_started_at': stamp(0.5), 'last_seen': stamp(2),
                            'sources': {'chrome': 'ok'}})
    assert not sc.allowed('chrome')


@pytest.mark.parametrize('invalid', [None, 123, 'broken', '2026-01-01', '2999-01-01T00:00:00Z'])
def test_invalid_request_marker_cannot_replace_a_newer_read(invalid):
    sc.record_bridge_status(requested_read(stamp(1), stamp(1)))
    older = requested_read(invalid, stamp(2), ['stale.example'])
    assert sc.bridge_request_epoch(older) is None
    sc.record_bridge_status(older)
    assert source_status('chrome')['field_counts']['chrome_history'] == 0


def test_request_order_metadata_does_not_reach_brief_text():
    local = requested_read(stamp(), stamp(), ['context.example'])
    prompt = cb.build_prompt(cb._load_prompt(), {'type': 'morning', 'local': local, 'google': {}})
    assert 'context.example' in prompt
    assert '_bridge_request_started_at' not in prompt


def test_unreadable_receipt_explanation_does_not_assume_a_disabled_source_list(tmp_path):
    path = tmp_path / 'config/source-state.json'
    path.parent.mkdir()
    path.write_text('{broken')
    prompt = cb.build_prompt(cb._load_prompt(), {'type': 'morning', 'local': {}, 'google': {}})
    assert "I couldn't check your Mac sources" in prompt
    assert 'list below' not in prompt
