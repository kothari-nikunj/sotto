"""A send whose acceptance stays unknown ends, tells the owner once, and never sends twice.

A receipt that arrives within a day of that close is still recorded; it never causes a send."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import time

HERE = Path(__file__).parent
TODAY, TOMORROW = '2026-11-09', '2026-11-10'


@contextmanager
def _transaction(path, default):
    target = Path(path)
    doc = json.loads(target.read_text()) if target.exists() else default
    yield doc
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc), encoding='utf-8')


def _box(tmp_path, name, receipt, label='cron:sotto-morning-brief'):
    spec = importlib.util.spec_from_file_location(name, HERE / 'outbox.py')
    box = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(box)
    state = {'today': TODAY, 'receipt': receipt, 'sends': [], 'records': []}

    def send(body, target, key, attempt, **_):
        state['sends'].append((key, attempt, body))
        if key == 'run-1' and attempt == 1:
            return False, 'chat transport acceptance unconfirmed', {'acceptance': 'unknown'}
        return True, '', {'acceptance': 'accepted', 'message_id': 'provider-' + key}

    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=_transaction,
                     local_today=lambda: state['today'], valid=lambda payload: True,
                     delivery_ready=lambda payload: True, brief_gate=lambda *a: box.GATE_SEND,
                     send=send, gallery_receipt=lambda key: state['receipt'],
                     record=lambda *a, **k: state['records'].append(a),
                     on_delivered=lambda payload: None, on_terminal=lambda row: True)
    box._enqueue('run-1', box.kind_for(label), {'label': label, 'body': 'Morning brief',
                                                'target': 'owner', 'run_id': 'run-1'},
                 time.time(), TODAY)
    assert box._attempt('run-1') is False
    return box, state


def _edit(box, **fields):
    with _transaction(box.path(), {}) as doc:
        for row in doc['rows']:
            row['next_at'] = 0
            for key, value in fields.items():
                if row['id'] == 'run-1':
                    row[key] = value(row) if callable(value) else value


def _rows(box):
    return {row['id']: row for row in json.loads(Path(box.path()).read_text())['rows']}


def _past_grace(box):
    _edit(box, window_ended_at=lambda row: time.time() - box.UNCONFIRMED_GRACE_SECS - 1)


def test_unknown_brief_closes_after_window_and_grace_with_one_owner_notice(tmp_path):
    box, state = _box(tmp_path, 'unconfirmed_a', {'acceptance': 'unknown'})
    _edit(box)
    box.drain()
    assert _rows(box)['run-1']['status'] == box.STATUS_PENDING   # still inside its window
    state['today'] = TOMORROW
    _edit(box)
    box.drain()
    assert _rows(box)['run-1']['status'] == box.STATUS_PENDING   # window over, grace running
    _past_grace(box)
    box.drain()
    rows = _rows(box)
    assert rows['run-1']['status'] == box.STATUS_FAILED
    assert rows['run-1']['last_error'] == box.UNCONFIRMED_DETAIL
    assert 'body' not in rows['run-1']['payload'] and not rows['run-1'].get('effects_pending')
    assert (state['records'][-1][1], state['records'][-1][2]) == (box.STATUS_FAILED, box.UNCONFIRMED_DETAIL)
    notices = [row for row in rows.values() if row['id'] != 'run-1']
    assert len(notices) == 1 and notices[0]['payload']['target'] == 'owner'
    assert notices[0]['payload']['body'] == box.unconfirmed_notice_body('morning')
    for _ in range(3):
        _edit(box)
        box.drain()
    # The brief itself was sent exactly once; the notice exactly once.
    assert [(key, attempt) for key, attempt, _ in state['sends']] == [('run-1', 1), (notices[0]['id'], 1)]
    assert _rows(box)[notices[0]['id']]['status'] == box.STATUS_DELIVERED


def test_not_found_counts_the_same_once_the_window_is_over(tmp_path):
    box, state = _box(tmp_path, 'unconfirmed_b', None)
    state['today'] = TOMORROW
    _edit(box)
    box.drain()
    _past_grace(box)
    box.drain()
    assert _rows(box)['run-1']['status'] == box.STATUS_FAILED
    assert [key for key, *_ in state['sends']] == ['run-1']


def test_timeout_then_late_receipt_inside_grace_delivers_without_notice_or_resend(tmp_path):
    box, state = _box(tmp_path, 'unconfirmed_c', {'acceptance': 'unknown'})
    state['today'] = TOMORROW
    _edit(box)
    box.drain()
    state['receipt'] = {'acceptance': 'accepted', 'message_id': 'late-provider-id'}
    _edit(box)
    box.drain()
    rows = _rows(box)
    assert rows['run-1']['status'] == box.STATUS_DELIVERED
    assert list(rows) == ['run-1']
    assert [key for key, *_ in state['sends']] == ['run-1']


def _closed(tmp_path, name):
    box, state = _box(tmp_path, name, {'acceptance': 'unknown'})
    state['today'] = TOMORROW
    _edit(box)
    box.drain()
    _past_grace(box)
    box.drain()
    assert _rows(box)['run-1']['status'] == box.STATUS_FAILED
    return box, state


def test_receipt_after_close_records_delivery_withdraws_notice_and_sends_nothing(tmp_path):
    box, state = _closed(tmp_path, 'unconfirmed_d')
    state['receipt'] = {'acceptance': 'accepted', 'message_id': 'late-provider-id'}
    for _ in range(3):
        _edit(box)
        box.drain()
    rows = _rows(box)
    brief = rows['run-1']
    assert brief['status'] == box.STATUS_DELIVERED and brief['acceptance'] == 'accepted'
    assert brief['receipt']['message_id'] == 'late-provider-id' and brief['receipt']['late'] is True
    assert 'body' not in brief['payload'] and brief['effects_status'] == 'applied'
    (notice,) = [row for row in rows.values() if row['id'] != 'run-1']
    assert notice['status'] == box.STATUS_SUPERSEDED   # the "could not confirm" notice is withdrawn
    assert [key for key, *_ in state['sends']] == ['run-1']
    assert any(record[1] == box.STATUS_DELIVERED for record in state['records'])


def test_receipt_after_the_late_window_changes_nothing_and_sends_nothing(tmp_path):
    box, state = _closed(tmp_path, 'unconfirmed_j')
    _edit(box, late_until=time.time() - 1)
    state['receipt'] = {'acceptance': 'accepted', 'message_id': 'too-late'}
    for _ in range(3):
        _edit(box)
        box.drain()
    rows = _rows(box)
    assert rows['run-1']['status'] == box.STATUS_FAILED
    assert len(rows) == 2  # noqa: PLR2004 — the brief and its one notice
    assert [key for key, *_ in state['sends']].count('run-1') == 1


def test_late_proof_of_no_send_reopens_terminal_bookkeeping_without_sending(tmp_path):
    box, state = _closed(tmp_path, 'unconfirmed_k')
    terminal = []
    box.HOOKS['on_terminal'] = lambda row: terminal.append(dict(row)) or True
    state['receipt'] = {'acceptance': 'rejected'}
    _edit(box)
    box.drain()
    brief = _rows(box)['run-1']
    assert brief['status'] == box.STATUS_FAILED and brief['acceptance'] == 'rejected'
    assert not brief['acceptance_uncertain'] and 'late_until' not in brief
    _edit(box)
    box.drain()
    # Seen once while uncertain (a brief keeps its day), then again with the definite answer.
    assert [row['acceptance'] for row in terminal if row['id'] == 'run-1'] == ['unknown', 'rejected']
    assert [key for key, *_ in state['sends']].count('run-1') == 1


def test_late_not_found_after_close_is_not_proof_of_no_send(tmp_path):
    """The runtime prunes and can lose dispatch records; absence hours later keeps the day."""
    box, state = _closed(tmp_path, 'unconfirmed_l')
    terminal = []
    box.HOOKS['on_terminal'] = lambda row: terminal.append(dict(row)) or True
    state['receipt'] = {'acceptance': 'not_found'}
    for _ in range(3):
        _edit(box, contacted_at=time.time() - box.NOT_FOUND_AFTER_SECS * 100)
        box.drain()
    brief = _rows(box)['run-1']
    assert brief['status'] == box.STATUS_FAILED and brief['acceptance'] == 'unknown'
    assert brief['acceptance_uncertain'] and brief['late_until'] > time.time()
    assert brief['terminal_reconciled'] is True
    assert [row['acceptance'] for row in terminal if row['id'] == 'run-1'] in ([], ['unknown'])
    assert [key for key, *_ in state['sends']].count('run-1') == 1


def test_restart_mid_send_retries_only_when_node_never_saw_the_dispatch(tmp_path):
    box, state = _box(tmp_path, 'unconfirmed_e', {'acceptance': 'not_found'})
    # Crash between _begin_send and Node: the row is in_flight and Node has no dispatch.
    _edit(box, acceptance='in_flight', contacted_at=time.time())
    box.drain()
    assert [key for key, *_ in state['sends']] == ['run-1']      # too soon to prove absence
    _edit(box, contacted_at=time.time() - box.NOT_FOUND_AFTER_SECS - 1)
    box.drain()
    assert _rows(box)['run-1']['status'] == box.STATUS_DELIVERED
    assert [(key, attempt) for key, attempt, _ in state['sends']] == [('run-1', 1), ('run-1', 3)]


def test_restart_mid_send_holds_when_node_recorded_the_dispatch_or_lookup_failed(tmp_path):
    for name, receipt in (('unconfirmed_f', {'acceptance': 'unknown'}), ('unconfirmed_g', None)):
        box, state = _box(tmp_path / name, name, receipt)
        _edit(box, acceptance='in_flight', contacted_at=time.time() - box.NOT_FOUND_AFTER_SECS - 1)
        for _ in range(3):
            _edit(box)
            box.drain()
        assert _rows(box)['run-1']['status'] == box.STATUS_PENDING
        assert [key for key, *_ in state['sends']] == ['run-1']


def test_requested_brief_closes_without_a_second_notice(tmp_path):
    label = 'requested:' + 'a' * 32 + ':sotto-morning-brief'
    box, state = _box(tmp_path, 'unconfirmed_h', {'acceptance': 'unknown'}, label=label)
    state['today'] = TOMORROW
    _edit(box)
    box.drain()
    _past_grace(box)
    box.drain()
    rows = _rows(box)
    assert rows['run-1']['status'] == box.STATUS_FAILED and list(rows) == ['run-1']


def test_owner_reminder_rows_keep_their_own_reconciliation(tmp_path):
    box, state = _box(tmp_path, 'unconfirmed_i', {'acceptance': 'unknown'}, label='owner-reminder:int_' + 'a' * 32)
    _edit(box, created_at=0, window_ended_at=0)
    box.drain()
    assert _rows(box)['run-1']['status'] == box.STATUS_PENDING
