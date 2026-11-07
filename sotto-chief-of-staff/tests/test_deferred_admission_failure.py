import pytest
import test_event_triage as fixtures


def test_deferred_admission_propagates_storage_failure_and_can_retry(tmp_path, monkeypatch):
    import deferred_queue
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    fixtures._seed_snapshot(tmp_path)
    fixtures._no_llm(monkeypatch)
    original = deferred_queue.bounded_append
    def fail(*args, **kwargs):
        raise OSError("disk full")
    event = {"events": [fixtures._im("on it", is_from_me=True)]}
    monkeypatch.setattr(deferred_queue, 'bounded_append', fail)
    with pytest.raises(OSError, match="disk full"):
        fixtures.te.triage(event, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    monkeypatch.setattr(deferred_queue, 'bounded_append', original)
    assert fixtures.te.triage(event, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)['verdict'] == 'queue'
    assert len(fixtures._queue_entries(tmp_path)) == 1


def test_partial_deferred_batch_retry_preserves_each_event_once(tmp_path, monkeypatch):
    import deferred_queue
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    fixtures._seed_snapshot(tmp_path)
    fixtures._no_llm(monkeypatch)
    original = deferred_queue.json.dumps
    def fail_second(value, *args, **kwargs):
        if isinstance(value, dict) and value.get('verdict_class') and value['event']['rowid'] == payload['events'][-1]['rowid']:
            raise OSError('disk full')
        return original(value, *args, **kwargs)
    payload = {'events': [fixtures._im('on it', is_from_me=True, rowid=n) for n in (1, 2)]}
    monkeypatch.setattr(deferred_queue.json, 'dumps', fail_second)
    with pytest.raises(OSError, match='disk full'):
        fixtures.te.triage(payload, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    assert fixtures._queue_entries(tmp_path) == []
    assert fixtures._surfaced_entries(tmp_path) == []
    monkeypatch.setattr(deferred_queue.json, 'dumps', original)
    fixtures.te.triage(payload, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    assert [row['event']['rowid'] for row in fixtures._queue_entries(tmp_path)] == [1, 2]


def test_deferred_retry_recognizes_ordinary_preupgrade_jsonl(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    fixtures._seed_snapshot(tmp_path)
    fixtures._no_llm(monkeypatch)
    event = fixtures._im('on it', is_from_me=True)
    path = tmp_path / 'events' / 'queue.jsonl'
    path.parent.mkdir(exist_ok=True)
    original = json.dumps({'ts': '2026-08-06T09:00:00Z', 'verdict_class': 'signal',
                           'sender': 'Alice', 'event': event}) + '\n'
    path.write_text(original)
    fixtures.te.triage({'events': [event, fixtures._im('on it', is_from_me=True, rowid=2)]},
                       now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    assert path.read_text().startswith(original)
    assert [row['event']['rowid'] for row in fixtures._queue_entries(tmp_path)] == [1, 2]


def test_budget_demotion_and_ordinary_queue_share_one_commit(tmp_path, monkeypatch):
    import deferred_queue
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    fixtures._seed_snapshot(tmp_path)
    monkeypatch.setattr(fixtures.te, 'classify_event', lambda e, ctx:
                        ('queue', 'signal', 'first', 'Sarah') if e['rowid'] == 1
                        else ('agent', 'actionable', 'second', 'Sarah'))
    def reject_bundle(*args):
        assert fixtures._queue_entries(tmp_path) == []
    monkeypatch.setattr(fixtures.te, '_accept_bundle', reject_bundle)
    original = deferred_queue.json.dumps
    def fail_second(value, *args, **kwargs):
        if isinstance(value, dict) and value.get('verdict_class') == 'budget':
            raise OSError('disk full')
        return original(value, *args, **kwargs)
    payload = {'events': [fixtures._im(rowid=n) for n in (1, 2)]}
    monkeypatch.setattr(deferred_queue.json, 'dumps', fail_second)
    with pytest.raises(OSError, match='disk full'):
        fixtures.te.triage(payload, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    assert fixtures._queue_entries(tmp_path) == []
    assert fixtures._surfaced_entries(tmp_path) == []
    monkeypatch.setattr(deferred_queue.json, 'dumps', original)
    fixtures.te.triage(payload, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    assert [row['verdict_class'] for row in fixtures._queue_entries(tmp_path)] == ['signal', 'budget']


@pytest.mark.parametrize('count', [1, 2])
def test_deferred_append_separates_unterminated_old_bytes(tmp_path, monkeypatch, count):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    fixtures._seed_snapshot(tmp_path)
    fixtures._no_llm(monkeypatch)
    path = tmp_path / 'events' / 'queue.jsonl'
    path.parent.mkdir(exist_ok=True)
    path.write_text('{corrupt old row')
    fixtures.te.triage({'events': [fixtures._im('on it', is_from_me=True, rowid=n)
                                  for n in range(count)]},
                       now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    import json
    lines = path.read_text().splitlines()
    assert lines[0] == '{corrupt old row'
    assert [json.loads(line)['event']['rowid'] for line in lines[1:]] == list(range(count))


def test_accepted_agent_receipt_survives_mixed_queue_failure(tmp_path, monkeypatch):
    import deferred_queue
    import work_queue
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    fixtures._seed_snapshot(tmp_path)
    monkeypatch.setattr(fixtures.te, 'classify_event', lambda e, ctx:
                        ('agent', 'actionable', 'first', 'Sarah') if e['rowid'] == 1
                        else ('queue', 'signal', 'second', 'Sarah'))
    payload = {'events': [fixtures._im(rowid=n) for n in (1, 2)]}
    original = deferred_queue.bounded_append
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(deferred_queue, 'bounded_append', fail)
    with pytest.raises(OSError, match='disk full'):
        fixtures.te.triage(payload, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    keys = [work_queue.event_item_key(event) for event in payload['events']]
    assert set(work_queue.owned_items(tmp_path, 'event', keys)) == {keys[0]}
    assert [(row['verdict'], row['item_key']) for row in fixtures._surfaced_entries(tmp_path)] == [
        ('agent', keys[0])]
    monkeypatch.setattr(deferred_queue, 'bounded_append', original)
    fixtures.te.triage(payload, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    assert [(row['verdict'], row['item_key']) for row in fixtures._surfaced_entries(tmp_path)] == [
        ('agent', keys[0]), ('queue', keys[1])]
    assert [row['event']['rowid'] for row in fixtures._queue_entries(tmp_path)] == [2]


def test_batch_replace_failure_preserves_prior_crlf_and_cleans_temp(tmp_path, monkeypatch):
    import deferred_queue
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    fixtures._seed_snapshot(tmp_path)
    fixtures._no_llm(monkeypatch)
    path = tmp_path / 'events' / 'queue.jsonl'
    path.parent.mkdir(exist_ok=True)
    before = b'{"retained":"ordinary old text"}\r\n{malformed old text\r\n'
    path.write_bytes(before)
    original = deferred_queue.os.replace
    def fail(source, target):
        if str(target) == str(path):
            raise OSError('replace failed')
        return original(source, target)
    monkeypatch.setattr(deferred_queue.os, 'replace', fail)
    payload = {'events': [fixtures._im('on it', is_from_me=True, rowid=n) for n in (1, 2)]}
    with pytest.raises(OSError, match='replace failed'):
        fixtures.te.triage(payload, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    assert path.read_bytes() == before
    assert list(path.parent.glob('.queue-*')) == []
    assert fixtures._surfaced_entries(tmp_path) == []
    monkeypatch.setattr(deferred_queue.os, 'replace', original)
    fixtures.te.triage(payload, now_local=fixtures.DAY, now_utc=fixtures.NOW_UTC)
    assert path.read_bytes().startswith(before)
    assert list(path.parent.glob('.queue-*')) == []
