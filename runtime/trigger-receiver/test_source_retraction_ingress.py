"""Legacy retraction payloads must never mutate facts or obstruct ordinary events."""
from pathlib import Path

import pytest
from test_proactive_procedure import rec as rec  # noqa: PLC0414 — fixture export


@pytest.mark.parametrize('changes', [
    [{'source': source, 'refs': ['msg:v1:' + 'a' * 24]}]
    for source in ('imessage', 'whatsapp')
] + [None, {'malformed': True}])
def test_disabled_retractions_are_acknowledged_without_graph_access(rec, monkeypatch, changes):
    root = Path(rec.DATA) / 'knowledge'
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / 'source-retractions.json'
    ledger.write_text('broken legacy ledger')
    people = root / 'people'
    people.mkdir()
    person = people / 'c_abcdef.md'
    person.write_text('---\ncanonical_id: c_abcdef\nname: Synthetic Person\nfacts:\n  f1:\n'
                      '    text: Lives in Paris\n    status: active\n    source: brief_extraction\n'
                      '    source_ref: imessage:msg:v1:' + 'a' * 24 + '\n---\n')
    before = {p: p.read_bytes() for p in (ledger, person)}
    monkeypatch.setattr(rec, '_load_shared_lib', lambda *_: pytest.fail('retraction module loaded'))
    monkeypatch.setattr(rec, 'run_triage', lambda *_: pytest.fail('empty events triaged'))
    body = {'events': [], 'source_retractions': changes}
    for _ in range(2):
        status, reply = rec.handle_events(body)
        assert status == 200
        assert reply['source_retractions'] == {'status': 'disabled', 'applied': 0}
    assert {p: p.read_bytes() for p in before} == before


def test_disabled_retractions_do_not_block_or_replay_new_events(rec, monkeypatch):
    seen = []
    monkeypatch.setattr(rec, 'run_triage', lambda events, catchup: (
        seen.extend(events) or {'verdict': 'drop', 'reason': 'fixture'}))
    body = {'events': [{'source': 'imessage', 'rowid': 42, 'text': 'New ask'}],
            'source_retractions': [{'source': 'whatsapp', 'refs': ['msg:v1:' + 'b' * 24]}]}
    for _ in range(2):
        status, reply = rec.handle_events(body)
        assert status == 200
        assert reply['source_retractions'] == {'status': 'disabled', 'applied': 0}
    assert seen == body['events']
    assert not (Path(rec.DATA) / 'knowledge/source-retractions.json').exists()


def test_disabled_retraction_metadata_does_not_expand_source_consent(rec, monkeypatch):
    import managed
    monkeypatch.setattr(managed, 'enabled', lambda: True)
    monkeypatch.setattr(managed, 'read_state', lambda *_: {
        'sources': {'imessage': {'consented': True}}})
    managed.validate_local_ingress(rec.DATA, {
        'events': [{'source': 'imessage'}], 'source_retractions': [{'source': 'whatsapp'}]}, events=True)
    with pytest.raises(ValueError):
        managed.validate_local_ingress(rec.DATA, {
            'events': [{'source': 'whatsapp'}], 'source_retractions': []}, events=True)
