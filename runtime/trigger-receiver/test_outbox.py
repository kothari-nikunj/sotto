"""Crash recovery contracts owned directly by the durable delivery outbox."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time


HERE = Path(__file__).parent


def _load_outbox(name='crash_outbox'):
    spec = importlib.util.spec_from_file_location(name, HERE / 'outbox.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _transaction(path, default):
    target = Path(path)
    doc = json.loads(target.read_text()) if target.exists() else default
    yield doc
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc), encoding='utf-8')


def test_identity_aware_text_send_uses_stable_row_id_and_charged_attempt(tmp_path, monkeypatch):
    box = _load_outbox('identity_outbox')
    path = tmp_path / 'events' / 'outbox.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'rows': [{
        'id': 'stable-dispatch', 'kind': box.KIND_NUDGE, 'created_at': time.time(),
        'day': '2026-09-29', 'attempts': 0, 'next_at': 0, 'status': box.STATUS_PENDING,
        'acceptance': 'not_attempted', 'acceptance_uncertain': False,
        'payload': {'label': 'event', 'body': 'hello', 'target': 'owner'},
    }]}))
    calls = []
    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=_transaction,
                     local_today=lambda: '2026-09-29', brief_gate=lambda *_: box.GATE_SEND,
                     send=lambda *args: (calls.append(args) or
                         (False, 'offline', {'acceptance': 'not_attempted'})))
    assert box._attempt('stable-dispatch') is False
    state = json.loads(path.read_text())
    assert state['rows'][0]['attempts'] == 1
    state['rows'][0]['next_at'] = 0
    path.write_text(json.dumps(state))
    assert box._attempt('stable-dispatch') is False
    assert calls == [('hello', 'owner', 'stable-dispatch', 1),
                     ('hello', 'owner', 'stable-dispatch', 2)]


def test_unknown_text_holds_across_restart_while_unrelated_text_delivers(tmp_path, monkeypatch):
    box = _load_outbox('unknown_text_outbox')
    sends = []
    late = {'receipt': None}

    def send(body, target, key, attempt):
        sends.append((key, attempt))
        if key == 'uncertain':
            return False, 'response lost', {'acceptance': 'unknown'}
        return True, '', {'acceptance': 'accepted', 'message_id': 'safe-provider-id'}

    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=_transaction,
                     local_today=lambda: '2026-09-29', valid=lambda payload: True,
                     send=send, gallery_receipt=lambda key: late['receipt'],
                     record=lambda *args, **kwargs: None,
                     on_delivered=lambda payload: None)
    now = time.time()
    box._enqueue('uncertain', box.KIND_NUDGE,
                 {'label': 'first-event', 'body': 'first', 'target': 'owner'}, now, '2026-09-29')
    assert box._attempt('uncertain') is False
    box._enqueue('safe', box.KIND_NUDGE,
                 {'label': 'second-event', 'body': 'second', 'target': 'owner'}, now, '2026-09-29')
    with _transaction(box.path(), {}) as doc:
        doc['rows'][0]['next_at'] = 0
        doc['rows'][0]['payload']['valid_until'] = 0
    assert box.drain()['delivered'] == 1
    assert sends == [('uncertain', 1), ('safe', 1)]

    # Simulate a new receiver process loading the same outbox. Absence of a sidecar receipt
    # cannot prove the provider did not accept the first text.
    restarted = _load_outbox('restarted_unknown_text_outbox')
    restarted.HOOKS.update(box.HOOKS)
    with _transaction(restarted.path(), {}) as doc:
        doc['rows'][0]['next_at'] = 0
    assert restarted._attempt('uncertain') is False
    assert sends == [('uncertain', 1), ('safe', 1)]
    late['receipt'] = {'acceptance': 'accepted', 'message_id': 'late-provider-id'}
    with _transaction(restarted.path(), {}) as doc:
        doc['rows'][0]['next_at'] = 0
    assert restarted._attempt('uncertain') is True
    rows = json.loads(Path(restarted.path()).read_text())['rows']
    assert [(row['id'], row['status']) for row in rows] == [
        ('uncertain', restarted.STATUS_DELIVERED), ('safe', restarted.STATUS_DELIVERED)]
    assert sends == [('uncertain', 1), ('safe', 1)]



def test_process_death_inside_effect_callback_consumes_budget_then_quarantines(tmp_path):
    box = _load_outbox()
    outbox_path = tmp_path / 'events' / 'outbox.json'
    outbox_path.parent.mkdir(parents=True)
    outbox_path.write_text(json.dumps({'rows': [{
        'id': 'accepted-once', 'status': box.STATUS_DELIVERED, 'effects_pending': True,
        'effects_next_at': 0, 'effects_attempts': 0, 'effect_phase': 'accepted',
        'payload': {'label': 'event', 'run_id': 'accepted-once',
                    'effects': [{'kind': 'test', 'anchor_key': 'stable',
                                 'handle': '+15550001111', 'chat_guid': 'iMessage;-;+15550001111'}]},
        'receipt': {'message_id': 'provider-accepted'},
    }]}), encoding='utf-8')

    child = '''
from contextlib import contextmanager
import importlib.util,json,os,pathlib,sys
spec=importlib.util.spec_from_file_location("child_outbox", sys.argv[1])
box=importlib.util.module_from_spec(spec); spec.loader.exec_module(box)
target=pathlib.Path(sys.argv[2])
@contextmanager
def tx(path, default):
    doc=json.loads(target.read_text())
    yield doc
    target.write_text(json.dumps(doc))
box.HOOKS["data_root"]=lambda: str(target.parent.parent)
box.HOOKS["json_transaction"]=tx
box.HOOKS["on_delivered"]=lambda payload: os._exit(73)
box._apply_effects("accepted-once")
'''
    for expected in range(1, box.MAX_EFFECT_ATTEMPTS + 1):
        proc = subprocess.run([sys.executable, '-c', child, str(HERE / 'outbox.py'), str(outbox_path)])
        assert proc.returncode == 73
        doc = json.loads(outbox_path.read_text())
        assert doc['rows'][0]['effects_attempts'] == expected
        doc['rows'][0]['effects_next_at'] = 0
        outbox_path.write_text(json.dumps(doc), encoding='utf-8')

    receipts, callbacks = [], []
    box.HOOKS['data_root'] = lambda: str(tmp_path)
    box.HOOKS['json_transaction'] = _transaction
    box.HOOKS['record'] = lambda label, status, detail='', **kwargs: receipts.append(
        (label, status, detail))
    box.HOOKS['on_delivered'] = lambda payload: callbacks.append(payload)
    assert box._apply_effects('accepted-once') is False

    row = json.loads(outbox_path.read_text())['rows'][0]
    assert callbacks == []
    assert row['effects_attempts'] == box.MAX_EFFECT_ATTEMPTS
    assert row['effects_pending'] is False and row['effects_status'] == 'quarantined'
    assert row['receipt'] == {'message_id': 'provider-accepted'}
    # A quarantined row is terminal: it keeps the diagnosis and drops the addressing, exactly as an
    # applied one does. Seven days of a dead row holding a handle is keeping we cannot justify.
    assert row['payload'] == {'label': 'event', 'run_id': 'accepted-once'}
    assert '15550001111' not in json.dumps(row) and 'stable' not in json.dumps(row)
    assert receipts[-1][0:2] == ('event', box.STATUS_FAILED)
    assert 'delivered, but its follow-up effects failed permanently' in receipts[-1][2]


def test_offer_route_capture_survives_lost_response_and_restart(tmp_path, monkeypatch):
    box = _load_outbox('offer_route_outbox')
    captured = {'route_digest': 'a' * 64, 'chat_id': 'iMessage;-;+14155550100'}
    calls, effects = [], []

    def send(*args):
        calls.append(args)
        row = json.loads(Path(box.path()).read_text())['rows'][0]
        assert row['acceptance'] == 'in_flight'
        assert row['offer_route_at_contact'] == captured
        return False, 'lost response', {'acceptance': 'unknown'}

    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=_transaction,
                     local_today=lambda: '2026-09-29', valid=lambda _: True,
                     offer_route=lambda _: captured, send=send,
                     record=lambda *args, **kwargs: None,
                     on_delivered=lambda payload: effects.append(payload) or True)
    box._enqueue('offer', box.KIND_NUDGE,
                 {'label': 'followup', 'body': 'Want a draft?', 'target': 'photon',
                  'effects': [{'kind': 'pending_offer'}]}, time.time(), '2026-09-29')
    assert box._attempt('offer') is False
    restarted = _load_outbox('offer_route_restarted')
    restarted.HOOKS.update(box.HOOKS)
    # A newly selected route must never rewrite the route used by the original contact.
    restarted.HOOKS['offer_route'] = lambda _: (_ for _ in ()).throw(AssertionError('recaptured'))
    restarted.HOOKS['gallery_receipt'] = lambda _: {
        'acceptance': 'accepted', 'message_id': 'accepted-offer',
        'route_proof_version': 1, 'route_digest': 'a' * 64, 'payload_hash': 'b' * 64}
    with _transaction(restarted.path(), {}) as doc:
        doc['rows'][0]['next_at'] = 0
    assert restarted._attempt('offer') is True
    assert len(calls) == 1 and len(effects) == 1
    assert effects[0]['offer_route_at_contact'] == captured
    assert effects[0]['receipt']['route_digest'] == captured['route_digest']
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['status'] == box.STATUS_DELIVERED and not row['effects_pending']
    assert 'offer_route_at_contact' not in row['payload']


def test_missing_offer_route_refuses_before_provider_contact(tmp_path, monkeypatch):
    box = _load_outbox('missing_offer_route')
    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=_transaction,
                     local_today=lambda: '2026-09-29', valid=lambda _: True,
                     offer_route=lambda _: None,
                     send=lambda *_: (_ for _ in ()).throw(AssertionError('sent')))
    box._enqueue('offer', box.KIND_NUDGE,
                 {'label': 'followup', 'body': 'Want a draft?', 'target': 'photon'},
                 time.time(), '2026-09-29')
    assert box._attempt('offer') is False
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['acceptance'] == 'not_attempted'


def test_offer_capture_does_not_change_routine_payload_on_definitive_retry(tmp_path, monkeypatch):
    box = _load_outbox('routine_offer_route')
    calls = []
    captured = {'route_digest': 'a' * 64, 'chat_id': 'iMessage;-;+14155550100'}

    def send(*args):
        calls.append(args)
        if len(calls) == 1:
            return False, 'not contacted', {'acceptance': 'not_attempted'}
        return True, '', {'acceptance': 'accepted', 'message_id': 'accepted',
                          'route_proof_version': 1, 'route_digest': 'a' * 64}

    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=_transaction,
                     local_today=lambda: '2026-09-29', valid=lambda _: True,
                     offer_route=lambda _: captured, routine_authorized=lambda _: True,
                     send=send, on_delivered=lambda _: True,
                     record=lambda *args, **kwargs: None)
    payload = {'label': 'routine:fresh:fixture', 'body': 'Want a draft?', 'target': 'photon',
               'effects': [{'kind': 'pending_offer'}]}
    payload['routine_handoff_digest'] = box.routine_payload_digest(payload)
    box._enqueue('routine-offer', box.KIND_NUDGE, payload, time.time(), '2026-09-29')
    assert box._attempt('routine-offer') is False
    with _transaction(box.path(), {}) as doc:
        row = doc['rows'][0]
        assert row['payload'] == payload
        assert row['offer_route_at_contact'] == captured
        row['next_at'] = 0
    assert box._attempt('routine-offer') is True
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert len(calls) == 2 and not row['effects_pending']
    assert 'offer_route_at_contact' not in row
    assert 'offer_route_at_contact' not in row['payload']


def test_delivery_readiness_holds_without_attempt_then_accepts_once_and_expiry_still_runs(tmp_path):
    box = _load_outbox('discovery_quiet_outbox')
    ready, sent = [False], []
    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=_transaction,
        local_today=lambda: '2026-10-10', brief_gate=lambda *_: box.GATE_SEND,
        delivery_ready=lambda _: ready[0], on_delivered=lambda _: True,
        send=lambda *args: (sent.append(args) or
            (True, 'accepted', {'acceptance': 'accepted', 'message_id': 'invented-1',
                                'accepted_at': time.time()})))
    payload = {'label': 'onboarding:history-discovery:sotto-welcome-brief',
               'body': 'Invented grounded discovery', 'target': 'owner', 'run_id': 'a' * 32}
    assert box.deliver(payload) is False
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['attempts'] == 0 and sent == []
    ready[0] = True
    assert box._attempt(row['id']) is True
    assert box._attempt(row['id']) is False
    assert len(sent) == 1
    ready[0] = False
    expired = {**payload, 'run_id': 'b' * 32, 'valid_until': time.time() - 1}
    assert box.deliver(expired) is False
    rows = json.loads(Path(box.path()).read_text())['rows']
    assert any(row['status'] == box.STATUS_EXPIRED for row in rows)
    assert len(sent) == 1


def test_proved_background_text_forwards_composition_revision_without_replaying(tmp_path):
    box = _load_outbox('context_text_outbox')
    sent = []
    def send(body, target, key, attempt, **context):
        sent.append((body, context))
        return True, '', {'acceptance':'accepted','message_id':'pulse-accepted'}
    box.HOOKS.update(data_root=lambda:str(tmp_path), json_transaction=_transaction,
        local_today=lambda:'2026-10-13', valid=lambda payload:True, brief_gate=lambda *_:box.GATE_SEND,
        send=send, on_delivered=lambda payload:None)
    payload = {'label':'skill:'+'a'*64+':relationship-pulse','body':'I can draft a reply to Jackson Stokes.',
        'target':'owner','prep_source_revision':'b'*64,'prep_route_digest':'c'*64}
    box._enqueue('pulse-proof', box.KIND_NUDGE, payload, time.time(), '2026-10-13')
    assert box._attempt('pulse-proof') is True
    assert sent == [(payload['body'], {'source_revision':'b'*64})]
    box._attempt('pulse-proof')
    assert len(sent) == 1
