import json
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
