"""Retired correction payloads preserve ordinary ingestion and historical state."""
from pathlib import Path
import pytest
from test_proactive_procedure import rec as rec  # noqa: PLC0414


@pytest.mark.parametrize('candidates', [[], 'malformed', [{'source_ref': 'legacy'}]])
def test_retired_corrections_do_not_touch_records_or_block_events(rec, monkeypatch, candidates):
    root = Path(rec.DATA) / 'knowledge'
    root.mkdir()
    ledger = root / 'source-retractions.json'
    ledger.write_bytes(b'broken legacy ledger')
    seen = []
    monkeypatch.setattr(rec, 'run_triage', lambda events, catchup: seen.extend(events) or {'verdict': 'drop'})
    monkeypatch.setattr(rec, '_knowledge_module', lambda *_: pytest.fail('graph accessed'))
    event = {'source': 'imessage', 'rowid': 99, 'text': 'ordinary event'}
    status, body = rec.handle_events({'events': [event], 'source_corrections': candidates})
    assert status == 200 and seen == [event]
    assert body['source_corrections'] == {'status': 'disabled', 'accepted': 0}
    assert ledger.read_bytes() == b'broken legacy ledger'
    assert not (Path(rec.DATA) / 'events/work.sqlite3').exists()


def test_old_correction_job_never_becomes_a_delivery(rec, monkeypatch):
    monkeypatch.setattr(rec, '_execute_work', lambda *_: pytest.fail('retired work executed'))
    monkeypatch.setattr(rec, '_deliver_text', lambda *_: pytest.fail('retired work delivered'))
    rec.WORK_QUEUE.enqueue(rec.DATA, 'source_correction', {'receipt_id': 'old', 'after': ''}, key='old')
    job = rec.WORK_QUEUE.claim(rec.DATA, 'test-owner')
    rec._work_one(job)
    row = rec.WORK_QUEUE.get(rec.DATA, job['id'])
    assert row['status'] == 'failed'
    assert row['error'] == 'automatic source corrections retired'
    assert row['finished'] is not None
    assert rec.WORK_QUEUE.claim(rec.DATA, 'later-owner', now=rec.time.time() + 120) is None
    assert row['payload'] == {'receipt_id': 'old', 'after': ''}


def test_retired_correction_transition_respects_current_lease(rec):
    rec.WORK_QUEUE.enqueue(rec.DATA, 'source_correction', {'receipt_id': 'old'}, key='old')
    job = rec.WORK_QUEUE.claim(rec.DATA, 'current-owner')
    assert not rec.WORK_QUEUE.fail(rec.DATA, job['id'], 'stale-owner', 'retired')
    assert rec.WORK_QUEUE.get(rec.DATA, job['id'])['status'] == 'leased'
    assert rec.WORK_QUEUE.fail(rec.DATA, job['id'], job['owner'], 'retired') == 'failed'
    assert not rec.WORK_QUEUE.fail(rec.DATA, job['id'], job['owner'], 'retired')
