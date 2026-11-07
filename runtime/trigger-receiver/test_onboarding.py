import json

import pytest

import onboarding


def _fresh_mac_read(root, monkeypatch):
    import receiver as rec
    monkeypatch.setenv('SOTTO_DATA', str(root))
    rec._source_context().record_bridge_status(
        {'source_status': {'recent_files': 'ok'}, 'recent_files': []}, data_root=str(root))


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


def test_model_budget_hold_is_durable_and_only_resumes_after_capability_recovers(tmp_path):
    started = []
    assert onboarding.tick(tmp_path, True, lambda: started.append('first'), 100) == 'started'
    onboarding.model_budget_held(tmp_path)
    state_path = tmp_path / 'config/onboarding.json'
    held = json.loads(state_path.read_text())
    assert held['phase'] == 'model_held'
    assert held['lease_until'] == held['retry_at'] == 0
    assert held['attempts'] == 0
    assert onboarding.status(tmp_path, 2000) == 'model_held'
    for capability in (None, lambda: False, lambda: (_ for _ in ()).throw(OSError('offline'))):
        assert onboarding.tick(tmp_path, True, lambda: started.append('unexpected'), 2000,
                               model_ready=capability) == 'model_held'
    assert json.loads(state_path.read_text()) == held
    assert started == ['first']
    assert onboarding.tick(tmp_path, True, lambda: started.append('resumed'), 2000,
                           model_ready=lambda: True) == 'started'
    assert started == ['first', 'resumed']
    assert json.loads(state_path.read_text())['attempts'] == 1


def test_third_fault_attempt_budget_denial_refunds_once_then_resumes_third(tmp_path):
    started = []
    for at in (100, 2000, 4000):
        assert onboarding.tick(tmp_path, True, lambda: started.append(at), at) == 'started'
    state_path = tmp_path / 'config/onboarding.json'
    assert json.loads(state_path.read_text())['attempts'] == 3
    onboarding.model_budget_held(tmp_path)
    held = json.loads(state_path.read_text())
    assert held['phase'] == 'model_held' and held['attempts'] == 2
    onboarding.model_budget_held(tmp_path)
    assert json.loads(state_path.read_text()) == held
    assert onboarding.tick(tmp_path, True, lambda: started.append(4001), 4001,
                           model_ready=lambda: True) == 'started'
    assert started == [100, 2000, 4000, 4001]
    assert json.loads(state_path.read_text())['attempts'] == 3


def test_no_start_after_recovered_capability_preserves_model_hold(tmp_path):
    onboarding.tick(tmp_path, True, lambda: None, 100)
    onboarding.model_budget_held(tmp_path)
    def second_check_denies():
        onboarding.model_budget_held(tmp_path)
        return False
    assert onboarding.tick(tmp_path, True, second_check_denies, 2000,
                           model_ready=lambda: True) == 'model_held'
    state = json.loads((tmp_path / 'config/onboarding.json').read_text())
    assert state['phase'] == 'model_held' and state['attempts'] == 0
    assert state['lease_until'] == state['retry_at'] == 0


def test_model_budget_hold_never_overrides_delivery_or_existing_and_pending_wins(tmp_path):
    onboarding.tick(tmp_path, True, lambda: None, 100)
    onboarding.model_budget_held(tmp_path)
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    outbox.write_text(json.dumps({'rows': [{'status': 'pending',
                                           'payload': {'label': onboarding.LABEL}}]}))
    assert onboarding.status(tmp_path, 2000) == 'queued'
    assert onboarding.tick(tmp_path, True, lambda: 1 / 0, 2000,
                           model_ready=lambda: 1 / 0) == 'queued'
    outbox.write_text(json.dumps({'rows': [{'status': 'delivered',
                                           'payload': {'label': onboarding.LABEL}}]}))
    assert onboarding.status(tmp_path, 2000) == 'delivered'
    assert onboarding.tick(tmp_path, True, lambda: 1 / 0, 2000,
                           model_ready=lambda: 1 / 0) == 'delivered'
    onboarding.model_budget_held(tmp_path)
    assert json.loads((tmp_path / 'config/onboarding.json').read_text())['phase'] == 'delivered'
    other = tmp_path / 'existing'
    (other / 'config').mkdir(parents=True)
    (other / 'config/onboarding.json').write_text(json.dumps({'phase': 'existing'}))
    onboarding.model_budget_held(other)
    assert onboarding.status(other) == 'existing'


def test_scheduled_first_brief_wins_while_welcome_model_is_held(tmp_path, monkeypatch):
    onboarding.tick(tmp_path, True, lambda: None, 100)
    onboarding.model_budget_held(tmp_path)
    monkeypatch.setattr(onboarding, '_unresolved_scheduled_brief', lambda _: 'queued')
    assert onboarding.status(tmp_path, 2000) == 'queued'
    assert onboarding.tick(tmp_path, True, lambda: 1 / 0, 2000,
                           model_ready=lambda: 1 / 0) == 'queued'
    monkeypatch.setattr(onboarding, '_unresolved_scheduled_brief', lambda _: None)
    monkeypatch.setattr(onboarding, '_accepted_scheduled_brief', lambda _: True)
    assert onboarding.status(tmp_path, 2000) == 'existing'
    assert onboarding.tick(tmp_path, True, lambda: 1 / 0, 2000,
                           model_ready=lambda: 1 / 0) == 'existing'
    assert json.loads((tmp_path / 'config/onboarding.json').read_text())['phase'] == 'existing'


def test_failed_composition_has_bounded_retry_and_legacy_pilot_is_preserved(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import os
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    started = []
    for at in (1, 2000, 4000, 6000):
        onboarding.tick(tmp_path, True, lambda: started.append(at), at)
    assert started == [1, 2000, 4000]
    legacy = tmp_path / 'legacy/briefs'
    legacy.mkdir(parents=True)
    marker = legacy / '2026-09-06.evening.delivered'
    marker.write_text('old-run')
    claimed_at = datetime(2026, 9, 6, 18, tzinfo=timezone.utc).timestamp()
    os.utime(marker, (claimed_at, claimed_at))
    events = legacy.parent / 'events'
    events.mkdir()
    stamp = datetime.fromtimestamp(marker.stat().st_mtime + 1, timezone.utc)
    (events / 'delivery.jsonl').write_text(json.dumps({
        'ts': stamp.isoformat(), 'label': 'cron:sotto-evening-brief',
        'status': 'delivered'}) + '\n')
    assert onboarding.tick(legacy.parent, lambda: 1/0, lambda: 1/0, 6000) == 'existing'


def test_unaccepted_marker_does_not_create_existing_phase(tmp_path):
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    marker.write_text('f' * 32)
    events = tmp_path / 'events'
    events.mkdir()
    (events / 'outbox.json').write_text(json.dumps({'rows': [{
        'id': 'f' * 32, 'day': '2026-09-26', 'status': 'expired',
        'acceptance': 'not_attempted', 'acceptance_uncertain': False,
        'payload': {'label': 'cron:sotto-morning-brief'}}]}))
    assert onboarding.status(tmp_path, 100) == 'waiting'
    started = []
    assert onboarding.tick(tmp_path, True, lambda: started.append(True), 100) == 'started'
    assert started == [True]


def test_current_accepted_brief_marker_adopts_existing(tmp_path):
    import importlib.util
    from pathlib import Path
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    marker.write_text('a' * 32)
    spec = importlib.util.spec_from_file_location('onboarding_outbox_fixture', Path(__file__).with_name('outbox.py'))
    box = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(box)
    box.HOOKS['data_root'] = lambda: str(tmp_path)
    box.HOOKS['json_transaction'] = onboarding.json_transaction
    box.HOOKS['on_delivered'] = lambda payload: True
    assert box._enqueue('a' * 32, box.KIND_BRIEF,
                        {'label': 'cron:sotto-morning-brief', 'run_id': 'a' * 32},
                        100, '2026-09-26') == 'pending'
    assert box._settle('a' * 32, True, '', 1, {'message_id': 'provider-message'})[0] == 'delivered'
    assert box._apply_effects('a' * 32) is True
    events = tmp_path / 'events'
    row = json.loads((events / 'outbox.json').read_text())['rows'][0]
    assert row['payload'] == {'label': 'cron:sotto-morning-brief'}
    assert row['id'] == 'a' * 32 and row['acceptance'] == 'accepted'
    assert onboarding.status(tmp_path, 100) == 'existing'
    assert onboarding.tick(tmp_path, lambda: 1/0, lambda: 1/0, 100) == 'existing'
    assert json.loads((tmp_path / 'config/onboarding.json').read_text())['acceptance_proven'] is True
    # A terminal adoption survives normal outbox retention.
    (events / 'outbox.json').unlink()
    assert onboarding.status(tmp_path, 101) == 'existing'


def test_modern_accepted_outbox_requires_marker_run_id(tmp_path):
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    marker.write_text('a' * 32)
    events = tmp_path / 'events'
    events.mkdir()
    row = {'day': '2026-09-26', 'status': 'delivered', 'acceptance': 'accepted',
           'receipt': {'message_id': 'provider-message', 'accepted_at': 100},
           'payload': {'label': 'cron:sotto-morning-brief'}}
    path = events / 'outbox.json'
    path.write_text(json.dumps({'rows': [row]}))
    assert onboarding.status(tmp_path, 100) == 'waiting'
    row['id'] = 'b' * 32
    path.write_text(json.dumps({'rows': [row]}))
    assert onboarding.status(tmp_path, 100) == 'waiting'
    row['id'] = 'a' * 32
    path.write_text(json.dumps({'rows': [row]}))
    assert onboarding.status(tmp_path, 100) == 'existing'


def test_existing_phase_survives_pruned_acceptance_receipts(tmp_path):
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    marker.write_text('a' * 32)
    state = tmp_path / 'config/onboarding.json'
    state.parent.mkdir()
    state.write_text(json.dumps({'phase': 'existing', 'completed_at': 90}))
    assert onboarding.status(tmp_path, 100) == 'existing'
    assert onboarding.tick(tmp_path, lambda: 1/0, lambda: 1/0, 100) == 'existing'
    assert json.loads(state.read_text())['phase'] == 'existing'
    marker.unlink()  # artifact retention may remove the claim too
    assert onboarding.status(tmp_path, 101) == 'existing'
    assert onboarding.tick(tmp_path, lambda: 1/0, lambda: 1/0, 101) == 'existing'


def test_longstanding_existing_survives_new_rejected_brief(tmp_path):
    state = tmp_path / 'config/onboarding.json'
    state.parent.mkdir()
    state.write_text(json.dumps({'phase': 'existing', 'completed_at': 100}))
    # The acceptance receipt that established this installation has aged out.
    # A much later rejected brief says nothing about that original adoption.
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    marker.write_text('b' * 32)
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    outbox.write_text(json.dumps({'rows': [{
        'id': 'b' * 32, 'day': '2026-09-26', 'status': 'failed',
        'acceptance': 'rejected', 'acceptance_uncertain': False,
        'payload': {'label': 'cron:sotto-morning-brief'}}]}))
    assert onboarding.status(tmp_path, 200000) == 'existing'
    assert onboarding.tick(tmp_path, True, lambda: 1/0, 200000) == 'existing'
    assert json.loads(state.read_text()) == {'phase': 'existing', 'completed_at': 100}


def test_ambiguous_expired_claim_does_not_repair_existing_phase(tmp_path):
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    marker.write_text('a' * 32)
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    outbox.write_text(json.dumps({'rows': [{
        'id': 'a' * 32, 'day': '2026-09-26', 'status': 'expired',
        'acceptance': 'unknown', 'acceptance_uncertain': True,
        'payload': {'label': 'cron:sotto-morning-brief'}}]}))
    # Unknown means the provider may have accepted a request whose response was
    # lost; the old adoption cannot be disproved by this terminal row.
    assert onboarding.status(tmp_path, 100) == 'waiting'
    started = []
    assert onboarding.tick(tmp_path, True, lambda: started.append(True), 100) == 'waiting'
    assert started == []
    state = tmp_path / 'config/onboarding.json'
    assert json.loads(state.read_text())['scheduled_ambiguous'] is True
    outbox.unlink()  # the terminal row eventually ages out before a provider receipt arrives
    assert onboarding.status(tmp_path, 101) == 'waiting'
    assert onboarding.tick(tmp_path, True, lambda: started.append(True), 101) == 'waiting'
    assert started == []
    state.write_text(json.dumps({'phase': 'existing', 'completed_at': 90}))
    assert onboarding.status(tmp_path, 100) == 'existing'
    assert onboarding.tick(tmp_path, lambda: 1/0, lambda: 1/0, 100) == 'existing'


def test_pending_scheduled_claim_holds_fresh_welcome(tmp_path):
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    marker.write_text('a' * 32)
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    outbox.write_text(json.dumps({'rows': [{
        'id': 'a' * 32, 'day': '2026-09-26', 'status': 'pending',
        'acceptance': 'in_flight', 'acceptance_uncertain': False,
        'payload': {'label': 'cron:sotto-morning-brief'}}]}))
    started = []
    assert onboarding.status(tmp_path, 100) == 'queued'
    assert onboarding.tick(tmp_path, True, lambda: started.append(True), 100) == 'queued'
    assert started == []
    assert not (tmp_path / 'config/onboarding.json').exists()


def test_held_first_gallery_counts_as_ambiguous_not_queued(tmp_path):
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    marker.write_text('a' * 32)
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    gallery = {'id': 'a' * 32, 'day': '2026-09-26', 'status': 'pending',
               'payload': {'label': 'brief:sotto-morning-brief', 'presentation': {'images': ['/x.png']}}}
    started = []
    # Unknown after a reply, and in_flight after a crash mid-dispatch: both stay pending forever.
    for acceptance, uncertain in (('unknown', True), ('in_flight', False)):
        outbox.write_text(json.dumps({'rows': [{**gallery, 'acceptance': acceptance,
                                                'acceptance_uncertain': uncertain}]}))
        assert onboarding.status(tmp_path, 100) == 'waiting'
        assert onboarding.scheduled_inflight(tmp_path)  # still never a duplicate welcome
    assert onboarding.tick(tmp_path, True, lambda: started.append(True), 100) == 'waiting'
    assert started == []
    assert json.loads((tmp_path / 'config/onboarding.json').read_text())['scheduled_ambiguous'] is True
    # A genuinely queued, never-attempted brief beside it still counts as queued.
    queued = {'id': 'b' * 32, 'day': '2026-09-27', 'status': 'pending', 'acceptance': 'not_attempted',
              'acceptance_uncertain': False, 'payload': {'label': 'cron:sotto-morning-brief'}}
    rows = json.loads(outbox.read_text())['rows'] + [queued]
    outbox.write_text(json.dumps({'rows': rows}))
    assert onboarding._unresolved_scheduled_brief(tmp_path) == 'queued'


def test_unresolved_first_gallery_does_not_block_later_scheduled_briefs(tmp_path, monkeypatch):
    from datetime import datetime
    import receiver as rec
    from test_fresh_selfhost_readiness import _fresh_selfhost
    _fresh_selfhost(tmp_path, monkeypatch, datetime(2026, 9, 28, 6, 40))
    (tmp_path / 'google_token.json').write_text('{}')
    monkeypatch.setattr(rec, '_channel_status', lambda channel=None: 'linked')
    box = rec.OUTBOX
    for key in ('record', 'on_invalid'):
        monkeypatch.setitem(box.HOOKS, key, lambda *a, **k: None)
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'gallery_receipt', lambda key: {'acceptance': 'unknown'})
    monkeypatch.setitem(box.HOOKS, 'brief_gate', lambda *a: box.GATE_SEND)
    sends = []
    monkeypatch.setitem(box.HOOKS, 'send_gallery', lambda *a: sends.append(a) or
                        (False, 'gallery acceptance unconfirmed', {'acceptance': 'unknown'}))
    run_id = 'a' * 32
    (tmp_path / 'briefs').mkdir(exist_ok=True)
    (tmp_path / 'briefs/2026-09-28.morning.delivered').write_text(run_id)
    box.deliver({'label': 'brief:sotto-morning-brief', 'body': 'Brief', 'target': 'photon',
                 'run_id': run_id, 'day': '2026-09-28',
                 'presentation': {'summary': 'Brief', 'images': ['/x.png']}})
    # A week later the operation receipt is still unknown, so the outbox keeps holding the row.
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 10, 5, 6, 30))
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
        doc['rows'][0]['payload']['valid_until'] = 0
    box.drain()
    row = json.loads((tmp_path / 'events/outbox.json').read_text())['rows'][0]
    assert row['status'] == 'pending' and len(sends) == 1  # the gallery itself is never resent
    assert onboarding.status(str(tmp_path)) == 'waiting'
    assert not rec._welcome_admission_active()
    admitted = []
    monkeypatch.setattr(rec, '_managed_brief', lambda skill, label, **kw:
                        admitted.append((skill, label)) or True)
    rec._cron_tick()
    assert ('sotto-morning-brief', 'cron:sotto-morning-brief') in admitted
    assert all(label != onboarding.LABEL for _, label in admitted)


def test_legacy_delivery_receipt_must_match_marker_claim(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import os
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    marker = tmp_path / 'briefs/2026-09-26.evening.delivered'
    marker.parent.mkdir()
    marker.write_text('a' * 32)
    claimed_at = datetime(2026, 9, 26, 18, tzinfo=timezone.utc).timestamp()
    os.utime(marker, (claimed_at, claimed_at))
    events = tmp_path / 'events'
    events.mkdir()
    stamp = datetime.fromtimestamp(marker.stat().st_mtime + 1, timezone.utc).isoformat()
    receipt = {'ts': stamp, 'label': 'cron:sotto-evening-brief',
               'status': 'delivered', 'run_id': 'b' * 32}
    path = events / 'delivery.jsonl'
    path.write_text(json.dumps(receipt) + '\n')
    assert onboarding.status(tmp_path) == 'waiting'
    receipt['run_id'] = 'a' * 32
    path.write_text(json.dumps(receipt) + '\n')
    assert onboarding.tick(tmp_path, lambda: 1/0, lambda: 1/0) == 'existing'


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


def test_bridge_wake_folds_context_while_first_brief_is_composing_or_queued(tmp_path, monkeypatch):
    import receiver as rec
    import managed
    import time
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(managed, 'brief_hold', lambda _: None)
    monkeypatch.setattr(rec, '_in_brief_cron_window', lambda _: False)
    folded = []
    spawned = []
    monkeypatch.setattr(rec, '_fold_into_snapshot', lambda *args: (folded.append(args) or
                                                                    (200, {'status': 'onboarding'})))
    monkeypatch.setattr(rec, 'run_skill', lambda *args: spawned.append(args))
    now = time.time()
    assert onboarding.tick(tmp_path, True, lambda: True, now) == 'started'
    wake = {'type': 'morning_ready', 'date': '2026-09-26', 'local_data': {'messages': []}}
    assert rec.handle_trigger(wake) == (200, {'status': 'onboarding'})
    assert len(folded) == 1 and not spawned
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    outbox.write_text(json.dumps({'rows': [{'status': 'pending', 'payload': {'label': onboarding.LABEL}}]}))
    state = tmp_path / 'config/onboarding.json'
    saved = json.loads(state.read_text())
    saved['lease_until'] = 0
    state.write_text(json.dumps(saved))
    assert rec.handle_trigger(wake) == (200, {'status': 'onboarding'})
    assert len(folded) == 2 and not spawned


def test_wake_admission_wins_before_welcome_without_second_brief(tmp_path, monkeypatch):
    import receiver as rec
    import managed
    import threading
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(managed, 'enabled', lambda: False)
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    monkeypatch.setattr(rec, '_delivery_channel_ready', lambda _: True)
    monkeypatch.setattr(rec, '_in_brief_cron_window', lambda _: False)
    rec._source_context()  # load the real helper before its lookup is stubbed
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *args: None)
    monkeypatch.setattr(rec, '_CONTEXT_LAST_STARTED', 0)
    _fresh_mac_read(tmp_path, monkeypatch)
    welcomes = []
    monkeypatch.setattr(rec, '_managed_brief', lambda *args: welcomes.append(args))
    admitted = threading.Event()
    release = threading.Event()
    def enqueue_wake(*args):
        rec.WORK_QUEUE.enqueue(str(tmp_path), 'run', {'label': 'brief:sotto-morning-brief'},
                               key='competing-wake')
        admitted.set()
        assert release.wait(5)
    monkeypatch.setattr(rec, 'run_skill', enqueue_wake)
    wake_result = []
    wake = threading.Thread(target=lambda: wake_result.append(rec.handle_trigger(
        {'type': 'morning_ready', 'date': '2026-09-26'})))
    wake.start()
    assert admitted.wait(5)
    heartbeat = threading.Thread(target=rec._background_context_tick)
    heartbeat.start()
    release.set()
    wake.join(5)
    heartbeat.join(5)
    assert not wake.is_alive() and not heartbeat.is_alive()
    assert wake_result == [(202, {'status': 'enqueued', 'skill': 'sotto-morning-brief'})]
    assert not welcomes and not (tmp_path / 'config/onboarding.json').exists()


def test_pending_scheduled_outbox_without_marker_holds_welcome(tmp_path):
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    outbox.write_text(json.dumps({'rows': [{'status': 'pending', 'payload': {
        'label': 'brief:sotto-morning-brief'}}]}))
    started = []
    assert onboarding.tick(tmp_path, True, lambda: started.append(True)) == 'queued'
    assert not started
    assert not (tmp_path / 'config/onboarding.json').exists()


def test_cron_and_welcome_admissions_choose_one_first_brief(tmp_path, monkeypatch):
    import receiver as rec
    import managed
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(managed, 'brief_hold', lambda _: None)
    monkeypatch.setattr(managed, 'enabled', lambda: False)
    monkeypatch.setattr(rec.RELAY, 'bridge_connected', lambda: True)
    monkeypatch.setattr(rec, '_delivery_channel_ready', lambda _: True)
    rec._source_context()  # load the real helper before its lookup is stubbed
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *args: None)
    monkeypatch.setattr(rec, '_brief_revision', lambda kind: 'fixed')
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *args: [
        ('sotto-morning-brief', '30 6 * * *', '', 'sotto-morning-brief')])
    _fresh_mac_read(tmp_path, monkeypatch)
    admitted = []
    def enqueue(skill, label, **kwargs):
        admitted.append(label)
        rec.WORK_QUEUE.enqueue(str(tmp_path), 'run', {'label': label}, key=label)
        return True
    monkeypatch.setattr(rec, '_managed_brief', enqueue)
    assert rec._fire_cron_job('sotto-morning-brief', 'cron:sotto-morning-brief')['ok']
    rec._background_context_tick()
    assert admitted == ['cron:sotto-morning-brief']
    assert not (tmp_path / 'config/onboarding.json').exists()
    # On a separate installation, welcome wins the same admission lock first.
    other = tmp_path / 'other'
    monkeypatch.setattr(rec, 'DATA', str(other))
    _fresh_mac_read(other, monkeypatch)
    rec._background_context_tick()
    assert admitted[-1] == onboarding.LABEL
    assert rec._fire_cron_job('sotto-morning-brief', 'cron:sotto-morning-brief')['error'] == 'onboarding'
    assert admitted.count('cron:sotto-morning-brief') == 1


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
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(home_path=lambda _: tmp_path / 'absent'))
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
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(home_path=lambda _: tmp_path / 'absent'))
    monkeypatch.setattr(rec.CONNECTORS, 'service_status', lambda: [{'service': 'granola', 'connected': True}])
    monkeypatch.setattr(rec, '_connector_error', lambda _: 'authorization expired')
    monkeypatch.setattr(rec, '_managed_brief', lambda *args: 1 / 0)
    rec._background_context_tick()
    assert not (tmp_path / 'config/onboarding.json').exists()


def _accepted_morning(root):
    (root / 'config').mkdir(exist_ok=True)
    (root / 'events').mkdir(exist_ok=True)
    (root / 'briefs').mkdir(exist_ok=True)
    (root / 'briefs/2026-09-27.morning.delivered').write_text('a' * 32)
    (root / 'events/outbox.json').write_text(json.dumps({'rows': [{
        'id': 'a' * 32, 'day': '2026-09-27', 'status': 'delivered', 'acceptance': 'accepted',
        'payload': {'label': 'cron:sotto-morning-brief'},
        'receipt': {'accepted_at': 1, 'message_id': 'm1'}}]}))


def test_failed_welcome_phase_is_adopted_once_a_scheduled_brief_was_accepted(tmp_path):
    _accepted_morning(tmp_path)
    for phase in ('composing', 'waiting', 'queued'):
        (tmp_path / 'config/onboarding.json').write_text(json.dumps({
            'phase': phase, 'attempts': 3, 'attempt_day': 1, 'lease_until': 1, 'retry_at': 1}))
        assert onboarding.status(str(tmp_path)) == 'existing'
        started = []
        assert onboarding.tick(str(tmp_path), True, lambda: started.append(1) or True) == 'existing'
        assert started == []  # no second "first look" for someone already receiving briefs
        assert json.loads((tmp_path / 'config/onboarding.json').read_text())['phase'] == 'existing'


def test_welcome_with_uncertain_acceptance_is_held_not_recomposed(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'events').mkdir()
    day0 = 20_000 * 86400
    (tmp_path / 'config/onboarding.json').write_text(json.dumps({
        'phase': 'composing', 'attempts': 1, 'attempt_day': 20_000,
        'lease_until': day0 + 1200, 'retry_at': day0 + 1800}))
    (tmp_path / 'events/outbox.json').write_text(json.dumps({'rows': [{
        'id': 'w' * 32, 'day': '2024-10-04', 'status': 'failed', 'acceptance': 'unknown',
        'acceptance_uncertain': True, 'payload': {'label': onboarding.LABEL}}]}))
    started = []
    assert onboarding.tick(str(tmp_path), True, lambda: started.append(1) or True,
                           now=day0 + 86400 + 60) == 'waiting'
    assert started == []
    assert onboarding.status(str(tmp_path), now=day0 + 86400 + 60) == 'waiting'
    # The outbox row is pruned after a week; the persisted flag still refuses a replay.
    (tmp_path / 'events/outbox.json').write_text(json.dumps({'rows': []}))
    assert onboarding.tick(str(tmp_path), True, lambda: started.append(1) or True,
                           now=day0 + 9 * 86400) == 'waiting'
    assert started == []


def test_welcome_rejected_before_send_still_retries(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'events').mkdir()
    (tmp_path / 'events/outbox.json').write_text(json.dumps({'rows': [{
        'id': 'w' * 32, 'day': '2024-10-04', 'status': 'failed', 'acceptance': 'not_attempted',
        'payload': {'label': onboarding.LABEL}}]}))
    started = []
    assert onboarding.tick(str(tmp_path), True, lambda: started.append(1) or True) == 'started'
    assert started == [1]


def _saved_discovery(root, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import sys
    pack = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
    sys.path.insert(0, str(pack / '_shared/knowledge'))
    import knowledge as kg
    monkeypatch.setenv('SOTTO_DATA', str(root))
    today = datetime.now(timezone.utc).date()
    people = root / 'knowledge/people'
    people.mkdir(parents=True)
    for cid, name in [('a', 'Alex'), ('z', 'Zara')]:
        fact = kg.FactMeta(text=f'{name} is developing a community garden', type='context',
                           source='observed_message', source_ref='gmail:invented-1',
                           evidence_refs=['gmail:invented-1', 'imessage:invented-2'],
                           first=today.isoformat(), last=today.isoformat())
        kg.write_person_file(str(people / f'{cid}.md'), kg.PersonFile(
            canonical_id=cid, name=name, facts={'f_1': fact}))
    days = [(today - timedelta(days=n)).isoformat() for n in (0, 1, 8, 9, 16, 17)]
    history = {'z': {'name': 'Zara', 'channels': {'gmail': today.isoformat(), 'imessage': today.isoformat()},
                     'importance_evidence': {'active_days': days, 'sent_days': days[:3],
                                             'received_days': days[3:]}}}
    (root / 'knowledge/relationship_state.json').write_text(json.dumps({'history': history}))
    checkpoints = {source: {'initial_complete': True, 'complete': True,
                            'last_success': today.isoformat(),
                            'since': 1, 'until': 2, 'initial_since': 1, 'initial_until': 2,
                            'pages': 1, 'window_page_base': 0,
                            'observation_totals': {'pages': 1, 'rows': 10, 'eligible': 10,
                                                   'selected': 10, 'skipped': 0, 'omitted': 0,
                                                   'truncated': 0, 'rejected_people': 0},
                            'reviewed': 10}
                   for source in ('gmail', 'imessage')}
    (root / 'knowledge/history-state.json').write_text(json.dumps({'sources': checkpoints}))
    return pack, checkpoints


def test_discovery_selects_grounded_reciprocal_person_and_holds_incomplete_or_revoked(tmp_path, monkeypatch):
    pack, checkpoints = _saved_discovery(tmp_path, monkeypatch)
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert value['observations'][0]['person'] == 'Zara'  # existing reciprocal classifier outranks filename order
    assert value['observations'][0]['importance']['tier'] == 'vip'
    assert value['sources'] == ['gmail', 'imessage', 'whatsapp']
    assert value['observations'][0]['fact_sources'] == ['gmail', 'imessage']
    assert value['observations'][0]['source_refs'] == ['gmail:invented-1', 'imessage:invented-2']
    assert onboarding.discovery_context(tmp_path, pack, lambda s: s != 'imessage') is None
    for hold in ({'initial_complete': False}, {'error_code': 'bridge_offline'},
                 {'excluded_pages': {'one': {}}}, {'excluded_pages': ['legacy-page']},
                 {'excluded_pages': 'unrecognized'}, {'rejected_request_revision': 'unchanged'}):
        modified = {source: {**row} for source, row in checkpoints.items()}
        modified['imessage'].update(hold)
        (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': modified}))
        assert onboarding.discovery_context(tmp_path, pack, lambda _: True) is None


def test_discovery_uses_completed_source_during_partial_history_and_never_repeats(tmp_path, monkeypatch):
    import sys
    pack, checkpoints = _saved_discovery(tmp_path, monkeypatch)
    sys.path.insert(0, str(pack / '_shared/knowledge'))
    import knowledge as kg

    people = tmp_path / 'knowledge/people'
    zara_path = people / 'z.md'
    zara = kg.parse_person_file(zara_path.read_text())
    zara.facts['f_1'].source_ref = 'gmail:invented-1'
    zara.facts['f_1'].evidence_refs = ['gmail:invented-1']
    kg.write_person_file(str(zara_path), zara)
    checkpoints['imessage'].update(initial_complete=False, complete=False,
                                   last_attempt={'status': 'held'})
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': checkpoints}))

    discovery = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert discovery['observations'][0]['person'] == 'Zara'
    assert discovery['sources'] == ['gmail', 'imessage', 'whatsapp']
    assert discovery['observations'][0]['fact_sources'] == ['gmail']
    assert discovery['coverage']['gmail']['status'] == 'reviewed'
    assert discovery['coverage']['imessage']['status'] == 'held'
    assert 'action' not in discovery and 'task' not in discovery

    onboarding.delivered(tmp_path, 100)
    started = []
    assert onboarding.discovery_tick(tmp_path, lambda: bool(discovery),
                                     lambda: started.append('first') or True, 101) == 'started'
    onboarding.discovery_delivered(tmp_path, 102)

    # Later completion may improve the available history, but the one-time handoff stays settled.
    checkpoints['imessage'].update(initial_complete=True, complete=True,
                                   last_success='2026-10-24T00:00:00+00:00', last_attempt={'status': 'succeeded'})
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': checkpoints}))
    assert onboarding.discovery_context(tmp_path, pack, lambda _: True) is not None
    assert onboarding.discovery_tick(tmp_path, lambda: True,
                                     lambda: started.append('duplicate') or True, 103) == 'delivered'
    assert started == ['first']


def test_discovery_uses_frozen_initial_coverage_across_later_failure_not_legacy_error(tmp_path, monkeypatch):
    import sys
    pack, checkpoints = _saved_discovery(tmp_path, monkeypatch)
    sys.path.insert(0, str(pack / '_shared/knowledge'))
    import knowledge as kg

    zara_path = tmp_path / 'knowledge/people/z.md'
    zara = kg.parse_person_file(zara_path.read_text())
    zara.facts['f_1'].source_ref = 'gmail:invented-1'
    zara.facts['f_1'].evidence_refs = ['gmail:invented-1']
    kg.write_person_file(str(zara_path), zara)
    legacy = checkpoints['imessage']
    legacy.update(error_code='bridge_timeout', last_attempt={'status': 'failed'})
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': checkpoints}))
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert value is not None
    assert value['coverage']['imessage']['status'] == 'partial'

    # A retained initial snapshot is authoritative even if a later rolling window fails.
    legacy['initial_coverage'] = {'since': 1, 'until': 2, 'complete': True,
                                  'initial_complete': True, 'reviewed': 10,
                                  'pages': 1, 'window_page_base': 0,
                                  'observation_totals': {'pages': 1, 'rows': 10, 'eligible': 10,
                                                         'selected': 10, 'skipped': 0, 'omitted': 0,
                                                         'truncated': 0, 'rejected_people': 0},
                                  'excluded_page_count': 0}
    legacy.update(initial_complete=True, last_success='2026-10-24T00:00:00+00:00')
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': checkpoints}))
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert value is not None
    assert value['coverage']['imessage']['status'] == 'reviewed'


def test_discovery_coverage_uses_initial_window_gaps_without_withholding_grounded_fact(tmp_path, monkeypatch):
    import sys
    pack, checkpoints = _saved_discovery(tmp_path, monkeypatch)
    sys.path.insert(0, str(pack / '_shared/knowledge'))
    import knowledge as kg
    from history_coverage import window_coverage

    zara_path = tmp_path / 'knowledge/people/z.md'
    zara = kg.parse_person_file(zara_path.read_text())
    zara.facts['f_1'].source_ref = 'imessage:invented-2'
    zara.facts['f_1'].evidence_refs = ['imessage:invented-2']
    kg.write_person_file(str(zara_path), zara)

    totals = checkpoints['imessage']['observation_totals']
    for gap in ('skipped', 'omitted', 'truncated', 'rejected_people'):
        modified = {source: {**row} for source, row in checkpoints.items()}
        changed_totals = {**totals, gap: 1}
        modified['imessage']['observation_totals'] = changed_totals
        (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': modified}))

        value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
        projected = window_coverage(modified['imessage'])['initial_review']
        assert value is not None
        assert value['observations'][0]['person'] == 'Zara'
        assert value['observations'][0]['fact_sources'] == ['imessage']
        assert value['coverage']['imessage']['review_scope'] == projected['review_scope'] == 'partial'
        assert value['coverage']['imessage']['observation_counts'][gap] == 1
        assert value['coverage']['imessage']['status'] == 'partial'

    # Legacy checkpoints may establish a grounded completed scan while lacking measured
    # extraction totals. Keep the fact eligible, but do not turn missing measurements into
    # a reviewed-window claim.
    modified = {source: {**row} for source, row in checkpoints.items()}
    for field in ('since', 'until', 'pages', 'window_page_base', 'observation_totals'):
        modified['imessage'].pop(field, None)
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': modified}))
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert value is not None
    assert value['observations'][0]['person'] == 'Zara'
    assert value['observations'][0]['fact_sources'] == ['imessage']
    assert value['coverage']['imessage']['status'] == 'unknown'
    assert value['coverage']['imessage']['review_scope'] == 'unknown'


def test_discovery_does_not_claim_reviewed_without_success_and_keeps_legacy_exclusion_partial(
        tmp_path, monkeypatch):
    import sys
    pack, checkpoints = _saved_discovery(tmp_path, monkeypatch)
    sys.path.insert(0, str(pack / '_shared/knowledge'))
    import knowledge as kg

    zara_path = tmp_path / 'knowledge/people/z.md'
    zara = kg.parse_person_file(zara_path.read_text())
    zara.facts['f_1'].source_ref = 'gmail:invented-1'
    zara.facts['f_1'].evidence_refs = ['gmail:invented-1']
    kg.write_person_file(str(zara_path), zara)

    modified = {source: {**row} for source, row in checkpoints.items()}
    modified['imessage'].pop('last_success')
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': modified}))
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert value is not None
    assert value['observations'][0]['fact_sources'] == ['gmail']
    assert value['coverage']['imessage']['status'] == 'unknown'
    assert value['coverage']['imessage']['review_scope'] == 'unknown'

    modified['imessage'] = {**checkpoints['imessage'], 'excluded_pages': {'legacy': {}}}
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': modified}))
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert value is not None
    assert value['observations'][0]['fact_sources'] == ['gmail']
    assert value['coverage']['imessage']['status'] == 'partial'
    assert value['coverage']['imessage']['review_scope'] == 'partial'


def test_discovery_initial_failure_stays_partial_before_traversal_finishes(tmp_path, monkeypatch):
    pack, checkpoints = _saved_discovery(tmp_path, monkeypatch)
    import knowledge as kg
    path = tmp_path / 'knowledge/people/z.md'
    person = kg.parse_person_file(path.read_text())
    person.facts['f_1'].source_ref = 'gmail:invented-1'
    person.facts['f_1'].evidence_refs = ['gmail:invented-1']
    kg.write_person_file(str(path), person)
    for measured in (True, False):
        row = {**checkpoints['imessage'], 'complete': False, 'initial_complete': False,
               'error_code': 'bridge_timeout', 'last_attempt': {'status': 'failed'}}
        if not measured:
            row.pop('observation_totals')
        modified = {**checkpoints, 'imessage': row}
        (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': modified}))
        value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
        assert value is not None
        assert value['coverage']['imessage']['status'] == 'partial'
        assert value['coverage']['imessage']['review_scope'] == 'partial'


def test_history_discovery_prompt_discloses_partial_coverage_without_old_work_offer(monkeypatch):
    from pathlib import Path
    import sys
    pack = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
    sys.path.insert(0, str(pack / '_shared/scripts'))
    import compose_brief

    note = compose_brief._first_run_note({'type': 'welcome', 'history_discovery': {
        'observations': [{'person': 'Zara', 'fact': 'Developing a community garden',
                          'source_refs': ['gmail:invented-1']}], 'sources': ['gmail'],
        'coverage': {'gmail': {'status': 'reviewed'}, 'imessage': {'status': 'held'}}}}, {}, {}, [], [])
    assert 'partial, bounded initial history review' in note
    assert 'which permitted sources have completed their initial window' in note
    assert 'Do not suggest work or a draft based only on this historical fact' in note


def test_discovery_restart_pending_unknown_and_accepted_never_repeat(tmp_path):
    started = []
    fire = lambda: started.append(True)
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, 100) == 'waiting_for_first_look'
    assert not (tmp_path / 'config/onboarding.json').exists()
    onboarding.delivered(tmp_path, 101)
    assert onboarding.discovery_tick(tmp_path, lambda: False, fire, 102) == 'waiting_for_history'
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, 103) == 'started'
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, 104) == 'composing'
    event = tmp_path / 'events/outbox.json'
    event.parent.mkdir()
    event.write_text(json.dumps({'rows': [{'status': 'pending', 'acceptance': 'not_attempted',
        'payload': {'label': onboarding.DISCOVERY_LABEL}}]}))
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, 4000) == 'queued'
    onboarding.discovery_delivered(tmp_path, 4100)
    event.unlink()  # receipts can age out; durable terminal state still wins
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, 86400) == 'delivered'
    assert len(started) == 1


def test_discovery_unknown_acceptance_remains_held_after_outbox_pruning(tmp_path):
    onboarding.delivered(tmp_path, 100)
    event = tmp_path / 'events/outbox.json'
    event.parent.mkdir()
    event.write_text(json.dumps({'rows': [{'status': 'failed', 'acceptance': 'unknown',
        'payload': {'label': onboarding.DISCOVERY_LABEL}}]}))
    fire = lambda: (_ for _ in ()).throw(AssertionError('must not replay'))
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, 101) == 'ambiguous'
    event.unlink()
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, 186400) == 'ambiguous'


def test_discovery_key_and_marker_are_distinct_from_welcome(tmp_path, monkeypatch):
    import receiver as rec
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_composed_brief_recently', lambda *_: True)
    assert rec._job_key(onboarding.DISCOVERY_LABEL, '') == onboarding.DISCOVERY_KEY + ':' + str(rec._local_now().date())
    assert rec._job_key(onboarding.LABEL, '') != onboarding.DISCOVERY_KEY
    assert rec._brief_delivery_gate(onboarding.LABEL, '2026-10-10', 'a' * 32) == rec.OUTBOX.GATE_SEND
    assert rec._brief_delivery_gate(onboarding.DISCOVERY_LABEL, '2026-10-10', 'b' * 32) == rec.OUTBOX.GATE_SEND
    assert rec._brief_delivery_gate(onboarding.DISCOVERY_LABEL, '2026-10-10', 'c' * 32) == rec.OUTBOX.GATE_SUPERSEDED
    # A new day's marker alone is not enough: the earlier claim may have landed.
    assert rec._brief_delivery_gate(onboarding.DISCOVERY_LABEL, '2026-10-11', 'c' * 32) == rec.OUTBOX.GATE_SUPERSEDED
    (tmp_path / 'events').mkdir(exist_ok=True)
    (tmp_path / 'events/outbox.json').write_text(json.dumps({'rows': [{'id': 'b' * 32,
        'status': 'failed', 'acceptance': 'not_attempted', 'payload': {'label': onboarding.DISCOVERY_LABEL}}]}))
    assert rec._brief_delivery_gate(onboarding.DISCOVERY_LABEL, '2026-10-11', 'c' * 32) == rec.OUTBOX.GATE_SEND


def test_discovery_uses_existing_quiet_snooze_and_budget_policy_only_for_unsolicited_handoff(monkeypatch):
    import receiver as rec
    from types import SimpleNamespace
    state = {'quiet': True, 'snooze': False, 'budget': 8}
    policy = SimpleNamespace(_in_quiet_hours=lambda _: state['quiet'], _prefs=SimpleNamespace(
        load_explicit=lambda: {}, snooze_active=lambda **_: state['snooze'],
        effective_nudge_budget=lambda _: state['budget']),
        _local_day=lambda now: str(now.date()), _budget_left=lambda _day: state['budget'],
        _budget_try_spend=lambda _day, n, reservation='': state['budget'] >= n)
    monkeypatch.setattr(rec, '_discovery_interruption_policy', lambda: policy)
    payload = {'label': onboarding.DISCOVERY_LABEL}
    assert rec._history_discovery_delivery_ready(payload) is False
    assert rec._history_discovery_delivery_ready({'label': onboarding.LABEL}) is True
    state['quiet'] = False
    assert rec._history_discovery_delivery_ready(payload) is True
    state['snooze'] = True
    assert rec._history_discovery_delivery_ready(payload) is False
    state.update(snooze=False, budget=0)
    assert rec._history_discovery_delivery_ready(payload) is False


def test_late_accepted_discovery_settles_previous_ambiguity(tmp_path):
    onboarding.delivered(tmp_path, 100)
    event = tmp_path / 'events/outbox.json'
    event.parent.mkdir()
    row = {'status': 'failed', 'acceptance': 'unknown',
           'payload': {'label': onboarding.DISCOVERY_LABEL}}
    event.write_text(json.dumps({'rows': [row]}))
    assert onboarding.discovery_tick(tmp_path, lambda: True, lambda: None, 101) == 'ambiguous'
    row.update(status='delivered', acceptance='accepted',
               receipt={'accepted_at': 102, 'message_id': 'invented-accepted'})
    event.write_text(json.dumps({'rows': [row]}))
    assert onboarding.discovery_tick(tmp_path, lambda: True, lambda: None, 102) == 'delivered'


def test_discovery_definitive_expiry_gets_next_day_identity_but_acceptance_stops_retries(tmp_path, monkeypatch):
    import receiver as rec
    from datetime import datetime, timezone
    from pathlib import Path
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    clock = [datetime(2026, 10, 10, 20, tzinfo=timezone.utc)]
    monkeypatch.setattr(rec, '_local_now', lambda: clock[0])
    monkeypatch.setattr(rec, '_brief_revision', lambda _: 'fixture')
    pack = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: str(pack.joinpath(*parts)))
    attempts = []
    monkeypatch.setattr(rec, '_spawn_and_deliver', lambda _runner, prompt, *_args, **_kwargs:
                        attempts.append(json.loads(prompt)['work_key']))
    onboarding.delivered(tmp_path, clock[0].timestamp())
    fire = lambda: rec._managed_brief('sotto-welcome-brief', onboarding.DISCOVERY_LABEL, retry_terminal=True)
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, clock[0].timestamp()) == 'started'
    event = tmp_path / 'events/outbox.json'
    event.parent.mkdir(exist_ok=True)
    event.write_text(json.dumps({'rows': [{'status': 'expired', 'acceptance': 'not_attempted',
        'payload': {'label': onboarding.DISCOVERY_LABEL}}]}))
    clock[0] = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, clock[0].timestamp()) == 'started'
    assert attempts == [onboarding.DISCOVERY_KEY + ':2026-10-10', onboarding.DISCOVERY_KEY + ':2026-10-11']
    onboarding.discovery_delivered(tmp_path, clock[0].timestamp())
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, clock[0].timestamp() + 86400) == 'delivered'
    assert len(attempts) == 2


def test_discovery_worker_requires_its_own_archive_before_outbox(tmp_path, monkeypatch):
    import receiver as rec
    from types import SimpleNamespace
    from pathlib import Path
    import sys
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec, '_composition_evidence', lambda *_: {'source_revision': 'a' * 64, 'route_digest': 'b' * 64})
    monkeypatch.setattr(rec, '_record_delivery', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rec, '_spawn_env', lambda *_: {})
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(run_argv=lambda *_: ['fixture']))
    process = SimpleNamespace(returncode=0, communicate=lambda **_: ('Grounded discovery', ''), poll=lambda: 0)
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *_args, **_kwargs: process)
    seen = []
    monkeypatch.setattr(rec, '_composed_brief_recently', lambda kind, _day: seen.append(kind) or False)
    request = {'runner': [sys.executable, str(Path(rec.__file__).with_name('brief_runner.py'))],
               'prompt': '{}', 'label': onboarding.DISCOVERY_LABEL}
    import pytest
    with pytest.raises(RuntimeError, match='no archived composition'):
        rec._execute_work({'id': 'e' * 32, 'kind': 'run', 'payload': request})
    assert seen == ['history-discovery']


def test_discovery_holds_retained_checkpoint_and_same_day_fact_after_regrant(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    pack, checkpoints = _saved_discovery(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    config = tmp_path / 'config/source-state.json'
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({'sources': {source: {'status': 'ok', 'consent_regrant_epoch': now.timestamp()}
        for source in ('gmail', 'imessage')}}))
    assert onboarding.discovery_context(tmp_path, pack, lambda _: True) is None
    # Even a fresh successful scan cannot upgrade a retained date-only fact from regrant day.
    for row in checkpoints.values():
        row.update(initial_until=now.timestamp() + 2, last_success=(now + timedelta(seconds=3)).isoformat())
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': checkpoints}))
    assert onboarding.discovery_context(tmp_path, pack, lambda _: True) is None
    # With a provably later fact, old reciprocal dates still cannot establish VIP importance.
    import knowledge as kg
    path = tmp_path / 'knowledge/people/z.md'
    person = kg.parse_person_file(path.read_text())
    person.facts['f_1'].last = (now + timedelta(days=1)).date().isoformat()
    kg.write_person_file(str(path), person)
    # A future fact may be filtered by canonical freshness; the important assertion is no old VIP.
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert value is None or value['observations'][0]['importance']['tier'] == 'regular'


def test_discovery_retained_source_generation_is_rechecked_at_delivery(tmp_path, monkeypatch):
    import receiver as rec
    proof = {'source_revision': 'a' * 64, 'route_digest': 'b' * 64}
    monkeypatch.setattr(rec, '_retained_proof_current', lambda current: current == proof)
    payload = {'label': onboarding.DISCOVERY_LABEL, 'effects': [],
               'prep_source_revision': proof['source_revision'], 'prep_route_digest': proof['route_digest']}
    assert rec._delivery_valid(payload) is True
    proof['source_revision'] = 'c' * 64  # disable→regrant restores booleans, changes generation
    assert rec._delivery_valid(payload) is False


def test_discovery_fresh_post_regrant_learning_can_resume_with_immutable_initial_window(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    pack, checkpoints = _saved_discovery(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    restored = now - timedelta(days=1)
    config = tmp_path / 'config/source-state.json'
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({'sources': {source: {'status': 'ok', 'consent_regrant_epoch': restored.timestamp()}
        for source in ('gmail', 'imessage')}}))
    for row in checkpoints.values():
        row.update(last_success=now.isoformat())
        assert row['initial_until'] < restored.timestamp()  # frozen initial coverage is deliberately old
    (tmp_path / 'knowledge/history-state.json').write_text(json.dumps({'sources': checkpoints}))
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert value is not None
    assert value['observations'][0]['observed_date'] == now.date().isoformat()
    assert value['observations'][0]['importance']['tier'] == 'regular'  # pre-regrant reciprocal dates are still excluded
    assert all(coverage['requested_window']['initial_until'] ==
               '1970-01-01T00:00:02+00:00' for source, coverage in value['coverage'].items()
               if source in ('gmail', 'imessage'))
    assert value['coverage']['whatsapp']['status'] == 'unknown'


def test_discovery_rejected_previous_day_claim_cannot_block_next_day_gate(tmp_path, monkeypatch):
    import receiver as rec
    from datetime import datetime, timezone
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_composed_brief_recently', lambda *_: True)
    clock = [datetime(2026, 10, 10, 20, tzinfo=timezone.utc)]
    attempts = []
    def fire():
        ident = ('a' if not attempts else 'b') * 32
        outcome = rec._brief_delivery_gate(onboarding.DISCOVERY_LABEL, str(clock[0].date()), ident)
        attempts.append(outcome)
        return outcome == rec.OUTBOX.GATE_SEND
    onboarding.delivered(tmp_path, clock[0].timestamp())
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, clock[0].timestamp()) == 'started'
    event = tmp_path / 'events/outbox.json'
    event.parent.mkdir(exist_ok=True)
    event.write_text(json.dumps({'rows': [{'id': 'a' * 32, 'status': 'failed', 'acceptance': 'not_attempted',
        'payload': {'label': onboarding.DISCOVERY_LABEL}}]}))
    clock[0] = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, clock[0].timestamp()) == 'started'
    assert attempts == [rec.OUTBOX.GATE_SEND, rec.OUTBOX.GATE_SEND]
    onboarding.discovery_delivered(tmp_path, clock[0].timestamp())
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, clock[0].timestamp() + 86400) == 'delivered'
    assert len(attempts) == 2


@pytest.mark.parametrize('days_until_next_tick', [1, 8])
def test_accepted_discovery_with_failed_callback_never_repeats_after_outbox_pruning(
        tmp_path, monkeypatch, days_until_next_tick):
    """The send seam's permanent claim, not the delivery callback, fences the lifetime discovery."""
    import receiver as rec
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_composed_brief_recently', lambda *_: True)
    onboarding.delivered(tmp_path, 100)
    sends = []
    def fire():
        day = '2026-10-%02d' % (10 + len(sends))
        sends.append(rec._brief_delivery_gate(onboarding.DISCOVERY_LABEL, day, str(len(sends)) * 32))
    assert onboarding.discovery_tick(tmp_path, lambda: True, fire, 1000) == 'started'
    assert sends == [rec.OUTBOX.GATE_SEND]
    event = tmp_path / 'events/outbox.json'
    event.parent.mkdir(exist_ok=True)
    # Provider accepted it, but _on_delivered never ran (discovery_delivered was not called).
    event.write_text(json.dumps({'rows': [{'id': '0' * 32, 'status': 'delivered', 'acceptance': 'accepted',
        'receipt': {'accepted_at': 1001, 'message_id': 'invented-1'},
        'payload': {'label': onboarding.DISCOVERY_LABEL}}]}))
    if days_until_next_tick > 7:
        event.write_text(json.dumps({'rows': []}))  # RETENTION_SECS pruned the closed row
    later = 1000 + days_until_next_tick * 86400
    expected = 'delivered' if days_until_next_tick <= 7 else 'ambiguous'
    for tick in range(3):
        assert onboarding.discovery_tick(tmp_path, lambda: True, fire, later + tick * 86400) == expected
        if tick == 0:
            event.write_text(json.dumps({'rows': []}))  # pruning after the first observation
    assert sends == [rec.OUTBOX.GATE_SEND]
    # Even a stray composition reaching the send seam on a later day is refused.
    assert rec._brief_delivery_gate(onboarding.DISCOVERY_LABEL, '2026-10-30', 'f' * 32) == rec.OUTBOX.GATE_SUPERSEDED


def test_discovery_claim_unrecorded_holds_the_send(tmp_path, monkeypatch):
    import receiver as rec
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_composed_brief_recently', lambda *_: True)
    def broken(*_args):
        raise OSError('volume read-only')
    monkeypatch.setattr(onboarding, 'discovery_claimed', broken)
    assert rec._brief_delivery_gate(onboarding.DISCOVERY_LABEL, '2026-10-10', 'a' * 32) == rec.OUTBOX.GATE_RETRY


def test_discovery_spends_the_shared_daily_interrupt_budget_once_per_run(tmp_path, monkeypatch):
    import receiver as rec
    from datetime import datetime, timezone
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '2')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 10, 10, 13, tzinfo=timezone.utc))
    policy = rec._discovery_interruption_policy()
    budget = tmp_path / 'events/budget.json'
    budget.parent.mkdir(parents=True, exist_ok=True)
    budget.write_text(json.dumps({'date': '2026-10-10', 'count': 2}))  # the day's nudges are spent
    payload = {'label': onboarding.DISCOVERY_LABEL, 'run_id': 'a' * 32}
    assert rec._history_discovery_delivery_ready({'label': onboarding.DISCOVERY_LABEL}) is False
    assert rec._history_discovery_delivery_ready(payload) is False
    budget.write_text(json.dumps({'date': '2026-10-10', 'count': 1}))
    assert rec._history_discovery_delivery_ready({'label': onboarding.DISCOVERY_LABEL}) is True
    assert policy._budget_spent('2026-10-10') == 1  # admission asks, never spends
    assert rec._history_discovery_delivery_ready(payload) is True
    assert rec._history_discovery_delivery_ready(payload) is True  # a retry rides its reservation
    assert policy._budget_spent('2026-10-10') == 2
    assert rec._history_discovery_delivery_ready({**payload, 'run_id': 'b' * 32}) is False


def test_reserved_discovery_respects_owner_disabling_nudges(tmp_path, monkeypatch):
    import receiver as rec
    from datetime import datetime, timezone
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '2')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 10, 10, 13, tzinfo=timezone.utc))
    payload = {'label': onboarding.DISCOVERY_LABEL, 'run_id': 'c' * 32}
    assert rec._history_discovery_delivery_ready(payload) is True
    policy = rec._discovery_interruption_policy()
    assert policy._budget_spent('2026-10-10') == 1
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '0')
    assert rec._history_discovery_delivery_ready(payload) is False
    assert policy._budget_spent('2026-10-10') == 1
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '2')
    assert rec._history_discovery_delivery_ready(payload) is True
    assert policy._budget_spent('2026-10-10') == 1


def test_discovery_ranks_fact_support_not_total_coverage_scope(tmp_path, monkeypatch):
    pack, _checkpoints = _saved_discovery(tmp_path, monkeypatch)
    import knowledge as kg
    # Both candidates have equal relationship importance; only Zara has two supporting sources.
    (tmp_path / 'knowledge/relationship_state.json').write_text(json.dumps({'history': {}}))
    path = tmp_path / 'knowledge/people/a.md'
    alex = kg.parse_person_file(path.read_text())
    alex.facts['f_1'].evidence_refs = ['gmail:invented-1']
    kg.write_person_file(str(path), alex)
    result = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert result['observations'][0]['person'] == 'Zara'
    assert result['observations'][0]['fact_sources'] == ['gmail', 'imessage']
    assert result['sources'] == ['gmail', 'imessage', 'whatsapp']



def test_discovery_keeps_lower_ranked_observations_for_current_work_selection(tmp_path, monkeypatch):
    pack, _ = _saved_discovery(tmp_path, monkeypatch)
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    observations = value['observations']
    assert [row['canonical_id'] for row in observations] == ['z', 'a']
    assert observations[0]['importance']['tier'] == 'vip'
    assert observations[1]['person'] == 'Alex'
    for row in observations:
        assert row['observed_date']
        assert row['source_refs'] == ['gmail:invented-1', 'imessage:invented-2']
        assert row['fact_sources'] == ['gmail', 'imessage']
    assert 'fact' not in value


def test_discovery_bounds_observations_without_one_person_crowding_out_others(tmp_path, monkeypatch):
    pack, _ = _saved_discovery(tmp_path, monkeypatch)
    import knowledge as kg
    from dataclasses import replace
    people = tmp_path / 'knowledge/people'
    zara = kg.parse_person_file((people / 'z.md').read_text())
    zara.facts = {f'f_{i}': replace(zara.facts['f_1'], text=f'Zara observation {i}') for i in range(10)}
    kg.write_person_file(str(people / 'z.md'), zara)
    for i in range(8):
        kg.write_person_file(str(people / f'extra{i}.md'), kg.PersonFile(
            canonical_id=f'extra{i}', name=f'Person {i}', facts={'f_1': replace(
                zara.facts['f_0'], text=f'Person {i} observation')}))
    rows = onboarding.discovery_context(tmp_path, pack, lambda _: True)['observations']
    assert len(rows) == 6
    assert len({row['canonical_id'] for row in rows}) == 6
    assert rows[0]['canonical_id'] == 'z'


def test_discovery_same_names_keep_distinct_canonical_identity(tmp_path, monkeypatch):
    pack, _ = _saved_discovery(tmp_path, monkeypatch)
    import knowledge as kg
    path = tmp_path / 'knowledge/people/a.md'
    alex = kg.parse_person_file(path.read_text())
    alex.name = 'Zara'
    kg.write_person_file(str(path), alex)
    rows = onboarding.discovery_context(tmp_path, pack, lambda _: True)['observations']
    assert {row['canonical_id'] for row in rows} == {'a', 'z'}
    assert {row['person'] for row in rows} == {'Zara'}
    assert len({row['fact'] for row in rows}) == 2



def test_discovery_candidates_still_exclude_conflicts_and_ineligible_sources(tmp_path, monkeypatch):
    pack, _ = _saved_discovery(tmp_path, monkeypatch)
    import knowledge as kg
    from dataclasses import replace
    path = tmp_path / 'knowledge/people/z.md'
    zara = kg.parse_person_file(path.read_text())
    zara.facts['f_2'] = replace(zara.facts['f_1'], text='Zara stopped developing the garden')
    zara.facts['f_3'] = replace(zara.facts['f_1'], text='Unreviewed source observation',
                              source_ref='whatsapp:not-reviewed', evidence_refs=['whatsapp:not-reviewed'])
    kg.write_person_file(str(path), zara)
    (tmp_path / 'knowledge/conflicts.json').write_text(json.dumps(
        {'people': {'z': {'pairs': [['f_1', 'f_2']]}}}))
    before = {p.name: p.read_text() for p in path.parent.glob('*.md')}
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert [row['canonical_id'] for row in value['observations']] == ['a']
    assert value['coverage']['whatsapp']['status'] == 'unknown'
    assert {p.name: p.read_text() for p in path.parent.glob('*.md')} == before


@pytest.mark.parametrize('explicit', [
    {'mute_people': ['  zArA  ']},
    {'mute_senders': ['zara@example.test']},
    {'mute_senders': ['@example.test']},
])
def test_discovery_excludes_muted_people_before_candidate_selection(tmp_path, monkeypatch, explicit):
    pack, _ = _saved_discovery(tmp_path, monkeypatch)
    monkeypatch.syspath_prepend(str(pack / '_shared/scripts'))
    import knowledge as kg
    import preferences
    path = tmp_path / 'knowledge/people/z.md'
    person = kg.parse_person_file(path.read_text())
    person.identifiers = ['zara@example.test']
    kg.write_person_file(str(path), person)
    monkeypatch.setattr(preferences, 'load_explicit', lambda: explicit)
    value = onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert [row['canonical_id'] for row in value['observations']] == ['a']
    monkeypatch.setattr(preferences, 'load_explicit', lambda: {'mute_people': ['Alex', 'Zara']})
    assert onboarding.discovery_context(tmp_path, pack, lambda _: True) is None
