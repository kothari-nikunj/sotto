"""Research coverage excludes unrelated Contacts facts without trusting bad research."""
import json

from test_prewarm_graph import pw, kg, _local
from test_prep_persist import pp


def test_prewarm_nonassertion_contacts_note_does_not_block_valid_research(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_PREWARM_RESEARCH', raising=False)
    stub = tmp_path / 'research.json'
    stub.write_text(json.dumps({'attendees': [{'email': 'dhruv@acme.com', 'title': 'Eng Lead',
        'company': 'Acme', 'summary': 'Builds infrastructure tools.'}]}))
    monkeypatch.setenv('SOTTO_LLM_STUB', str(stub))
    local = _local()
    local['contacts'][0]['notes'] = '555-0100'
    result = pw.prewarm(local)
    assert result['researched'] == 1
    assert pp.profile_is_fresh('Dhruv', 'dhruv@acme.com')
    path = kg.find_person_file(name='Dhruv', identifier='dhruv@acme.com')
    person = kg.parse_person_file(open(path).read())
    assert not any(fact.text == '555-0100' for fact in person.facts.values())


def test_valid_contacts_fact_cannot_cover_withdrawn_research(tmp_path, monkeypatch):
    from test_prep_persist import ku
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(kg, 'source_invalidated_refs', lambda: {'withdrawn-research'})
    result = ku.apply({'person_updates': [{'person_name': 'Dana Roe', 'identifier': 'dana@acme.com',
        'last_researched': '2026-11-03', 'facts': [
            {'fact': 'Birthday: June 1', 'source': 'apple_contacts', 'confidence': 1.0},
            {'fact': 'Per web search: CEO at Acme', 'source': 'web_research',
             'source_ref': 'withdrawn-research', 'confidence': 0.55}]}]})
    assert result['fact_outcomes']['counts']['rejected'] == 1
    assert result['fact_outcomes']['items'][1]['reason'] == 'withdrawn_source_evidence'
    assert not pp.profile_is_fresh('Dana Roe', 'dana@acme.com')
