"""Fresh self-host scheduling must wait for useful context and delivery setup."""

import json
import io
import time
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

import managed
import receiver as rec


BRIEF = {
    'name': 'sotto-morning-brief',
    'schedule': '30 6 * * *',
    'prompt': 'Run my morning brief',
    'skill': 'sotto-morning-brief',
    'runner': 'receiver',
}


def _fresh_selfhost(tmp_path, monkeypatch, at):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(managed, 'enabled', lambda: False)
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: False)
    monkeypatch.setattr(rec.CONNECTORS, 'service_status', lambda: [])
    monkeypatch.setattr(rec, '_hermes_adapter', lambda name: type('Runtime', (), {
        'home_path': staticmethod(lambda filename: str(tmp_path / filename)),
    })())
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'telegram')
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'unknown')
    monkeypatch.setattr(rec, '_local_now', lambda: at)
    monkeypatch.setattr(rec, '_CRON_FIRED', {})
    monkeypatch.setattr(rec, '_CRON_UNPARSED', set())
    monkeypatch.setattr(rec, '_SOURCE_PROBE_LAST_STARTED', 0.0)
    monkeypatch.setattr(rec, '_SOURCE_PROBE_INFLIGHT', False)
    monkeypatch.setattr(rec, '_SOURCE_PROBE_WAIT', 0.0)
    monkeypatch.setattr(rec, '_SOURCE_PROBE_BASIS', None)
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    cron = tmp_path / 'crons.json'
    cron.write_text(json.dumps([BRIEF]))
    monkeypatch.setenv('SOTTO_CRONS_JSON', str(cron))
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: str(
        tmp_path / 'sotto-chief-of-staff' / '_shared' / 'scripts' / 'compose_brief.py'))


def test_fresh_selfhost_due_brief_waits_for_context_and_channel(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    assert rec._delivery_channel_ready('cron:sotto-morning-brief') is False
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == []
    assert rec._CRON_FIRED == {}
    assert not (tmp_path / 'config/onboarding.json').exists()


def test_fresh_selfhost_google_only_and_linked_channel_can_run(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    (tmp_path / 'google_token.json').write_text('{}')
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'linked')
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == [('sotto-welcome-brief', 'onboarding:sotto-welcome-brief')]


def test_connected_bridge_waits_without_successful_allowed_read(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'linked')
    monkeypatch.setattr(rec, '_maybe_bootstrap_selfhost_source_read', lambda: None)
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == []
    assert rec._CRON_FIRED == {}


def test_fresh_valid_local_read_opens_first_brief_even_when_empty(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    source = rec._source_context()
    source.record_bridge_status({'source_status': {'recent_files': 'ok'},
                                 'recent_files': []}, data_root=str(tmp_path))
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'linked')
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == [('sotto-welcome-brief', 'onboarding:sotto-welcome-brief')]


def test_contacts_only_read_does_not_open_first_brief(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    source = rec._source_context()
    source.record_bridge_status({'source_status': {'contacts': 'ok'},
                                 'contacts': [], 'contacts_total': 0},
                                data_root=str(tmp_path))
    assert next(row for row in source.source_health(str(tmp_path))['sources']
                if row['source'] == 'contacts')['status'] == 'empty'
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'linked')
    monkeypatch.setattr(rec, '_maybe_bootstrap_selfhost_source_read', lambda: None)
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert rec._context_sources_ready() is False
    assert admitted == []
    assert rec._CRON_FIRED == {}


def test_disabled_and_unreadable_local_receipts_do_not_open_first_brief(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    source = rec._source_context()
    source.record_bridge_status({'source_status': {'recent_files': 'ok'},
                                 'recent_files': []}, data_root=str(tmp_path))
    source.record_bridge_status({'sources': {'recent_files': 'disabled'}},
                                data_root=str(tmp_path))
    assert rec._context_sources_ready() is False
    receipt = tmp_path / 'config/source-state.json'
    row = json.loads(receipt.read_text())
    row['sources']['recent_files']['status'] = 'ok'
    row['sources']['recent_files']['read']['payload_valid'] = False
    receipt.write_text(json.dumps(row))
    assert rec._context_sources_ready() is False
    receipt.write_text('{broken')
    assert rec._context_sources_ready() is False


def test_regrant_requires_a_read_started_after_consent_restoration(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 5, 30))
    source = rec._source_context()
    now = datetime.now(timezone.utc)

    def stamp(seconds):
        return (now + timedelta(seconds=seconds)).isoformat()

    def read(started):
        started = started if isinstance(started, str) else stamp(started)
        source.record_bridge_status({
            '_bridge_request_started_at': started, 'generated_at': started,
            'source_status': {'recent_files': 'ok'}, 'recent_files': [],
        }, data_root=str(tmp_path))

    def health(started, status):
        source.record_bridge_status({
            '_bridge_request_started_at': stamp(started), 'last_seen': stamp(started),
            'sources': {'recent_files': status},
        }, data_root=str(tmp_path))

    read(-15)
    assert rec._context_sources_ready() is True
    health(-10, 'disabled')
    assert rec._context_sources_ready() is False
    health(-5, 'ok')
    assert next(row for row in source.source_health(str(tmp_path))['sources']
                if row['source'] == 'recent_files')['status'] == 'empty'
    assert rec._context_sources_ready() is False  # retained pre-regrant read is not consent proof
    read(-7)  # delayed response that began before the regrant
    assert rec._context_sources_ready() is False
    read(-4)  # started during a slow regrant probe, before its server acceptance
    assert rec._context_sources_ready() is False
    health(-2, 'ok')  # an ordinary healthy heartbeat must not move the regrant watermark
    assert rec._context_sources_ready() is False
    read(datetime.now(timezone.utc).isoformat())
    assert rec._context_sources_ready() is True
    health(0, 'ok')
    assert rec._context_sources_ready() is True
    receipt = tmp_path / 'config/source-state.json'
    for invalid in (0, -1, 'invalid'):
        state = json.loads(receipt.read_text())
        state['sources']['recent_files']['consent_regrant_epoch'] = invalid
        receipt.write_text(json.dumps(state))
        assert rec._context_sources_ready() is False


def test_quiet_fresh_bridge_bootstraps_a_real_read_before_welcome(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 5, 30))
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'linked')
    source = rec._source_context()
    collected = Event()
    calls = []

    def health(name):
        assert name == 'health'
        calls.append(name)
        payload = {'sources': {'recent_files': 'ok'}}
        source.record_bridge_status(payload, data_root=str(tmp_path))
        return payload

    def read(hours):
        calls.append(('read_local', hours))
        source.record_bridge_status({'source_status': {'recent_files': 'ok'},
                                     'recent_files': []}, data_root=str(tmp_path))
        collected.set()
        return {'recent_files': []}

    monkeypatch.setattr(source, 'bridge_call', health)
    monkeypatch.setattr(source, 'read_local', read)
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()
    assert collected.wait(2)
    rec._cron_tick()

    assert calls == ['health', ('read_local', 24)]
    assert admitted == [('sotto-welcome-brief', 'onboarding:sotto-welcome-brief')]


def test_bootstrap_skips_read_when_mac_disables_every_source(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    source = rec._source_context()
    monkeypatch.setattr(source, 'bridge_call', lambda name: {
        'sources': {'recent_files': 'disabled', 'imessage': 'disabled'}})
    calls = []
    monkeypatch.setattr(source, 'read_local', lambda hours: calls.append(hours))

    rec._bootstrap_selfhost_source_read()

    assert calls == []
    assert rec._context_sources_ready() is False


def test_bootstrap_skips_read_when_only_contacts_are_available(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    source = rec._source_context()
    monkeypatch.setattr(source, 'bridge_call', lambda name: {
        'sources': {'contacts': 'ok', 'recent_files': 'disabled'}})
    calls = []
    monkeypatch.setattr(source, 'read_local', lambda hours: calls.append(hours))

    rec._bootstrap_selfhost_source_read()

    assert calls == []
    assert rec._context_sources_ready() is False


def test_new_source_grant_is_retried_on_next_heartbeat(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 5, 30))
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'linked')
    monkeypatch.setenv('SOTTO_MCP_TOKEN', 'offline-fixture')
    now = [time.time()]
    monkeypatch.setattr(rec.time, 'time', lambda: now[0])
    calls = []
    read_done = Event()

    def response(request, timeout):
        name = json.loads(request.data)['params']['name']
        calls.append(name)
        if name == 'health':
            payload = {'sources': {'recent_files': 'disabled' if calls.count('health') == 1
                                   else 'ok'}}
        else:
            payload = {'source_status': {'recent_files': 'ok'}, 'recent_files': []}
            read_done.set()
        # The real relay adds its server-authored request-start marker to each result.
        payload['_bridge_request_started_at'] = datetime.now(timezone.utc).isoformat()
        return io.BytesIO(json.dumps({'result': {'structuredContent': payload}}).encode())

    source = rec._source_context()
    monkeypatch.setattr(source.urllib.request, 'urlopen', response)
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()
    deadline = time.monotonic() + 2
    while rec._SOURCE_PROBE_INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.001)
    assert calls == ['health']
    assert source.allowed('recent_files') is False  # real bridge_call recorded the Mac switch
    assert admitted == []

    now[0] += rec.CRON_TICK_SECS - 1
    rec._cron_tick()
    assert calls == ['health']
    now[0] += 1
    rec._cron_tick()
    assert read_done.wait(2)
    deadline = time.monotonic() + 2
    while rec._SOURCE_PROBE_INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.001)
    rec._cron_tick()

    assert calls == ['health', 'health', 'read_local']
    assert source.allowed('recent_files') is True
    assert admitted == [('sotto-welcome-brief', 'onboarding:sotto-welcome-brief')]


def _run_inline(monkeypatch):
    monkeypatch.setattr(rec.threading, 'Thread', lambda target, daemon: type(
        'Inline', (), {'start': lambda self: target()})())


def test_unqualified_bootstrap_reads_back_off_to_hourly_and_restart_on_health_change(
        tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 5, 30))
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    source = rec._source_context()
    clock = [time.time()]
    monkeypatch.setattr(rec.time, 'time', lambda: clock[0])
    monkeypatch.setattr(source, 'bridge_call', lambda name: {'sources': {'imessage': 'ok'}})
    status = ['partial']  # the Mac's deferred-unread reader keeps failing
    reads = []

    def read_local(hours):
        reads.append(round((clock[0] - start) / 60))
        source.record_bridge_status({'source_status': {'imessage': status[0]}, 'imessage': [{'x': 1}],
                                     'deferred_unread_imessage': []}, data_root=str(tmp_path))
    monkeypatch.setattr(source, 'read_local', read_local)
    _run_inline(monkeypatch)
    start = clock[0]
    for _ in range(4 * 60):
        rec._maybe_bootstrap_selfhost_source_read()
        clock[0] += rec.CRON_TICK_SECS
    assert reads == [0, 1, 3, 7, 15, 31, 63, 123, 183]  # waits 1, 2, 4 … up to 60 minutes
    assert rec._context_sources_ready() is False

    # A change in the Mac's reported source health starts over at once.
    source.record_bridge_status({'sources': {'recent_files': 'needs_fda'}}, data_root=str(tmp_path))
    rec._maybe_bootstrap_selfhost_source_read()
    assert reads[-1] == round((clock[0] - start) / 60)


def test_regrant_resets_backoff_even_if_retained_read_has_same_health_status(
        tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 5, 30))
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    source = rec._source_context()
    source.record_bridge_status({'source_status': {'imessage': 'partial'},
                                 'imessage': [], 'deferred_unread_imessage': []},
                                data_root=str(tmp_path))
    before = rec._source_probe_basis()
    assert rec._context_sources_ready() is False
    monkeypatch.setattr(rec, '_SOURCE_PROBE_BASIS', before)
    monkeypatch.setattr(rec, '_SOURCE_PROBE_WAIT', rec.SOURCE_PROBE_MAX_WAIT_SECS)
    monkeypatch.setattr(rec, '_SOURCE_PROBE_LAST_STARTED', time.time())
    source.record_bridge_status({'sources': {'imessage': 'disabled'}}, data_root=str(tmp_path))
    source.record_bridge_status({'sources': {'imessage': 'ok'}}, data_root=str(tmp_path))
    after = rec._source_probe_basis()
    assert [row[1] for row in after] == [row[1] for row in before]
    assert after != before  # a new consent generation requires a post-regrant read
    started = []
    monkeypatch.setattr(rec.threading, 'Thread', lambda target, daemon: type(
        'Recorded', (), {'start': lambda self: started.append(target)})())

    rec._maybe_bootstrap_selfhost_source_read()

    assert started == [rec._bootstrap_selfhost_source_read]


@pytest.mark.parametrize('phase', ['retrying', 'model_held'])
def test_bootstrap_runs_for_every_held_first_brief_status(tmp_path, monkeypatch, phase):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 5, 30))
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    (tmp_path / 'config').mkdir(exist_ok=True)
    (tmp_path / 'config/onboarding.json').write_text(json.dumps(
        {'phase': 'composing' if phase == 'retrying' else phase,
         'lease_until': 1, 'retry_at': 1, 'attempts': 1}))
    import onboarding
    assert onboarding.status(tmp_path) == phase
    assert rec._fresh_selfhost_brief_hold() is True
    started = []
    monkeypatch.setattr(rec.threading, 'Thread', lambda target, daemon: type(
        'Recorded', (), {'start': lambda self: started.append(target)})())

    rec._maybe_bootstrap_selfhost_source_read()

    assert started == [rec._bootstrap_selfhost_source_read]


def test_setup_page_waits_for_a_real_mac_read_not_the_link(tmp_path, monkeypatch):
    real_adapter = rec._hermes_adapter
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    monkeypatch.setattr(rec, '_hermes_adapter', real_adapter)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(rec.CONNECTORS, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'linked')
    monkeypatch.setattr(rec, 'google_connected', lambda: (False, 'nope'))
    monkeypatch.setattr(rec, 'RAILWAY_DOMAIN', 'myapp.up.railway.app')
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    source = rec._source_context()
    source.record_bridge_status({'source_status': {'contacts': 'ok'}, 'contacts': [{'n': 1}],
                                 'contacts_total': 1}, data_root=str(tmp_path))
    hint = "Sotto hasn't read a Mac source besides Contacts yet"

    page = rec._setup_page('abc')  # Contacts only, no Google: no brief can start

    assert 'hero-cta' not in page and hint in page
    assert "<h2 class='tile-title'>Link your Mac</h2><span class='tile-state'>to do</span>" in page

    source.record_bridge_status({'source_status': {'recent_files': 'ok'}, 'recent_files': []},
                                data_root=str(tmp_path))
    page = rec._setup_page('abc')
    assert 'hero-cta' in page and hint not in page
    assert "<h2 class='tile-title'>Link your Mac</h2><span class='tile-state'>done</span>" in page


def test_fresh_selfhost_google_only_waits_while_channel_unlinked(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    (tmp_path / 'google_token.json').write_text('{}')
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == []
    assert rec._CRON_FIRED == {}


def test_fresh_selfhost_preparation_waits_for_context_and_channel(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 25))
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == []
    assert rec.WORK_QUEUE.active_job(str(tmp_path), 'run',
                                     'background:prepare:sotto-morning-brief') is None
    assert not (tmp_path / 'config/onboarding.json').exists()


def test_established_selfhost_keeps_brief_during_source_and_provider_outage(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'onboarding.json').write_text(json.dumps({
        'phase': 'delivered', 'completed_at': 100,
    }))
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == [('sotto-morning-brief', 'cron:sotto-morning-brief')]


def test_accepted_scheduled_first_brief_keeps_recovery_when_sources_offline(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    briefs = tmp_path / 'briefs'
    briefs.mkdir()
    (briefs / '2026-09-27.morning.delivered').write_text('first-run')
    events = tmp_path / 'events'
    events.mkdir()
    (events / 'outbox.json').write_text(json.dumps({'rows': [{
        'id': 'first-run', 'day': '2026-09-27', 'status': 'delivered',
        'acceptance': 'accepted', 'payload': {'label': 'cron:sotto-morning-brief'},
        'receipt': {'accepted_at': '2026-09-27T13:30:00Z', 'message_id': 'provider-id'},
    }]}))
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == [('sotto-morning-brief', 'cron:sotto-morning-brief')]
    assert json.loads((tmp_path / 'config/onboarding.json').read_text())['phase'] == 'existing'


def test_managed_cron_keeps_its_existing_capability_gate(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 30))
    monkeypatch.setattr(managed, 'enabled', lambda: True)
    monkeypatch.setattr(managed, 'brief_hold', lambda data: None)
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    rec._cron_tick()

    assert admitted == [('sotto-morning-brief', 'cron:sotto-morning-brief')]


def test_fresh_selfhost_wake_folds_local_context_until_channel_is_linked(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 10, 0))
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    monkeypatch.setattr(rec, '_in_brief_cron_window', lambda skill: False)
    folded = []
    monkeypatch.setattr(rec, '_fold_into_snapshot', lambda *args: (
        folded.append(args), (200, {'status': 'waiting_for_setup'}))[1])
    monkeypatch.setattr(rec, 'run_skill', lambda *args: (_ for _ in ()).throw(
        AssertionError('fresh wake admitted a model brief')))
    local = {'messages': [{'id': 'fixture'}]}

    result = rec.handle_trigger({'type': 'morning_ready', 'date': '2026-09-28',
                                 'local_data': local})

    assert result == (200, {'status': 'waiting_for_setup'})
    assert folded and folded[0][:3] == ('morning_ready', '2026-09-28', local)
    assert not rec.os.path.exists(rec.delivered_flag('2026-09-28', 'morning'))


def test_fresh_selfhost_payloadless_wake_holds_without_claim(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 10, 0))
    monkeypatch.setattr(rec, '_in_brief_cron_window', lambda skill: False)
    monkeypatch.setattr(rec, 'run_skill', lambda *args: (_ for _ in ()).throw(
        AssertionError('fresh wake admitted a model brief')))

    code, result = rec.handle_trigger({'type': 'morning_ready', 'date': '2026-09-28'})

    assert code == 200 and result['status'] == 'held'
    assert not rec.os.path.exists(rec.delivered_flag('2026-09-28', 'morning'))


def test_explicit_run_now_remains_available_during_fresh_setup(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 10, 0))
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kwargs:
                        admitted.append((skill, label)) or True)

    result = rec._run_dashboard_job('sotto-morning-brief')

    assert result == {'ok': True, 'skill': 'sotto-morning-brief'}
    assert admitted == [('sotto-morning-brief', 'run-now:sotto-morning-brief')]


def test_established_selfhost_wake_can_enqueue_during_channel_outage(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 10, 0))
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'onboarding.json').write_text(json.dumps({'phase': 'delivered'}))
    monkeypatch.setattr(rec, '_in_brief_cron_window', lambda skill: False)
    admitted = []
    monkeypatch.setattr(rec, 'run_skill', lambda *args: admitted.append(args))

    result = rec.handle_trigger({'type': 'morning_ready', 'date': '2026-09-28'})

    assert result == (202, {'status': 'enqueued', 'skill': 'sotto-morning-brief'})
    assert len(admitted) == 1


def test_only_exact_unaccepted_completed_handoff_bypasses_first_setup(tmp_path, monkeypatch):
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 10, 0))
    label, day = 'cron:sotto-morning-brief', '2026-09-28'
    queue = rec.WORK_QUEUE
    run_id = queue.enqueue(str(tmp_path), 'run', {'label': label}, key='prior-composition')
    queue.claim(str(tmp_path), 'worker')
    queue.save_result(str(tmp_path), run_id, 'worker', {'label': label, 'text': 'Old brief'})
    assert queue.finish(str(tmp_path), run_id, 'worker')
    assert queue.get(str(tmp_path), run_id)['result'] is None  # handoff scrubs the saved brief
    outbox = tmp_path / 'events/outbox.json'

    def rejected_row(acceptance='rejected', row_day=day):
        outbox.write_text(json.dumps({'rows': [{
            'id': run_id, 'day': row_day, 'status': 'superseded',
            'effect_phase': 'invalidate', 'acceptance': acceptance,
            'acceptance_uncertain': acceptance == 'unknown',
            'payload': {'label': label},
        }]}))

    rejected_row()
    assert rec._validated_invalid_brief_recovery({'run_id': run_id, 'day': day}, label)
    assert not rec._validated_invalid_brief_recovery({'run_id': 'missing', 'day': day}, label)
    assert not rec._validated_invalid_brief_recovery({'run_id': run_id, 'day': day},
                                                     'cron:sotto-evening-brief')
    rejected_row(row_day='2026-09-27')
    assert not rec._validated_invalid_brief_recovery({'run_id': run_id, 'day': '2026-09-27'}, label)
    for acceptance in ('accepted', 'unknown'):
        rejected_row(acceptance=acceptance)
        assert not rec._validated_invalid_brief_recovery({'run_id': run_id, 'day': day}, label)
    assert rec._fresh_selfhost_brief_hold()  # unrelated next admission remains gated


def _selfhost_photon(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.delenv('PHOTON_ALLOW_ALL_USERS', raising=False)
    for key, value in (('SOTTO_CRON_DELIVER', 'photon'), ('PHOTON_HOME_CHANNEL', '+15551234567'),
                       ('PHOTON_ALLOWED_USERS', '+15551234567'), ('PHOTON_PROJECT_ID', 'proj'),
                       ('PHOTON_PROJECT_SECRET', 'secret')):
        monkeypatch.setenv(key, value)
    (tmp_path / 'config').mkdir(exist_ok=True)


def test_established_selfhost_photon_without_owner_receipt_keeps_delivering(tmp_path, monkeypatch):
    # Installs made before owner receipts existed never wrote photon-activation.json.
    _selfhost_photon(tmp_path, monkeypatch)
    (tmp_path / 'config/onboarding.json').write_text(json.dumps({'phase': 'existing'}))
    assert rec._channel_status('photon') == 'linked'
    assert rec._delivery_channel_ready('cron:sotto-proactive') is True


def test_fresh_or_managed_photon_still_waits_for_the_owner_hello(tmp_path, monkeypatch):
    _selfhost_photon(tmp_path, monkeypatch)
    assert rec._channel_status('photon') == 'pairing'  # fresh self-host: no accepted brief yet
    (tmp_path / 'config/onboarding.json').write_text(json.dumps({'phase': 'existing'}))
    (tmp_path / 'config/photon-activation.json').write_text('{broken')
    assert rec._channel_status('photon') == 'pairing'  # a corrupt receipt is never trusted
    (tmp_path / 'config/photon-activation.json').unlink()
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    assert rec._channel_status('photon') == 'pairing'  # Cloud always required the receipt
