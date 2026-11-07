"""Stable native references bind history and brief facts to observed content versions."""
from datetime import datetime, timezone

import pytest

import compose_brief
import context_learning
from render_local import message_evidence_id

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
SOURCE_REF = 'msg:v2:imessage:' + 'a' * 64
CONTENT_VERSION = 'b' * 64
CITATION = SOURCE_REF + ':v:' + CONTENT_VERSION


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')


def native_row(**changes):
    return {'source_id': 42, 'source_ref': SOURCE_REF, 'content_version': CONTENT_VERSION,
            'handle': '+14155550123', 'is_from_me': False,
            'timestamp': '2026-10-20T10:00:00Z', 'text': 'I work on clinical software.', **changes}


def test_native_recent_and_history_use_same_version_bound_reference():
    row = native_row()
    assert message_evidence_id(row, 'imessage') == CITATION
    records = context_learning.observations('imessage', [row], NOW)
    assert [record['ref'] for record in records] == [CITATION]


def test_native_reference_does_not_require_or_depend_on_history_row_cursor():
    row = native_row(source_id=None)
    assert [record['ref'] for record in context_learning.observations('imessage', [row], NOW)] == [CITATION]
    assert message_evidence_id(native_row(source_id=999, handle='renamed@example.test',
        timestamp='2026-10-20T03:00:00-07:00'), 'imessage') == CITATION


def test_brief_fact_binds_exact_measured_version_reference():
    out = {'extracted_knowledge': {'person_updates': [{'facts': [
        {'fact': 'Works on clinical software.', 'source_ref': CITATION}]}]}}
    compose_brief._bind_brief_fact_evidence(out, {'local': {'imessage': [native_row()]}})
    facts = out['extracted_knowledge']['person_updates'][0]['facts']
    assert len(facts) == 1 and facts[0]['source_ref'] == CITATION
    assert facts[0]['observed_date'] == '2026-10-20'


def test_brief_reused_row_cursor_cannot_select_between_distinct_native_messages():
    other = native_row(source_ref='msg:v2:imessage:' + 'c' * 64)
    out = {'extracted_knowledge': {'person_updates': [{'facts': [
        {'fact': 'Works on clinical software.', 'source_ref': '42'}]}]}}
    compose_brief._bind_brief_fact_evidence(out, {'local': {'imessage': [native_row(), other]}})
    assert out['extracted_knowledge']['person_updates'][0]['facts'] == []


def test_explicit_legacy_plan_requires_retained_native_identity_and_same_historical_version():
    from render_local import native_message_source_ref, verified_legacy_reference_mapping
    original = native_row(guid='A1-B2-C3', source_ref=None)
    current = native_row(source_ref=native_message_source_ref('imessage', original['guid']), source_id=999)
    plan = verified_legacy_reference_mapping('imessage', 'imessage:42', [original], current)
    assert plan == {'legacy_ref': 'imessage:42', 'new_version_ref': message_evidence_id(current, 'imessage')}
    assert verified_legacy_reference_mapping('imessage', 'imessage:42', [original],
        {**current, 'content_version': 'c' * 64}) is None
    assert verified_legacy_reference_mapping('imessage', 'imessage:42',
        [{key: value for key, value in original.items() if key != 'guid'}], current) is None
    assert verified_legacy_reference_mapping('imessage', 'imessage:42',
        [{key: value for key, value in original.items() if key != 'content_version'}], current) is None
    assert verified_legacy_reference_mapping('imessage', 'imessage:42',
        [original, {**original, 'guid': 'Another-Native-Key'}], current) is None
    assert verified_legacy_reference_mapping('imessage', 'msg:v1:' + 'd' * 24,
        [original], current) is None


@pytest.mark.parametrize('source', ['imessage', 'whatsapp'])
def test_content_edit_changes_citation_while_alias_timezone_and_cursor_do_not(source):
    stable = 'msg:v2:' + source + ':' + 'a' * 64
    base = native_row(source_ref=stable, contact_jid='15555550123@s.whatsapp.net')
    citation = message_evidence_id(base, source)
    renamed = {**base, 'source_id': 999, 'handle': 'new-alias@example.test',
               'contact_jid': 'a-new-alias@s.whatsapp.net', 'partner_name': 'A renamed contact',
               'timestamp': '2026-10-20T03:00:00-07:00'}
    assert message_evidence_id(renamed, source) == citation
    edited = {**base, 'content_version': 'c' * 64, 'text': 'I work on climate software.'}
    assert message_evidence_id(edited, source) == stable + ':v:' + 'c' * 64
    sibling = {**base, 'source_ref': 'msg:v2:' + source + ':' + 'd' * 64}
    assert message_evidence_id(sibling, source) != citation


@pytest.mark.parametrize('changes', [
    {'content_version': None}, {'content_version': 'private body'},
    {'source_ref': 'msg:v2:whatsapp:' + 'a' * 64}, {'source_ref': 'msg:v2:imessage:bad'},
])
def test_incomplete_native_pair_remains_legacy_without_correction_capability(changes):
    assert message_evidence_id(native_row(**changes), 'imessage') == '42'


def test_learning_persists_exact_native_citation_and_rejects_unobserved_version(monkeypatch):
    import json
    records = context_learning.observations('imessage', [native_row()], NOW)
    applied = []
    monkeypatch.setattr(context_learning.knowledge_update, 'apply', lambda value, **_: applied.append(value))
    def model(*args, **kwargs):
        return json.dumps({'people': [{'subject_id': 'p1', 'facts': [
            {'text': 'Works on clinical software.', 'refs': [CITATION]}]}]})
    context_learning.learn(records, llm=model, now=NOW)
    fact = applied[0]['person_updates'][0]['facts'][0]
    assert fact['source_ref'] == CITATION and fact['evidence_refs'] == [CITATION]
    stale = CITATION[:-64] + 'c' * 64
    def stale_model(*args, **kwargs):
        return json.dumps({'people': [{'subject_id': 'p1', 'facts': [
            {'text': 'Works on climate software.', 'refs': [stale]}]}]})
    with pytest.raises(context_learning.MemoryExtractionError, match='unknown_reference'):
        context_learning.learn(records, llm=stale_model, now=NOW)
    assert len(applied) == 1


def test_gmail_and_granola_remain_unversioned_without_a_native_revision_contract():
    gmail = {'id': 'mail-1', 'from': 'doctor@example.test', 'date': NOW.isoformat(),
             'body': 'I work on clinical software.', 'content_version': 'a' * 64}
    assert context_learning.observations('gmail', [gmail], NOW)[0]['ref'] == 'gmail:mail-1'
    out = {'extracted_knowledge': {'person_updates': [{'facts': [
        {'fact': 'Works on clinical software.', 'source_ref': 'meeting-1'}]}]}}
    compose_brief._bind_brief_fact_evidence(out, {'granola': {'meetings': [
        {'id': 'meeting-1', 'date': NOW.isoformat(), 'content_version': 'a' * 64}]}})
    fact = out['extracted_knowledge']['person_updates'][0]['facts'][0]
    assert fact['source_ref'] == 'meeting-1' and ':v:' not in fact['source_ref']


def test_permission_fingerprint_changes_for_accepted_corrections_and_preserves_off_shape(monkeypatch):
    import hashlib
    import json
    import source_context
    import source_corrections
    state = {'revision': ''}
    monkeypatch.setattr(source_corrections, 'revision', lambda data_dir=None: state['revision'])
    legacy = hashlib.sha256(json.dumps({source: source_context.allowed(source)
        for source in source_context.CONTEXT_SOURCES}, sort_keys=True).encode()).hexdigest()[:24]
    assert source_context.permission_fingerprint() == legacy
    state['revision'] = 'a' * 64
    assert source_context.permission_fingerprint() != legacy
    def fail(data_dir=None):
        raise ValueError('invalid correction state')
    monkeypatch.setattr(source_corrections, 'revision', fail)
    with pytest.raises(ValueError, match='invalid correction state'):
        source_context.permission_fingerprint()


def test_permission_fingerprint_refuses_correction_change_during_permission_read(monkeypatch):
    import source_context
    import source_corrections
    revisions = iter(['a' * 64, 'b' * 64])
    monkeypatch.setattr(source_corrections, 'revision', lambda data_dir=None: next(revisions))
    with pytest.raises(ValueError, match='source corrections changed'):
        source_context.permission_fingerprint()


def test_standalone_knowledge_reader_loads_its_disabled_correction_dependency(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    directory = Path(__file__).resolve().parents[1] / '_shared' / 'knowledge'
    result = subprocess.run([sys.executable, '-I', '-c',
        "import sys; sys.path.insert(0, sys.argv[1]); import knowledge; "
        "assert knowledge.source_invalidated_refs(sys.argv[2]) == set(); "
        "knowledge.parse_person_file(chr(10).join(['---', 'name: Synthetic', '---', '']))",
        str(directory), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
