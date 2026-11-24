"""Onboarding discovery shows only facts that belong to the person."""
import json

import test_onboarding as base


def test_discovery_never_offers_a_wrong_person_fact_from_a_conflict_pair(tmp_path, monkeypatch):
    pack, _ = base._saved_discovery(tmp_path, monkeypatch)
    import knowledge as kg
    from dataclasses import replace
    path = tmp_path / 'knowledge/people/z.md'
    zara = kg.parse_person_file(path.read_text())
    zara.facts['f_2'] = replace(zara.facts['f_1'], text='Sam Lee is the CEO of Blockworks.')
    kg.write_person_file(str(path), zara)
    kg.write_person_file(str(path.parent / 'sam.md'), kg.PersonFile(canonical_id='sam', name='Sam Lee'))
    (tmp_path / 'knowledge/conflicts.json').write_text(json.dumps(
        {'people': {'z': {'pairs': [['f_1', 'f_2']]}}}))
    rows = base.onboarding.discovery_context(tmp_path, pack, lambda _: True)['observations']
    assert not [row for row in rows if row['fact'].startswith('Sam Lee')]


def test_discovery_keeps_an_eligible_copy_if_the_newest_copy_is_unavailable(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import date, timedelta

    pack, _ = base._saved_discovery(tmp_path, monkeypatch)
    import knowledge as kg
    path = tmp_path / 'knowledge/people/z.md'
    zara = kg.parse_person_file(path.read_text())
    old = (date.fromisoformat(zara.facts['f_1'].last) - timedelta(days=1)).isoformat()
    zara.facts['f_1'] = replace(zara.facts['f_1'], last=old, first=old,
                                source_ref='gmail:invented-1', evidence_refs=['gmail:invented-1'])
    zara.facts['f_2'] = replace(zara.facts['f_1'], last=(date.fromisoformat(old) + timedelta(days=1)).isoformat(),
                                source_ref='imessage:invented-2', evidence_refs=['imessage:invented-2'])
    kg.write_person_file(str(path), zara)
    permitted = base.onboarding.discovery_context(tmp_path, pack, lambda source: source == 'gmail')
    assert permitted is not None
    assert [row['source_refs'] for row in permitted['observations'] if row['canonical_id'] == 'z'] == [
        ['gmail:invented-1']]
    all_sources = base.onboarding.discovery_context(tmp_path, pack, lambda _: True)
    assert [row['source_refs'] for row in all_sources['observations'] if row['canonical_id'] == 'z'] == [
        ['imessage:invented-2']]
