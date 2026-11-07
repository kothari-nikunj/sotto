"""Private owner reminder writer and durable intention proof; no provider calls."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
PACK = HERE.parents[1] / 'sotto-chief-of-staff'
sys.path.insert(0, str(PACK / '_shared' / 'lib'))
sys.path.insert(0, str(PACK / '_shared' / 'scripts'))
import owner_loop_capability as capability  # noqa: E402
import schedule_wakeup as schedule  # noqa: E402
import delivery_effects as effects  # noqa: E402

spec = importlib.util.spec_from_file_location('owner_reminder_receiver', HERE / 'runtime_owner_reminder.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
receiver_spec = importlib.util.spec_from_file_location('owner_reminder_http_receiver', HERE / 'receiver.py')
receiver = importlib.util.module_from_spec(receiver_spec)
receiver_spec.loader.exec_module(receiver)

WHEN = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
THREAD = 'imessage:iMessage;-;owner~+15555550123'
ROOT = 'synthetic-owner-reminder-root-' + 'x' * 40
PROJECT = 'synthetic-project'
IDENTITY = {'tenant_id': 'tenant', 'channel': 'photon', 'account_id': 'account',
            'owner_id': '+15555550123', 'line_key': 'primary', 'target': 'photon',
            'route_mode': 'fixed', 'route_id': THREAD,
            'project_fingerprint': hashlib.sha256(('sotto-photon-project-v1:' + PROJECT).encode()).hexdigest()}


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    policy = {'SOTTO_DEPLOYMENT_MODE': 'managed',
              'SOTTO_CRON_DELIVER': 'photon', 'SOTTO_TENANT_ID': 'tenant',
              'PHOTON_HOME_CHANNEL': IDENTITY['owner_id'],
              'PHOTON_ALLOWED_USERS': IDENTITY['owner_id'],
              'PHOTON_PROJECT_ID': PROJECT, 'SOTTO_IMESSAGE_NUMBER': '+15555550123',
              'SOTTO_DATA': str(tmp_path)}
    activation = SimpleNamespace(verified_current_photon_route=lambda _root, _policy:
                                 {'chat_id': 'iMessage;-;owner'})
    return tmp_path, policy, activation


def request(text, operation, event='a' * 64):
    return {'version': 1, 'operation': operation, 'identity': IDENTITY,
            'action_id': hashlib.sha256(json.dumps(['sotto.owner-reminder-command.v1', event],
                                                    separators=(',', ':')).encode()).hexdigest(),
            'event_id': event, 'payload_hash': 'b' * 64,
            'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'text': text,
            'admitted_at_ms': int(WHEN.timestamp() * 1000), 'provider_thread_id': THREAD}


def call(body, context, *, bearer=None):
    root, policy, activation = context
    return runtime.perform(body, bearer=bearer or capability.derive(ROOT, IDENTITY), root=ROOT,
                           policy=policy, data_root=root, capability=capability,
                           activation=activation)


def test_create_replay_survives_fired_and_canceled_terminal_rows(context):
    command = '/reminder at 2026-10-09T01:15:00-07:00 Review the cedar packet'
    body = request(command, 'create')
    first = call(body, context)
    assert first['status'] == 'committed' and first['operation'] == 'create'
    assert re.fullmatch(r'int_[a-f0-9]{32}', first['reminder_id'])
    row = schedule.current()[0]
    assert row['due'] == '2026-10-09T08:15:00Z'
    assert row['owner_create']['event_id'] == body['event_id']
    assert row['owner_create']['payload_hash'] == body['payload_hash']
    assert row['owner_create']['command'] == command
    assert call(request(command, 'inspect'), context) == first
    assert call(body, context) == first
    schedule.mark_accepted(first['reminder_id'], '2026-10-09T08:15:01Z')
    fired = call(body, context)
    assert fired['reminder_id'] == first['reminder_id'] and fired['reminder_status'] == 'fired'
    assert call(request(command, 'inspect'), context) == fired
    assert len(schedule.current()) == 1
    assert call(request('/reminder list', 'list', 'c' * 64), context)['items'] == []

    second = request('/reminder at 2026-10-09T08:20:00Z Review the cedar packet', 'create', 'd' * 64)
    created = call(second, context)
    cancel_command = '/reminder cancel ' + created['reminder_id']
    cancel = request(cancel_command, 'cancel', 'e' * 64)
    canceled = call(cancel, context)
    assert canceled['reminder_status'] == 'canceled'
    assert call(cancel, context) == canceled
    assert call(request(cancel_command, 'inspect', 'e' * 64), context) == canceled
    assert call(second, context)['reminder_id'] == created['reminder_id']
    assert call(second, context)['reminder_status'] == 'canceled'
    assert len(schedule.current()) == 2
    assert schedule.current()[-1]['owner_cancel']['event_id'] == 'e' * 64


def test_distinct_equal_owner_events_and_conflicts(context):
    command = '/reminder at 2026-10-09T08:15:00Z Review the cedar packet'
    first = call(request(command, 'create'), context)
    second = call(request(command, 'create', 'c' * 64), context)
    assert first['reminder_id'] != second['reminder_id']
    assert len(call(request('/reminder list', 'list', 'd' * 64), context)['items']) == 2
    changed = request('/reminder at 2026-10-09T08:30:00Z Another task', 'create')
    assert call(changed, context) == {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
    assert call(request('/reminder cancel int_' + 'f' * 32, 'cancel', 'e' * 64), context) == {
        'version': 1, 'status': 'rejected', 'code': 'target_unavailable'}
    legacy = schedule.create('2026-10-09T08:45:00Z', 'Legacy reminder')
    schedule._write_row(dict(legacy, id='int_' + 'f' * 32))
    assert call(request('/reminder cancel int_' + 'f' * 32, 'cancel', 'f' * 64), context) == {
        'version': 1, 'status': 'rejected', 'code': 'target_not_owner'}
    listed = call(request('/reminder list', 'list', 'e' * 64), context)['items']
    assert {item['id'] for item in listed} == {first['reminder_id'], second['reminder_id']}


def test_auth_route_and_malformed_commands_do_not_write(context):
    good = request('/reminder at 2026-10-09T08:15:00Z Review the cedar packet', 'create')
    with pytest.raises(runtime.UnavailableError):
        call(good, context, bearer='wrong')
    root, policy, activation = context
    changed = dict(good, provider_thread_id='imessage:iMessage;-;other~+15555550123')
    with pytest.raises(runtime.UnavailableError):
        call(changed, context)
    policy['PHOTON_ALLOWED_USERS'] = '+15550000000'
    with pytest.raises(runtime.UnavailableError):
        call(good, context)
    policy['PHOTON_ALLOWED_USERS'] = IDENTITY['owner_id']
    for text in ('/reminder at 2026-10-09T08:15:00 Review it',
                 '/reminder at 2026-10-09T08:15:00Z  Extra space',
                 '/reminder cancel int_' + 'A' * 32,
                 '/reminder list extra'):
        with pytest.raises(ValueError):
            call(request(text, 'create'), context)
    for text in ('/reminder at 2026-10-09T07:59:00Z Already due',
                 '/reminder at 2026-02-30T09:00:00-08:00 Invalid calendar day'):
        assert call(request(text, 'create'), context) == {
            'version': 1, 'status': 'rejected', 'code': 'due_unavailable'}
    assert call(request('/reminder at 2026-10-09T08:15:00Z Review it', 'inspect'), context) == {
        'version': 1, 'status': 'not_found'}
    assert schedule.current() == []


def test_cancel_validation_race_converges_on_confirmed_acceptance(context):
    command = '/reminder at 2026-10-09T08:15:00Z Review the cedar packet'
    created = call(request(command, 'create'), context)
    ident = created['reminder_id']
    guarded = [{'kind': 'eligibility', 'intention_id': ident}]
    assert effects.valid(guarded, WHEN.timestamp())
    canceled = call(request('/reminder cancel ' + ident, 'cancel', 'c' * 64), context)
    assert canceled['reminder_status'] == 'canceled'
    # Cancellation before the delivery check suppresses provider contact.
    contacts = []
    if effects.valid(guarded, WHEN.timestamp()):
        contacts.append('sent')
    assert contacts == []
    # A send already past the check may accept after cancellation. Only its
    # accepted effect can correct status to fired, without another contact.
    assert effects.finalize([{'kind': 'intention', 'id': ident}], {}) is False
    assert schedule.current()[0]['status'] == 'canceled'
    contacts.append('accepted-once')
    assert effects.finalize([{'kind': 'intention', 'id': ident}], {'accepted_at': '2026-10-09T08:15:01Z'})
    assert effects.finalize([{'kind': 'intention', 'id': ident}], {'accepted_at': '2026-10-09T08:15:01Z'})
    row = schedule.current()[0]
    assert contacts == ['accepted-once'] and row['status'] == 'fired'
    assert row['owner_cancel']['event_id'] == 'c' * 64
    assert call(request('/reminder cancel ' + ident, 'inspect', 'c' * 64), context)['reminder_status'] == 'fired'


@contextmanager
def _outbox_transaction(path, default):
    target = Path(path)
    value = json.loads(target.read_text()) if target.exists() else default
    yield value
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))


def test_actual_outbox_send_gate_and_cancel_race(context, monkeypatch):
    box_spec = importlib.util.spec_from_file_location('owner_reminder_outbox', HERE / 'outbox.py')
    box = importlib.util.module_from_spec(box_spec)
    box_spec.loader.exec_module(box)
    root, _, _ = context
    sends = []

    def setup(ident, *, cancel_after_valid):
        def valid(payload):
            result = effects.valid(payload['effects'], time.time())
            if cancel_after_valid and result:
                call(request('/reminder cancel ' + ident, 'cancel', 'd' * 64), context)
            return result
        box.HOOKS.update(data_root=lambda: str(root), json_transaction=_outbox_transaction,
                         local_today=lambda: '2026-10-09', valid=valid,
                         send=lambda *args: (sends.append(args) or
                             (True, '', {'acceptance': 'accepted', 'message_id': 'provider-' + ident})),
                         on_delivered=lambda payload: effects.finalize(
                             payload['effects'], payload.get('receipt') or {}),
                         record=lambda *args, **kwargs: None)
        payload = {'label': 'cron:sotto-proactive', 'body': 'Review the cedar packet',
                   'target': 'owner', 'effects': [{'kind': 'eligibility', 'intention_id': ident},
                                                  {'kind': 'intention', 'id': ident}]}
        box._enqueue(ident, box.KIND_NUDGE, payload, time.time(), '2026-10-09')

    first = call(request('/reminder at 2026-10-09T08:15:00Z Review the cedar packet', 'create'), context)
    setup(first['reminder_id'], cancel_after_valid=False)
    call(request('/reminder cancel ' + first['reminder_id'], 'cancel', 'c' * 64), context)
    assert box._attempt(first['reminder_id']) is False
    assert sends == []
    assert next(row for row in schedule.current() if row['id'] == first['reminder_id'])['status'] == 'canceled'

    second = call(request('/reminder at 2026-10-09T08:20:00Z Check the waiver', 'create', 'e' * 64), context)
    setup(second['reminder_id'], cancel_after_valid=True)
    assert box._attempt(second['reminder_id']) is True
    assert len(sends) == 1 and sends[0][2] == second['reminder_id']
    row = next(row for row in schedule.current() if row['id'] == second['reminder_id'])
    assert row['status'] == 'fired' and row['owner_cancel']['event_id'] == 'd' * 64
    assert row['accepted_at']
    assert box._attempt(second['reminder_id']) is False
    assert len(sends) == 1


def test_legacy_canceled_effect_stays_canceled(context):
    legacy = schedule.create('2026-10-09T08:15:00Z', 'Legacy reminder')
    schedule.transition(legacy['id'], 'canceled')
    assert effects.finalize([{'kind': 'intention', 'id': legacy['id']}], {'accepted_at': 1234})
    row = schedule.current()[0]
    assert row['status'] == 'canceled' and 'accepted_at' not in row


def test_private_http_endpoint_requires_exact_auth_and_body(context, monkeypatch):
    root, policy, activation = context
    for name, value in policy.items():
        if isinstance(value, str):
            monkeypatch.setenv(name, value)
    monkeypatch.setenv('SOTTO_MCP_TOKEN', ROOT)
    monkeypatch.setattr(receiver, 'DATA', str(root))
    monkeypatch.setattr(receiver, 'RUNTIME_ACTIVATION', activation)

    def post(payload, *, peer='127.0.0.1', auth=True, path='/runtime/owner-reminder-intent', ctype='application/json'):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        sent = []
        fake = SimpleNamespace(path=path, client_address=(peer, 1234),
                               headers={'Content-Length': str(len(raw)), 'Content-Type': ctype},
                               rfile=io.BytesIO(raw),
                               _authed=lambda token: auth and token == capability.derive(ROOT, IDENTITY),
                               _send=lambda status, body: sent.append((status, body)))
        receiver.Handler.do_POST(fake)
        return sent[0]

    body = request('/reminder at 2026-10-09T08:15:00Z Review the cedar packet', 'create')
    assert post(body, peer='203.0.113.1')[0] == 401
    assert post(body, auth=False)[0] == 401
    assert post(body, path='/runtime/owner-reminder-intent?x=1')[0] == 404
    assert post(body, ctype='text/plain')[0] == 400
    assert post(b'{' + json.dumps(body)[1:-1].encode() + b',"version":1}')[0] == 400
    code, result = post(body)
    assert code == 200 and result['status'] == 'committed'
    assert post(body) == (200, result)
    assert post(request('/reminder list', 'list', 'c' * 64))[1]['items'][0]['id'] == result['reminder_id']


def test_selfhost_photon_reminder_uses_same_owner_receipt_and_rejects_crossed_profile(context):
    root, policy, activation = context
    policy.update(SOTTO_DEPLOYMENT_MODE='self_host',
                  SOTTO_RUNTIME_PROFILE='self-host-photon-fixed-five-v1',
                  GOOGLE_AI_API_KEY='test-key')
    command = '/reminder at 2026-10-09T08:15:00Z Review the cedar packet'
    body = request(command, 'create')
    committed = call(body, context)
    assert committed['status'] == 'committed'
    assert call(body, context) == committed
    assert call(request(command, 'inspect'), context) == committed
    assert len(schedule.current()) == 1
    policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-telegram-fixed-five-v1'
    with pytest.raises(runtime.UnavailableError):
        call(request(command, 'create', 'c' * 64), context)
    policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-photon-fixed-five-v1'
    policy['SOTTO_MODEL_PROXY_TOKEN'] = 'forbidden'
    with pytest.raises(runtime.UnavailableError):
        call(request(command, 'create', 'd' * 64), context)
    assert len(schedule.current()) == 1


def test_selfhost_first_contact_reminder_requires_current_activation_route(context):
    root, policy, activation = context
    policy.update(SOTTO_DEPLOYMENT_MODE='self_host',
                  SOTTO_RUNTIME_PROFILE='self-host-photon-fixed-five-v1',
                  GOOGLE_AI_API_KEY='test-key')
    identity = dict(IDENTITY, route_mode='first_contact', route_id='+15555550123')
    body = request('/reminder at 2026-10-09T08:15:00Z Review the cedar packet', 'create')
    body['identity'] = identity
    bearer = capability.derive(ROOT, identity)
    current = runtime.perform(body, bearer=bearer, root=ROOT, policy=policy,
                              data_root=root, capability=capability, activation=activation)
    assert current['status'] == 'committed'
    stale = SimpleNamespace(verified_current_photon_route=lambda _root, _policy:
                            {'chat_id': 'iMessage;-;different'})
    with pytest.raises(runtime.UnavailableError):
        runtime.perform(request('/reminder at 2026-10-09T08:20:00Z Review the cedar packet',
                                'create', 'c' * 64) | {'identity': identity},
                        bearer=bearer, root=ROOT, policy=policy, data_root=root,
                        capability=capability, activation=stale)
    assert len(schedule.current()) == 1


def test_selfhost_photon_http_profile_gate_is_closed():
    policy = {'SOTTO_DEPLOYMENT_MODE': 'self_host',
              'SOTTO_RUNTIME_PROFILE': 'self-host-photon-fixed-five-v1',
              'SOTTO_CRON_DELIVER': 'photon', 'GOOGLE_AI_API_KEY': 'direct-key'}
    assert receiver._owner_intents_enabled(policy)
    for mutation in ({'SOTTO_RUNTIME_PROFILE': 'self-host-telegram-fixed-five-v1'},
                     {'SOTTO_CRON_DELIVER': 'telegram'}, {'GOOGLE_AI_API_KEY': ''},
                     {'SOTTO_MODEL_PROXY_URL': 'https://proxy.example'}):
        assert not receiver._owner_intents_enabled({**policy, **mutation})
