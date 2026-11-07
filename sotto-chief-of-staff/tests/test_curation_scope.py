"""Offline writer contracts for bounded, versioned curation windows."""
import hashlib
import json
from datetime import datetime, timezone

import pytest

import knowledge as kg
import knowledge_update as ku

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
CID = 'c_123456789abc'


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))


def person(*texts, summary_refs=None, summary='', updated_by=''):
    p = kg.PersonFile(canonical_id=CID, name='Alex',
                      facts={f'f{i}': kg.FactMeta(text=text, source='gmail', source_ref=f'event:{i}')
                             for i, text in enumerate(texts)},
                      summary_refs=summary_refs or [], summary=summary, updated_by=updated_by)
    path = kg.safe_path(kg.people_dir(), CID)
    kg.write_person_file(path, p, NOW)
    return path, p


def digest(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


def versions(p, *ids):
    return {fid: ku.curation_fact_version(p.facts[fid]) for fid in ids}


def loaded(path):
    return kg.parse_person_file(open(path, encoding='utf-8').read())


def conflicts():
    import jsonstore
    return jsonstore.read(f'{kg.data_root()}/knowledge/conflicts.json', default={'people': {}})['people']


def test_fact_version_tracks_authority_without_bookkeeping():
    fact = kg.FactMeta(text='A fact', source='gmail', source_ref='event:one',
                       evidence_refs=['gmail:one'], first='2026-01-01', last='2026-01-02')
    original = ku.curation_fact_version(fact)
    fact.seen, fact.conf, fact.first, fact.last = 9, .5, '2026-06-01', '2026-06-02'
    assert ku.curation_fact_version(fact) == original
    fact.evidence_refs.append('gmail:two')
    assert ku.curation_fact_version(fact) != original
    changed = ku.curation_fact_version(fact)
    fact.source = 'user_edit'
    assert ku.curation_fact_version(fact) != changed


def test_scoped_sweeps_leave_unsupplied_tail_and_partial_summary_intact():
    path, p = person('Same assertion.', 'Same assertion.', 'Same assertion.', '123456',
                     summary_refs=['f2'], summary='Same assertion.')
    p.facts['f3'].source = 'apple_contacts'
    kg.write_person_file(path, p, NOW)
    result = ku.consolidate(CID, digest(path), ['f0'], [], NOW,
                            supplied_versions=versions(p, 'f0', 'f1'))
    assert result['status'] == 'ok'
    after = loaded(path)
    assert after.facts['f1'].status == 'archived'
    assert after.facts['f2'].status == 'active' and after.facts['f3'].status == 'active'
    assert after.summary_refs == ['f0']
    assert after.facts['f2'].text == 'Same assertion.'
    assert set(result['committed_versions']) == {'f0', 'f1'}


def test_conflict_pair_survives_other_window_then_can_be_cleared():
    path, p = person('A.', 'B.', 'C.', 'D.')
    assert ku.consolidate(CID, digest(path), [], [['f0', 'f1']], NOW,
                          supplied_versions=versions(p, 'f0', 'f1'))['status'] == 'ok'
    current = loaded(path)
    assert ku.consolidate(CID, digest(path), [], [], NOW,
                          supplied_versions=versions(current, 'f2', 'f3'))['status'] == 'ok'
    assert conflicts()[CID]['pairs'] == [['f0', 'f1']]
    current = loaded(path)
    assert ku.consolidate(CID, digest(path), [], [], NOW,
                          supplied_versions=versions(current, 'f0', 'f1'))['status'] == 'ok'
    assert CID not in conflicts()


def test_full_person_conflict_store_keeps_new_findings_and_evicts_oldest():
    """Three stored pairs plus a new finding used to refuse forever ('conflict_capacity'): the
    summary and dedupe were never written and the work parked. The newest review wins room."""
    path, p = person('A.', 'B.', 'C.', 'D.', 'E.', 'F.', 'G.', 'H.')
    first = [['f0', 'f1'], ['f2', 'f3'], ['f4', 'f5']]
    assert ku.consolidate(CID, digest(path), [], first, NOW,
                          supplied_versions=versions(p, *[f'f{i}' for i in range(6)]))['status'] == 'ok'
    current = loaded(path)
    result = ku.consolidate(CID, digest(path), ['f6'], [['f6', 'f7']], NOW,
                            supplied_versions=versions(current, 'f6', 'f7'))
    assert result['status'] == 'ok' and result['conflicts'] == 3
    assert conflicts()[CID]['pairs'] == [['f6', 'f7'], ['f0', 'f1'], ['f2', 'f3']]
    assert loaded(path).summary_refs == ['f6']


def test_101st_person_with_a_conflict_evicts_the_oldest_record(tmp_path):
    """main evicted the oldest of 100 records; refusing instead stopped Dreamer curating every new
    person once 100 people had stored conflicts (summary/dedupe never written, work parked)."""
    path, p = person('A.', 'B.', 'C.')
    conflict_path = tmp_path / 'knowledge/conflicts.json'
    others = {f'c_{i:012x}': {'pairs': [['a', 'b']], 'at': f'2026-01-{i % 28 + 1:02d}T00:00:{i % 60:02d}Z'}
              for i in range(100)}
    conflict_path.write_text(json.dumps({'people': others}))
    oldest = min(others, key=lambda cid: (others[cid]['at'], cid))
    result = ku.consolidate(CID, digest(path), ['f2'], [['f0', 'f1']], NOW,
                            supplied_versions=versions(p, 'f0', 'f1', 'f2'))
    assert result['status'] == 'ok'
    stored = conflicts()
    assert len(stored) == 100 and oldest not in stored and stored[CID]['pairs'] == [['f0', 'f1']]
    assert loaded(path).summary_refs == ['f2']


def test_stale_conflict_pair_is_dropped_and_frees_its_slot():
    """A pair whose side was archived is invisible to every reader; the writer must not keep it,
    let alone let it occupy one of the three slots."""
    import knowledge_edit as ke
    path, p = person('A.', 'B.', 'C.', 'D.', 'E.', 'F.', 'G.', 'H.')
    first = [['f0', 'f1'], ['f2', 'f3'], ['f4', 'f5']]
    assert ku.consolidate(CID, digest(path), [], first, NOW,
                          supplied_versions=versions(p, *[f'f{i}' for i in range(6)]))['status'] == 'ok'
    ke.op_archive(CID, 'f1')
    current = loaded(path)
    assert ku.live_conflict_pairs(current, conflicts()[CID]['pairs']) == [['f2', 'f3'], ['f4', 'f5']]
    result = ku.consolidate(CID, digest(path), [], [['f6', 'f7']], NOW,
                            supplied_versions=versions(current, 'f6', 'f7'))
    assert result['status'] == 'ok'
    assert conflicts()[CID]['pairs'] == [['f6', 'f7'], ['f2', 'f3'], ['f4', 'f5']]
    # The only stored pair going stale clears the person's record on the next curation.
    path2, _ = path, None
    for fid in ('f2', 'f4', 'f6'):
        ke.op_archive(CID, fid)
    current = loaded(path2)
    assert ku.consolidate(CID, digest(path2), [], [], NOW,
                          supplied_versions=versions(current, 'f0'))['status'] == 'ok'
    assert CID not in conflicts()


def test_evicted_pair_endpoint_still_survives_this_writes_duplicate_sweep():
    texts = ['Same.', 'One.', 'Two.', 'Three.', 'Four.', 'Five.', 'Same.',
             'Six.', 'Seven.', 'Eight.', 'Nine.', 'Ten.', 'Eleven.']
    path, p = person(*texts)
    first = [['f0', 'f1'], ['f2', 'f3'], ['f4', 'f5']]
    assert ku.consolidate(CID, digest(path), [], first, NOW,
                          supplied_versions=versions(p, *[f'f{i}' for i in range(6)]))['status'] == 'ok'
    current = loaded(path)
    current.facts['f6'].seen = 5   # the better-corroborated copy keeps; f0 would be the one folded
    kg.write_person_file(path, current, NOW)
    scope = ['f0', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12']
    new = [['f7', 'f8'], ['f9', 'f10'], ['f11', 'f12']]
    result = ku.consolidate(CID, digest(path), [], new, NOW, supplied_versions=versions(current, *scope))
    assert result['status'] == 'ok' and conflicts()[CID]['pairs'] == new
    after = loaded(path)
    assert after.facts['f0'].status == after.facts['f6'].status == 'active'


@pytest.mark.parametrize('record', [{'pairs': [['f0', 'f1'], ['bad']]}, 'invalid', None])
def test_malformed_prior_conflict_state_fails_closed(tmp_path, record):
    path, p = person('A.', 'B.')
    conflict_path = tmp_path / 'knowledge/conflicts.json'
    conflict_path.write_text(json.dumps({'people': {CID: record}}))
    conflict_before = conflict_path.read_bytes()
    before = digest(path)
    assert ku.consolidate(CID, before, [], [], NOW,
                          supplied_versions=versions(p, 'f0', 'f1'))['status'] == 'invalid_conflict_state'
    assert digest(path) == before and conflict_path.read_bytes() == conflict_before


def test_conflict_revision_race_refuses_without_person_mutation(tmp_path):
    path, p = person('A.', 'B.', 'C.')
    expected_pairs = []
    revision = hashlib.sha256(json.dumps(expected_pairs, sort_keys=True).encode()).hexdigest()
    (tmp_path / 'knowledge/conflicts.json').write_text(json.dumps({'people': {
        CID: {'pairs': [['f0', 'f1']], 'at': '2026-10-07'}}}))
    before = digest(path)
    result = ku.consolidate(CID, before, ['f2'], [], NOW,
                            supplied_versions=versions(p, 'f2'),
                            supplied_conflict_revision=revision)
    assert result['status'] == 'changed_during_review'
    assert digest(path) == before and conflicts()[CID]['pairs'] == [['f0', 'f1']]


def test_proposed_conflict_endpoints_survive_exact_duplicate_sweep():
    path, p = person('Same assertion.', 'Same assertion.')
    result = ku.consolidate(CID, digest(path), [], [['f0', 'f1']], NOW,
                            supplied_versions=versions(p, 'f0', 'f1'))
    assert result['status'] == 'ok'
    after = loaded(path)
    assert after.facts['f0'].status == after.facts['f1'].status == 'active'
    assert conflicts()[CID]['pairs'] == [['f0', 'f1']]


def test_version_race_and_manual_summary_and_legacy_call():
    path, p = person('A.', 'B.', summary='My own summary.', updated_by='user_edit')
    bad = versions(p, 'f0')
    bad['f0'] = '0' * 64
    before = digest(path)
    assert ku.consolidate(CID, before, [], [], NOW,
                          supplied_versions=bad)['status'] == 'changed_during_review'
    assert digest(path) == before
    assert ku.consolidate(CID, before, ['f0'], [], NOW,
                          supplied_versions=versions(p, 'f0'))['protected_summary']
    assert loaded(path).summary == 'My own summary.'
    assert ku.consolidate(CID, digest(path), [], [], NOW)['status'] == 'ok'
