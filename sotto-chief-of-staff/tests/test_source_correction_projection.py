"""Positive, admitted source versions disappear from every shared graph projection."""
from datetime import datetime, timezone
from pathlib import Path
import json

import knowledge as kg
import knowledge_update as ku
import pytest


OLD = 'msg:v2:imessage:' + 'a' * 64 + ':v:' + 'b' * 64
OTHER = 'msg:v2:imessage:' + 'c' * 64 + ':v:' + 'd' * 64
NEW = 'msg:v2:imessage:' + 'a' * 64 + ':v:' + 'e' * 64
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(kg, 'source_invalidated_refs', lambda *_: {OLD}, raising=False)
    directory = tmp_path / 'knowledge/people'
    directory.mkdir(parents=True)
    return directory


def fact(text, refs, **kwargs):
    return kg.FactMeta(text=text, source='observed_message', source_ref=refs[0] if refs else '',
                       evidence_refs=refs, first='2026-10-20', last='2026-10-20', **kwargs)


def person():
    return kg.PersonFile(canonical_id='c_abcdef', name='Synthetic Dana', facts={
        'withdrawn': fact('Withdrawn assertion.', [OLD]),
        'supported': fact('Independently supported.', [OLD, OTHER]),
        'new': fact('Current version.', [NEW]),
        'legacy': fact('Unreferenced legacy.', []),
        'owner': kg.FactMeta(text='Owner correction.', source='user_edit', source_ref=OLD),
    }, summary='Withdrawn assertion. Current version.', summary_refs=['withdrawn', 'new'])


def test_pending_receipt_filters_shared_reads_without_rewriting_source_file(graph):
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(person(), NOW))
    original = path.read_bytes()
    current = kg.parse_person_file(path.read_text())
    assert current.facts['withdrawn'].status == 'archived'
    assert current.facts['withdrawn'].text == ''
    assert current.summary == 'Current version.'
    assert current.facts['supported'].evidence_refs == [OTHER]
    assert current.facts['new'].status == current.facts['owner'].status == current.facts['legacy'].status == 'active'
    assert path.read_bytes() == original


def test_projection_is_bounded_restart_safe_and_skips_malformed_people(graph):
    (graph / 'a-broken.md').write_text('---\nfacts: [broken\n---\n')
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(person(), NOW))
    first = ku.project_source_correction([OLD], now=NOW, limit=1)
    assert first['unreadable'] == 1 and not first['complete']
    second = ku.project_source_correction([OLD], now=NOW, after=first['next_cursor'], limit=1)
    assert second['complete'] and second['archived'] == 1
    saved = path.read_bytes()
    assert ku.project_source_correction([OLD], now=NOW)['changed'] == 0
    assert path.read_bytes() == saved
    assert 'broken' in (graph / 'a-broken.md').read_text()


def test_unadmitted_versions_cannot_be_projected(graph):
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(person(), NOW))
    before = path.read_bytes()
    result = ku.project_source_correction([NEW], now=NOW)
    assert result['changed'] == 0
    assert path.read_bytes() == before


def test_owner_summary_and_independent_duplicate_survive(graph):
    p = person()
    p.summary = 'Owner-written summary.'
    p.facts['duplicate'] = fact('Independent original wording.', [OTHER], status='archived',
                               archived_reason='duplicate', duplicate_of='withdrawn')
    current = kg.parse_person_file(kg.serialize_person_file(p, NOW))
    assert current.summary == 'Owner-written summary.'
    assert current.facts['duplicate'].status == 'active'
    assert current.facts['duplicate'].duplicate_of is None


def test_withdrawn_evidence_cannot_be_relearned_but_new_version_can(graph):
    update = {'person_updates': [{'person_name': 'Synthetic Dana', 'identifier': 'dana@example.invalid',
                                 'facts': [{'fact': 'Withdrawn assertion.', 'source': 'observed_message',
                                            'source_ref': OLD, 'evidence_refs': [OLD]}]}]}
    result = ku.apply(update, now=NOW)
    assert result['applied']['new'] == 0
    update['person_updates'][0]['facts'][0].update(source_ref=NEW, evidence_refs=[NEW])
    assert ku.apply(update, now=NOW)['applied']['new'] == 1


def test_shared_model_and_dashboard_views_hide_pending_withdrawal(graph, monkeypatch):
    import knowledge_query as query
    import dreamer
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'runtime/trigger-receiver'))
    import dashboard
    monkeypatch.setitem(dashboard.HOOKS, 'data_root', lambda: str(graph.parents[1]))
    monkeypatch.setitem(dashboard.HOOKS, 'project_person',
                        lambda content: kg.project_person_content(content, str(graph.parents[1])))
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(person(), NOW))
    stale = person()
    assert 'Withdrawn assertion.' not in query.pack_person(stale, True, NOW)
    assert 'withdrawn' not in {f['id'] for f in query.editable_person(stale, NOW)['facts']}
    assert stale.facts['withdrawn'].status == 'active'  # projecting never mutates the caller
    assert dreamer._people()['c_abcdef'][1].facts['withdrawn'].status == 'archived'
    result = dashboard.api_person('c_abcdef')
    assert 'Withdrawn assertion.' not in json.dumps(result)
    assert dashboard.api_people()['people'][0]['fact_count'] == 4


def test_incomplete_legacy_support_is_not_guessed_away(graph):
    p = person()
    p.facts['incomplete'] = fact('Unknown other support.', [OLD], seen=2)
    p.facts['legacy_reference'] = kg.FactMeta(text='Legacy support.', source_ref=OLD)
    result = kg.project_invalidated_evidence(p, {OLD})
    assert result['unsupported'] == 2
    assert p.facts['incomplete'].status == p.facts['legacy_reference'].status == 'active'


def test_sequential_corrections_remove_each_exact_support(graph):
    p = person()
    p.facts['supported'].seen = 2
    kg.project_invalidated_evidence(p, {OLD})
    assert p.facts['supported'].seen == 1
    kg.project_invalidated_evidence(p, {OTHER})
    assert p.facts['supported'].archived_reason == 'source_withdrawn'


def test_projection_preserves_legacy_owner_summary_authority(graph):
    p = person()
    p.summary, p.summary_refs, p.updated_by = 'Owner-written summary.', [], 'user_edit'
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(p, NOW))
    ku.project_source_correction([OLD], now=NOW)
    saved = kg.parse_person_file(path.read_text())
    assert saved.summary == 'Owner-written summary.'
    assert kg.summary_protected(saved)


def test_failed_projection_write_is_not_counted_as_archived(graph, monkeypatch):
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(person(), NOW))
    before = path.read_bytes()
    def fail(*_):
        raise OSError('synthetic interrupted write')
    monkeypatch.setattr(kg, 'write_person_file', fail)
    with pytest.raises(RuntimeError, match='write failed'):
        ku.project_source_correction([OLD], now=NOW)
    assert path.read_bytes() == before


def test_oversized_and_symlink_people_do_not_block_valid_projection(graph, monkeypatch):
    outside = graph.parent / 'outside.md'
    outside.write_text(kg.serialize_person_file(person(), NOW))
    (graph / 'a-link.md').symlink_to(outside)
    (graph / 'b-large.md').write_text('x' * (ku.SOURCE_CORRECTION_FILE_BYTES + 1))
    valid = graph / 'c_abcdef.md'
    valid.write_text(outside.read_text())
    original = outside.read_bytes()
    result = ku.project_source_correction([OLD], now=NOW)
    assert result['unreadable'] == 2 and result['changed'] == 1
    assert outside.read_bytes() == original


def migration_fixture(graph):
    from render_local import native_message_source_ref
    record = {'source_id': 42, 'guid': 'Native-A1-B2', 'content_version': 'f' * 64}
    current = {**record, 'source_ref': native_message_source_ref('imessage', record['guid'])}
    p = kg.PersonFile(canonical_id='c_abcdef', name='Synthetic Dana', facts={
        'f_legacy': fact('Reviewed retained assertion.', ['imessage:42']),
        'f_sibling': fact('Separate legacy assertion.', ['imessage:43']),
    })
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(p, NOW))
    expected = kg.person_fact_version(p.canonical_id, 'f_legacy', p.facts['f_legacy'])
    return path, p, expected, record, current


def test_reviewed_legacy_migration_is_exact_and_does_not_refresh_evidence(graph):
    path, p, expected, record, current = migration_fixture(graph)
    result = ku.migrate_source_reference('c_abcdef', 'f_legacy', expected,
        channel='imessage', legacy_ref='imessage:42', original_records=[record], current_record=current)
    assert result['status'] == 'migrated'
    saved = kg.parse_person_file(path.read_text(), invalid_refs=set())
    new_ref = current['source_ref'] + ':v:' + current['content_version']
    assert saved.facts['f_legacy'].source_ref == new_ref
    assert saved.facts['f_legacy'].evidence_refs == [new_ref]
    assert saved.facts['f_legacy'].seen == p.facts['f_legacy'].seen
    assert saved.facts['f_legacy'].last == p.facts['f_legacy'].last
    assert saved.facts['f_sibling'].to_yaml_dict() == p.facts['f_sibling'].to_yaml_dict()
    before = path.read_bytes()
    assert ku.migrate_source_reference('c_abcdef', 'f_legacy', expected,
        channel='imessage', legacy_ref='imessage:42', original_records=[record],
        current_record=current)['status'] == 'changed_during_review'
    assert path.read_bytes() == before


def test_legacy_migration_rejects_stale_target_or_unproven_version(graph):
    path, _, expected, record, current = migration_fixture(graph)
    before = path.read_bytes()
    assert ku.migrate_source_reference('c_abcdef', 'f_legacy', '0' * 64,
        channel='imessage', legacy_ref='imessage:42', original_records=[record],
        current_record=current)['status'] == 'changed_during_review'
    assert ku.migrate_source_reference('c_abcdef', 'f_legacy', expected,
        channel='imessage', legacy_ref='imessage:42', original_records=[record],
        current_record={**current, 'content_version': '1' * 64})['status'] == 'unsupported'
    assert path.read_bytes() == before


@pytest.mark.parametrize('owner', [False, True])
def test_migration_preserves_owner_words_and_incomplete_provenance(graph, owner):
    path, p, _, record, current = migration_fixture(graph)
    target = p.facts['f_legacy']
    target.evidence_refs = []
    target.source = 'user_edit' if owner else target.source
    path.write_text(kg.serialize_person_file(p, NOW))
    before = path.read_bytes()
    expected = kg.person_fact_version(p.canonical_id, 'f_legacy', target)
    result = ku.migrate_source_reference('c_abcdef', 'f_legacy', expected,
        channel='imessage', legacy_ref='imessage:42', original_records=[record], current_record=current)
    assert result['status'] == ('protected' if owner else 'migrated')
    saved = kg.parse_person_file(path.read_text(), invalid_refs=set())
    assert saved.facts['f_legacy'].evidence_refs == []
    assert saved.facts['f_legacy'].text == target.text
    if owner:
        assert path.read_bytes() == before


def test_migration_rejects_symlink_target(graph):
    path, _, expected, record, current = migration_fixture(graph)
    real = graph / 'c_123456.md'
    path.rename(real)
    path.symlink_to(real)
    before = real.read_bytes()
    assert ku.migrate_source_reference('c_abcdef', 'f_legacy', expected,
        channel='imessage', legacy_ref='imessage:42', original_records=[record],
        current_record=current)['status'] == 'unsupported'
    assert real.read_bytes() == before


def test_withdrawal_does_not_resurrect_duplicate_without_independent_evidence(graph):
    p = person()
    p.facts['uncertain_duplicate'] = fact('Unproven duplicate wording.', [], status='archived',
        archived_reason='duplicate', duplicate_of='withdrawn')
    p.facts['withdrawn_duplicate'] = fact('Legacy duplicate wording.', [], status='archived',
        archived_reason='duplicate', duplicate_of='withdrawn')
    p.facts['withdrawn_duplicate'].source_ref = OLD
    kg.project_invalidated_evidence(p, {OLD})
    assert p.facts['withdrawn'].archived_reason == 'source_withdrawn'
    assert p.facts['uncertain_duplicate'].status == 'archived'
    assert p.facts['withdrawn_duplicate'].status == 'archived'


@pytest.mark.parametrize('reason', ['superseded', 'user_corrected', 'stale'])
def test_projection_preserves_existing_archive_history(graph, reason):
    p = person()
    archived = fact('Earlier assertion.', [OLD], status='archived', archived_reason=reason)
    p.facts['history'] = archived
    before_refs = list(archived.evidence_refs)
    kg.project_invalidated_evidence(p, {OLD})
    assert archived.archived_reason == reason
    assert archived.text == 'Earlier assertion.'
    assert archived.status == 'archived'
    assert reason in kg.serialize_person_file(p, NOW)
    assert archived.evidence_refs == before_refs


def test_withdrawn_words_absent_from_persisted_graph(graph):
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(person(), NOW))
    ku.project_source_correction([OLD], now=NOW)
    assert b'Withdrawn assertion.' not in path.read_bytes()


def test_restored_duplicate_removes_withdrawn_citations(graph):
    p = person()
    p.facts['copy'] = fact('Independent copy.', [OLD, OTHER], seen=2,
                           status='archived', archived_reason='duplicate', duplicate_of='withdrawn')
    kg.project_invalidated_evidence(p, {OLD})
    assert p.facts['copy'].status == 'active'
    assert p.facts['copy'].source_ref == OTHER
    assert p.facts['copy'].evidence_refs == [OTHER]
    assert p.facts['copy'].seen == 1


def test_dashboard_projection_preserves_extension_metadata_and_isolates_private_parse_errors(graph):
    raw = kg.serialize_person_file(person(), NOW).replace('schema:', 'custom_dashboard_key: retained\nschema:', 1)
    assert 'custom_dashboard_key: retained' in kg.project_person_content(raw)
    with pytest.raises(OSError) as failure:
        kg.project_person_content('---\nfacts: [PRIVATE_SYNTHETIC\n---\n')
    assert 'PRIVATE_SYNTHETIC' not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_dashboard_skips_malformed_person_next_to_valid_person(graph, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'runtime/trigger-receiver'))
    import dashboard
    monkeypatch.setitem(dashboard.HOOKS, 'data_root', lambda: str(graph.parents[1]))
    monkeypatch.setitem(dashboard.HOOKS, 'project_person', kg.project_person_content)
    (graph / 'broken.md').write_text('---\nfacts: [PRIVATE_SYNTHETIC\n---\n')
    (graph / 'c_abcdef.md').write_text(kg.serialize_person_file(person(), NOW))
    assert len(dashboard.api_people()['people']) == 1
    assert dashboard.api_person('broken') is None


@pytest.mark.parametrize('refs,source_ref,seen,expected_seen', [
    ([], OTHER, 1, 1), ([OLD], OTHER, 2, 1), ([OLD, OTHER], OLD, 5, 5),
    ([], OTHER, 5, 5),
])
def test_restored_duplicate_preserves_unknown_support_counts(graph, refs, source_ref, seen, expected_seen):
    p = person()
    duplicate = fact('Independent historical wording.', refs, status='archived',
                     archived_reason='duplicate', duplicate_of='withdrawn', seen=seen)
    duplicate.source_ref = source_ref
    p.facts['copy'] = duplicate
    kg.project_invalidated_evidence(p, {OLD})
    assert duplicate.status == 'active'
    assert duplicate.seen == expected_seen
    assert duplicate.source_ref == OTHER
    assert OLD not in duplicate.evidence_refs
    if not refs:
        assert duplicate.evidence_refs == []  # Never manufacture complete legacy provenance.


def test_withdrawn_duplicate_without_independent_support_is_persisted_as_tombstone(graph):
    p = person()
    p.facts['copy'] = fact('Withdrawn duplicate private wording.', [OLD], status='archived',
                           archived_reason='duplicate', duplicate_of='withdrawn')
    p.facts['copy'].archived_text = 'Withdrawn duplicate archived wording.'
    path = graph / 'c_abcdef.md'
    path.write_text(kg.serialize_person_file(p, NOW))
    ku.project_source_correction([OLD], now=NOW)
    saved = path.read_text()
    assert 'Withdrawn duplicate' not in saved
    duplicate = kg.parse_person_file(saved, invalid_refs=set()).facts['copy']
    assert duplicate.archived_reason == 'source_withdrawn'
    assert duplicate.duplicate_of is None


def test_active_dashboard_projection_accepts_no_frontmatter(graph):
    projected = kg.project_person_content('## Summary\nLegacy markdown summary.\n')
    assert 'Legacy markdown summary.' in projected


@pytest.mark.parametrize('reason', ['duplicate', 'superseded', 'stale', 'user_corrected'])
def test_archived_history_with_independent_provenance_is_preserved(graph, reason):
    p = person()
    archived = fact('Independent archive wording.', [OLD, OTHER], status='archived',
                    archived_reason=reason, duplicate_of='new' if reason == 'duplicate' else None)
    archived.archived_text = 'Independent original wording.'
    p.facts['history'] = archived
    before = archived.to_yaml_dict()
    kg.project_invalidated_evidence(p, {OLD})
    assert archived.to_yaml_dict() == before
