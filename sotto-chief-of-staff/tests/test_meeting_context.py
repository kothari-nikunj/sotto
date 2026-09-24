import json

import meeting_context as context


def test_meeting_evidence_folded_into_existing_email_loop_stays_linked(monkeypatch):
    monkeypatch.setattr(context, 'allowed', lambda source: True)
    row = {'source_thread_id': 'email:thread', 'source_snippet': 'An older email quote',
           'source_refs': [{'source': 'granola', 'source_id': 'meeting-1',
                            'snippet': 'Dana: I will send the deck.'}],
           'ask': 'send the deck', 'action_type': 'waiting_on', 'status': 'open'}
    result = context.obligations('meeting-1', [row])
    assert len(result) == 1
    assert result[0]['direction'] == 'they_owe_you'
    assert result[0]['source_snippet'] == 'Dana: I will send the deck.'
    assert context.obligations('meeting-2', [row]) == []
    assert context.obligations('meeting', [row]) == []


def test_reopened_row_does_not_inherit_old_completion_claim(monkeypatch):
    monkeypatch.setattr(context, 'allowed', lambda source: True)
    row = {'source_thread_id': 'granola:m:ask', 'status': 'open', 'resolution': 'user_resolved'}
    assert context.obligations('m', [row])[0]['completion_basis'] == 'ledger_state'


def test_exact_occurrence_keeps_directions_and_distinguishes_closure_basis(monkeypatch):
    monkeypatch.setattr(context, 'allowed', lambda source: True)
    def row(key, **extra):
        return {'source_thread_id': f'granola:{key}:item', 'ask': 'send deck', 'status': 'open',
                'action_type': 'follow_up', **extra}
    rows = [row('m'), row('m', action_type='waiting_on'),
            row('m', status='resolved', resolution='replied', resolution_evidence='Here is the deck',
                resolution_source_refs=[{'sourceType': 'email', 'sourceId': 'sent-1'}]),
            row('m', status='dismissed', resolution='user_dismissed'), row('different')]
    result = context.obligations('m', rows)
    assert len(result) == 4
    assert {r['direction'] for r in result} == {'you_owe', 'they_owe_you'}
    assert next(r for r in result if r['status'] == 'resolved')['completion_basis'] == 'source_evidence'
    assert next(r for r in result if r['status'] == 'dismissed')['completion_basis'] == 'ledger_state'
    monkeypatch.setattr(context, 'allowed', lambda source: source == 'granola')
    assert all(r['resolution_evidence'] is None for r in context.obligations('m', rows))
    monkeypatch.setattr(context, 'allowed', lambda source: False)
    assert context.obligations('m', rows) == []


def test_read_only_missing_window_is_not_no_history(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(context, 'allowed', lambda source: True)
    notes = {'meetings': [{'meeting_id': 'm', 'title': 'Sync', 'your_notes': 'Decided to wait.'}]}
    result = context.query('m', notes)
    assert result['notes']['your_notes'] == 'Decided to wait.'
    assert result['obligations'] == [] and 'does not establish' in result['coverage']
    assert context.query('missing', notes)['status'] == 'not_found'
    assert list(tmp_path.rglob('*')) == []
    monkeypatch.setattr(context, 'allowed', lambda source: False)
    assert 'Decided' not in json.dumps(context.query('m', notes))
