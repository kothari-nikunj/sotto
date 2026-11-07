"""Natural reminder tools retain original owner provenance and exact durable effects."""
from datetime import datetime, timezone
import hashlib
import io
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import runtime_owner_reminder_tool as tool
from test_runtime_owner_reminder import (
    context, IDENTITY, ROOT, WHEN, THREAD, capability, schedule, receiver, request as command_request,
    call as command_call,
)
from test_telegram_owner_commands import (
    context as telegram_context, IDENTITY as TELEGRAM_IDENTITY,
    THREAD as TELEGRAM_THREAD, _call as telegram_call,
)

__all__ = ['context', 'telegram_context']  # Re-export the real protected-route fixtures.

ARGS = {'due': '2026-10-09T01:15:00-07:00', 'action': 'Review the cedar packet 🌲'}
TEXT = 'Please remind me to review the cedar packet in fifteen minutes.'


def request(operation='create', event='a' * 64, args=ARGS, text=TEXT, identity=IDENTITY, thread=THREAD):
    if operation in ('context', 'list'):
        args = None
    # Independently spell the JavaScript canonical action identity, including Unicode.
    canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    ident = hashlib.sha256(json.dumps([event, 'owner_reminder_tool', canonical],
                                     ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'version': 1, 'operation': operation, 'identity': identity,
            'action_id': ident, 'event_id': event, 'payload_hash': 'b' * 64,
            'text_hash': hashlib.sha256(text.encode(errors='surrogatepass')).hexdigest(), 'text': text,
            'admitted_at_ms': int(WHEN.timestamp() * 1000), 'provider_thread_id': thread, 'args': args}


def call(body, context, **kwargs):
    data, policy, activation = context
    policy.update(SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1',
                  _configured_tz_name=lambda: 'America/Los_Angeles',
                  _tz_resolve=lambda name: ZoneInfo(name))
    return tool.perform(body, bearer=kwargs.get('bearer', capability.derive(ROOT, body['identity'])),
                        root=ROOT, policy=policy, data_root=data, capability=capability,
                        activation=activation)


def test_context_has_configured_zone_and_no_write(context):
    before = datetime.now(timezone.utc)
    result = call(request('context'), context)
    after = datetime.now(timezone.utc)
    assert result['status'] == 'context' and result['zone'] == 'America/Los_Angeles'
    assert result['event_id'] == 'a' * 64 and result['payload_hash'] == 'b' * 64
    now = datetime.fromisoformat(result['now'])
    assert now.utcoffset() == now.astimezone(ZoneInfo(result['zone'])).utcoffset()
    assert before.timestamp() - .001 <= now.timestamp() <= after.timestamp()
    assert schedule.current() == []
    assert set(result) == {'version', 'status', 'event_id', 'payload_hash', 'zone', 'now'}


def test_create_inspect_fire_cancel_and_compaction_preserve_tool_provenance(context, monkeypatch):
    body = request()
    assert call(dict(body, operation='inspect'), context) == {'version': 1, 'status': 'not_found'}
    first = call(body, context)
    assert first['status'] == 'committed' and first['due'] == '2026-10-09T08:15:00Z'
    row = schedule.current()[0]
    assert row['created_by'] == 'owner_tool'
    assert row['owner_create']['domain'] == 'sotto.owner-reminder-tool.v1'
    assert row['owner_create']['text_hash'] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert 'text' not in row['owner_create'] and 'command' not in row['owner_create']
    assert row['owner_create']['args'] == ARGS
    assert row['owner_create']['args_hash'] == first['args_hash']
    assert call(dict(body, operation='inspect'), context) == first
    assert call(body, context) == first
    listed = command_call(command_request('/reminder list', 'list', event='c' * 64), context)
    assert listed['items'][0]['id'] == first['reminder_id']
    canceled = command_call(command_request('/reminder cancel ' + first['reminder_id'], 'cancel',
                                           event='d' * 64), context)
    assert canceled['reminder_status'] == 'canceled'
    # Acceptance racing the cancellation retains the protected acceptance transition.
    fired = schedule.mark_accepted(first['reminder_id'], '2026-10-09T08:15:01Z')
    assert fired['status'] == 'fired' and fired['accepted_at'] == '2026-10-09T08:15:01Z'
    monkeypatch.setattr(schedule, 'MAX_ROWS', 1)
    schedule.create('2026-10-10T08:00:00Z', 'Unrelated legacy reminder')
    assert len(schedule._rows()) == 2  # compacted latest rows, not discarded receipts
    recovered = call(dict(body, operation='inspect'), context)
    assert recovered['reminder_id'] == first['reminder_id'] and recovered['reminder_status'] == 'fired'
    assert call(body, context) == recovered
    assert schedule.current()[0]['owner_create'] == row['owner_create']


def test_changed_args_and_cross_domain_retry_conflict(context):
    body = request()
    call(body, context)
    for args in ({**ARGS, 'action': 'Changed action'}, {**ARGS, 'due': '2026-10-09T08:15:00Z'}):
        changed = request(args=args)
        assert call(changed, context) == {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
        assert call(dict(changed, operation='inspect'), context)['code'] == 'event_conflict'
    literal = command_request('/reminder at 2026-10-09T08:15:00Z Changed action', 'create')
    assert command_call(literal, context)['code'] == 'event_conflict'
    assert len(schedule.current()) == 1
    # The fence also applies if the literal command effect came first.
    command_call(command_request('/reminder at 2026-10-09T08:15:00Z Literal action', 'create',
                                 event='e' * 64), context)
    assert call(request(event='e' * 64), context)['code'] == 'event_conflict'
    assert len(schedule.current()) == 2


def test_due_is_relative_to_admission_and_inspect_never_creates(context):
    past = request(args={**ARGS, 'due': WHEN.isoformat()})
    assert call(past, context) == {'version': 1, 'status': 'rejected', 'code': 'due_unavailable'}
    assert call(dict(past, operation='inspect'), context) == {'version': 1, 'status': 'not_found'}
    assert schedule.current() == []
    # Already elapsed wall time is allowed on retry; only original admission defines availability.
    first = call(request(), context)
    assert call(request('inspect'), context) == first


@pytest.mark.parametrize('mutation', [
    {'text_hash': '0' * 64}, {'action_id': '0' * 64}, {'admitted_at_ms': True},
    {'provider_thread_id': 'imessage:iMessage;-;foreign~+15555550123'},
    {'args': {'due': ARGS['due'], 'action': ARGS['action'], 'event_id': 'f' * 64}},
])
def test_untrusted_admission_or_extra_model_arguments_refused(context, mutation):
    with pytest.raises((ValueError, tool.UnavailableError)):
        call({**request(), **mutation}, context)
    assert schedule.current() == []


@pytest.mark.parametrize('args,text', [
    ({**ARGS, 'action': ''}, TEXT), ({**ARGS, 'action': 'x' * 501}, TEXT),
    ({**ARGS, 'action': 'bad\x00text'}, TEXT), ({**ARGS, 'action': '\ud800'}, TEXT),
    ({**ARGS, 'due': '2026-10-09T08:15:00'}, TEXT),
    ({**ARGS, 'due': '2026-02-30T08:15:00Z'}, TEXT),
    (ARGS, 'bad\x00text'), (ARGS, 'x' * 16001),
])
def test_bounded_valid_unicode_and_explicit_due_required(context, args, text):
    with pytest.raises(ValueError):
        call(request(args=args, text=text), context)
    assert schedule.current() == []


def test_http_tool_route_auth_size_duplicates_and_receipt(context, monkeypatch):
    data, policy, _activation = context
    call(request('context'), context)  # configure the supported profile
    for name, value in policy.items():
        if isinstance(value, str):
            monkeypatch.setenv(name, value)
    monkeypatch.setenv('SOTTO_MCP_TOKEN', ROOT)
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    monkeypatch.setattr(receiver, 'DATA', str(data))
    monkeypatch.setattr(receiver, 'RUNTIME_ACTIVATION', context[2])

    def post(body, *, authenticated=True, path='/runtime/owner-reminder-tool', size=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        sent = []
        fake = SimpleNamespace(path=path, client_address=('127.0.0.1', 1234),
                               headers={'Content-Length': str(size or len(raw)), 'Content-Type': 'application/json'},
                               rfile=io.BytesIO(raw), _authed=lambda _token: authenticated,
                               _send=lambda status, payload: sent.append((status, payload)))
        receiver.Handler.do_POST(fake)
        return sent[0]

    assert post(request(), authenticated=False)[0] == 401
    assert post(request(), size=65537)[0] == 400
    assert post(b'{"identity":{},"identity":{}}')[0] == 400
    status, committed = post(request())
    assert status == 200 and committed['status'] == 'committed'
    assert post(request(args={**ARGS, 'action': 'Changed action'}))[0] == 409
    assert post(request('inspect')) == (200, committed)
    assert post(request('list'), authenticated=False)[0] == 401
    listed_status, listed = post(request('list'))
    assert listed_status == 200 and listed['status'] == 'listed'
    assert listed['items'][0]['id'] == committed['reminder_id']
    assert post(dict(request('list'), args={}))[0] == 400
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'other')
    assert post(request('context'))[0] == 503


def test_telegram_tool_checks_current_pair_and_loaded_identity(telegram_context):
    body = request(identity=TELEGRAM_IDENTITY, thread=TELEGRAM_THREAD)
    # This authenticated profile uses the existing actual protected-channel proof implementation.
    result = telegram_call(tool, body, telegram_context)
    assert result['status'] == 'committed'
    data, link, _expected, _client, _opener, _policy = telegram_context
    link.write_text(json.dumps({'bot_token': 'changed', 'allowed_user': 999}))
    with pytest.raises(tool.UnavailableError):
        telegram_call(tool, dict(body, operation='inspect'), telegram_context)
    assert len(schedule.current()) == 1 and data.joinpath('intentions.jsonl').is_file()


def test_concurrent_create_is_one_effect_and_changed_envelope_conflicts(context):
    from concurrent.futures import ThreadPoolExecutor

    body = request()
    call(request('context'), context)
    # Only mutate policy once before racing real file-lock/writer calls.
    data, policy, activation = context

    def commit():
        return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT,
                            policy=policy, data_root=data, capability=capability, activation=activation)

    with ThreadPoolExecutor(max_workers=6) as executor:
        receipts = list(executor.map(lambda _: commit(), range(12)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(schedule.current()) == 1 and len(schedule._rows()) == 1
    changed = request(text=TEXT + ' Changed original text.')
    assert call(changed, context)['code'] == 'event_conflict'
    assert call({**body, 'payload_hash': 'f' * 64}, context)['code'] == 'event_conflict'


def test_profile_stale_route_and_bearer_refused_for_context_and_create(context):
    call(request('context'), context)
    data, policy, activation = context
    for operation in ('context', 'create'):
        body = request(operation)
        for change in ({'SOTTO_RUNTIME_PROFILE': 'unexpected'}, {'SOTTO_TENANT_ID': 'foreign'},
                       {'PHOTON_ALLOWED_USERS': 'foreign'}):
            with pytest.raises(tool.UnavailableError):
                tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT,
                             policy={**policy, **change}, data_root=data,
                             capability=capability, activation=activation)
        stale = SimpleNamespace(verified_current_photon_route=lambda *_: {'chat_id': 'stale'})
        with pytest.raises(tool.UnavailableError):
            tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT,
                         policy=policy, data_root=data, capability=capability, activation=stale)
        with pytest.raises(tool.UnavailableError):
            call(body, context, bearer='wrong')
    assert schedule.current() == []


def test_natural_list_reads_both_origins_filters_terminal_foreign_and_preserves_log(context):
    natural = call(request(), context)
    slash = command_call(command_request('/reminder at 2026-10-10T08:00:00Z Call Dana', 'create', event='c'*64), context)
    slash_row = next(row for row in schedule.current() if row['id'] == slash['reminder_id'])
    fired = call(request(event='d'*64, args={'due': '2026-10-11T08:00:00Z', 'action': 'Already fired'}), context)
    schedule.mark_accepted(fired['reminder_id'], '2026-10-11T08:00:01Z')
    canceled = call(request(event='e'*64, args={'due': '2026-10-12T08:00:00Z', 'action': 'Canceled'}), context)
    command_call(command_request('/reminder cancel '+canceled['reminder_id'], 'cancel', event='f'*64), context)
    schedule.create('2026-10-13T08:00:00Z', 'Legacy unadmitted')
    proof = dict(schedule.current()[0]['owner_create'], identity_digest='0'*64, event_id='1'*64)
    schedule.create_tool_admitted('2026-10-14T08:00:00Z', 'Foreign owner', proof)
    from pathlib import Path
    path = Path(schedule.log_path())
    before = path.read_bytes()
    body = request('list', text='What reminders do I have?')
    result = call(body, context)
    assert result == {'version': 1, 'status': 'listed', 'event_id': body['event_id'],
        'payload_hash': body['payload_hash'], 'zone': 'America/Los_Angeles', 'items': [
            {'id': natural['reminder_id'], 'due': natural['due'], 'action': natural['action'], 'status': 'scheduled'},
            {'id': slash['reminder_id'], 'due': slash_row['due'], 'action': slash_row['action'], 'status': 'scheduled'}]}
    assert call(body, context) == result
    assert path.read_bytes() == before


def test_natural_list_bounded_50_and_read_only_empty(context):
    from pathlib import Path
    path = Path(schedule.log_path())
    assert call(request('list'), context)['items'] == []
    assert not path.exists()
    for index in range(55):
        args = {'due': '2026-10-15T08:00:00Z', 'action': f'Reminder {index:02}'}
        call(request(event=f'{index+1:064x}', args=args), context)
    before = path.read_bytes()
    listed = call(request('list'), context)['items']
    assert len(listed) == 50
    assert [(row['due'], row['id']) for row in listed] == sorted((row['due'], row['id']) for row in schedule.current())[:50]
    assert path.read_bytes() == before


def test_natural_list_original_proof_route_and_args_checked(context):
    body = request('list')
    with pytest.raises(ValueError):
        call(dict(body, args={}), context)
    with pytest.raises(ValueError):
        call(dict(body, text='changed owner message'), context)
    with pytest.raises(tool.UnavailableError):
        call(body, context, bearer='wrong')
    with pytest.raises(tool.UnavailableError):
        call(request('list', thread='imessage:other~+15555550123'), context)


def test_natural_cancel_lists_absent_and_inspects_once(context, monkeypatch):
    created = call(request(), context)
    args = {'operation': 'cancel', 'reminder_id': created['reminder_id'], 'cancellation': 'Cancel the cedar reminder'}
    body = request('cancel', event='c' * 64, args=args, text=args['cancellation'])
    assert call(request('list', event='d' * 64), context)['items'][0]['id'] == created['reminder_id']
    saved = call(body, context)
    assert saved['operation'] == 'cancel' and saved['reminder_status'] == 'canceled'
    assert call(request('list', event='d' * 64), context)['items'] == []
    monkeypatch.setattr(schedule, 'cancel_admitted', lambda *a: pytest.fail('Cancellation repeated'))
    assert call(dict(body, operation='inspect'), context) == saved
    assert call(body, context) == saved


@pytest.mark.parametrize('state', ['missing', 'fired', 'canceled', 'foreign'])
def test_cancel_refuses_unavailable_or_foreign_targets(context, state):
    created = call(request(), context)
    ident = created['reminder_id']
    if state in ('fired', 'canceled'):
        schedule.transition(ident, state)
    elif state == 'missing':
        ident = 'int_' + '0' * 32
    elif state == 'foreign':
        rows = schedule.current()
        rows[0]['owner_create']['identity_digest'] = '0' * 64
        schedule._write_row(rows[0])
    args = {'operation': 'cancel', 'reminder_id': ident, 'cancellation': 'Cancel the cedar reminder'}
    before = schedule.current()
    result = call(request('cancel', event='c' * 64, args=args, text=args['cancellation']), context)
    assert result == {'version': 1, 'status': 'rejected', 'code': 'target_not_owner' if state == 'foreign' else 'target_unavailable'}
    assert schedule.current() == before


def test_cancel_requires_original_owner_quote_and_exact_args(context):
    created = call(request(), context)
    args = {'operation': 'cancel', 'reminder_id': created['reminder_id'], 'cancellation': 'Cancel the cedar reminder'}
    assert call(request('cancel', event='c' * 64, args=args, text='Hello'), context)['code'] == 'owner_span_unavailable'
    with pytest.raises(ValueError):
        call(request('create', event='c' * 64, args=args), context)
    assert schedule.current()[0]['status'] == 'scheduled'


def _conditional_args(context):
    import runtime_owner_loop
    from delivery_effects import loop_identity, loop_version
    data, policy, _ = context
    cr = runtime_owner_loop._load_writer()._load_continuity()
    row = {'anchor_key': 'gmail:waiting_on:cedar', 'status': 'open', 'source': 'gmail',
           'source_message_id': 'cedar-message', 'source_thread_id': 'cedar-thread',
           'source_refs': [{'sourceType': 'gmail', 'sourceId': 'cedar-message'}],
           'contact_name': 'Dana', 'contact_identifier': 'dana@example.com',
           'action_type': 'waiting_on', 'summary': 'Send the Cedar deck', 'created_at': '2026-10-04'}
    cr._persist(row)
    policy['_source_authority'] = lambda: {'complete': True, 'revision': 'e' * 64,
                                         'bridge_sources': [], 'google_sources': ['gmail'], 'connector_sources': []}
    return {**ARGS, 'operation': 'conditional', 'condition': "if Dana hasn't replied",
            'anchor_key': row['anchor_key'], 'loop_version': loop_version(row),
            'loop_identity': loop_identity(row), 'source_revision': 'e' * 64}, row, cr


def test_conditional_create_and_historical_inspect_preserve_original_proof(context, monkeypatch):
    args, row, cr = _conditional_args(context)
    body = request(args=args, text="Remind me if Dana hasn't replied.")
    saved = call(body, context)
    assert saved['operation'] == 'conditional' and saved['anchor_key'] == row['anchor_key']
    item = schedule.current()[0]
    assert item['condition_binding'] == {'anchor_key': row['anchor_key'], 'loop_identity': args['loop_identity'], 'sources': ['gmail']}
    assert item['owner_create']['args'] == args
    context[1]['_source_authority'] = lambda: {'complete': False}
    monkeypatch.setattr(schedule, 'create_tool_admitted', lambda *a, **k: pytest.fail('recreated'))
    assert call(dict(body, operation='inspect'), context) == saved
    assert call(body, context) == saved


@pytest.mark.parametrize('change', ['version', 'identity', 'direction', 'revoked', 'condition'])
def test_conditional_current_exact_waiting_target_required(context, change):
    args, row, cr = _conditional_args(context)
    text = "Remind me if Dana hasn't replied."
    if change in ('version', 'identity'):
        args['loop_' + change] = '0' * 64
    elif change == 'direction':
        row['action_type'] = 'reply'; cr._persist(row)
        from delivery_effects import loop_identity, loop_version
        args.update(loop_identity=loop_identity(row), loop_version=loop_version(row))
    elif change == 'revoked':
        context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'e' * 64, 'google_sources': []}
    else:
        text = 'Hello Dana'
    result = call(request(args=args, text=text), context)
    assert result['status'] == 'rejected'
    assert result['code'] == ('source_unavailable' if change == 'revoked' else 'owner_span_unavailable' if change == 'condition' else 'target_changed')
    assert schedule.current() == []


def test_accepted_conditional_delivery_survives_automatic_loop_close_cancel(context, monkeypatch):
    import ask_research_pipeline
    import importlib.util
    from pathlib import Path
    args, row, cr = _conditional_args(context)
    saved = call(request(args=args, text="Remind me if Dana hasn't replied."), context)
    spec = importlib.util.spec_from_file_location('conditional_acceptance_race_scan',
        Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff/proactive/scripts/proactive_scan.py')
    scan = importlib.util.module_from_spec(spec); spec.loader.exec_module(scan)
    monkeypatch.setattr(ask_research_pipeline, 'current_authority', context[1]['_source_authority'])
    row['status'] = 'resolved'; cr._persist(row)
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    assert scan._intention_candidates(now) == []
    assert schedule.current()[0]['status'] == 'canceled'
    confirmed = schedule.mark_accepted(saved['reminder_id'], now.isoformat())
    assert confirmed['status'] == 'fired' and confirmed['accepted_at'] == now.isoformat()
    before = schedule.current()
    assert schedule.mark_accepted(saved['reminder_id'], now.isoformat()) == confirmed
    assert schedule.current() == before
    # A legacy/unconditional canceled intention retains its existing behavior.
    ordinary = call(request(event='f'*64), context)
    schedule.transition(ordinary['reminder_id'], 'canceled')
    assert schedule.mark_accepted(ordinary['reminder_id'], now.isoformat())['status'] == 'canceled'
