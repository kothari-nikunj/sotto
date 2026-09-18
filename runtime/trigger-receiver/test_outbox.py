"""Crash recovery contracts owned directly by the durable delivery outbox."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


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
