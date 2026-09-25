import json
import onboarding


def test_first_look_holds_then_resumes_without_duplicate_and_ack_completes(tmp_path):
    started = []
    fire = lambda: started.append(True)
    assert onboarding.tick(tmp_path, False, fire, 100) == 'waiting_for_context'
    assert onboarding.tick(tmp_path, True, fire, 101) == 'started'
    assert onboarding.tick(tmp_path, True, fire, 102) == 'composing'
    assert len(started) == 1
    event = tmp_path / 'events/outbox.json'
    event.parent.mkdir()
    event.write_text(json.dumps({'rows': [{'status': 'pending', 'payload': {'label': onboarding.LABEL}}]}))
    assert onboarding.tick(tmp_path, True, fire, 4000) == 'queued'
    onboarding.delivered(tmp_path, 4100)
    assert onboarding.tick(tmp_path, True, fire, 86400) == 'delivered'
    assert len(started) == 1


def test_failed_composition_has_bounded_retry_and_legacy_pilot_is_preserved(tmp_path):
    started = []
    for at in (1, 2000, 4000, 6000):
        onboarding.tick(tmp_path, True, lambda: started.append(at), at)
    assert started == [1, 2000, 4000]
    legacy = tmp_path / 'legacy/briefs'
    legacy.mkdir(parents=True)
    (legacy / '2026-09-06.evening.delivered').write_text('old-run')
    assert onboarding.tick(legacy.parent, lambda: 1/0, lambda: 1/0, 6000) == 'existing'


def test_delivery_receipt_recovers_crash_between_send_and_state_update(tmp_path):
    path = tmp_path / 'events/outbox.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'rows': [{'status': 'delivered', 'payload': {'label': onboarding.LABEL}}]}))
    assert onboarding.tick(tmp_path, True, lambda: 1/0, 100) == 'delivered'


def test_first_look_holds_scheduled_duplicate_but_releases_later(tmp_path):
    assert not onboarding.scheduled_hold(tmp_path, 100)
    onboarding.tick(tmp_path, True, lambda: None, 100)
    assert onboarding.scheduled_hold(tmp_path, 101)
    onboarding.delivered(tmp_path, 200)
    assert onboarding.scheduled_hold(tmp_path, 300)
    assert not onboarding.scheduled_hold(tmp_path, 3000)


def test_custom_runner_that_cannot_start_does_not_hold_welcome_lease(tmp_path):
    assert onboarding.tick(tmp_path, True, lambda: False, 100) == 'not_started'
    state = json.loads((tmp_path / 'config/onboarding.json').read_text())
    assert state['phase'] == 'waiting' and state['attempts'] == 0 and state['lease_until'] == 0
    assert onboarding.tick(tmp_path, True, lambda: True, 101) == 'started'


def test_background_tick_runs_one_quiet_process_without_waiting_for_chat(tmp_path, monkeypatch):
    import receiver as rec
    import managed
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(managed, 'enabled', lambda: True)
    monkeypatch.setattr(managed, 'has_sources', lambda _: True)
    monkeypatch.setattr(managed, 'messaging_activated', lambda _: False)
    monkeypatch.setattr(rec, '_CONTEXT_LAST_STARTED', 0)
    monkeypatch.setattr(rec, '_RUNS_INFLIGHT', {})
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *args: '/pack/memory_cycle.py')
    runs = []
    monkeypatch.setattr(rec, '_spawn_and_deliver', lambda *args: runs.append(args))
    rec._background_context_tick()
    rec._background_context_tick()
    assert len(runs) == 1
    assert runs[0][0][-1] == '/pack/memory_cycle.py'
    assert runs[0][1:] == ('{}', 'background:sotto-memory')
    assert rec._spawn_env()['SOTTO_UNATTENDED'] == '1'


def test_zero_sources_do_not_start_background_model_work(tmp_path, monkeypatch):
    import receiver as rec
    import managed
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(managed, 'enabled', lambda: True)
    monkeypatch.setattr(managed, 'has_sources', lambda _: False)
    monkeypatch.setattr(rec, '_spawn_and_deliver', lambda *args: 1/0)
    rec._background_context_tick()


def test_setup_progress_follows_acceptance_and_recovers_without_writing(tmp_path):
    assert onboarding.status(tmp_path, 100) == 'waiting'
    assert not (tmp_path / 'config').exists()
    onboarding.tick(tmp_path, True, lambda: None, 100)
    assert onboarding.status(tmp_path, 101) == 'composing'
    assert onboarding.status(tmp_path, 2000) == 'retrying'
    path = tmp_path / 'events/outbox.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'rows': [{'status': 'pending', 'payload': {'label': onboarding.LABEL}}]}))
    assert onboarding.status(tmp_path, 2000) == 'queued'
    path.write_text(json.dumps({'rows': [{'status': 'delivered', 'payload': {'label': onboarding.LABEL}}]}))
    # Acceptance before a crash is enough even if the completion callback has not run.
    assert onboarding.status(tmp_path, 2000) == 'delivered'
    assert json.loads((tmp_path / 'config/onboarding.json').read_text())['phase'] == 'composing'


def test_granola_only_self_host_gets_automatic_first_look(tmp_path, monkeypatch):
    import receiver as rec
    import managed
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(managed, 'enabled', lambda: False)
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: False)
    from types import SimpleNamespace
    monkeypatch.setattr(rec, '_hermes_adapter', lambda _: SimpleNamespace(home_path=lambda _: tmp_path / 'absent'))
    monkeypatch.setattr(rec.CONNECTORS, 'service_status', lambda: [{'service': 'granola', 'connected': True}])
    monkeypatch.setattr(rec, '_delivery_channel_ready', lambda _: True)
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *a: None)
    runs = []
    monkeypatch.setattr(rec, '_managed_brief', lambda *args: runs.append(args))
    rec._background_context_tick()
    rec._background_context_tick()
    assert runs == [('sotto-welcome-brief', onboarding.LABEL)]


def test_failed_granola_connection_does_not_start_first_look(tmp_path, monkeypatch):
    import receiver as rec
    import managed
    from types import SimpleNamespace
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(managed, 'enabled', lambda: False)
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: False)
    monkeypatch.setattr(rec, '_hermes_adapter', lambda _: SimpleNamespace(home_path=lambda _: tmp_path / 'absent'))
    monkeypatch.setattr(rec.CONNECTORS, 'service_status', lambda: [{'service': 'granola', 'connected': True}])
    monkeypatch.setattr(rec, '_connector_error', lambda _: 'authorization expired')
    monkeypatch.setattr(rec, '_managed_brief', lambda *args: 1 / 0)
    rec._background_context_tick()
    assert not (tmp_path / 'config/onboarding.json').exists()
