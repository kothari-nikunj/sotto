"""Credential-free receiver tests for the protected owner-only retune projection."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('test_owner_retune_receiver', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


@pytest.fixture(autouse=True)
def isolated_receiver(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 9, 29, 18, 0,
                                                           tzinfo=ZoneInfo('America/Los_Angeles')))
    rec._SCRIPT_CACHE.clear()
    if hasattr(rec._owner_continuity, '_module'):
        delattr(rec._owner_continuity, '_module')


def request(body=b'{}', *, peer='127.0.0.1', auth=True,
            path='/runtime/owner-retune', content_type='application/json', method='POST'):
    sent = []
    fake = SimpleNamespace(
        path=path, client_address=(peer, 1234),
        headers={'Content-Length': str(len(body)), 'Content-Type': content_type},
        rfile=io.BytesIO(body),
        _authed=lambda token: auth and token == rec.MCP_TOKEN,
        _send=lambda status, payload: sent.append((status, payload)),
    )
    if method == 'POST':
        rec.Handler.do_POST(fake)
    else:
        rec.Handler.do_GET(fake)
    return sent[0]


def write_ledger(root: Path, name: str, row: dict) -> None:
    from ledger_provenance import digest_frontmatter
    if name.startswith('owner'):
        row['content_provenance'] = {'version': 1, 'kind': 'owner', 'sources': [],
                                     'digest': digest_frontmatter(row)}
    if name.startswith('source'):
        row['content_provenance'] = {'version': 1, 'kind': 'sources', 'sources': ['gmail'],
                                     'digest': digest_frontmatter(row)}
    directory = root / 'knowledge' / 'continuity'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'{name}.md').write_text('---\n' + yaml.safe_dump(row, sort_keys=False) + '---\n')


def test_exact_read_auth_shape_and_method():
    assert request(peer='203.0.113.1')[0] == 401
    assert request(auth=False)[0] == 401
    assert request(path='/runtime/owner-retune?x=1')[0] == 404
    assert request(method='GET')[0] == 404
    for body in (b'{"include_parked":false}', b'{"extra":1}', b'{"x":1,"x":2}',
                 b'[]', b'{broken', b'x' * 65):
        assert request(body)[0] == 400
    assert request(content_type='text/plain')[0] == 400


def test_owner_only_stale_projection_never_reads_preferences_or_source_rows(tmp_path, monkeypatch):
    write_ledger(tmp_path, 'owner-open', {
        'anchor_key': 'manual-open', 'action_type': 'follow_up', 'channel': 'manual',
        'status': 'open', 'source': 'user_added', 'source_thread_id': 'manual:open',
        'owner_event_id': 'evt-1', 'summary': 'Send the signed deck',
        'created_at': '2026-09-20', 'deadline': '2026-09-29',
    })
    write_ledger(tmp_path, 'source-gmail', {
        'anchor_key': 'gmail-open', 'action_type': 'reply', 'channel': 'email',
        'status': 'open', 'source': 'gmail', 'source_thread_id': 'gmail-thread',
        'source_refs': [{'sourceType': 'gmail', 'sourceId': 'msg-1'}],
        'summary': 'Source secret', 'created_at': '2026-09-10',
    })
    write_ledger(tmp_path, 'legacy', {
        'anchor_key': 'legacy-open', 'action_type': 'follow_up', 'channel': 'manual',
        'status': 'open', 'source': 'user_added', 'source_thread_id': 'manual:old',
        'summary': 'Legacy secret', 'created_at': '2026-09-10',
    })
    (tmp_path / 'preferences.json').write_text(json.dumps({'explicit': {'tone_notes': ['private preference']}}))
    before = {str(path.relative_to(tmp_path)): path.read_bytes()
              for path in tmp_path.rglob('*') if path.is_file()}
    rec._owner_continuity()
    monkeypatch.setattr(sys.modules['retune_scan'].pref, 'load_explicit',
                        lambda: (_ for _ in ()).throw(AssertionError('preferences read')))
    def no_external_io(*_args, **_kwargs):
        raise AssertionError('endpoint attempted external I/O')
    monkeypatch.setattr(rec.subprocess, 'run', no_external_io)
    monkeypatch.setattr(rec.urllib.request, 'urlopen', no_external_io)
    code, result = request()
    assert code == 200
    assert result['coverage'] == 'owner_authored_only'
    assert result['counts'] == {'stale': 1, 'mute_suggestions': 0}
    assert result['stale_loops'][0]['what'] == 'Send the signed deck'
    assert result['stale_loops'][0]['overdue'] is False  # owner's local Sep 29
    assert result['current'] == {} and result['mute_suggestions'] == []
    assert 'secret' not in json.dumps(result) and 'private preference' not in json.dumps(result)
    after = {str(path.relative_to(tmp_path)): path.read_bytes()
             for path in tmp_path.rglob('*') if path.is_file()}
    assert before == after


def test_unavailable_ledger_and_bad_helper_fail_closed(tmp_path, monkeypatch):
    directory = tmp_path / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)
    os.mkfifo(directory / 'bad.md')
    assert request() == (503, {'error': 'owner continuity unavailable'})
    monkeypatch.setattr(rec, '_owner_continuity', lambda: SimpleNamespace(
        read_owner_continuity=lambda *_args, **_kwargs: {'stale_loops': []}))
    assert request() == (503, {'error': 'owner continuity unavailable'})
