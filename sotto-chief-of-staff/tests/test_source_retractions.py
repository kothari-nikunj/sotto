"""Untrusted legacy retraction ledgers are inert while historical retraction is disabled."""
import json
from pathlib import Path
from datetime import datetime, timezone

import pytest
import knowledge as kg
import knowledge_update as writer
import knowledge_query as query

REF = 'imessage:msg:v1:' + 'a' * 24
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize('legacy_ledger', [json.dumps([REF]), 'broken ledger'])
def test_legacy_retraction_ledger_cannot_withdraw_or_block_facts(tmp_path, monkeypatch, legacy_ledger):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    payload = {'person_updates': [{'person_name': 'Synthetic Person', 'facts': [
        {'fact': 'Lives in Paris', 'source_ref': REF, 'confidence': 0.8}]}]}
    result = writer.apply(payload, now=NOW)
    path = Path(kg.people_dir()) / result['person_files'][0]
    ledger = tmp_path / 'knowledge/source-retractions.json'
    ledger.write_text(legacy_ledger)
    before = path.read_bytes()
    assert 'Lives in Paris' in query.pack_person(query._load(path), True, NOW)
    assert path.read_bytes() == before
    payload['person_updates'][0]['facts'] = [
        {'fact': 'Enjoys hiking', 'source_ref': REF, 'confidence': 0.8}]
    receipt = writer.apply(payload, now=NOW)['fact_outcomes']
    assert not any(item.get('reason') == 'source_changed' for item in receipt['items'])
    assert 'Enjoys hiking' in query.pack_person(query._load(path), True, NOW)
    assert ledger.read_text() == legacy_ledger
