"""Fresh confirmed definitions reuse the real occurrence queue and delivery handoff."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import fresh_routines as store
import runtime_owner_routine_tool as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, THREAD, capability
from test_routine_completion_reconciliation import rec

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

__all__ = ['context']
ARGS = {'operation': 'propose', 'name': 'user-open-loops', 'schedule': {'hour': 9, 'minute': 0, 'weekday': None},
        'prompt': 'Summarize my current open loops in five lines.'}


def request(args, operation='apply', event='a' * 64, text='yes'):
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_routine_tool', canonical],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'version': 1, 'operation': operation, 'identity': IDENTITY, 'action_id': action,
            'event_id': event, 'payload_hash': 'b' * 64, 'text_hash': hashlib.sha256(text.encode()).hexdigest(),
            'text': text, 'admitted_at_ms': int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000),
            'provider_thread_id': THREAD, 'args': args}


def call(body, context):
    root, policy, activation = context
    (root / 'runtime').mkdir(mode=0o700, exist_ok=True)
    policy.update(SOTTO_DATA=str(root), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1',
                  SOTTO_ROUTINE_EPOCH='e' * 32, _source_authority=lambda: {'complete': True, 'revision': 'c' * 64})
    activation = SimpleNamespace(verified_current_photon_route=lambda *args: {
        'platform': 'imessage', 'chat_id': 'iMessage;-;owner', 'thread_id': None})
    return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
                        data_root=root, capability=capability, activation=activation)


def create(context):
    proposed = request(ARGS, 'preview', event='d' * 64, text='Every morning summarize my open loops.')
    preview = call(proposed, context)
    body = request({'operation': 'create', 'definition': preview['definition'], 'proposal': {
        'event_id': proposed['event_id'], 'action_id': proposed['action_id'],
        'args_hash': store.digest(ARGS), 'accepted_dispatch_id': 'fixture-proposal-accepted', 'source_revision': 'c' * 64}})
    return body, call(body, context)


def test_confirmed_create_cancel_original_receipts_and_consumed_proposal(context):
    body, created = create(context)
    rid = created['outcome']['routine_id']
    assert created['outcome']['status'] == 'active'
    assert call(dict(body, operation='inspect'), context) == created
    row = store.load(context[0])['routines'][rid]
    assert row['create_proof']['text_hash'] == hashlib.sha256(b'yes').hexdigest()
    assert 'text' not in row['create_proof'] and row['create_proof']['domain'] == store.DOMAIN
    second = request(body['args'], event='f' * 64)
    assert call(second, context)['code'] == 'proposal_unavailable'
    canceled = call(request({'operation': 'cancel', 'routine_id': rid}, event='f' * 64, text='Cancel it.'), context)
    assert canceled['outcome']['status'] == 'cancelled'
    assert call(dict(body, operation='inspect'), context) == created
    assert call(body, context) == created
    assert call(request(None, 'list'), context)['routines'][0]['status'] == 'cancelled'
    assert call(request({'operation': 'cancel', 'routine_id': '0' * 64}, event='0' * 64), context)['code'] == 'target_unavailable'


def test_unclear_yes_changed_proposal_and_changed_event_refuse(context):
    body, _ = create(context)
    changed = request({'operation': 'cancel', 'routine_id': '0' * 64})
    assert call(changed, context)['code'] == 'event_conflict'
    # A separate proposed definition has not been consumed, but only a clear yes can create it.
    args = dict(ARGS, name='user-weekly-loops', schedule={'hour': 9, 'minute': 0, 'weekday': 1})
    proposal = request(args, 'preview', event='1' * 64, text='Weekly loops please')
    preview = call(proposal, context)
    bad = request({'operation': 'create', 'definition': preview['definition'], 'proposal': {
        'event_id': proposal['event_id'], 'action_id': proposal['action_id'], 'args_hash': store.digest(args),
        'accepted_dispatch_id': 'accepted', 'source_revision': 'c' * 64}}, event='2' * 64, text='Maybe later')
    assert call(bad, context)['code'] == 'proposal_unavailable'
    assert call(dict(bad, operation='inspect'), context)['status'] == 'not_found'
    with pytest.raises(ValueError):
        call(request(dict(ARGS, schedule={'hour': 9, 'minute': 60, 'weekday': None}), 'preview'), context)


@pytest.mark.parametrize(('fall_back', 'pending_ack'), [(False, False), (True, False), (False, True), ('weekdays', False), ('weekday_set', False), ('adjust', False), ('monthly', False), ('monthly_year', False), ('monthly_fold', False), ('monthly_gap', False), ('monthly_adjust', False), ('hourly', False), ('hourly_fold', False), ('hourly_gap', False), ('hourly_adjust', False)])
def test_actual_queue_outbox_successor_restart_and_cancel_fence(context, monkeypatch, fall_back, pending_ack):
    monthly_cases = {
        'monthly': ('2026-01-01T09:01:00+00:00', 'UTC', 9, 0, '2026-02-01T09:00:00.000000Z'),
        'monthly_year': ('2026-12-01T09:01:00+00:00', 'UTC', 9, 0, '2027-01-01T09:00:00.000000Z'),
        'monthly_fold': ('2026-11-01T04:31:00+00:00', 'America/Havana', 0, 30, '2026-12-01T05:30:00.000000Z'),
        'monthly_gap': ('2017-09-01T04:31:00+00:00', 'America/Asuncion', 0, 30, '2017-11-01T03:30:00.000000Z'),
    }
    hourly_cases = {
        'hourly': ('2026-10-06T23:16:00+00:00', 'UTC', None, 15, '2026-10-07T00:15:00.000000Z'),
        'hourly_fold': ('2026-11-01T05:16:00+00:00', 'America/New_York', None, 15, '2026-11-01T07:15:00.000000Z'),
        'hourly_gap': ('2026-10-03T14:31:00+00:00', 'Australia/Lord_Howe', None, 0, '2026-10-03T16:00:00.000000Z'),
    }
    timed_cases = monthly_cases | hourly_cases
    if fall_back in timed_cases:
        now, zone, hour, minute, expected = timed_cases[fall_back]
        class MonthlyClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat(now).astimezone(tz)
        monkeypatch.setattr(rec, 'datetime', MonthlyClock)
        monkeypatch.setattr(rec.WORK_QUEUE, 'datetime', MonthlyClock)
        monkeypatch.setattr(rec.WORK_QUEUE.time, 'time', lambda: MonthlyClock.now(timezone.utc).timestamp())
        monkeypatch.setattr(sys.modules[__name__], 'datetime', MonthlyClock)
        monkeypatch.setitem(ARGS, 'schedule', {'hour':hour,'minute':minute,'weekday':None if fall_back in hourly_cases else 'first_of_month'})
        context[1]['_configured_tz_name'] = lambda: zone
    if fall_back in ('weekdays', 'weekday_set'):
        class WeekdayClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat('2026-10-09T16:01:00+00:00').astimezone(tz)
        monkeypatch.setattr(rec, 'datetime', WeekdayClock)
        monkeypatch.setattr(rec.WORK_QUEUE, 'datetime', WeekdayClock)
        monkeypatch.setattr(rec.WORK_QUEUE.time, 'time', lambda: WeekdayClock.now(timezone.utc).timestamp())
        monkeypatch.setattr(sys.modules[__name__], 'datetime', WeekdayClock)
        monkeypatch.setitem(ARGS, 'schedule', {'hour': 9, 'minute': 0,
                                              'weekday': 'weekdays' if fall_back == 'weekdays' else [1, 3, 5]})
        context[1]['_configured_tz_name'] = lambda: 'America/Los_Angeles'
    if fall_back is True:
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat('2026-11-01T08:31:00+00:00').astimezone(tz)
        monkeypatch.setattr(sys.modules[__name__], 'datetime', Clock)
        monkeypatch.setattr(rec, 'datetime', Clock)
        monkeypatch.setattr(rec.WORK_QUEUE, 'datetime', Clock)
        monkeypatch.setattr(rec.WORK_QUEUE.time, 'time', lambda: Clock.now(timezone.utc).timestamp())
        monkeypatch.setitem(ARGS, 'schedule', {'hour': 1, 'minute': 30, 'weekday': None})
        # Test configured zone remains canonical across initial creation and completion.
        context[1]['_configured_tz_name'] = lambda: 'America/Los_Angeles'
    body, created = create(context)
    data, policy, _ = context
    row = store.load(data)['routines'][created['outcome']['routine_id']]
    for key, value in policy.items():
        if not key.startswith('_'):
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(rec, 'DATA', str(data))
    monkeypatch.setattr(rec.RUNTIME_OWNER_ROUTINE_TOOL, 'current_binding', lambda *args: row['binding'])
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'photon')
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: '9' * 64)
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete': True, 'revision': '8' * 64})
    monkeypatch.setattr(rec, '_record_delivery', lambda *args, **kwargs: None)
    sent = []
    def send(*args):
        sent.append(args)
        return True, '', {'message_id': 'accepted-routine'}
    for name, hook in {'send': send, 'on_delivered': lambda *args: True,
                       'valid': lambda *args: True, 'brief_gate': lambda *args: 'send'}.items():
        monkeypatch.setitem(rec.OUTBOX.HOOKS, name, hook)
    pack = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: str(pack.joinpath(*parts)))
    model_calls = []
    def execute(job):
        model_calls.append(job['id'])
        return {'text': 'Five actual model-produced lines.', 'label': job['payload']['label'], 'effects': [],
                'source_revision_at_composition': '8' * 64, 'route_digest_at_composition': '9' * 64,
                'source_text_sha256': hashlib.sha256(b'Five actual model-produced lines.').hexdigest()}
    rec._fresh_routine_tick()
    job = rec.WORK_QUEUE.claim(data, 'fixture-worker')
    assert job is not None and json.loads(job['payload']['prompt'])['kind'] == 'personal-routine'
    if fall_back in ('adjust','monthly_adjust','hourly_adjust'):
        old_job = job
        _, _, updated = adjust(context, row['routine_id'], schedule={'hour':None if fall_back=='hourly_adjust' else 0,'minute':0,'weekday':'first_of_month' if fall_back=='monthly_adjust' else None})
        assert rec._fresh_routine_authority(old_job['id'], 'photon') is None
        assert rec.WORK_QUEUE.get(data,old_job['id'])['status'] == 'leased'
        row = store.load(data)['routines'][updated['outcome']['routine_id']]
        if fall_back in ('monthly_adjust','hourly_adjust'):
            due = datetime.fromisoformat(row['initial_utc']) + timedelta(minutes=1)
            class AdjustedClock(datetime):
                @classmethod
                def now(cls,tz=None):
                    return due.astimezone(tz)
            monkeypatch.setattr(rec,'datetime',AdjustedClock)
            monkeypatch.setattr(rec.WORK_QUEUE,'datetime',AdjustedClock)
            monkeypatch.setattr(rec.WORK_QUEUE.time,'time',lambda:due.timestamp())
        rec._fresh_routine_tick()
        job = rec.WORK_QUEUE.claim(data,'replacement-worker')
        if fall_back in ('monthly_adjust','hourly_adjust') and job['id'] == old_job['id']:
            assert rec._fresh_routine_authority(job['id'],'photon') is None
            rec._work_one(job)
            job = rec.WORK_QUEUE.claim(data,'replacement-worker')
        assert job is not None and job['id'] != old_job['id']
    if pending_ack:
        monkeypatch.setattr(rec, '_validate_fixed_work', lambda *args: None)
        monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *args: None)
        monkeypatch.setattr(rec, '_composition_evidence', lambda *args: None)
        def forbidden_spawn(*args, **kwargs):
            raise AssertionError('pending activation ACK must not spawn a model child')
        monkeypatch.setattr(rec.subprocess, 'Popen', forbidden_spawn)
        rec._work_one(job)
        held = rec.WORK_QUEUE.get(data, job['id'])
        assert held['result'] is None and held['attempts'] == 0 and not sent and not model_calls
        cursor = store.cursors(data)[rec.WORK_QUEUE._routine_hash(store.cursor_binding(row)['owner_scope'], store.cursor_binding(row)['profile_id'], row['routine_id'])]
        assert cursor['pending_job_id'] == job['id'] and cursor['completed_count'] == 0
        # The existing defer waits 60 seconds; resume the same slot after proof renewal.
        actual_time = rec.WORK_QUEUE.time.time()
        monkeypatch.setattr(rec.WORK_QUEUE.time, 'time', lambda: actual_time + 61)
        job = rec.WORK_QUEUE.claim(data, 'ack-renewed-worker')
        assert job['id'] == held['id']
        monkeypatch.setattr(rec, '_composition_evidence', lambda *args: {
            'source_revision': '8' * 64, 'route_digest': '9' * 64})
    monkeypatch.setattr(rec, '_execute_work', execute)
    rec._work_one(job)
    assert rec.WORK_QUEUE.get(data, job['id'])['status'] == 'done'
    assert len(sent) == len(model_calls) == 1
    cursor = store.cursors(data)[rec.WORK_QUEUE._routine_hash(store.cursor_binding(row)['owner_scope'], store.cursor_binding(row)['profile_id'], row['routine_id'])]
    assert cursor['pending_job_id'] is None and cursor['completed_count'] == 1
    if fall_back in timed_cases:
        assert cursor['current_utc'] == expected
        assert store.expression(row['definition']) == (f'{minute} * * * *' if fall_back in hourly_cases else f'{minute} {hour} 1 * *')
    if fall_back == 'weekdays':
        assert cursor['current_utc'] == '2026-10-12T16:00:00.000000Z'
        assert store.expression(row['definition']) == '0 9 * * 1-5'
    if fall_back == 'weekday_set':
        assert cursor['current_utc'] == '2026-10-12T16:00:00.000000Z'
        assert store.expression(row['definition']) == '0 9 * * 1,3,5'
    if fall_back is True:
        assert row['initial_utc'] == '2026-11-01T01:30:00-07:00'
        assert cursor['current_utc'] == '2026-11-02T09:30:00.000000Z'
        assert row['definition']['zone'] == 'America/Los_Angeles'
    rec._fresh_routine_tick()  # stored cursor is used again as on restart
    assert rec.WORK_QUEUE.claim(data, 'restart-worker') is None
    call(request({'operation': 'cancel', 'routine_id': row['routine_id']}, event='f' * 64), context)
    rec._fresh_routine_tick()
    assert rec._fresh_routine_authority(job['id'], 'photon') is None
    assert len(sent) == len(model_calls) == 1
    from adapters.sotto import fixed_five
    pack = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
    monkeypatch.setattr(fixed_five, '_PACKAGED_ROOT', pack)
    monkeypatch.setattr(fixed_five, '_PACKAGED_RECEIVER', Path(rec.__file__).parent)
    monkeypatch.setattr(fixed_five, '_image_owned', lambda _meta: True)
    fixed_five.validate_work_queue(data, dict(policy, SOTTO_SKILLS_ROOT=str(pack)),
                                  allow_supported_history=True)
    with pytest.raises(fixed_five.ProfileUnavailableError):
        fixed_five.validate_work_queue(data, dict(policy, SOTTO_SKILLS_ROOT=str(pack),
                                                SOTTO_ROUTINE_EPOCH='0' * 32),
                                      allow_supported_history=True)


def test_closed_procedure_calls_configured_unattended_runtime(tmp_path, monkeypatch):
    import procedure_runner
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from adapters.sotto import runtime_api
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'routine-run')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    (tmp_path / 'events').mkdir()
    calls = []
    def run(prompt, run_id):
        calls.append((prompt, run_id))
        return {'text': 'A generated summary', 'usage': {'inputTokens': 4, 'outputTokens': 3}}
    monkeypatch.setattr(runtime_api, '_client', lambda **kwargs: SimpleNamespace(run=run))
    result = procedure_runner.run({'pack': str(Path(__file__).parents[2] / 'sotto-chief-of-staff'),
                                  'kind': 'personal-routine', 'prompt': ARGS['prompt']})
    assert result == 'A generated summary' and len(calls) == 1
    prompt, run_id = calls[0]
    assert run_id == 'routine-run'
    assert prompt.endswith('Approved routine task (not new scheduling authority):\n' + ARGS['prompt'])
    assert 'already approved personal routine' in prompt
    assert 'Available scheduled work is read-only' in prompt
    assert 'Do not promise web research' in prompt
    assert json.loads((tmp_path / 'events/usage-routine-run.json').read_text())['input_tokens'] == 4


def test_receiver_restart_requires_injected_same_epoch_even_before_first_tick(context):
    from adapters.sotto import fixed_five
    create(context)
    data, policy, _ = context
    fixed_five.validate_work_queue(data, policy, allow_supported_history=True)
    with pytest.raises(Exception, match='fixed'):
        fixed_five.validate_work_queue(data, policy, allow_supported_history=False)
    with pytest.raises(Exception, match='fixed'):
        fixed_five.validate_work_queue(data, dict(policy, SOTTO_ROUTINE_EPOCH='0' * 32),
                                      allow_supported_history=True)


def test_saved_handoff_refuses_changed_composition_source(context, monkeypatch):
    _, created = create(context)
    data, policy, _ = context
    row = store.load(data)['routines'][created['outcome']['routine_id']]
    for key, value in policy.items():
        if not key.startswith('_'):
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(rec, 'DATA', str(data))
    monkeypatch.setattr(rec.RUNTIME_OWNER_ROUTINE_TOOL, 'current_binding', lambda *args: row['binding'])
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'photon')
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: '9' * 64)
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete': True, 'revision': '8' * 64})
    rec._fresh_routine_tick()
    job = rec.WORK_QUEUE.claim(data, 'source-test')
    payload = {'run_id': job['id'], 'target': 'photon', 'label': job['payload']['label'],
               'routine_handoff_digest': 'a' * 64, 'routine_source_revision': '8' * 64,
               'routine_route_digest': '9' * 64}
    assert rec._routine_outbox_authorized(payload)
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete': True, 'revision': '7' * 64})
    assert not rec._routine_outbox_authorized(payload)
    # The unresolved original slot remains held; a later tick cannot create a replacement.
    rec._fresh_routine_tick()
    assert rec.WORK_QUEUE.claim(data, 'second') is None


def test_preview_refuses_system_prompts_and_existing_definition(context):
    for prompt in store.SYSTEM_PROMPTS:
        assert call(request(dict(ARGS, prompt=prompt), 'preview'), context)['code'] == 'unsupported_definition'
    create(context)
    assert call(request(ARGS, 'preview', event='3' * 64), context)['code'] == 'routine_conflict'


def test_adapter_exposes_only_supported_epoch_bound_projection(context, monkeypatch):
    from adapters.sotto import runtime_api
    _, result = create(context)
    data, policy, _ = context
    for key, value in policy.items():
        if not key.startswith('_'):
            monkeypatch.setenv(key, value)
    assert runtime_api.personal_routines_supported()
    assert runtime_api.personal_routines()[0]['routine_id'] == result['outcome']['routine_id']
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-telegram-fixed-five-v1')
    assert runtime_api.personal_routines_supported()
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'unsupported-profile')
    assert not runtime_api.personal_routines_supported()
    with pytest.raises(runtime_api.UnsupportedRuntimeOperationError):
        runtime_api.personal_routines()


def test_empty_projection_before_first_receiver_write(tmp_path):
    assert store.load(tmp_path) == {'version': 1, 'routines': {}, 'receipts': {}, 'consumed': {}}
    assert not (tmp_path / 'runtime').exists()


@pytest.mark.parametrize(('expr', 'completed', 'expected'), [
    ('30 1 * * *', '2026-11-01T08:31:00+00:00', '2026-11-02T01:30:00-08:00'),
    ('30 1 * * *', '2026-11-01T09:31:00+00:00', '2026-11-02T01:30:00-08:00'),
    ('30 1 * * *', '2026-11-01T08:29:00+00:00', '2026-11-01T01:30:00-07:00'),
    ('30 2 * * *', '2026-03-07T11:00:00+00:00', '2026-03-09T02:30:00-07:00'),
    ('30 1 * * 0', '2026-11-01T08:31:00+00:00', '2026-11-08T01:30:00-08:00'),
])
def test_fresh_wall_date_successor_keeps_daily_weekly_zone(expr, completed, expected):
    assert store.successor_after_completion(expr=expr, completed_at=datetime.fromisoformat(completed),
                                           zone='America/Los_Angeles') == expected


def test_first_contact_list_create_and_current_row_route_remain_bound(context, monkeypatch):
    data, policy, _ = context
    monkeypatch.setitem(IDENTITY, 'route_mode', 'first_contact')
    monkeypatch.setitem(IDENTITY, 'route_id', '+15555550123')
    assert call(request(None, 'list'), context)['routines'] == []
    _, created = create(context)
    row = store.load(data)['routines'][created['outcome']['routine_id']]
    activation = SimpleNamespace(verified_current_photon_route=lambda *args: row['binding']['route'])
    assert tool.current_binding(IDENTITY, policy, data, activation) == row['binding']
    changed = SimpleNamespace(verified_current_photon_route=lambda *args: {
        'platform': 'imessage', 'chat_id': 'iMessage;-;other', 'thread_id': None})
    assert tool.current_binding(IDENTITY, policy, data, changed) != row['binding']
    # The same saved first-contact identity cannot silently rebind the original row.
    monkeypatch.setattr(rec, 'DATA', str(data))
    monkeypatch.setattr(rec, 'RUNTIME_ACTIVATION', changed)
    for key, value in policy.items():
        if not key.startswith('_'):
            monkeypatch.setenv(key, value)
    assert not rec._fresh_row_current(row)


def test_telegram_pair_bound_create_due_restart_cancel(tmp_path, monkeypatch):
    from test_telegram_owner_commands import IDENTITY as TELEGRAM_IDENTITY, ROOT as TELEGRAM_ROOT
    from test_telegram_channel_proof import _fixture, ZONE
    import telegram_channel_proof as telegram
    data, link, _, client, opener, policy = _fixture(tmp_path)
    policy.update(GOOGLE_AI_API_KEY='fixture-key', SOTTO_ROUTINE_EPOCH='e' * 32, _configured_tz_name=lambda: ZONE,
                  _source_authority=lambda: {'complete': True, 'revision': 'c' * 64})
    def invoke(body):
        body = dict(body, identity=TELEGRAM_IDENTITY, provider_thread_id=TELEGRAM_IDENTITY['route_id'])
        return tool.perform(body, bearer=capability.derive(TELEGRAM_ROOT, TELEGRAM_IDENTITY),
                            root=TELEGRAM_ROOT, policy=policy, data_root=data, capability=capability,
                            activation=None, telegram_verifier=telegram, chat_client=client)
    proposed = request(ARGS, 'preview', event='d' * 64, text='Every morning summarize my open loops.')
    grouped = dict(proposed, identity=TELEGRAM_IDENTITY, provider_thread_id='telegram:-100123')
    with pytest.raises(Exception):
        tool.perform(grouped, bearer=capability.derive(TELEGRAM_ROOT, TELEGRAM_IDENTITY),
                     root=TELEGRAM_ROOT, policy=policy, data_root=data, capability=capability,
                     activation=None, telegram_verifier=telegram, chat_client=client)
    preview = invoke(proposed)
    assert preview['definition']['destination'] == 'this Telegram conversation'
    body = request({'operation': 'create', 'definition': preview['definition'], 'proposal': {
        'event_id': proposed['event_id'], 'action_id': proposed['action_id'],
        'args_hash': store.digest(ARGS), 'accepted_dispatch_id': 'accepted', 'source_revision': 'c' * 64}})
    unclear = request(body['args'], event='f' * 64, text='Maybe later')
    assert invoke(unclear)['code'] == 'proposal_unavailable'
    created = invoke(body)
    assert invoke(dict(body, operation='inspect')) == created
    row = store.load(data)['routines'][created['outcome']['routine_id']]
    for key, value in policy.items():
        if not key.startswith('_'):
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(rec, 'DATA', str(data))
    monkeypatch.setattr(rec, '_configured_tz_name', lambda: ZONE)
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(_client=lambda **kwargs: client, gallery_available=lambda: False))
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'telegram')
    pack = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
    monkeypatch.setenv('SOTTO_SKILLS_ROOT', str(pack))
    monkeypatch.setattr(rec, '_SCRIPT_CACHE', {})
    assert rec._fresh_row_current(row)
    rec._fresh_routine_tick()
    cursor = store.cursors(data)[rec.WORK_QUEUE._routine_hash(store.cursor_binding(row)['owner_scope'], store.cursor_binding(row)['profile_id'], row['routine_id'])]
    assert cursor['pending_job_id'] is not None
    job_id = cursor['pending_job_id']
    assert rec._fresh_routine_authority(job_id, 'telegram') is not None
    assert rec._fresh_routine_authority(job_id, 'photon') is None
    rec._fresh_routine_tick()
    assert store.cursors(data)[rec.WORK_QUEUE._routine_hash(store.cursor_binding(row)['owner_scope'], store.cursor_binding(row)['profile_id'], row['routine_id'])]['pending_job_id'] == job_id
    original = link.read_text()
    link.write_text(json.dumps({'bot_token': json.loads(original)['bot_token'], 'allowed_user': 999}))
    assert rec._fresh_routine_authority(job_id, 'telegram') is None
    with pytest.raises(Exception):
        invoke(request(None, 'list'))
    link.write_text(original)
    assert rec._fresh_routine_authority(job_id, 'telegram') is not None
    original_credential = opener.wire['credential_proof']
    opener.wire['credential_proof'] = '0' * 64
    assert rec._fresh_routine_authority(job_id, 'telegram') is None
    opener.wire['credential_proof'] = original_credential
    sent = []
    def send(*args):
        sent.append(args)
        return True, '', {'message_id': 'accepted-telegram-routine'}
    for name, hook in {'send': send, 'on_delivered': lambda *args: True,
                       'valid': lambda *args: True, 'brief_gate': lambda *args: 'send'}.items():
        monkeypatch.setitem(rec.OUTBOX.HOOKS, name, hook)
    monkeypatch.setattr(rec, '_record_delivery', lambda *args, **kwargs: None)
    from adapters.sotto import fixed_five
    pack = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
    monkeypatch.setattr(fixed_five, '_PACKAGED_ROOT', pack)
    monkeypatch.setattr(fixed_five, '_PACKAGED_RECEIVER', Path(rec.__file__).parent)
    monkeypatch.setattr(fixed_five, '_PACKAGED_RECEIVER', Path(rec.__file__).parent)
    monkeypatch.setattr(fixed_five, '_image_owned', lambda _meta: True)
    monkeypatch.setenv('SOTTO_SKILLS_ROOT', str(pack))
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete': True, 'revision': '8' * 64})
    # Validate the actual persisted request before execution; only image ownership
    # and the external model child are synthetic, never profile or source proof.
    adapter = SimpleNamespace(_client=lambda **kwargs: client, gallery_available=lambda: False,
                              validate_fixed_work=lambda kind, payload: fixed_five.validate_work(kind, payload, dict(os.environ)),
                              run_argv=lambda runner, *args: runner, read_usage=lambda path: None)
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: adapter)
    label = 'routine:fresh:' + row['routine_id']
    evidence = rec._composition_evidence(label)
    assert evidence is not None and evidence['source_revision'] == '8' * 64
    assert rec._composition_evidence('cron:sotto-morning-brief') is None
    assert rec._composition_evidence('prep:meeting') is None
    calls = []
    class ModelChild:
        returncode = 0
        def __init__(self, argv, **kwargs):
            calls.append(argv)
        def communicate(self, **kwargs):
            return 'Model-produced Telegram routine result.', ''
        def poll(self):
            return 0
    monkeypatch.setattr(rec.subprocess, 'Popen', ModelChild)
    job = rec.WORK_QUEUE.claim(data, 'telegram-fixture-worker')
    rec._validate_fixed_work(job['kind'], job['payload'])
    opener.wire['credential_proof'] = '0' * 64
    assert rec._composition_evidence(label) is None
    with pytest.raises(rec._WorkDeferredError):
        rec._execute_work(job)
    assert not calls
    opener.wire['credential_proof'] = original_credential
    with monkeypatch.context() as revoked:
        revoked.setattr(rec, '_runtime_source_authority', lambda: {'complete': False, 'revision': None})
        assert rec._composition_evidence(label) is None
        with pytest.raises(rec._WorkDeferredError):
            rec._execute_work(job)
        assert not calls
    result = rec._execute_work(job)
    assert result['source_revision_at_composition'] == evidence['source_revision']
    # Keep the saved queue result path while avoiding a second synthetic child.
    monkeypatch.setattr(rec, '_execute_work', lambda job: result)
    rec._work_one(job)
    assert rec.WORK_QUEUE.get(data, job_id)['status'] == 'done' and len(sent) == len(calls) == 1
    rec._fresh_routine_tick()
    assert rec.WORK_QUEUE.claim(data, 'restart-worker') is None
    assert invoke(dict(body, operation='inspect')) == created
    cancelled = invoke(request({'operation': 'cancel', 'routine_id': created['outcome']['routine_id']},
                               event='f' * 64, text='Cancel it.'))
    assert cancelled['outcome']['status'] == 'cancelled'
    rec._fresh_routine_tick()
    assert rec.WORK_QUEUE.claim(data, 'cancelled-worker') is None
    assert invoke(dict(body, operation='inspect')) == created


@pytest.mark.parametrize('weekday',['monthly','hourly','1-5',True,7,[],{},[1,1],[3,1],[0,7],[False,1]])
def test_only_exact_weekdays_variant_extends_schedule(weekday):
    with pytest.raises(ValueError):
        store.validate_proposal(dict(ARGS,schedule={'hour':9,'minute':0,'weekday':weekday}))


def test_original_daily_weekly_definition_bytes_unchanged():
    assert store.expression({'schedule':{'hour':9,'minute':0,'weekday':None}})=='0 9 * * *'
    assert store.expression({'schedule':{'hour':9,'minute':0,'weekday':1}})=='0 9 * * 1'
    original=store.definition(ARGS,'UTC')
    assert store.digest(original)=='3544e20cf8ef319ffbdd4ea8f6802ef2cd20774886f78c627f194f382de57016'
    weekly=store.definition(dict(ARGS,schedule={'hour':9,'minute':0,'weekday':1}),'UTC')
    assert store.digest(weekly)=='d785cfc7caca5fb590e40ef48a25ad3bc7ab67767e7a063c58aec20e30a8c8c3'


def test_weekday_set_successor_skips_dst_second_fold_and_spring_gap():
    schedule = {'hour': 1, 'minute': 30, 'weekday': [0, 1, 3]}
    store.validate_proposal(dict(ARGS, schedule=schedule))
    expr = store.expression({'schedule': schedule})
    assert expr == '30 1 * * 0,1,3'
    assert store.successor_after_completion(expr=expr,
        completed_at=datetime.fromisoformat('2026-11-01T08:31:00+00:00'),
        zone='America/Los_Angeles') == '2026-11-02T01:30:00-08:00'
    gap = '30 2 * * 0,1,3'
    assert store.successor_after_completion(expr=gap,
        completed_at=datetime.fromisoformat('2026-03-07T10:31:00+00:00'),
        zone='America/Los_Angeles') == '2026-03-09T02:30:00-07:00'


def test_weekday_set_aliases_cannot_duplicate_an_active_routine(context):
    _, created = create(context)
    row = store.load(context[0])['routines'][created['outcome']['routine_id']]
    alternate = dict(row['definition'], name='user-same-prompt',
                     schedule={'hour': 9, 'minute': 0, 'weekday': list(range(7))})
    with pytest.raises(store.RefusalError, match='routine_conflict'):
        store.check_available(store.load(context[0]), alternate, row['binding'])
    weekdays = dict(row['definition'], schedule={'hour': 9, 'minute': 0, 'weekday': 'weekdays'})
    narrow = dict(row['definition'], name='user-weekday-set',
                  schedule={'hour': 9, 'minute': 0, 'weekday': [1, 2, 3, 4, 5]})
    with pytest.raises(store.RefusalError, match='routine_conflict'):
        store.check_available({'routines': {'weekday': dict(row, definition=weekdays)}}, narrow, row['binding'])


def adjust(context, rid, *, event='3'*64, schedule=None):
    args={'operation':'propose_adjust','routine_id':rid,
          'schedule':schedule or {'hour':17,'minute':0,'weekday':5}}
    proposed=request(args,'preview',event='2'*64,text='Move that routine to Friday at five.')
    preview=call(proposed,context)
    body=request({'operation':'adjust','routine_id':rid,
                  'previous_definition_hash':preview['previous_definition_hash'],
                  'definition':preview['definition'],'proposal':{
                      'event_id':proposed['event_id'],'action_id':proposed['action_id'],
                      'args_hash':store.digest(args),'accepted_dispatch_id':'accepted-adjustment',
                      'source_revision':'c'*64}},event=event)
    return preview,body,call(body,context)


def test_atomic_adjustment_preserves_captured_zone_and_original_receipts(context):
    context[1]['_configured_tz_name']=lambda:'America/Los_Angeles'
    original_body,created=create(context)
    old_id=created['outcome']['routine_id']
    before=store.load(context[0])['routines'][old_id]
    context[1]['_configured_tz_name']=lambda:'UTC'
    preview,body,result=adjust(context,old_id)
    assert preview['definition']['zone']=='America/Los_Angeles'
    assert preview['definition']=={**before['definition'],'schedule':{'hour':17,'minute':0,'weekday':5}}
    assert result['outcome']['operation']=='adjust' and result['outcome']['previous_routine_id']==old_id
    doc=store.load(context[0]);new=doc['routines'][result['outcome']['routine_id']]
    assert doc['routines'][old_id]['definition']==before['definition']
    assert doc['routines'][old_id]['status']=='cancelled' and new['status']=='active'
    assert datetime.fromisoformat(new['initial_utc']) > datetime.fromtimestamp(body['admitted_at_ms']/1000,timezone.utc)
    assert call(dict(original_body,operation='inspect'),context)==created
    before=(context[0]/'runtime/owner-routines.json').read_bytes()
    assert call(dict(body,operation='inspect'),context)==result
    assert (context[0]/'runtime/owner-routines.json').read_bytes()==before


def test_adjustment_loss_after_atomic_write_never_repeats(context,monkeypatch):
    _,created=create(context)
    helper=store._preferences();original=helper._durable_preferences_replace
    monkeypatch.setattr(store,'_preferences',lambda:helper)
    def lost(path,doc):
        original(path,doc)
        raise OSError('lost atomic response')
    monkeypatch.setattr(helper,'_durable_preferences_replace',lost)
    rid=created['outcome']['routine_id']
    args={'operation':'propose_adjust','routine_id':rid,'schedule':{'hour':17,'minute':0,'weekday':5}}
    proposal=request(args,'preview',event='2'*64)
    preview=call(proposal,context)
    body=request({'operation':'adjust','routine_id':rid,'previous_definition_hash':preview['previous_definition_hash'],
        'definition':preview['definition'],'proposal':{'event_id':proposal['event_id'],'action_id':proposal['action_id'],
        'args_hash':store.digest(args),'accepted_dispatch_id':'accepted','source_revision':'c'*64}},event='3'*64)
    with pytest.raises(OSError):
        call(body,context)
    monkeypatch.setattr(helper,'_durable_preferences_replace',lambda *a:pytest.fail('no repeated adjustment'))
    result=call(dict(body,operation='inspect'),context)
    assert result['outcome']['operation']=='adjust'
    assert len(store.load(context[0])['routines'])==2


def test_adjustment_stale_and_noop_refuse(context):
    _,created=create(context);rid=created['outcome']['routine_id']
    old=store.load(context[0])['routines'][rid]
    assert call(request({'operation':'propose_adjust','routine_id':rid,'schedule':old['definition']['schedule']},'preview'),context)['code']=='unsupported_definition'
    args={'operation':'propose_adjust','routine_id':rid,'schedule':{'hour':17,'minute':0,'weekday':5}}
    proposed=request(args,'preview',event='2'*64);preview=call(proposed,context)
    body=request({'operation':'adjust','routine_id':rid,'previous_definition_hash':'0'*64,
        'definition':preview['definition'],'proposal':{'event_id':proposed['event_id'],'action_id':proposed['action_id'],
        'args_hash':store.digest(args),'accepted_dispatch_id':'accepted','source_revision':'c'*64}},event='3'*64)
    assert call(body,context)['code']=='target_unavailable'
    assert len(store.load(context[0])['routines'])==1


def test_adjustment_at_ten_active_routines_excludes_replaced_target(context):
    _,created=create(context)
    for n in range(9):
        args=dict(ARGS,name='user-extra-'+str(n),schedule={'hour':n,'minute':0,'weekday':None})
        proposed=request(args,'preview',event=f'{n+16:064x}')
        preview=call(proposed,context)
        body=request({'operation':'create','definition':preview['definition'],'proposal':{
            'event_id':proposed['event_id'],'action_id':proposed['action_id'],'args_hash':store.digest(args),
            'accepted_dispatch_id':'accepted-'+str(n),'source_revision':'c'*64}},event=f'{n+32:064x}')
        assert call(body,context)['status']=='committed'
    _,_,result=adjust(context,created['outcome']['routine_id'])
    assert result['status']=='committed'
    assert len([r for r in store.load(context[0])['routines'].values() if r['status']=='active'])==10


@pytest.mark.parametrize(('completed','expected'),[
    ('2024-01-31T12:00:00+00:00','2024-02-01T09:00:00+00:00'),
    ('2024-02-29T12:00:00+00:00','2024-03-01T09:00:00+00:00'),
    ('2026-02-01T09:00:00+00:00','2026-03-01T09:00:00+00:00'),
])
def test_monthly_calendar_boundaries_strict_after(completed,expected):
    assert store.successor_after_completion(expr='0 9 1 * *',completed_at=datetime.fromisoformat(completed),zone='UTC')==expected


@pytest.mark.parametrize(('zone','completed','minute','expected'), [
    ('UTC','2026-10-06T23:15:00+00:00',15,'2026-10-07T00:15:00+00:00'),
    ('UTC','2026-10-06T10:55:00+00:00',15,'2026-10-06T11:15:00+00:00'),
    ('America/New_York','2026-03-08T01:15:00-05:00',15,'2026-03-08T03:15:00-04:00'),
    ('America/New_York','2026-11-01T01:10:00-05:00',15,'2026-11-01T02:15:00-05:00'),
    ('Australia/Lord_Howe','2026-10-04T01:00:00+10:30',0,'2026-10-04T03:00:00+11:00'),
    ('Asia/Kathmandu','2026-10-06T00:30:00+05:45',15,'2026-10-06T01:15:00+05:45'),
])
def test_hourly_wall_successor_strict_future(zone,completed,minute,expected):
    result=store.successor_after_completion(expr=f'{minute} * * * *',completed_at=datetime.fromisoformat(completed),zone=zone)
    assert result==expected
    assert datetime.fromisoformat(result).astimezone(timezone.utc)>datetime.fromisoformat(completed).astimezone(timezone.utc)


@pytest.mark.parametrize('schedule',[{'hour':None,'minute':0,'weekday':1},{'hour':None,'minute':0,'weekday':'weekdays'},{'hour':None,'minute':0,'weekday':'first_of_month'},{'hour':None,'minute':True,'weekday':None},{'hour':'*','minute':0,'weekday':None}])
def test_hourly_shape_is_closed(schedule):
    with pytest.raises(ValueError):
        store.validate_proposal(dict(ARGS,schedule=schedule))


def test_hourly_definition_expression():
    value=store.definition(dict(ARGS,schedule={'hour':None,'minute':15,'weekday':None}),'Asia/Kathmandu')
    assert store.expression(value)=='15 * * * *'
    store.validate_definition(value)


def _call_as(identity, body, context):
    root, policy, _ = context
    (root / 'runtime').mkdir(mode=0o700, exist_ok=True)
    policy.update(SOTTO_DATA=str(root), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1',
                  SOTTO_ROUTINE_EPOCH='e' * 32, _source_authority=lambda: {'complete': True, 'revision': 'c' * 64})
    activation = SimpleNamespace(verified_current_photon_route=lambda *args: {
        'platform': 'imessage', 'chat_id': 'iMessage;-;owner', 'thread_id': None})
    return tool.perform({**body, 'identity': identity}, bearer=capability.derive(ROOT, identity), root=ROOT,
                        policy=policy, data_root=root, capability=capability, activation=activation)


def test_owner_binding_change_retires_old_routines_instead_of_orphaning_them(context):
    _, created = create(context)
    rid = created['outcome']['routine_id']
    moved = dict(IDENTITY, account_id='account-2')  # Photon account re-provisioned / line re-keyed
    assert _call_as(moved, request(None, 'list', event='5' * 64), context)['routines'] == []
    row = store.load(context[0])['routines'][rid]
    assert row['status'] == 'retired' and row['retired'] == {'reason': 'binding_changed'}
    cancel = _call_as(moved, request({'operation': 'cancel', 'routine_id': rid}, event='6' * 64, text='Cancel it.'), context)
    assert cancel['code'] == 'target_unavailable'
    again = _call_as(moved, request(ARGS, 'preview', event='7' * 64, text='Every morning summarize my open loops.'), context)
    assert again['status'] == 'preview', again  # no stale routine_conflict
    # The original approval receipt and its single-use fence survive retirement.
    doc = store.load(context[0])
    assert doc['receipts'] and set(doc['consumed'].values()) == {rid}


def test_routine_cap_and_conflicts_count_only_the_current_binding(context):
    _, created = create(context)
    doc = store.load(context[0])
    row = doc['routines'][created['outcome']['routine_id']]
    old = dict(row['binding'], identity=dict(IDENTITY, account_id='account-old'))
    stale = {'routines': {str(i): dict(row, binding=old) for i in range(10)}}
    assert store.check_available(stale, dict(row['definition'], name='user-other'), row['binding']) is None
    with pytest.raises(store.RefusalError) as conflict:
        store.check_available(doc, row['definition'], row['binding'])
    assert conflict.value.code == 'routine_conflict'
    with pytest.raises(store.RefusalError) as limit:
        store.check_available({'routines': {str(i): dict(row, definition=dict(row['definition'], name=f'user-x{i}'))
                                            for i in range(10)}}, dict(row['definition'], name='user-new',
                              prompt='Another prompt.'), row['binding'])
    assert limit.value.code == 'routine_limit'


@pytest.mark.parametrize(('old', 'new'), [('weekdays', [1, 2, 3, 4, 5]), ([1, 2, 3, 4, 5], 'weekdays'),
                                          (3, [3]), ([3], 3), (None, list(range(7))), (list(range(7)), None)])
def test_weekday_set_alias_adjustment_is_a_refused_no_op(old, new):
    definition = {'name': 'user-loops', 'prompt': 'Summarize my loops.', 'zone': 'America/Los_Angeles',
                  'destination': store.DESTINATION, 'schedule': {'hour': 9, 'minute': 0, 'weekday': old}}
    doc = {'routines': {'a' * 64: {'status': 'active', 'binding': 'b', 'definition': definition}}}
    with pytest.raises(store.RefusalError, match='unsupported_definition'):
        store.adjustment_definition(doc, {'operation': 'propose_adjust', 'routine_id': 'a' * 64,
                                          'schedule': {'hour': 9, 'minute': 0, 'weekday': new}}, 'b')
    _, value = store.adjustment_definition(doc, {'operation': 'propose_adjust', 'routine_id': 'a' * 64,
                                                 'schedule': {'hour': 10, 'minute': 0, 'weekday': new}}, 'b')
    assert value['schedule'] == {'hour': 10, 'minute': 0, 'weekday': new}
