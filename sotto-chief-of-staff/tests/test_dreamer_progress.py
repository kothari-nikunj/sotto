"""Offline bounded curation progress through the original writer and model seam."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import dreamer
import knowledge as kg

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true')
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)


def seed(index, count=1):
    cid = f'c_{index:020d}'
    path = Path(kg.people_dir()) / f'{cid}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    person = kg.PersonFile(canonical_id=cid, name=f'Person {index}', facts={
        f'f_{i:03d}': kg.FactMeta(text=f'Distinct observation number {i} category {index}',
                                 source='observed_message', source_ref=f'synthetic:{index}:{i}',
                                 evidence_refs=[f'synthetic:{index}:{i}'], first='2026-10-01',
                                 last='2026-10-01') for i in range(count)})
    kg.write_person_file(str(path), person, NOW)
    return cid, path


def valid(prompt, config, **kwargs):
    candidates = json.loads(prompt)
    return json.dumps({'people': [{'id': p['id'], 'summary_refs': [], 'conflicts': [],
                                   'equivalent_groups': []} for p in candidates]})


def test_failed_versions_park_and_ninth_advances():
    for i in range(9):
        seed(i)
    submitted = []
    def fail(prompt, *args, **kwargs):
        submitted.append([p['id'] for p in json.loads(prompt)])
        return 'not json'
    # Freeze the same bounded batch, simulating retry after process restart/regrouping.
    prepared = dreamer.prepare()
    for _ in range(2):
        with pytest.raises(dreamer.DreamerResponseError):
            dreamer.run(fail, NOW, prepared)
    next_batch = dreamer.prepare()
    assert len(next_batch['candidates']) == 1
    assert next_batch['candidates'][0]['id'] not in submitted[0]
    assert next_batch['parked'] == 8
    assert dreamer.run(valid, NOW, next_batch)['reviewed'] == 1


def test_tail_coverage_converges_and_changed_fact41_is_work():
    cid, path = seed(0, 120)
    calls = []
    def model(prompt, config, **kwargs):
        batch = json.loads(prompt)
        calls.extend(list(p['facts']) for p in batch)
        assert len(batch) <= 8 and all(len(p['facts']) <= 40 for p in batch)
        return valid(prompt, config, **kwargs)
    for _ in range(3):
        dreamer.run(model, NOW)
    assert len(set(fid for batch in calls for fid in batch)) == 120
    assert dreamer.run(lambda *a, **k: pytest.fail('covered backlog'), NOW)['reviewed'] == 0
    person = kg.parse_person_file(path.read_text())
    person.facts['f_040'].text += ' changed evidence'
    kg.write_person_file(str(path), person, NOW)
    batch = dreamer.prepare()
    assert batch['candidates'][0]['work'] == ['f_040']
    assert batch['candidates'][0]['id'] == cid


def state():
    return json.loads(Path(dreamer._state_path()).read_text())


def test_crash_reservation_and_regroup_do_not_reset_work_attempts():
    cid, _ = seed(0)
    batch = dreamer.prepare()
    reservation = dreamer._reserve(batch)
    assert reservation and state()['facts'][cid]['f_000']['attempts'] == 1
    seed(1)
    # Process loss does not execute _finish; a fresh preparation still charges the old version.
    with pytest.raises(dreamer.DreamerResponseError):
        dreamer.run(lambda *a, **k: 'malformed', NOW)
    assert state()['facts'][cid]['f_000']['attempts'] == 2
    assert cid not in [p['id'] for p in dreamer.prepare()['candidates']]


def test_proven_unadmitted_refund_is_exact_but_transport_is_charged(monkeypatch):
    cid, _ = seed(0)
    def refusal(*args, **kwargs):
        raise dreamer.gemini_transport.BackgroundModelHeldError('synthetic_hold', 'not admitted')
    monkeypatch.setattr(dreamer.model_work, 'call', refusal)
    with pytest.raises(dreamer.gemini_transport.BackgroundModelHeldError):
        dreamer.run(now=NOW)
    assert state()['facts'][cid]['f_000']['attempts'] == 0
    def unknown(*args, **kwargs):
        raise OSError('response lost')
    monkeypatch.setattr(dreamer.model_work, 'call', unknown)
    with pytest.raises(OSError):
        dreamer.run(now=NOW)
    assert state()['facts'][cid]['f_000']['attempts'] == 1
    # Another invocation cannot refund this reservation after it has completed/changed.
    dreamer._finish({'id': 'wrong'}, refund=True)
    assert state()['facts'][cid]['f_000']['attempts'] == 1


def test_malformed_whole_batch_never_partially_writes():
    paths = [seed(i)[1] for i in range(2)]
    before = [p.read_bytes() for p in paths]
    def malformed(prompt, *args, **kwargs):
        candidates = json.loads(prompt)
        return json.dumps({'people': [{'id': p['id'], 'summary_refs': [] if i == 0 else ['missing'],
                                       'conflicts': [], 'equivalent_groups': []}
                                      for i, p in enumerate(candidates)]})
    with pytest.raises(dreamer.DreamerResponseError):
        dreamer.run(malformed, NOW)
    assert [p.read_bytes() for p in paths] == before
    assert all(row['attempts'] == 1 for rows in state()['facts'].values() for row in rows.values())


def test_parked_owner_dependency_holds_without_contact_and_conflict_race_refuses():
    cid, path = seed(0, 2)
    person = kg.parse_person_file(path.read_text())
    person.facts['f_000'].source = 'user_edit'
    kg.write_person_file(str(path), person, NOW)
    dreamer.prepare()
    current = state()
    current['facts'][cid]['f_000']['attempts'] = 2
    dreamer.jsonstore.write_atomic(dreamer._state_path(), current)
    batch = dreamer.prepare()
    assert batch['candidates'] == [] and batch['blocked_dependency'] == 1
    dreamer.run(lambda *a, **k: pytest.fail('parked authority'), NOW, batch)
    # A newly changed authority version allows a bounded new selection.
    person.facts['f_000'].text += ' correction'
    kg.write_person_file(str(path), person, NOW)
    batch = dreamer.prepare()
    dreamer.jsonstore.write_atomic(str(Path(kg.data_root()) / 'knowledge/conflicts.json'),
                                 {'people': {cid: {'pairs': [['f_000', 'f_001']]}}})
    assert dreamer.run(lambda *a, **k: pytest.fail('new conflict'), NOW, batch)['status'] == 'changed_before_contact'


def test_twelve_people_with_120_facts_converge_fairly():
    for index in range(12):
        seed(index, 120)
    touched, calls = {}, []
    def model(prompt, config, **kwargs):
        batch = json.loads(prompt)
        calls.append(len(batch))
        assert len(batch) <= 8
        for person in batch:
            assert len(person['facts']) <= 40
            touched.setdefault(person['id'], set()).update(person['facts'])
        return valid(prompt, config, **kwargs)
    for _ in range(6):
        dreamer.run(model, NOW)
    assert len(touched) == 12 and all(len(facts) == 120 for facts in touched.values())
    assert len(calls) == 5
    assert dreamer.run(lambda *a, **k: pytest.fail('finished'), NOW)['reviewed'] == 0


def test_legacy_latch_import_is_exact_and_unmapped_hold_is_explicit():
    cid, _ = seed(0)
    people = dreamer._people()
    legacy = dreamer._legacy_batch(people)
    import hashlib
    digest = hashlib.sha256(json.dumps([(c, semantic) for c, semantic, _ in legacy],
                                      sort_keys=True).encode()).hexdigest()
    failure = {'request': 'old-contract', 'candidates': digest, 'attempts': 2}
    batch = dreamer.prepare(legacy_failure=failure)
    assert not batch['candidates'] and batch['parked'] == 1
    assert state()['facts'][cid]['f_000']['attempts'] == 2
    dreamer.prepare(legacy_failure=failure)
    assert state()['facts'][cid]['f_000']['attempts'] == 2
    batch = dreamer.prepare(legacy_failure={**failure, 'candidates': 'unmappable'})
    assert batch['legacy_unmapped'] and not batch['candidates']
    dreamer.run(lambda *a, **k: pytest.fail('unmapped hold'), NOW, batch)
    seed(1)
    assert dreamer.prepare()['legacy_unmapped']  # unrelated evidence cannot release unidentified work


def test_source_change_and_postwrite_race_earn_no_coverage(monkeypatch):
    cid, path = seed(0)
    prepared = dreamer.prepare()
    person = kg.parse_person_file(path.read_text())
    person.facts['f_000'].text += ' owner changed before contact'
    kg.write_person_file(str(path), person, NOW)
    assert dreamer.run(lambda *a, **k: pytest.fail('stale source'), NOW, prepared)['status'] == 'changed_before_contact'
    real = dreamer.ku.consolidate
    def raced(*args, **kwargs):
        result = real(*args, **kwargs)
        person = kg.parse_person_file(path.read_text())
        person.facts['f_000'].text += ' concurrent change after accepted write'
        kg.write_person_file(str(path), person, NOW)
        return result
    monkeypatch.setattr(dreamer.ku, 'consolidate', raced)
    dreamer.run(valid, NOW)
    assert not state()['facts'][cid]['f_000']['covered']
    assert dreamer.prepare()['candidates'][0]['work'] == ['f_000']


def test_person_cursor_prevents_changing_first_person_starving_others():
    paths = [seed(i)[1] for i in range(9)]
    first = dreamer.prepare()
    assert len(first['candidates']) == 8
    dreamer.run(valid, NOW, first)
    person = kg.parse_person_file(paths[0].read_text())
    person.facts['f_000'].text += ' new first-person evidence'
    kg.write_person_file(str(paths[0]), person, NOW)
    assert dreamer.prepare()['candidates'][0]['id'] == paths[-1].stem


def test_legacy_reviewed_head_is_skipped_when_importing_failure():
    import hashlib
    ids = [seed(i)[0] for i in range(10)]
    people = dreamer._people()
    # Stable mtime ordering makes legacy head reconstruction exact and explicit.
    import os
    for index, (path, _, _) in enumerate(people.values()):
        os.utime(path, (1000 - index, 1000 - index))
    all_legacy = dreamer._legacy_batch(people, limit=None)
    reviewed = {cid: {'semantic': semantic} for cid, semantic, _ in all_legacy[:8]}
    dreamer.jsonstore.write_atomic(dreamer._state_path(), {'reviewed': reviewed})
    pending = dreamer._legacy_batch(people, reviewed)
    failure = {'request': 'legacy', 'candidates': hashlib.sha256(json.dumps(
        [(cid, semantic) for cid, semantic, _ in pending], sort_keys=True).encode()).hexdigest(), 'attempts': 2}
    batch = dreamer.prepare(legacy_failure=failure)
    assert not batch['legacy_unmapped'] and batch['parked'] == 2
    assert not batch['candidates']
    assert all(state()['facts'][cid]['f_000']['covered'] for cid in ids[:8])


def test_context_capacity_and_deleted_fact_cleanup():
    cid, path = seed(0, 41)
    person = kg.parse_person_file(path.read_text())
    for fact in person.facts.values():
        fact.source = 'user_edit'
    kg.write_person_file(str(path), person, NOW)
    batch = dreamer.prepare()
    assert not batch['candidates'] and batch['context_capacity'] == 41
    del person.facts['f_040']
    kg.write_person_file(str(path), person, NOW)
    batch = dreamer.prepare()
    assert len(batch['candidates'][0]['facts']) == 40
    assert 'f_040' not in state()['facts'][cid]


@pytest.mark.parametrize('kind', ['busy', 'typed_budget', 'arbitrary_budget', 'preflight', 'work_hold'])
def test_refund_only_proven_provider_nonexecution(kind, monkeypatch):
    from urllib.error import HTTPError
    from io import BytesIO
    cid, _ = seed(0)
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    # Native URL matches the existing typed proxy denial recognizer, never arbitrary provider HTTP.
    def denied(*args, **kwargs):
        if kind == 'preflight':
            raise dreamer.gemini_transport.ModelBudgetUnavailableError('not admitted')
        if kind == 'work_hold':
            raise dreamer.model_work.ModelWorkHeldError('prior operation held')
        raise HTTPError('https://proxy.example/native/v1beta/models/gemini-test:generateContent',
                        429 if kind == 'busy' else 402, 'denied',
                        {'X-Sotto-Admission': 'busy'} if kind == 'busy' else {},
                        BytesIO(json.dumps({'error': {'code': 'sotto_budget_exhausted' if kind == 'typed_budget'
                                                     else 'arbitrary_provider_error'}}).encode()))
    monkeypatch.setattr(dreamer.model_work, 'call', denied)
    with pytest.raises(Exception):
        dreamer.run(now=NOW)
    assert state()['facts'][cid]['f_000']['attempts'] == (1 if kind == 'arbitrary_budget' else 0)


def test_success_on_second_attempt_remains_available_as_authority_support():
    cid, path = seed(0)
    person = kg.parse_person_file(path.read_text())
    person.facts['f_000'].source = 'user_edit'
    kg.write_person_file(str(path), person, NOW)
    with pytest.raises(dreamer.DreamerResponseError):
        dreamer.run(lambda *a, **k: 'bad', NOW)
    dreamer.run(valid, NOW)
    person = kg.parse_person_file(path.read_text())
    person.facts['f_new'] = kg.FactMeta(text='A distinct new observation', source='observed_message',
                                       source_ref='new', evidence_refs=['new'])
    kg.write_person_file(str(path), person, NOW)
    batch = dreamer.prepare()
    assert batch['parked'] == 0 and batch['blocked_dependency'] == 0
    assert batch['candidates'][0]['work'] == ['f_new']
    assert set(batch['candidates'][0]['facts']) == {'f_000', 'f_new'}


def test_direct_run_imports_legacy_scheduler_hold_before_contact():
    seed(0)
    dreamer.jsonstore.write_atomic(str(Path(kg.data_root()) / 'knowledge/history-state.json'),
                                 {'dreamer_failure': {'request': 'pre-upgrade', 'candidates': 'unknown',
                                                      'attempts': 2}})
    result = dreamer.run(lambda *a, **k: pytest.fail('direct legacy bypass'), NOW)
    assert result['legacy_unmapped'] and result['reviewed'] == 0


def test_conflict_change_during_model_call_is_not_committed():
    cid, path = seed(0, 2)
    before = path.read_bytes()
    conflict_path = str(Path(kg.data_root()) / 'knowledge/conflicts.json')
    new_conflicts = {'people': {cid: {'pairs': [['f_000', 'f_001']], 'at': NOW.isoformat()}}}
    def raced(prompt, config, **kwargs):
        dreamer.jsonstore.write_atomic(conflict_path, new_conflicts)
        return valid(prompt, config, **kwargs)
    result = dreamer.run(raced, NOW)
    assert result['reviewed'] == 0 and result['results'][cid]['status'] == 'changed_during_review'
    assert path.read_bytes() == before
    assert dreamer.jsonstore.read(conflict_path) == new_conflicts
    assert not any(row['covered'] for row in state()['facts'][cid].values())


def test_nonempty_summary_selections_cover_tail_without_accumulating_authority():
    _, path = seed(0, 120)
    seen = set()
    def model(prompt, config, **kwargs):
        batch = json.loads(prompt)
        for p in batch:
            seen.update(p['facts'])
        return json.dumps({'people': [{'id': p['id'], 'summary_refs': list(p['facts'])[-4:],
                                       'conflicts': [], 'equivalent_groups': []} for p in batch]})
    for _ in range(3):
        assert dreamer.run(model, NOW)['reviewed'] == 1
    assert len(seen) == 120
    person = kg.parse_person_file(path.read_text())
    assert len(person.facts) == 120 and len(person.summary_refs) == 4
    assert dreamer.run(lambda *a, **k: pytest.fail('positive summary tail finished'), NOW)['reviewed'] == 0


def test_archived_conflict_endpoint_does_not_block_the_remaining_fact():
    """A stored pair whose other side the owner archived can never be supplied again; counting it
    as a dependency blocked the remaining fact (and its whole person) forever."""
    cid, path = seed(0, 3)
    person = kg.parse_person_file(path.read_text())
    person.facts['f_000'].source = 'user_edit'
    kg.write_person_file(str(path), person, NOW)
    dreamer.jsonstore.write_atomic(str(Path(kg.data_root()) / 'knowledge/conflicts.json'),
                                   {'people': {cid: {'pairs': [['f_000', 'f_002']], 'at': '2026-10-01'}}})
    import knowledge_edit as ke
    ke.op_archive(cid, 'f_002')
    batch = dreamer.prepare()
    assert batch['blocked_dependency'] == 0
    assert batch['candidates'] and set(batch['candidates'][0]['work']) == {'f_000', 'f_001'}
    assert dreamer.run(valid, NOW, batch)['reviewed'] == 1
    # The writer drops the dead pair once the person is curated, so it never comes back.
    conflicts = dreamer.jsonstore.read(str(Path(kg.data_root()) / 'knowledge/conflicts.json'))
    assert cid not in conflicts['people']


def _converge(llm):
    while True:
        batch = dreamer.prepare()
        if not batch['candidates']:
            return
        dreamer.run(llm, NOW, batch)


def test_release_or_model_route_change_does_not_recurate_covered_graph(monkeypatch):
    """The request contract hashes this code, the Gemini client and the model env, so every release
    touching them used to re-curate the whole graph (N/8 extra model calls). Coverage is about a
    fact's evidence; only an intentional CURATION_VERSION bump reopens it."""
    for i in range(3):
        seed(i, 5)
    calls = []
    _converge(lambda prompt, *a, **k: calls.append(prompt) or valid(prompt, None))
    assert calls and dreamer.prepare()['candidates'] == []
    before = dreamer.request_revision()
    monkeypatch.setenv('SOTTO_GEMINI_MODEL', 'gemini-3.7-flash')
    assert dreamer.request_revision() != before
    assert dreamer.prepare()['candidates'] == []
    monkeypatch.setattr(dreamer, 'CURATION_VERSION', dreamer.CURATION_VERSION + 1)
    batch = dreamer.prepare()
    assert sum(len(c['work']) for c in batch['candidates']) == 15   # the deliberate re-review


def test_contract_change_releases_parked_work_but_keeps_coverage(monkeypatch):
    covered, _ = seed(0)
    _converge(valid)
    parked, _ = seed(1)
    for _ in range(2):
        with pytest.raises(dreamer.DreamerResponseError):
            dreamer.run(lambda *a, **k: 'not json', NOW, dreamer.prepare())
    rows = state()['facts']
    assert rows[covered]['f_000']['covered'] and rows[parked]['f_000']['attempts'] == 2
    assert dreamer.prepare()['candidates'] == []
    monkeypatch.setenv('SOTTO_GEMINI_MODEL', 'gemini-3.7-flash')   # a different model may now succeed
    batch = dreamer.prepare()
    assert [c['id'] for c in batch['candidates']] == [parked]
    assert state()['facts'][covered]['f_000']['covered']


def test_curation_semantics_change_is_a_deliberate_version_decision():
    """Coverage survives prompt edits, so a change in what curation MEANS must bump
    CURATION_VERSION. Changed the prompt or schema? Decide: bump the version if already-covered
    memory should be reviewed again, then update this digest either way."""
    import hashlib
    digest = hashlib.sha256(json.dumps([dreamer.SYSTEM, dreamer.SCHEMA], sort_keys=True).encode()).hexdigest()
    assert (dreamer.CURATION_VERSION, digest) == (
        2, 'daaf7c8b4335cd1e20251f52d5b1060e10181b4375c9353916d51f0cb3d1f618')


def test_corrupt_person_isolated_in_prepare_and_reservation_then_repaired():
    cid, good = seed(0)
    repaired_cid, broken = seed(1)
    repaired_bytes = broken.read_bytes()
    bad_bytes = b'---\nname: [private malformed text\n---\n'
    broken.write_bytes(bad_bytes)
    batch = dreamer.prepare()
    assert batch['unreadable_people'] == 1
    assert [p['id'] for p in batch['candidates']] == [cid]
    receipt = dreamer.run(valid, NOW, batch)
    assert receipt['reviewed'] == 1 and receipt['unreadable_people'] == 1
    assert broken.read_bytes() == bad_bytes
    assert 'private' not in json.dumps(receipt)
    broken.write_bytes(repaired_bytes)
    batch = dreamer.prepare()
    assert batch['unreadable_people'] == 0
    assert [p['id'] for p in batch['candidates']] == [repaired_cid]
    assert dreamer.run(valid, NOW, batch)['reviewed'] == 1


def test_unchanged_prepare_does_not_replace_state(monkeypatch):
    seed(0)
    dreamer.prepare()
    writes = []
    monkeypatch.setattr(dreamer.jsonstore, 'write_atomic', lambda *a, **k: writes.append(a))
    dreamer.prepare()
    assert writes == []


def test_corrupt_record_after_preparation_does_not_block_healthy_reservation():
    seed(0)
    _, bad = seed(1)
    bad.write_bytes(b'---\nname: [invalid\n---\n')
    batch = dreamer.prepare()
    assert batch['unreadable_people'] == 1
    _, later_bad = seed(2)
    later_bad.write_bytes(b'\xff')
    receipt = dreamer.run(valid, NOW, batch)
    assert receipt['reviewed'] == 1 and receipt['unreadable_people'] == 2


def test_selected_person_corrupted_before_reservation_holds_without_contact():
    _, path = seed(0)
    batch = dreamer.prepare()
    path.write_bytes(b'---\nname: [invalid\n---\n')
    receipt = dreamer.run(lambda *a, **k: pytest.fail('changed candidate contacted'), NOW, batch)
    assert receipt['status'] == 'changed_before_contact' and receipt['unreadable_people'] == 1
    assert state()['facts'][batch['candidates'][0]['id']]['f_000']['attempts'] == 0


@pytest.mark.parametrize('failure', ['yaml', 'read'])
def test_unreadable_people_keep_coverage_and_parked_attempts_after_repair(monkeypatch, failure):
    covered, covered_path = seed(0)
    dreamer.run(valid, NOW)
    parked, parked_path = seed(1)
    for _ in range(dreamer.ATTEMPTS):
        with pytest.raises(dreamer.DreamerResponseError):
            dreamer.run(lambda *a, **k: 'invalid json', NOW)
    prior = state()
    paths = {covered_path, parked_path}
    originals = {path: path.read_bytes() for path in paths}
    read_bytes = Path.read_bytes
    with monkeypatch.context() as broken:
        if failure == 'yaml':
            for path in paths:
                path.write_bytes(b'---\nname: [private malformed text\n---\n')
        else:
            def unreadable(path):
                if path in paths:
                    raise OSError('private path')
                return read_bytes(path)
            broken.setattr(Path, 'read_bytes', unreadable)
        healthy, _ = seed(2)
        batch = dreamer.prepare()
        assert batch['unreadable_people'] == 2
        assert [row['id'] for row in batch['candidates']] == [healthy]
        for cid in (covered, parked):
            assert state()['facts'][cid] == prior['facts'][cid]
        assert state()['reviewed'][covered] == prior['reviewed'][covered]
        assert dreamer.run(valid, NOW, batch)['reviewed'] == 1
    for path, raw in originals.items():
        path.write_bytes(raw)
    repaired = dreamer.prepare()
    assert repaired['candidates'] == [] and repaired['parked'] == 1
    for cid in (covered, parked):
        assert state()['facts'][cid] == prior['facts'][cid]
    covered_path.unlink()
    parked_path.unlink()
    dreamer.prepare()
    assert covered not in state()['facts'] and parked not in state()['facts']
    assert covered not in state()['reviewed']


def test_contract_change_during_unreadability_releases_parked_work_after_repair(monkeypatch):
    covered, covered_path = seed(0)
    dreamer.run(valid, NOW)
    parked, parked_path = seed(1)
    for _ in range(dreamer.ATTEMPTS):
        with pytest.raises(dreamer.DreamerResponseError):
            dreamer.run(lambda *a, **k: 'invalid json', NOW)
    originals = {path: path.read_bytes() for path in (covered_path, parked_path)}
    for path in originals:
        path.write_bytes(b'---\nname: [invalid\n---\n')
    monkeypatch.setattr(dreamer, 'request_revision', lambda: 'new-contract')
    assert dreamer.prepare()['candidates'] == []
    assert state()['facts'][covered]['f_000']['covered']
    assert state()['facts'][parked]['f_000']['attempts'] == 0
    for path, raw in originals.items():
        path.write_bytes(raw)
    assert [row['id'] for row in dreamer.prepare()['candidates']] == [parked]
    assert dreamer.run(valid, NOW)['reviewed'] == 1


def test_person_deleted_during_scan_drops_checkpoint_instead_of_holding(monkeypatch):
    cid, path = seed(0)
    dreamer.run(valid, NOW)
    original = Path.read_bytes
    def deleted(current):
        if current == path:
            current.unlink()
        return original(current)
    monkeypatch.setattr(Path, 'read_bytes', deleted)
    batch = dreamer.prepare()
    assert batch['candidates'] == [] and batch['unreadable_people'] == 0
    assert cid not in state()['facts'] and cid not in state()['reviewed']
