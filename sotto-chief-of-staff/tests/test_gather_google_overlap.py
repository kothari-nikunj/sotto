"""Calendar/Gmail overlap through the ordinary gather entrypoint."""
import importlib.util
import json
from pathlib import Path

import pytest

path = Path(__file__).parent.parent / '_shared' / 'scripts' / 'gather_google.py'
spec = importlib.util.spec_from_file_location('gather_google_overlap', path)
gg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gg)


@pytest.fixture(autouse=True)
def no_token_refresh(monkeypatch):
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)


def test_main_calendar_overlaps_gmail_without_parallelizing_dependent_lanes(tmp_path, monkeypatch):
    import threading
    import source_context
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    calendar_started = threading.Event()
    inbox_done = threading.Event()
    order = []
    def calendar(*args, observation, **kwargs):
        order.append('calendar:start')
        calendar_started.set()
        assert inbox_done.wait(1), 'Calendar must overlap the inbox read'
        observation.update(complete=True, calendars=['primary'])
        order.append('calendar:end')
        return [{'id': 'event'}]
    def inbox(*args, **kwargs):
        order.append('inbox:start')
        assert calendar_started.wait(1), 'Calendar still waits behind Gmail'
        order.append('inbox:end')
        inbox_done.set()
        return [{'id': 'inbox'}]
    def sent(*args, **kwargs):
        assert inbox_done.is_set()
        order.append('sent')
        return []
    def stale(*args, **kwargs):
        assert [step for step in order if not step.startswith('calendar:')][-1] == 'sent'
        order.append('stale')
        return []
    monkeypatch.setattr(gg, 'gather_calendar', calendar)
    monkeypatch.setattr(gg, 'gather_gmail', inbox)
    monkeypatch.setattr(gg, 'gather_sent', sent)
    monkeypatch.setattr(gg, 'gather_stale_sent', stale)
    gmail, cal, receipts = [tmp_path / name for name in ('g.json', 'c.json', 'r.json')]
    monkeypatch.setattr(gg.sys, 'argv', ['gather_google.py', '--gmail-out', str(gmail),
        '--cal-out', str(cal), '--source-results-out', str(receipts)])
    gg.main()
    assert order.index('calendar:start') < order.index('inbox:end')
    assert json.loads(gmail.read_text()) == [{'id': 'inbox'}]
    assert json.loads(cal.read_text()) == [{'id': 'event'}]
    result = json.loads(receipts.read_text())
    assert result['gmail']['status'] == result['calendar']['status'] == 'ok'
    assert result['calendar']['calendars'] == ['primary']


@pytest.mark.parametrize('failure', [None, 'gmail', 'calendar', 'both', 'quota', 'revoke-gmail', 'revoke-calendar'])
def test_main_parallel_sources_preserve_receipts_revocation_and_meeting_join(tmp_path, monkeypatch, capsys, failure):
    import ledger_io
    import source_context
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    permission = {'gmail': True, 'calendar': True}
    monkeypatch.setattr(source_context, 'allowed', lambda source: permission.get(source, True))
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(ledger_io, 'load_active', lambda: [])
    finished = set()
    def inbox(*args, **kwargs):
        finished.add('gmail')
        if failure in ('gmail', 'both'):
            raise RuntimeError('inbox fixture failure')
        rows = gg.GmailRows([{'id': 'inbox'}])
        rows.rate_limited = failure == 'quota'
        return rows
    def calendar(*args, observation, **kwargs):
        finished.add('calendar')
        if failure and failure.startswith('revoke-'):
            permission[failure.removeprefix('revoke-')] = False
        if failure in ('calendar', 'both'):
            raise RuntimeError('calendar fixture failure')
        observation.update(complete=False, calendars=['primary'])
        return [{'id': 'event'}]
    joins = []
    def meeting(api, events, emails):
        assert finished == {'gmail', 'calendar'}
        joins.append((events, emails))
        return None, {'status': 'ok', 'complete': True}
    monkeypatch.setattr(gg, 'gather_gmail', inbox)
    monkeypatch.setattr(gg, 'gather_calendar', calendar)
    monkeypatch.setattr(gg, 'gather_brief_meeting_context', meeting)
    local, gmail, cal, receipts = [tmp_path / name for name in ('l.json', 'g.json', 'c.json', 'r.json')]
    local.write_text('{}')
    monkeypatch.setattr(gg.sys, 'argv', ['gather_google.py', '--skip-sent', '--skip-stale',
        '--loop-local', str(local), '--gmail-out', str(gmail), '--cal-out', str(cal),
        '--source-results-out', str(receipts)])
    gg.main()
    result = json.loads(receipts.read_text())
    revoked = failure and failure.startswith('revoke-')
    gmail_empty = failure in ('gmail', 'both', 'revoke-gmail')
    assert json.loads(gmail.read_text()) == ([] if gmail_empty else [{'id': 'inbox'}])
    assert json.loads(cal.read_text()) == ([] if failure in ('calendar', 'both', 'revoke-calendar') else [{'id': 'event'}])
    assert result['gmail']['status'] == ('disabled' if failure == 'revoke-gmail' else
        'unavailable' if failure in ('gmail', 'both') else 'partial' if failure == 'quota' else 'ok')
    assert result['calendar']['status'] == ('disabled' if failure == 'revoke-calendar' else
        'unavailable' if failure in ('calendar', 'both') else 'partial')
    assert len(joins) == (0 if revoked or failure == 'quota' else 1)
    if failure == 'both':
        assert 'WARNING: gmail: inbox fixture failure; calendar: calendar fixture failure' in capsys.readouterr().out


def test_quota_denial_skips_extra_gmail_reads_but_keeps_calendar(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)

    def inbox(*args, **kwargs):
        calls.append('inbox')
        raise gg.GoogleCLIError('<HttpError 403: Quota exceeded for quota metric Queries and limit Total Query Cost Units per minute per user>')

    monkeypatch.setattr(gg, 'gather_gmail', inbox)
    monkeypatch.setattr(gg, 'gather_sent', lambda *a, **k: calls.append('sent'))
    monkeypatch.setattr(gg, 'gather_stale_sent', lambda *a, **k: calls.append('stale'))
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    assert calls.count('calendar') == 1
    assert [call for call in calls if call != 'calendar'] == ['inbox']
    assert json.loads(gmail.read_text()) == [] and json.loads(cal.read_text()) == []
    results = json.loads(receipt.read_text())
    assert results['gmail']['status'] == 'unavailable' and results['gmail']['error'] == 'rate_limited'
    assert results['calendar']['status'] == 'partial'
