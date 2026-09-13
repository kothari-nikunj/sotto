"""Source truth: empty is observed, failures retain age, revocation gates every cached read."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import compose_brief as cb
import gather_google as gg
import source_context as sc


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)


def stamp(hours=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def cached_message():
    return {'generated_at': stamp(1), 'source_status': {'imessage': 'ok'},
            'imessage': [{'text': 'cached synthetic message'}]}


@pytest.mark.parametrize('mode', ['self-host', 'managed'])
def test_revocation_does_not_resurrect_snapshot_when_another_source_keeps_brief_ready(tmp_path, monkeypatch, mode):
    cb._save_local_snapshot(cached_message())
    if mode == 'managed':
        monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
        monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
        (tmp_path / 'config/managed-capabilities.json').write_text(json.dumps({
            'tenant_id': 'test', 'sources': {'calendar': {'consented': True, 'connected': True},
                                          'imessage': {'consented': False, 'connected': False}}}))
    local = {'generated_at': stamp(), 'source_status': {'imessage': 'disabled'}, 'imessage': []}
    assert cb._live_local_observation(local)
    current = cb._save_local_snapshot(local)
    assert not current.get('imessage')
    assert current['source_status']['imessage'] == 'disabled'
    assert not cb._local_fallback({}).get('imessage')


def test_valid_empty_read_clears_previous_rows_and_stays_empty_offline():
    cb._save_local_snapshot(cached_message())
    current = cb._save_local_snapshot({'generated_at': stamp(), 'source_status': {'imessage': 'ok'}, 'imessage': []})
    assert current['imessage'] == []
    assert '_local_stale_since' not in current
    assert not cb._local_fallback({}).get('imessage')


def test_partial_pull_preserves_old_source_age_without_renewing_its_ttl():
    old = stamp(cb.LOCAL_SNAPSHOT_TTL_HOURS + 1)
    cb._save_local_snapshot({'generated_at': old, 'source_status': {'imessage': 'ok', 'whatsapp': 'ok'},
                            'imessage': [{'text': 'expired'}], 'whatsapp': [{'text': 'old'}]})
    current = cb._save_local_snapshot({'generated_at': stamp(),
                                      'source_status': {'imessage': 'degraded', 'whatsapp': 'ok'},
                                      'imessage': [], 'whatsapp': [{'text': 'fresh'}]})
    assert not current.get('imessage')
    assert current['whatsapp'][0]['text'] == 'fresh'
    assert current['_source_observed_at']['imessage'] == old
    assert not cb._local_fallback({}).get('imessage')


def test_out_of_order_snapshot_does_not_roll_back_live_data():
    fresh = cached_message()
    fresh.update(generated_at=stamp(), imessage=[{'text': 'newest'}])
    cb._save_local_snapshot(fresh)
    returned = cb._save_local_snapshot(cached_message())
    assert returned['imessage'][0]['text'] == 'newest'
    assert cb._local_fallback({})['imessage'][0]['text'] == 'newest'


def test_self_host_status_receipt_records_source_revocation_without_storing_history(tmp_path):
    now = datetime.now(timezone.utc)
    sc.record_bridge_status({'generated_at': now.isoformat(), 'sources': {'imessage': 'disabled'}})
    assert not sc.allowed('imessage')
    # A slower earlier read cannot re-enable the source; a newer explicit health state can.
    sc.record_bridge_status({'generated_at': stamp(1), 'source_status': {'imessage': 'ok'}})
    assert not sc.allowed('imessage')
    sc.record_bridge_status({'generated_at': (now + timedelta(seconds=1)).isoformat(), 'sources': {'imessage': 'ok'}})
    assert sc.allowed('imessage')
    state = json.loads((tmp_path / 'config/source-state.json').read_text())
    assert 'episodes' not in json.dumps(state)


def test_google_source_receipts_distinguish_failed_empty_from_complete_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(gg, '_find_google_api', lambda: 'synthetic-api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    outputs = [str(tmp_path / name) for name in ('gmail.json', 'cal.json', 'sources.json')]
    monkeypatch.setattr('sys.argv', ['gather_google', '--skip-gmail', '--gmail-out', outputs[0],
                                    '--cal-out', outputs[1], '--source-results-out', outputs[2]])
    def failed(*a, **k):
        raise RuntimeError('synthetic provider failure')
    monkeypatch.setattr(gg, 'gather_calendar', failed)
    gg.main()
    assert json.loads((tmp_path / 'cal.json').read_text()) == []  # legacy shape retained
    observation = json.loads((tmp_path / 'sources.json').read_text())['calendar']
    assert observation['status'] == 'unavailable' and observation['complete'] is False
    assert observation['error'] == 'RuntimeError'
    def empty(*a, observation=None, **k):
        observation.update(complete=True, coverage={'since': stamp(), 'until': stamp(-72)})
        return []
    monkeypatch.setattr(gg, 'gather_calendar', empty)
    gg.main()
    observation = json.loads((tmp_path / 'sources.json').read_text())['calendar']
    assert observation['status'] == 'ok' and observation['complete'] is True
    assert json.loads((tmp_path / 'cal.json').read_text()) == []


def test_source_failure_reaches_brief_availability_even_when_no_rows():
    local = cb._normalize_local({'source_results': {'calendar': sc.source_result('unavailable')}})
    assert local['_source_availability']['calendar'] == 'unavailable'


def test_delivery_source_manifest_includes_stale_email_threads_without_current_inbox_rows():
    assert sc.used_sources(gmail={'emails': [], 'staleThreads': [{'id': 'synthetic-thread'}]}) == ['gmail']


def test_permission_fingerprint_changes_only_when_source_permission_changes():
    before = sc.permission_fingerprint()
    sc.record_bridge_status({'sources': {'imessage': 'unavailable'}})
    assert sc.permission_fingerprint() == before  # availability is not revocation
    sc.record_bridge_status({'sources': {'imessage': 'disabled'}})
    assert sc.permission_fingerprint() != before
