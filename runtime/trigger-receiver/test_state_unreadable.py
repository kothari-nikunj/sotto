"""Only a missing state file gets the default; an unreadable or corrupt one is never overwritten."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest
from test_proactive_procedure import rec as rec  # noqa: PLC0414 — pytest fixture export

HERE = Path(__file__).parent
SHARED_LIB = HERE.parents[1] / 'sotto-chief-of-staff' / '_shared' / 'lib'


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _outbox_doc():
    return {'rows': [{'id': 'brief-1', 'status': 'pending', 'acceptance': 'in_flight', 'kind': 'brief'}]}


def _outbox(tmp_path, text):
    path = tmp_path / 'events' / 'outbox.json'
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_truncated_state_is_set_aside_and_never_replaced_by_the_default(tmp_path):
    conn = _load('state_conn_a', HERE / 'connectors.py')
    original = json.dumps(_outbox_doc())[:40]
    path = _outbox(tmp_path, original)
    with pytest.raises(conn.StateUnreadableError):
        with conn.json_transaction(str(path), default={'rows': []}):
            pytest.fail('the block must not run over a corrupt file')
    assert not path.exists()
    [aside] = path.parent.glob('outbox.json.corrupt.*')
    assert aside.read_text() == original


def test_unreadable_intact_state_raises_and_keeps_its_bytes_in_place(tmp_path):
    conn = _load('state_conn_b', HERE / 'connectors.py')
    original = json.dumps(_outbox_doc())
    path = _outbox(tmp_path, original)
    os.chmod(path, 0)
    try:
        with pytest.raises(conn.StateUnreadableError):
            with conn.json_transaction(str(path), default={'rows': []}):
                pytest.fail('the block must not run over an unreadable file')
    finally:
        os.chmod(path, 0o600)
    assert path.read_text() == original
    assert not list(path.parent.glob('outbox.json.corrupt.*'))


def test_missing_state_still_gets_the_default(tmp_path):
    conn = _load('state_conn_c', HERE / 'connectors.py')
    path = tmp_path / 'events' / 'outbox.json'
    with conn.json_transaction(str(path), default={'rows': []}) as doc:
        doc['rows'].append({'id': 'x'})
    assert json.loads(path.read_text()) == {'rows': [{'id': 'x'}]}


def _first_deliver(rec):
    with pytest.raises(rec.CONNECTORS.StateUnreadableError):
        rec.OUTBOX.deliver({'label': 'event:x', 'body': 'hello', 'target': 'owner', 'run_id': 'n1'})


def _first_reminder_tick(rec):
    with pytest.raises(rec.CONNECTORS.StateUnreadableError):
        rec._owner_timer_reserved('r1')


def _first_brief_admission(rec):
    with pytest.raises(rec.CONNECTORS.StateUnreadableError):
        with rec.CONNECTORS.json_transaction(rec.OUTBOX.path(), default={}):
            pytest.fail('the block must not run over a corrupt file')


@pytest.mark.parametrize('first_reader', [lambda rec: rec.OUTBOX.drain(), _first_deliver, _first_reminder_tick,
                                          _first_brief_admission])
def test_a_set_aside_outbox_is_recorded_once_whichever_reader_trips_it(rec, tmp_path, monkeypatch, first_reader):
    records = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: records.append(a))
    _outbox(tmp_path, '{"rows": [{"id": "brief-1", "status": "pending", "kind": "brief", "payload": {"body": "B"')
    first_reader(rec)
    for _ in range(3):
        assert rec.OUTBOX.drain() == {'attempted': 0, 'delivered': 0}   # now missing: a fresh, empty outbox
    assert records == [('state:outbox', 'failed', 'delivery records unreadable; set aside')]


def test_a_set_aside_file_that_is_not_the_outbox_adds_no_delivery_record(rec, tmp_path, monkeypatch):
    records = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: records.append(a))
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'onboarding.json').write_text('{"step": ')
    with pytest.raises(rec.CONNECTORS.StateUnreadableError):
        with rec.CONNECTORS.json_transaction(str(tmp_path / 'config' / 'onboarding.json'), default={}):
            pytest.fail('the block must not run over a corrupt file')
    assert records == []


@pytest.mark.parametrize('relative', ['events/outbox.json', 'config/onboarding.json', 'config/settings.json'])
@pytest.mark.parametrize('content', [b'{"rows": [{"id": "x", "note": "caf\xe9"}]}', b'\xff\xfe garbage',
                                     b'{"rows": [', b'["a list, not a document"]'])
def test_unreadable_content_is_set_aside_once_and_the_next_transaction_starts_fresh(tmp_path, relative, content):
    conn = _load('state_conn_e', HERE / 'connectors.py')
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with pytest.raises(conn.StateUnreadableError) as raised:
        with conn.json_transaction(str(path), default={}):
            pytest.fail('the block must not run over a corrupt file')
    assert raised.value.quarantined
    [aside] = path.parent.glob(path.name + '.corrupt.*')
    assert aside.read_bytes() == content
    with conn.json_transaction(str(path), default={}) as data:   # raised once; now a fresh start
        data['fresh'] = True
    assert json.loads(path.read_text()) == {'fresh': True}
    assert len(list(path.parent.glob(path.name + '.corrupt.*'))) == 1


def test_outbox_accepts_a_send_again_after_a_non_utf8_outbox_is_set_aside(tmp_path):
    conn = _load('state_conn_f', HERE / 'connectors.py')
    box = _load('state_outbox_f', HERE / 'outbox.py')
    sends = []

    def send(body, target, key, attempt, **_):
        sends.append(key)
        return True, '', {'acceptance': 'accepted', 'message_id': 'p-' + key}
    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=conn.json_transaction,
                     local_today=lambda: '2026-11-09', valid=lambda p: True, delivery_ready=lambda p: True,
                     brief_gate=lambda *a: box.GATE_SEND, send=send, gallery_receipt=lambda k: None,
                     record=lambda *a, **k: None, on_delivered=lambda p: None, on_terminal=lambda r: True)
    path = tmp_path / 'events' / 'outbox.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"rows": [{"id": "x", "payload": {"body": "caf\xe9"}}]}')
    for _ in range(3):
        assert box.drain() == {'attempted': 0, 'delivered': 0}
    assert len(list(path.parent.glob('outbox.json.corrupt.*'))) == 1
    assert box.deliver({'label': 'owner-reminder:r1', 'body': 'Call the school', 'target': 'owner',
                        'run_id': 'owner-reminder:r1'}) is True
    assert len(sends) == 1   # the new message once; nothing from the set-aside file is sent


def test_shared_jsonstore_transaction_never_replaces_corrupt_preferences(tmp_path):
    sys.path.insert(0, str(SHARED_LIB))
    store = _load('state_jsonstore', SHARED_LIB / 'jsonstore.py')
    path = tmp_path / 'preferences.json'
    path.write_text('{"explicit": {"muted": ["a@example.com"]')
    with pytest.raises(store.Unreadable):
        with store.transaction(str(path), default={}):
            pytest.fail('the block must not run over a corrupt file')
    assert not path.exists()
    [aside] = tmp_path.glob('preferences.json.corrupt.*')
    assert 'a@example.com' in aside.read_text()
    with store.transaction(str(path), default={}) as data:   # a missing file is a fresh start
        data['explicit'] = {}
    assert json.loads(path.read_text()) == {'explicit': {}}
