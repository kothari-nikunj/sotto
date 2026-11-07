"""Compatible receiver contracts using the existing dashboard and pulse fixtures."""
# ruff: noqa: PLR2004
import json
from datetime import datetime, timezone
from pathlib import Path

from test_dashboard import (_server, _fixtures, _cal_dates, _stub_gather, _raw_cal_events,
                            _login, _get, _gather_calls, _cal_file)
from test_relationship_pulse_pipeline import _write_source_commitment, _canonical_writer, PACK

pytest_plugins = ("test_relationship_pulse_pipeline",)


def test_calendar_refresh_file_rides_the_dashboard_s_fetch(tmp_path):
    """One gather serves both consumers: a dashboard hit warms the cache, and the refresh thread's
    work (run inline) writes calendar_today.json from THAT snapshot — no second fork inside the
    TTL, and the file carries the belief's generated_at, not the write's."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    today, tomorrow, later = _cal_dates(m)
    count = _stub_gather(m, tmp_path, _raw_cal_events(today, tomorrow, later))
    try:
        cookie = _login(base)
        cal = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal["cached"] is False and _gather_calls(count) == 1
        assert m.CALCACHE.refresh_once() is True
        assert _gather_calls(count) == 1                  # single flight: the SAME fetch
        data = json.load(open(_cal_file(tmp_path)))
        assert data["date"] == today
        assert data["refresh_secs"] == m.CALCACHE.REFRESH_SECS_DEFAULT
        assert data["generated_at"] == cal["generated_at"]
        # times + a count, never people; tomorrow's event is not "today"
        assert data["events"] == [{"summary": "Board sync",
                                   "start": f"{today}T09:00:00-07:00",
                                   "end": f"{today}T10:00:00-07:00",
                                   "attendees": 2, "all_day": False,
                                   "prep_location": "Zoom HQ", "id": "e1"}]
        assert "sarah@acme.com" not in json.dumps(data)
        # and the reverse direction: a thread refresh warms the endpoint
        m.CALCACHE._CAL_CACHE.update({"ts": 0.0, "value": None})
        assert m.CALCACHE.refresh_once() is True
        assert _gather_calls(count) == 2
        cal2 = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal2["cached"] is True and _gather_calls(count) == 2
    finally:
        srv.shutdown()


def test_pulse_uses_protected_ledger_ask_over_long_summary(flow):
    root, _, _, _, state, _ = flow
    import relationship_pulse_pipeline as pipeline
    state['google_sources'] = ['gmail']
    _write_source_commitment(_canonical_writer(), datetime.now(timezone.utc), name='Austin',
        ask='Respond about Project Cedar',
        summary='Austin followed up after the introduction, then poked again. ' * 5)
    from loops_query import query_permitted_rows
    assert query_permitted_rows(str(root), ['imessage'])['you_owe'] == []
    row = query_permitted_rows(str(root), ['gmail'], include_request_context=True)['you_owe'][0]
    assert row['ask'] == row['what'] == 'Respond about Project Cedar'
    trusted = query_permitted_rows(str(root), ['gmail'], include_ask=True)['you_owe'][0]
    assert trusted['ask'] == row['ask'] and 'poked' in trusted['what']
    paths = list((root / 'knowledge/continuity').glob('*.md'))
    before = {path: path.read_bytes() for path in paths}
    def command(script, *args, **kwargs):
        Path(args[args.index('--gmail-out') + 1]).write_text('[]')
        Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {'status': 'ok'}}))
    text = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())['text']
    assert 'Austin' in text and 'Respond about Project Cedar' in text and 'poked' not in text
    assert {path: path.read_bytes() for path in paths} == before
