"""Credential-free tests of the receiver owner-only read endpoint."""
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
spec = importlib.util.spec_from_file_location('test_owner_loops_receiver', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


@pytest.fixture(autouse=True)
def isolated_receiver(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    # UTC date is Sep 30; the owner's local date is still Sep 29.
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 9, 29, 18, 0,
                                                           tzinfo=ZoneInfo('America/Los_Angeles')))
    rec._SCRIPT_CACHE.clear()
    if hasattr(rec._owner_continuity, '_module'):
        delattr(rec._owner_continuity, '_module')


def request(body=b'{"include_parked":false}', *, peer='127.0.0.1', auth=True,
            path='/runtime/owner-loops', content_type='application/json', method='POST'):
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


def _write_ledger(root: Path, name: str, row: dict):
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
    assert request(path='/runtime/owner-loops?x=1')[0] == 404
    assert request(method='GET')[0] == 404
    for body in (b'{}', b'{"include_parked":1}', b'{"include_parked":"false"}',
                 b'{"include_parked":false,"extra":1}',
                 b'{"include_parked":false,"include_parked":true}',
                 b'[]', b'{broken', b'x' * 65):
        assert request(body)[0] == 400
    assert request(content_type='text/plain')[0] == 400


def test_actual_bounded_projection_withholds_source_and_legacy_rows(tmp_path, monkeypatch):
    _write_ledger(tmp_path, 'owner-open', {
        'anchor_key': 'manual-open', 'action_type': 'follow_up', 'channel': 'manual',
        'status': 'open', 'source': 'user_added', 'source_thread_id': 'manual:open',
        'owner_event_id': 'evt-1', 'summary': 'Send the signed deck',
        'created_at': '2026-09-20', 'deadline': '2026-09-29',
    })
    _write_ledger(tmp_path, 'owner-parked', {
        'anchor_key': 'manual-parked', 'action_type': 'waiting_on', 'channel': 'manual',
        'status': 'parked', 'source': 'user_added', 'source_thread_id': 'manual:parked',
        'owner_event_id': 'evt-2', 'summary': 'Wait for reply', 'created_at': '2026-09-10',
    })
    _write_ledger(tmp_path, 'source-gmail', {
        'anchor_key': 'gmail-open', 'action_type': 'reply', 'channel': 'email',
        'status': 'open', 'source': 'gmail', 'source_thread_id': 'gmail-thread',
        'source_refs': [{'sourceType': 'gmail', 'sourceId': 'msg-1'}],
        'summary': 'Source secret', 'created_at': '2026-09-10',
    })
    _write_ledger(tmp_path, 'legacy', {
        'anchor_key': 'legacy-open', 'action_type': 'follow_up', 'channel': 'manual',
        'status': 'open', 'source': 'user_added', 'source_thread_id': 'manual:old',
        'summary': 'Legacy secret', 'created_at': '2026-09-10',
    })
    files_before = {str(path.relative_to(tmp_path)): path.read_bytes()
                    for path in tmp_path.rglob('*') if path.is_file()}
    def no_external_io(*_args, **_kwargs):
        raise AssertionError('endpoint attempted external I/O')
    monkeypatch.setattr(rec.subprocess, 'run', no_external_io)
    monkeypatch.setattr(rec.urllib.request, 'urlopen', no_external_io)
    code, result = request()
    assert code == 200 and result['coverage'] == 'owner_authored_only'
    assert result['counts'] == {'you_owe': 1, 'waiting_on_them': 0}
    assert result['you_owe'][0]['what'] == 'Send the signed deck'
    assert result['you_owe'][0]['overdue'] is False  # owner's local Sep 29, not UTC Sep 30
    assert 'parked' not in result
    assert 'secret' not in json.dumps(result)
    code, result = request(b'{"include_parked":true}')
    assert code == 200 and result['counts']['parked'] == 1
    assert result['parked'][0]['what'] == 'Wait for reply'
    files_after = {str(path.relative_to(tmp_path)): path.read_bytes()
                   for path in tmp_path.rglob('*') if path.is_file()}
    assert files_after == files_before


@pytest.mark.parametrize('bad_input', ['symlink', 'fifo'])
def test_unavailable_ledger_yields_only_generic_error(tmp_path, bad_input):
    directory = tmp_path / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)
    bad = directory / 'bad.md'
    if bad_input == 'symlink':
        bad.symlink_to(tmp_path / 'outside-secret')
    else:
        os.mkfifo(bad)
    code, result = request()
    assert (code, result) == (503, {'error': 'owner continuity unavailable'})


def test_helper_failure_and_missing_coverage_are_generic(monkeypatch):
    monkeypatch.setattr(rec, '_owner_continuity', lambda: (_ for _ in ()).throw(RuntimeError('sensitive path')))
    assert request() == (503, {'error': 'owner continuity unavailable'})
    monkeypatch.setattr(rec, '_owner_continuity', lambda: SimpleNamespace(
        read_owner_continuity=lambda *_args, **_kwargs: {'you_owe': []}))
    assert request() == (503, {'error': 'owner continuity unavailable'})
    monkeypatch.setattr(rec, '_owner_continuity', lambda: SimpleNamespace(
        read_owner_continuity=lambda *_args, **_kwargs: {
            'coverage': 'owner_authored_only', 'you_owe': ['x' * 65536]}))
    assert request() == (503, {'error': 'owner continuity unavailable'})


def test_cached_helper_from_another_tree_is_rejected(monkeypatch):
    monkeypatch.setitem(sys.modules, 'loops_query', SimpleNamespace(__file__='/untrusted/loops_query.py'))
    assert request() == (503, {'error': 'owner continuity unavailable'})


def test_wire_response_bound_includes_json_unicode_escaping(monkeypatch):
    # _send uses ensure_ascii=True: checking only UTF-8 bytes would admit a
    # response that the bounded Node client must then reject on the wire.
    result = {'coverage': 'owner_authored_only', 'you_owe': ['é' * 11000]}
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 64 * 1024
    assert len(json.dumps(result).encode()) > 64 * 1024
    monkeypatch.setattr(rec, '_owner_continuity', lambda: SimpleNamespace(
        read_owner_continuity=lambda *_args, **_kwargs: result))
    assert request() == (503, {'error': 'owner continuity unavailable'})


@pytest.mark.parametrize('path,module_name', [
    ('/runtime/owner-loop-intent', 'RUNTIME_OWNER_LOOP'),
    ('/runtime/owner-quiet-intent', 'RUNTIME_OWNER_QUIET'),
    ('/runtime/owner-reminder-intent', 'RUNTIME_OWNER_REMINDER'),
    ('/runtime/owner-brief-request', 'RUNTIME_OWNER_BRIEF_REQUEST'),
])
@pytest.mark.parametrize('mode,authenticated', [
    ('first_contact_shared_project', True),
    ('first_contact_shared_project', False),
    ('first_contact', True),
])
def test_shared_owner_route_ack_refresh_is_authenticated_and_before_effect(
        monkeypatch, path, module_name, mode, authenticated):
    root = 'synthetic-owner-root-01234567890123456789'
    monkeypatch.setenv('SOTTO_MCP_TOKEN', root)
    identity = {'tenant_id': 'tenant', 'channel': 'photon', 'target': 'photon',
                'owner_id': '+15555550123', 'account_id': 'account', 'line_key': 'line',
                'route_mode': mode, 'route_id': 'shared_project' if mode.endswith('shared_project') else '+15555550999',
                'project_fingerprint': 'a' * 64}
    body = json.dumps({'identity': identity}).encode()
    expected = rec.OWNER_LOOP_CAPABILITY.derive(root, identity)
    order, sent = [], []
    monkeypatch.setattr(rec, '_activation_tick', lambda: order.append('ack'))
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(_client=lambda **_: object()))
    def perform(*args, **kwargs):
        order.append('perform')
        return {'status': 'committed'}
    monkeypatch.setattr(getattr(rec, module_name), 'perform', perform)
    handler = SimpleNamespace(path=path, client_address=('127.0.0.1', 1),
        headers={'Content-Length': str(len(body)), 'Content-Type': 'application/json'},
        rfile=io.BytesIO(body), _authed=lambda token: authenticated and token == expected,
        _send=lambda status, payload: sent.append((status,payload)))
    rec.Handler.do_POST(handler)
    if not authenticated:
        assert sent[0][0] == 401
        assert order == []
    else:
        assert sent[0][0] == 200
        assert order == (['ack','perform'] if mode.endswith('shared_project') else ['perform'])
