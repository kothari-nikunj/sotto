"""Original MCP gather and meeting query with synthetic HTTP, no live credentials."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import runtime_knowledge_read as read

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
REVISION = 'a' * 64
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def history(monkeypatch):
    for part in ('_shared/lib', '_shared/scripts'):
        monkeypatch.syspath_prepend(str(PACK / part))
    import granola_history
    return granola_history


def factory(history, *, duplicates=False, fail=None, transcript=True):
    calls = []
    def transport(url, body, headers, timeout):
        message = json.loads(body)
        assert headers['Authorization'] == 'Bearer synthetic' and timeout > 0
        method = message['method']
        if method == 'notifications/initialized':
            return 202, {}, b''
        if method == 'initialize':
            result = {}
        elif method == 'tools/list':
            names = ['list_meetings', 'get_meetings'] + (['get_meeting_transcript'] if transcript else [])
            result = {'tools': [{'name': name, 'inputSchema': {'properties': {
                'meeting_ids': {}} if name == 'get_meetings' else {}}} for name in names]}
        else:
            name = message['params']['name']; calls.append((name, message['params']['arguments']))
            if name == fail:
                return 500, {}, b'private provider text'
            if name == 'list_meetings':
                result = [{'id': 'm1', 'title': 'Cedar', 'start_time': '2026-09-30T12:00:00Z'}]
                if duplicates:
                    result *= 2
            elif name == 'get_meetings':
                result = [{'id': 'm1', 'notes': 'Actual note', 'summary': 'Actual decision'}]
            else:
                result = {'transcript': 'Actual transcript ' * 1000}
            result = {'structuredContent': result}
        return 200, {}, json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
    return lambda **_kwargs: history.gather.McpClient('https://synthetic.invalid/mcp', 'synthetic', transport), calls


def test_original_gather_discovery_then_exact_notes_transcript_and_no_write(history, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    create, calls = factory(history)
    # Factory-created clients execute the original initialize/tools/list/call parser.
    discovery = history.query(tmp_path, ['granola'], client_factory=create, now=NOW)
    assert discovery['meetings'] == [{'meeting_id': 'm1', 'title': 'Cedar',
        'start': '2026-09-30T12:00:00+00:00', 'end': None}]
    assert [name for name, _ in calls] == ['list_meetings']
    calls.clear()
    result = history.query(tmp_path, ['granola'], meeting_id='m1', client_factory=create, now=NOW)
    assert result['notes'] == {'your_notes': 'Actual note', 'ai_summary': 'Actual decision'}
    assert result['transcript_available'] and result['transcript_truncated']
    assert len(result['transcript']) == 12000
    assert [name for name, _ in calls] == ['list_meetings', 'get_meeting_transcript', 'get_meetings']
    assert list(tmp_path.rglob('*')) == []


def test_duplicate_and_missing_ids_never_fetch_details(history, tmp_path):
    create, calls = factory(history, duplicates=True)
    assert history.query(tmp_path, ['granola'], meeting_id='m1', client_factory=create, now=NOW)['status'] == 'ambiguous'
    assert [name for name, _ in calls] == ['list_meetings']
    calls.clear()
    assert history.query(tmp_path, ['granola'], meeting_id='missing', client_factory=create, now=NOW)['status'] == 'not_found'
    assert [name for name, _ in calls] == ['list_meetings']


def test_failure_not_empty_and_missing_transcript_honest(history, tmp_path):
    create, _ = factory(history, fail='get_meetings')
    with pytest.raises(history.UnavailableError):
        history.query(tmp_path, ['granola'], meeting_id='m1', client_factory=create, now=NOW)
    create, _ = factory(history, transcript=False)
    result = history.query(tmp_path, ['granola'], meeting_id='m1', client_factory=create, now=NOW)
    assert not result['transcript_available'] and result['transcript'] == ''


def test_mixed_source_obligations_project_before_text(history, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    spec = importlib.util.spec_from_file_location('granola_history_resolver', PACK / 'morning-brief/scripts/continuity_resolve.py')
    cr = importlib.util.module_from_spec(spec); spec.loader.exec_module(cr)
    for anchor, extra in [('visible', []), ('hidden', [{'sourceType': 'gmail', 'sourceId': 'reply'}])]:
        cr._persist({'anchor_key': anchor, 'summary': anchor, 'status': 'open', 'source': 'followup_commitment',
            'source_thread_id': 'commitment:m1', 'source_refs': [{'sourceType': 'granola', 'sourceId': 'm1'}],
            'resolution_source_refs': extra, 'action_type': 'reply'})
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    create, _ = factory(history)
    result = history.query(tmp_path, ['granola'], meeting_id='m1', client_factory=create, now=NOW)
    assert [row['what'] for row in result['obligations']] == ['visible']
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    result = history.query(tmp_path, ['granola', 'gmail'], meeting_id='m1', client_factory=create, now=NOW)
    assert {row['what'] for row in result['obligations']} == {'visible', 'hidden'}


@pytest.mark.parametrize('argv', [['--days', '0'], ['--days', '91'], ['--days', '01'],
    ['--meeting-id', ' x'], ['--meeting-id', 'x\ny'], ['--meeting-id', 'x' * 513], ['--out', '/tmp/file'],
    ['--days', '2', '--days', '3']])
def test_closed_arguments(argv):
    with pytest.raises(ValueError):
        read.validate({'operation': 'granola_history', 'arguments': {'argv': argv}, 'source_revision': REVISION})


def test_receiver_generation_bookends_and_disconnect(monkeypatch):
    body = {'operation': 'granola_history', 'arguments': {'argv': []}, 'source_revision': REVISION}
    state = {'complete': True, 'revision': REVISION, 'connector_sources': ['granola']}
    process = SimpleNamespace(returncode=0, communicate=lambda timeout: (b'{"status":"found"}', b''), poll=lambda: 0)
    monkeypatch.setattr(read.subprocess, 'Popen', lambda *a, **k: process)
    samples = iter([state, {**state, 'revision': 'b' * 64}])
    with pytest.raises(read.UnavailableError):
        read.perform(body, script='fixed', executable=sys.executable, env={}, authority=lambda: next(samples))
    events = []
    process.poll = lambda: None
    process.kill = lambda: events.append('killed')
    process.wait = lambda: events.append('waited')
    checks = iter([False, True])
    with pytest.raises(read.UnavailableError):
        read.perform(body, script='fixed', executable=sys.executable, env={}, authority=lambda: state, disconnected=lambda: next(checks))
    assert events == ['killed', 'waited']


def test_transport_deadline_bytes_and_refresh_share_bound(history, monkeypatch):
    import time
    seen = []
    def token(service, *, http, force_refresh=False):
        status, raw = http('https://synthetic.invalid/token', b'form', {}, 30)
        seen.append((status, raw))
        return 'synthetic', 'https://synthetic.invalid/mcp'
    monkeypatch.setattr(history.gather, 'get_access_token', token)
    transport = lambda *args: (200, {}, b'{}')
    client = history.bounded_factory(time.monotonic() + 45, transport=transport)()
    assert seen == [(200, b'{}')]
    client.initialize()
    with pytest.raises(history.UnavailableError):
        history.bounded_factory(time.monotonic() - 1, transport=transport)()
    monkeypatch.setattr(history, 'MAX_RESPONSE', 1)
    with pytest.raises(history.UnavailableError):
        history.bounded_factory(time.monotonic() + 45, transport=transport)()
    monkeypatch.setattr(history, 'MAX_RESPONSE', 20)
    with pytest.raises(history.UnavailableError):
        history.bounded_factory(time.monotonic() + 45, transport=lambda *a: (302, {}, b''))()


def test_provider_nonlist_and_selected_empty_detail_never_claim_empty(history, tmp_path):
    calls = []
    class Client:
        def initialize(self):
            pass
        def list_tools(self):
            return [{'name': name} for name in ('list_meetings', 'get_meetings', 'get_meeting_transcript')]
        def call_tool(self, name, args, timeout):
            calls.append(name)
            return 'private malformed response'
    for meeting_id in (None, 'm1'):
        with pytest.raises(history.UnavailableError):
            history.query(tmp_path, ['granola'], meeting_id=meeting_id, client_factory=lambda **k: Client(), now=NOW)
    create, _ = factory(history)
    client = create()
    original = client.call_tool
    client.call_tool = lambda name, args, timeout: [] if name == 'get_meetings' else original(name, args, timeout)
    with pytest.raises(history.UnavailableError):
        history.query(tmp_path, ['granola'], meeting_id='m1', client_factory=lambda **k: client, now=NOW)


def test_metadata_keeps_original_date_only_and_undated_records(history, tmp_path):
    class Client:
        def initialize(self):
            pass
        def list_tools(self):
            return [{'name': 'list_meetings', 'inputSchema': {'properties': {}}}]
        def call_tool(self, name, args, timeout):
            return [{'id': 'date', 'title': 'Dated', 'date': '2026-09-30'},
                    {'id': 'unknown', 'title': 'Undated'}]
    result = history.query(tmp_path, ['granola'], client_factory=lambda **k: Client(), now=NOW)
    assert [row['start'] for row in result['meetings']] == ['2026-09-30', None]


def test_selected_window_bound_ignores_old_and_other_meetings(history, tmp_path):
    create, calls = factory(history)
    client = create()
    original = client.call_tool
    rows = [{'id': 'old-' + str(index), 'title': 'Old', 'start_time': '2020-01-01T00:00:00Z'}
            for index in range(60)]
    eligible = {'id': 'm1', 'title': 'Cedar', 'start_time': '2026-09-30T12:00:00Z'}
    client.call_tool = lambda name, args, timeout: rows + [eligible] if name == 'list_meetings' else original(name, args, timeout)
    make = lambda **kwargs: client
    assert len(history.query(tmp_path, ['granola'], client_factory=make, now=NOW)['meetings']) == 1
    rows[:] = [{**eligible, 'id': 'other-' + str(index)} for index in range(60)]
    result = history.query(tmp_path, ['granola'], meeting_id='m1', client_factory=make, now=NOW)
    assert result['status'] == 'found' and result['notes']['your_notes'] == 'Actual note'
    with pytest.raises(RuntimeError, match='bound'):
        history.query(tmp_path, ['granola'], client_factory=make, now=NOW)


@pytest.mark.parametrize('detail,accepted', [({'id': 'm1', 'notes': '', 'summary': ''}, True),
    ({'id': 'm1', 'notes_markdown': ''}, True), ({'id': 'm1'}, False),
    ({'id': 'other', 'notes': ''}, False)])
def test_explicit_empty_notes_are_observed_not_missing(history, tmp_path, detail, accepted):
    create, _ = factory(history)
    client = create()
    original = client.call_tool
    client.call_tool = lambda name, args, timeout: [detail] if name == 'get_meetings' else original(name, args, timeout)
    call = lambda: history.query(tmp_path, ['granola'], meeting_id='m1', client_factory=lambda **k: client, now=NOW)
    if accepted:
        result = call()
        assert result['status'] == 'found' and result['transcript_available']
        assert result['notes'] == {'your_notes': '', 'ai_summary': ''}
    else:
        with pytest.raises(history.UnavailableError):
            call()
