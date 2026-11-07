"""Synthetic OAuth metadata only; no Google connection or bearer secrets."""
import hashlib
import json
import sqlite3

import google_source_authority as authority

GMAIL = 'https://www.googleapis.com/auth/gmail.readonly'
CALENDAR = 'https://www.googleapis.com/auth/calendar.readonly'
CONTACTS = 'https://www.googleapis.com/auth/contacts.readonly'


def token(path, scopes, generation='a' * 32, request_id=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {'refresh_token': 'synthetic-refresh', 'client_id': 'synthetic-client',
             'client_secret': 'synthetic-secret', 'token_uri': 'https://oauth2.googleapis.com/token',
             'scopes': scopes,
             'sotto_source_generation': generation}
    if request_id:
        value['sotto_cloud_request_id'] = request_id
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def applied(path, request_id, generation, accepted):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE bootstraps (id TEXT PRIMARY KEY, generation INTEGER, applied INTEGER)')
        db.execute('INSERT INTO bootstraps VALUES (?,?,?)', (request_id, generation, accepted))
    path.chmod(0o600)


def test_selfhost_explicit_generation_and_scopes_without_bridge(tmp_path):
    path = tmp_path / 'home/google_token.json'
    assert authority.state(tmp_path, path, managed_mode=False) == {
        'sources': [], 'generation': 'disconnected', 'body_digest': 'disconnected'}
    token(path, [GMAIL])
    first = authority.state(tmp_path, path, managed_mode=False)
    assert first['sources'] == ['gmail'] and first['generation'] == 'a' * 32
    token(path, [GMAIL, CALENDAR], generation='b' * 32)
    assert authority.state(tmp_path, path, managed_mode=False)['sources'] == ['calendar', 'gmail']
    token(path, [GMAIL], generation=None)
    assert authority.state(tmp_path, path, managed_mode=False) is None
    path.write_text('{"refresh_token":"first","refresh_token":"second"}')
    assert authority.state(tmp_path, path, managed_mode=False) is None
    path.unlink()
    path.symlink_to(tmp_path / 'other')
    assert authority.state(tmp_path, path, managed_mode=False) is None


def test_managed_requires_applied_current_tenant_origin_and_ledger_grant(tmp_path):
    path = tmp_path / 'home/google_token.json'
    request_id = 'a' * 64
    stamp = 'sotto-cloud-google-v1\0tenant-a\0' + request_id + '\0' + '3'
    generation = hashlib.sha256(stamp.encode()).hexdigest()[:32]
    token(path, [GMAIL, CALENDAR], generation, request_id)
    ledger = {'google_sources': ['gmail']}
    assert authority.state(tmp_path, path, managed_mode=True, tenant='tenant-a', managed_ledger=ledger) is None
    applied(tmp_path / 'config/cloud-pairing.sqlite', request_id, 3, 0)
    assert authority.state(tmp_path, path, managed_mode=True, tenant='tenant-a', managed_ledger=ledger) is None
    with sqlite3.connect(tmp_path / 'config/cloud-pairing.sqlite') as db:
        db.execute('UPDATE bootstraps SET applied=1')
    assert authority.state(tmp_path, path, managed_mode=True, tenant='tenant-a', managed_ledger=ledger)['sources'] == ['gmail']
    assert authority.state(tmp_path, path, managed_mode=True, tenant='tenant-b', managed_ledger=ledger) is None
    token(path, [GMAIL, CALENDAR], 'b' * 32, request_id)
    assert authority.state(tmp_path, path, managed_mode=True, tenant='tenant-a', managed_ledger=ledger) is None
    token(path, [GMAIL, CALENDAR], generation, request_id)
    assert authority.state(tmp_path, path, managed_mode=True, tenant='tenant-a', managed_ledger={'google_sources': []})['sources'] == []
    with sqlite3.connect(tmp_path / 'config/cloud-pairing.sqlite') as db:
        db.execute('INSERT INTO bootstraps VALUES (?,?,?)', ('b' * 64, 4, 0))
    assert authority.state(tmp_path, path, managed_mode=True, tenant='tenant-a', managed_ledger=ledger)['sources'] == ['gmail']
    with sqlite3.connect(tmp_path / 'config/cloud-pairing.sqlite') as db:
        db.execute('UPDATE bootstraps SET applied=1 WHERE id=?', ('b' * 64,))
    assert authority.state(tmp_path, path, managed_mode=True, tenant='tenant-a', managed_ledger=ledger) is None


def test_google_contacts_requires_its_own_scope_and_managed_grant(tmp_path):
    path = tmp_path / 'home/google_token.json'
    token(path, [CONTACTS])
    assert authority.state(tmp_path, path, managed_mode=False)['sources'] == ['google_contacts']
    token(path, ['https://www.googleapis.com/auth/contacts'])
    assert authority.state(tmp_path, path, managed_mode=False)['sources'] == ['google_contacts']
    request_id = 'a' * 64
    stamp = 'sotto-cloud-google-v1\0tenant-a\0' + request_id + '\0' + '3'
    generation = hashlib.sha256(stamp.encode()).hexdigest()[:32]
    token(path, [GMAIL, CONTACTS], generation, request_id)
    applied(tmp_path / 'config/cloud-pairing.sqlite', request_id, 3, 1)
    current = lambda grants: authority.state(tmp_path, path, managed_mode=True, tenant='tenant-a',
                                             managed_ledger={'google_sources': grants})
    assert current(['gmail', 'contacts'])['sources'] == ['gmail']
    assert current(['google_contacts'])['sources'] == ['google_contacts']
    token(path, [GMAIL], generation, request_id)
    assert current(['google_contacts'])['sources'] == []


def test_calendar_event_read_grants_enable_calendar_only(tmp_path):
    path = tmp_path / 'home/google_token.json'
    for scope in ('calendar.events', 'calendar.events.readonly'):
        token(path, ['https://www.googleapis.com/auth/' + scope])
        assert authority.state(tmp_path, path, managed_mode=False)['sources'] == ['calendar']
