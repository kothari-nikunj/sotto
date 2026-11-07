"""Learning receipts distinguish replay from corroboration without copying private facts."""
from datetime import datetime, timezone
import json

import knowledge_update as writer
import knowledge as kg


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_each_fact_reports_added_duplicate_confirmed_or_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    fact = {'fact': 'Enjoys hiking alpine trails', 'source_ref': 'imessage:synthetic-a',
            'confidence': 0.8}
    payload = {'person_updates': [{'person_name': 'Synthetic Person', 'facts': [fact]}]}
    first = writer.apply(payload, now=NOW)['fact_outcomes']
    replay = writer.apply(payload, now=NOW)['fact_outcomes']
    assert first['counts'] == {'added': 1}
    assert replay['counts'] == {'duplicate': 1}
    assert first['items'][0]['digest'] == replay['items'][0]['digest']
    assert first['items'][0]['fact_id'] == replay['items'][0]['fact_id']
    fact['source_ref'] = 'imessage:synthetic-b'
    corroborated = writer.apply(payload, now=NOW)['fact_outcomes']
    assert corroborated['counts'] == {'confirmed': 1}
    assert corroborated['items'][0]['digest'] != first['items'][0]['digest']
    fact['confidence'] = 0.2
    refused = writer.apply(payload, now=NOW)['fact_outcomes']
    assert refused['counts'] == {'rejected': 1}
    assert refused['items'][0]['reason'] == 'low_confidence'
    assert 'alpine' not in json.dumps(first)
    assert 'imessage:' not in json.dumps(first)


def test_bounded_items_keep_exact_totals_for_identity_rejections(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    receipt = writer.apply({'person_updates': [{'facts': [
        {'fact': f'Unbound fact {index}'} for index in range(300)]}]}, now=NOW)['fact_outcomes']
    assert receipt['total'] == 300 and receipt['counts'] == {'rejected': 300}
    assert len(receipt['items']) == 256 and receipt['truncated'] is True
    assert receipt['items'][0]['reason'] == 'missing_person_identity'
    assert receipt['items'][-1]['fact_index'] == 255


def test_stale_replay_is_duplicate_and_new_evidence_revives():
    fact = kg.FactMeta(text='Enjoys hiking', status='archived', source='observed_message',
                       archived_reason='stale', archived_text='Enjoys hiking',
                       source_ref='synthetic', evidence_refs=['synthetic'],
                       first='2026-01-01', last='2026-01-01')
    person = kg.PersonFile(facts={'f_existing': fact})
    before = dict(vars(fact))
    counts = {'new': 0, 'confirmed': 0}
    outcome = writer._apply_fact(person, 'c_synthetic',
                                 {'fact': fact.text, 'source': 'observed_message',
                                  'source_ref': 'synthetic'}, '2026-10-01', counts)
    assert outcome == {'disposition': 'duplicate', 'reason': 'no_new_evidence',
                       'fact_id': 'f_existing'}
    assert vars(fact) == before
    assert counts == {'new': 0, 'confirmed': 0}
    outcome = writer._apply_fact(person, 'c_synthetic',
                                 {'fact': fact.text, 'source': 'observed_message',
                                  'source_ref': 'synthetic-new'}, '2026-10-01', counts)
    assert outcome == {'disposition': 'confirmed', 'reason': 'independent_evidence',
                       'fact_id': 'f_existing'}
    assert fact.status == 'active'
    assert fact.archived_reason is None and fact.archived_text is None
    assert fact.evidence_refs == ['synthetic', 'synthetic-new']
    assert fact.seen == before['seen'] + 1
    assert counts == {'new': 0, 'confirmed': 1}
