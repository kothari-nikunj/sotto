"""Recorded obligation explanations through the actual consent-bound chat knowledge read."""
import json
import os
import sys

import pytest
import runtime_knowledge_read as read
from test_runtime_knowledge_read import REVISION, SCRIPT, graph, tree

__all__ = ['graph']


def _call(root, person='Dana', *, sources=('gmail', 'imessage'), topic='clinic report'):
    return read.perform({'operation': 'knowledge_query', 'arguments': {'argv': [
        '--person', person, '--person-coverage', '--topic', topic]}, 'source_revision': REVISION},
        script=str(SCRIPT), executable=sys.executable, env={**os.environ, 'SOTTO_DATA': str(root)},
        authority=lambda: {'complete': True, 'revision': REVISION,
                           'google_sources': [s for s in sources if s == 'gmail'],
                           'bridge_sources': [s for s in sources if s != 'gmail']})


def _write(root, key, **changes):
    import yaml
    row = dict(anchor_key=key, status='resolved', canonical_id='dana', contact_name='Dana',
               contact_identifier='dana@cedar.com', action_type='reply', channel='email', source='gmail',
               source_thread_id='original-thread', source_message_id='original-email',
               created_at='2026-06-01T14:00:00Z', source_timestamp='2026-05-31T14:00:00Z',
               summary='Send the clinic report', ask='Send the clinic report with the final clinic totals.',
               source_refs=[{'sourceType': 'email', 'sourceId': 'original-email',
                             'snippet': "I'll send the final clinic report tomorrow.",
                             'timestamp': '2026-05-31T14:00:00Z'}],
               closed_at='2026-06-02T14:00:00Z', resolved_at='2026-06-02', resolution='replied',
               resolution_reason='The requested report was sent over text.',
               resolution_evidence='Outgoing imessage: Here is the final clinic report with totals.',
               resolution_source_refs=[{'sourceType': 'imessage', 'sourceId': 'later-text',
                                        'snippet': 'Here is the final clinic report with totals.'}])
    row.update(changes)
    directory = root/'knowledge/continuity'; directory.mkdir(exist_ok=True)
    (directory/f'{key}.md').write_text('---\n'+yaml.safe_dump(row)+'---\n')
    return row


def test_later_chat_retrieves_original_and_closing_evidence_after_restart_without_mutation(graph):
    root, _ = graph
    _write(root, 'old-clinic')
    _write(root, 'other-task', status='open', summary='Arrange lunch', ask='Pick a lunch location.',
           resolution='', resolution_evidence='', resolution_source_refs=[], canonical_id='dana')
    before = tree(root)
    for _ in range(2):
        result = _call(root)
        history = result['obligation_history']
        assert history['status'] == 'matched' and history['omitted'] == 0
        item, other = history['items']
        assert item['anchor_key'] == 'old-clinic' and other['status'] == 'open'
        assert item['status'] == 'resolved' and item['completion_basis'] == 'source_evidence'
        assert item['source_refs'][0]['sourceId'] == 'original-email'
        assert "I'll send" in item['source_refs'][0]['snippet']
        assert item['resolution_source_refs'][0]['sourceId'] == 'later-text'
        assert 'final clinic report with totals' in item['resolution_evidence']
        assert item['source_timestamp'] != item['created_at']
        assert 'archives were not searched' in history['coverage']
    assert tree(root) == before


@pytest.mark.parametrize('sources', [('gmail',), ('imessage',), ()])
def test_revoking_either_original_or_closing_source_withholds_whole_outcome(graph, sources):
    root, _ = graph
    _write(root, 'old-clinic')
    history = _call(root, sources=sources)['obligation_history']
    assert history['items'] == [] and history['omitted'] == 0
    assert 'original-email' not in json.dumps(history)
    assert 'later-text' not in json.dumps(history)
    assert 'clinic report' not in json.dumps(history)


def test_shared_name_alias_and_conflicting_canonical_row_do_not_join_history(graph):
    import knowledge as kg
    root, _ = graph
    _write(root, 'right')
    _write(root, 'wrong-cid', canonical_id='other', contact_identifier='dana@cedar.com')
    other = kg.PersonFile(canonical_id='other', name='Dana', identifiers=['other@example.test'])
    (root/'knowledge/people/other.md').write_text(kg.serialize_person_file(other))
    ambiguous = _call(root)
    assert ambiguous['person_knowledge'] == {}
    assert ambiguous['coverage']['identity_resolution']['status'] == 'ambiguous'
    assert ambiguous['obligation_history']['status'] == 'identity_unresolved'
    selected = _call(root, 'dana@cedar.com')['obligation_history']
    assert [row['anchor_key'] for row in selected['items']] == ['right']
    other.identifiers.append('dana@cedar.com')
    (root/'knowledge/people/other.md').write_text(kg.serialize_person_file(other))
    assert _call(root, 'dana@cedar.com')['obligation_history']['status'] == 'identity_unresolved'
    assert [row['anchor_key'] for row in _call(root, 'dana')['obligation_history']['items']] == ['right']


def test_distinct_terminal_states_and_bounded_excerpts_do_not_assert_delivery(graph):
    root, _ = graph
    for key, status, resolution in [('a', 'resolved', 'user_resolved'), ('b', 'dismissed', 'user_dismissed'),
                                    ('c', 'expired', 'aged_out')]:
        _write(root, key, status=status, resolution=resolution, resolution_evidence='', resolution_source_refs=[],
               ask='clinic report ' + 'detail ' * 500)
    for index in range(15):
        _write(root, f'z-{index:02}', status='open')
    history = _call(root)['obligation_history']
    assert len(history['items']) <= 12 and history['omitted'] > 0
    assert len(json.dumps(history).encode()) <= 18 * 1024
    items = {row['anchor_key']: row for row in history['items']}
    assert items['a']['completion_basis'] == 'user_confirmed'
    assert items['b']['status'] == 'dismissed' and items['b']['completion_basis'] == 'ledger_state'
    assert items['c']['status'] == 'expired' and items['c']['completion_basis'] == 'ledger_state'
    assert 'ask' in items['a']['truncated_fields']


def test_source_wide_42_day_checkpoint_does_not_limit_old_outcome_or_prove_archive_complete(graph):
    root, _ = graph
    _write(root, 'old-clinic')
    history_state = {'sources': {'gmail': {
        'since': 1788739200, 'until': 1792368000, 'pages': 3, 'window_page_base': 0,
        'complete': True, 'initial_complete': True,
        'observation_totals': {'pages': 3, 'rows': 30, 'eligible': 30, 'selected': 30,
                               'skipped': 0, 'omitted': 0, 'truncated': 0, 'rejected_people': 0}}}}
    (root/'knowledge/history-state.json').write_text(json.dumps(history_state))
    result = _call(root)
    assert result['obligation_history']['items'][0]['source_timestamp'].startswith('2026-05-31')
    gmail = next(row for row in result['coverage']['historical_sources'] if row['source'] == 'gmail')
    assert gmail['learning']['review_scope'] == 'reviewed_window'
    assert gmail['learning']['outside_window_review'] == 'unknown'
    assert 'not person- or topic-specific completeness' in result['coverage']['learning_scope']
    assert 'archives were not searched' in result['obligation_history']['coverage']


def test_unavailable_ledger_does_not_turn_graph_read_into_false_no_match(graph):
    root, _ = graph
    directory = root/'knowledge/continuity'; directory.mkdir()
    (directory/'unsafe.md').symlink_to(root/'knowledge/people/legacy-dana.md')
    result = _call(root)
    assert 'dana' in result['person_knowledge']
    assert result['obligation_history']['status'] == 'unavailable'
    assert result['obligation_history']['items'] == []


def test_unreadable_person_cannot_make_a_shared_name_or_alias_appear_unique(graph):
    root, _ = graph
    _write(root, 'old-clinic')
    (root/'knowledge/people/other-dana.md').write_text('---\nname: [broken\n---\n')
    for selector in ('Dana', 'dana@cedar.com', 'dana'):
        result = _call(root, selector)
        assert result['person_knowledge'] == {}
        assert result['coverage']['identity_resolution']['status'] == 'unavailable'
        assert result['obligation_history']['status'] == 'unavailable'
        assert result['obligation_history']['items'] == []
