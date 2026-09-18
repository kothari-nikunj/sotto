"""Deadline, consent revision, and recovery across the procedure/work/outbox boundary."""
import fcntl
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).parent
PACK = HERE.parents[1] / 'sotto-chief-of-staff'


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_RUN_SKILL', 'hermes -z')
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    spec = importlib.util.spec_from_file_location('scheduled_receiver', HERE / 'receiver.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'DATA', str(tmp_path))
    monkeypatch.setattr(module, '_find_sotto_script', lambda *rel: str(PACK.joinpath(*rel)))
    monkeypatch.setattr(module, '_record_delivery', lambda *a, **k: None)
    return module


def test_worker_preserves_artifact_coverage_and_delivery_deadline_on_replay(rec, tmp_path, monkeypatch):
    cutoff = '2026-09-07T13:20:00Z'
    monkeypatch.setattr(rec, '_read_delivery_effects', lambda run_id: {
        'effects': [{'kind': 'source_permissions', 'sources': ['imessage'], 'coverage_until': cutoff}]})
    identity = rec.WORK_QUEUE.enqueue(str(tmp_path), 'run', {
        'runner': [sys.executable, '-c', 'print("Synthetic prepared output")'],
        'prompt': json.dumps({'deliver_not_before': 2000000000}), 'label': 'test'}, key='artifact-replay')
    job = rec.WORK_QUEUE.claim(str(tmp_path), rec._WORK_OWNER)
    assert job['id'] == identity
    result = rec._execute_work(job)
    assert result['coverage_until'] == cutoff  # retry time must not hide newer incoming messages
    assert result['not_before'] == 2000000000


@pytest.mark.parametrize('mode', ['selfhost', 'managed'])
def test_early_brief_is_one_job_per_calendar_and_consent_revision_in_both_modes(rec, tmp_path, monkeypatch, mode):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    import managed
    import onboarding
    import source_context
    monkeypatch.setattr(managed, 'brief_hold', lambda root: None)
    monkeypatch.setattr(onboarding, 'scheduled_hold', lambda root: False)
    permissions = {'imessage': True}
    monkeypatch.setattr(source_context, 'allowed', lambda source: permissions.get(source, True))
    now = datetime.now(timezone.utc).replace(hour=6, minute=20, second=0, microsecond=0)
    monkeypatch.setattr(rec, '_local_now', lambda: now)
    # The queue and scheduler must share the fixture clock; otherwise prep is already
    # expired when this test runs later in the real day.
    monkeypatch.setattr(rec.WORK_QUEUE.time, 'time', lambda: now.timestamp())
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *a: [
        ('sotto-morning-brief', '30 6 * * *', 'morning', 'sotto-morning-brief')])
    rec._prepare_briefs()
    rec._prepare_briefs()
    assert rec.WORK_QUEUE.status(str(tmp_path))['counts'] == {'pending': 2}
    prep = rec.WORK_QUEUE.claim(str(tmp_path), 'prep-worker', now=now.timestamp())
    assert json.loads(prep['payload']['prompt'])['prepare']
    # Same initial drain cannot let higher-priority composition race and finish before paid prep.
    assert rec.WORK_QUEUE.claim(str(tmp_path), 'normal-worker', now=now.timestamp()) is None
    compose_at = now.replace(minute=28).timestamp()
    assert rec.WORK_QUEUE.renew(str(tmp_path), prep['id'], 'prep-worker', now=compose_at - 30)
    normal = rec.WORK_QUEUE.claim(str(tmp_path), 'normal-worker', now=compose_at)
    normal_request = json.loads(normal['payload']['prompt'])
    prep_request = json.loads(prep['payload']['prompt'])
    assert normal_request['work_key'] == prep_request['work_key']
    assert normal_request['deliver_not_before'] == now.replace(minute=30).timestamp()
    assert normal['due'] == compose_at
    assert not normal_request.get('prepare') and prep_request['prepare']
    permissions['imessage'] = False
    rec._prepare_briefs()
    # A revision change while the composition is already under way admits NO second composition
    # (it used to: a full duplicate gather+compose for the send seam to supersede). The leased one
    # delivers, or the outbox supersedes it as stale and recomposes from there; only the cheap
    # preparation lane re-runs for the new revision.
    assert rec.WORK_QUEUE.status(str(tmp_path))['counts'] == {'leased': 2, 'pending': 1}
    still = rec.WORK_QUEUE.active_job(str(tmp_path), 'run', 'cron:sotto-morning-brief')
    assert still['id'] == normal['id'] and still['status'] == 'leased'


def test_delivered_brief_skips_all_preparation(rec, tmp_path, monkeypatch):
    import managed
    import onboarding
    monkeypatch.setattr(managed, 'brief_hold', lambda root: None)
    monkeypatch.setattr(onboarding, 'scheduled_hold', lambda root: False)
    now = datetime(2026, 9, 7, 6, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(rec, '_local_now', lambda: now)
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *a: [
        ('sotto-morning-brief', '30 6 * * *', '', 'sotto-morning-brief')])
    marker = Path(rec.delivered_marker('2026-09-07', 'morning'))
    marker.parent.mkdir(parents=True)
    marker.write_text('accepted-run')
    monkeypatch.setattr(rec, '_managed_brief', lambda *a, **k: pytest.fail('compose was admitted'))
    rec._prepare_briefs()
    assert rec.WORK_QUEUE.status(str(tmp_path))['counts'] == {}


def test_terminal_prepare_key_isolated_and_not_retried_each_tick(rec, tmp_path, monkeypatch):
    import managed
    import onboarding
    monkeypatch.setattr(managed, 'brief_hold', lambda root: None)
    monkeypatch.setattr(onboarding, 'scheduled_hold', lambda root: False)
    now = datetime(2026, 9, 7, 6, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(rec, '_local_now', lambda: now)
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *a: [
        ('sotto-morning-brief', '30 6 * * *', '', 'sotto-morning-brief'),
        ('sotto-evening-brief', '30 6 * * *', '', 'sotto-evening-brief')])
    monkeypatch.setattr(rec, '_managed_brief', lambda *a, **k: None)
    calls = []

    def enqueue(*args, **kwargs):
        calls.append(kwargs['key'])
        if kwargs['key'].startswith('prepare:sotto-morning-brief:'):
            raise RuntimeError('work request is terminal (failed)')

    monkeypatch.setattr(rec.WORK_QUEUE, 'enqueue', enqueue)
    rec._prepare_briefs()
    rec._prepare_briefs()
    assert calls == [
        'prepare:sotto-morning-brief:2026-09-07:' + rec._brief_revision('morning'),
        'prepare:sotto-evening-brief:2026-09-07:' + rec._brief_revision('evening'),
        'prepare:sotto-evening-brief:2026-09-07:' + rec._brief_revision('evening'),
    ]
    assert rec._CRON_FIRED == {'sotto-morning-brief': '2026-09-07'}


def test_calendar_order_is_not_a_new_brief_revision_but_time_change_is(rec, tmp_path):
    cache = tmp_path / 'cache/calendar_today.json'
    cache.parent.mkdir()
    first = {'id': 'one', 'start': '2026-09-08T10:00:00Z'}
    second = {'id': 'two', 'start': '2026-09-08T11:00:00Z'}
    cache.write_text(json.dumps({'events': [first, second]}))
    revision = rec._brief_revision()
    cache.write_text(json.dumps({'events': [second, first]}))
    assert rec._brief_revision() == revision
    cache.write_text(json.dumps({'events': [second, {**first, 'start': '2026-09-08T12:00:00Z'}]}))
    assert rec._brief_revision() != revision


def test_early_outbox_row_never_claims_or_sends_before_due(rec, monkeypatch):
    box = rec.OUTBOX
    clock = {'now': 1000}
    monkeypatch.setattr(box.time, 'time', lambda: clock['now'])
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: '2026-09-08')
    actions = []
    monkeypatch.setitem(box.HOOKS, 'brief_gate', lambda *a: actions.append('claim') or 'send')
    monkeypatch.setitem(box.HOOKS, 'send', lambda *a: (actions.append('send') is None, '', {}))
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda *a: True)
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: True)
    assert not box.deliver({'run_id': 'early-brief', 'label': 'cron:sotto-morning-brief',
                            'body': 'Prepared early', 'target': 'test:owner', 'not_before': 1600})
    assert not actions
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['attempts'] == 0
    clock['now'] = 1600
    assert box.drain()['delivered'] == 1
    assert actions == ['claim', 'send']


def test_invalid_brief_replacement_enqueue_failure_is_durably_retried(rec, tmp_path, monkeypatch):
    box = rec.OUTBOX
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: False)
    monkeypatch.setitem(box.HOOKS, 'send', lambda *a: pytest.fail('invalid old brief sent'))
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda *a: pytest.fail('invalid brief activated delivery effects'))
    attempts = []
    def replace(payload):
        attempts.append(payload['label'])
        if len(attempts) == 1:
            raise OSError('synthetic queue write failure')
        rec.WORK_QUEUE.enqueue(str(tmp_path), 'run', {'label': payload['label']}, key='replacement-generation')
        return True
    monkeypatch.setitem(box.HOOKS, 'on_invalid', replace)
    assert not box.deliver({'run_id': 'invalid-brief', 'label': 'cron:sotto-morning-brief',
                            'body': 'Old meeting time', 'target': 'test:owner'})
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['effects_pending'] and 'body' not in row['payload']
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        doc['rows'][0]['effects_next_at'] = 0
    box.drain()
    box.drain()
    assert len(attempts) == 2
    assert rec.WORK_QUEUE.status(str(tmp_path))['counts'] == {'pending': 1}
    assert box.counts()['effects_pending'] == 0


@pytest.mark.parametrize('retry_enqueue', [False, True])
def test_failed_claimed_brief_can_be_replaced_after_its_context_changes(rec, tmp_path, monkeypatch,
                                                                      retry_enqueue):
    import managed
    box = rec.OUTBOX
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    monkeypatch.setattr(rec, '_local_now', lambda: now)
    monkeypatch.setattr(managed, 'brief_hold', lambda root: None)
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: day)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: True)
    archive = tmp_path / 'briefs' / f'{day}_morning.json'
    archive.parent.mkdir()
    archive.write_text('{}')
    valid = {'old': True, 'fresh': True}
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: valid[payload['run_id']])
    sent = []
    def send(body, target):
        sent.append(body)
        return ((False, 'explicit rejection', {'acceptance': 'rejected'}) if body == 'Old brief'
                else (True, '', {'acceptance': 'accepted', 'message_id': 'new-provider-id'}))
    monkeypatch.setitem(box.HOOKS, 'send', send)
    replacement_attempts = []
    def replace(name, label):
        replacement_attempts.append(label)
        if retry_enqueue and len(replacement_attempts) == 1:
            raise OSError('synthetic enqueue failure after releasing the old claim')
        assert box.deliver({'run_id': 'fresh', 'label': label, 'body': 'Fresh brief', 'target': 'test:owner'})
        return {'ok': True}
    monkeypatch.setattr(rec, '_fire_cron_job', replace)
    assert not box.deliver({'run_id': 'old', 'label': 'cron:sotto-morning-brief',
                            'body': 'Old brief', 'target': 'test:owner'})
    marker = Path(rec.delivered_marker(day, 'morning'))
    assert marker.read_text() == 'old'
    valid['old'] = False
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
    box.drain()
    if retry_enqueue:
        assert not marker.exists()
        with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
            doc['rows'][0]['effects_next_at'] = 0
        box.drain()
    assert sent == ['Old brief', 'Fresh brief']
    assert marker.read_text() == 'fresh'
    assert box.counts()['effects_pending'] == 0


@pytest.mark.parametrize('outcomes', [('unknown',), ('unknown', 'rejected'), ('crash',)])
def test_ambiguous_or_crashed_send_never_releases_the_brief_claim(rec, tmp_path, monkeypatch, outcomes):
    box = rec.OUTBOX
    day = datetime.now(timezone.utc).date().isoformat()
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: day)
    archive = tmp_path / 'briefs' / f'{day}_morning.json'
    archive.parent.mkdir()
    archive.write_text('{}')
    valid = [True]
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: valid[0])
    monkeypatch.setattr(rec, '_fire_cron_job', lambda *a: pytest.fail('ambiguous brief was replaced'))
    results = iter(outcomes)
    def send(body, target):
        outcome = next(results)
        if outcome == 'crash':
            raise SystemExit('synthetic process exit after provider invocation')
        return False, 'synthetic failure', {'acceptance': outcome}
    monkeypatch.setitem(box.HOOKS, 'send', send)
    payload = {'run_id': 'old', 'label': 'cron:sotto-morning-brief', 'body': 'Old brief', 'target': 'test:owner'}
    if outcomes[0] == 'crash':
        with pytest.raises(SystemExit):
            box.deliver(payload)
    else:
        assert not box.deliver(payload)
    for _ in outcomes[1:]:
        with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
            doc['rows'][0]['next_at'] = 0
        box.drain()
    valid[0] = False
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
    box.drain()
    assert Path(rec.delivered_marker(day, 'morning')).read_text() == 'old'
    assert box.counts()['effects_pending'] == 0


@pytest.mark.parametrize('claim_owner', ['accepted-run', 'another-run'])
def test_invalidation_never_removes_an_accepted_or_another_runs_claim(rec, tmp_path, monkeypatch, claim_owner):
    box = rec.OUTBOX
    day = datetime.now(timezone.utc).date().isoformat()
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: day)
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'send', lambda *a: (True, '', {'acceptance': 'accepted', 'message_id': 'provider'}))
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: True)
    archive = tmp_path / 'briefs' / f'{day}_morning.json'
    archive.parent.mkdir()
    archive.write_text('{}')
    assert box.deliver({'run_id': 'accepted-run', 'label': 'cron:sotto-morning-brief',
                        'body': 'Accepted brief', 'target': 'test:owner'})
    marker = Path(rec.delivered_marker(day, 'morning'))
    if claim_owner != 'accepted-run':
        marker.write_text(claim_owner)
    monkeypatch.setattr(rec, '_fire_cron_job', lambda *a: pytest.fail('protected claim was replaced'))
    assert rec._invalid_delivery({'run_id': 'accepted-run', 'day': day,
                                  'label': 'cron:sotto-morning-brief'})
    assert marker.read_text() == claim_owner


@pytest.mark.parametrize('transition', ['permission', 'calendar'])
def test_invalidated_brief_gets_fresh_generation_even_when_source_or_time_reverts(rec, tmp_path, monkeypatch, transition):
    import source_context
    allowed = {'imessage': True}
    monkeypatch.setattr(source_context, 'allowed', lambda source: allowed.get(source, True))
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(rec, '_local_now', lambda: now)
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *a: [
        ('sotto-morning-brief', '30 6 * * *', 'morning', 'sotto-morning-brief')])
    requests = []
    monkeypatch.setattr(rec, '_spawn_and_deliver', lambda runner, prompt, label, **kwargs:
                            requests.append(json.loads(prompt)))
    cache = tmp_path / 'cache/calendar_today.json'
    cache.parent.mkdir()
    event = {'id': 'meeting', 'start': now.isoformat()}
    cache.write_text(json.dumps({'events': [event]}))
    rec._managed_brief('sotto-morning-brief', 'cron:sotto-morning-brief')
    original = requests[-1]['work_key']
    if transition == 'permission':
        allowed['imessage'] = False
    else:
        cache.write_text(json.dumps({'events': [{**event, 'start': '2026-09-08T12:00:00Z'}]}))
    box = rec.OUTBOX
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: str(now.date()))
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: False)
    monkeypatch.setitem(box.HOOKS, 'on_invalid', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'send', lambda *a: pytest.fail('invalid candidate sent'))
    assert not box.deliver({'run_id': 'invalidated-original-generation', 'label': 'cron:sotto-morning-brief',
                            'body': 'Earlier source context', 'target': 'test:owner'})
    # Current facts now match their original values, but the earlier queue/artifact is terminal.
    allowed['imessage'] = True
    cache.write_text(json.dumps({'events': [event]}))
    rec._managed_brief('sotto-morning-brief', 'cron:sotto-morning-brief')
    assert requests[-1]['work_key'] != original


@pytest.mark.parametrize('waiting_in', ['outbox', 'saved-result'])
def test_old_brief_recovery_admits_one_fresh_generation(rec, tmp_path, monkeypatch, waiting_in):
    """Real worker, queue, outbox and replacement admission; only the provider/composer are fake."""
    import brief_runner
    import managed
    import onboarding
    clock = [datetime(2026, 9, 8, 6, 30, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(rec.time, 'time', lambda: clock[0])
    monkeypatch.setattr(rec, '_local_now', lambda: datetime.fromtimestamp(clock[0], timezone.utc))
    monkeypatch.setattr(managed, 'brief_hold', lambda *a: None)
    monkeypatch.setattr(onboarding, 'scheduled_hold', lambda *a: False)
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'test:owner')
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: [
        ('sotto-morning-brief', '30 6 * * *', '', 'sotto-morning-brief')])
    box, queue = rec.OUTBOX, rec.WORK_QUEUE
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: '2026-09-08')
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: True)
    archive = tmp_path / 'briefs/2026-09-08_morning.json'
    archive.parent.mkdir()
    archive.write_text('{}')
    attempts, accepted = [], []
    def send(body, target):
        attempts.append(body)
        if body == 'Old brief':
            return False, 'channel unavailable', {'acceptance': 'rejected'}
        accepted.append(body)
        return True, '', {'acceptance': 'accepted', 'message_id': 'fresh-accepted'}
    monkeypatch.setitem(box.HOOKS, 'send', send)
    label = 'cron:sotto-morning-brief'
    old_id = queue.enqueue(str(tmp_path), 'run', {'label': label}, key='old-composition', valid_until=clock[0] + 4 * 3600)
    old = queue.claim(str(tmp_path), 'initial-worker')
    old_result = {'text': 'Old brief', 'label': label, 'valid_until': clock[0] + 4 * 3600,
                  'effects': [{'kind': 'eligibility', 'valid_until': clock[0] + brief_runner.ARTIFACT_MAX_AGE_SECONDS}]}
    queue.save_result(str(tmp_path), old_id, 'initial-worker', old_result)
    old['result'] = old_result
    if waiting_in == 'outbox':
        rec._work_one(old)
    clock[0] += 3 * 3600
    if waiting_in == 'saved-result':
        recovered = queue.claim(str(tmp_path), 'recovered-worker')
        assert recovered['id'] == old_id
        rec._work_one(recovered)
        # Wait until the old handoff lease settles before retrying replacement admission.
        assert queue.get(str(tmp_path), old_id)['status'] == 'done'
        assert box.counts()['effects_pending'] == 1
        clock[0] += 61
    box.drain()
    replacement = queue.active_job(str(tmp_path), 'run', label)
    assert replacement is not None and replacement['id'] != old_id
    assert json.loads(replacement['payload']['prompt'])['kind'] == 'morning'
    assert attempts == (['Old brief'] if waiting_in == 'outbox' else [])
    monkeypatch.setattr(rec, '_execute_work', lambda job: {
        'text': 'Fresh brief', 'label': label, 'effects': [{'kind': 'eligibility', 'valid_until': clock[0] + 7200}]})
    fresh_job = queue.claim(str(tmp_path), 'fresh-worker')
    assert fresh_job['id'] == replacement['id']
    rec._work_one(fresh_job)
    box.drain()
    assert accepted == ['Fresh brief']
    assert Path(rec.delivered_marker('2026-09-08', 'morning')).read_text() == fresh_job['id']
    assert box.counts()['effects_pending'] == 0


def test_generation_contention_refunds_attempts_then_delivers_saved_artifact(rec, tmp_path, monkeypatch):
    """Real child, file lock and work queue, with only the lock wait shortened for the test."""
    clock = [rec.time.time()]
    monkeypatch.setattr(rec.time, 'time', lambda: clock[0])
    request = {'pack': str(PACK), 'kind': 'morning', 'day': '2026-09-08', 'work_key': 'contention'}
    run_key = hashlib.sha256(request['work_key'].encode()).hexdigest()[:24]
    scratch = tmp_path / 'events/work-inputs' / ('brief-' + run_key)
    scratch.mkdir(parents=True)
    (scratch / 'artifact.json').write_text(json.dumps({
        'brief_text': 'Fresh saved brief', 'composed_at': clock[0], 'learning_revision': 'current'}))
    for phase in ('essential', 'ancillary'):
        (scratch / (phase + '-done.json')).write_text('{"complete": true}')
    delivered = []
    monkeypatch.setattr(rec, '_deliver_text', lambda text, *a, **k: delivered.append(text.strip()))
    monkeypatch.setattr(rec, '_composed_brief_recently', lambda *a: True)
    real_popen = rec.subprocess.Popen
    def short_lock_wait(argv, **kwargs):
        # Use the real CLI/exit-code contract. No provider or script call is mocked.
        bootstrap = ('import sys, runpy; sys.path.insert(0, sys.argv[1]); '
                     'import jsonstore; jsonstore.LOCK_TIMEOUT_SECS = 0.05; '
                     'sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name="__main__")')
        return real_popen([argv[0], '-c', bootstrap, str(PACK / '_shared/lib'), *argv[1:]], **kwargs)
    monkeypatch.setattr(rec.subprocess, 'Popen', short_lock_wait)
    queue = rec.WORK_QUEUE
    identity = queue.enqueue(str(tmp_path), 'run', {
        'runner': [sys.executable, str(HERE / 'brief_runner.py')],
        'prompt': json.dumps(request), 'label': 'cron:sotto-morning-brief'},
        key='contention', valid_until=clock[0] + 3600)
    with (scratch / 'run.lock').open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        for attempt in range(queue.MAX_ATTEMPTS + 1):
            job = queue.claim(str(tmp_path), f'worker-{attempt}')
            assert job['id'] == identity
            rec._work_one(job)
            pending = queue.get(str(tmp_path), identity)
            assert pending['status'] == 'pending' and pending['attempts'] == 0
            assert pending['due'] == clock[0] + 60
            assert queue.claim(str(tmp_path), 'too-early') is None
            assert delivered == []
            clock[0] += 60
    recovered = queue.claim(str(tmp_path), 'recovered')
    rec._work_one(recovered)
    assert queue.get(str(tmp_path), identity)['status'] == 'done'
    assert delivered == ['Fresh saved brief']


def test_other_runner_tempfail_still_counts_as_failure(rec, tmp_path):
    queue = rec.WORK_QUEUE
    identity = queue.enqueue(str(tmp_path), 'run', {
        'runner': [sys.executable, '-c', 'raise SystemExit(75)'],
        'label': 'test'}, key='ordinary-failure')
    rec._work_one(queue.claim(str(tmp_path), 'worker'))
    failed = queue.get(str(tmp_path), identity)
    assert failed['status'] == 'pending' and failed['attempts'] == 1
    assert failed['error'].startswith('worker_exit_75:')
