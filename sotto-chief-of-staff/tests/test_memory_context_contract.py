"""Offline memory → original composition/critic → obligation writer across cycles.

The model is scripted; this proves the plumbing and exact evidence gates, not model judgment.
"""
import json
from pathlib import Path
from datetime import datetime

import pytest

import compose_brief as cb
import knowledge as kg
import knowledge_query as kq
import knowledge_update as ku
from delivery_effects import loop_version


@pytest.mark.parametrize('include_group_observation', [False, True])
@pytest.mark.parametrize('learn_identity_after_capture', [False, True])
def test_intro_completion_keeps_meeting_open_and_replay_has_no_extra_learning(
        tmp_path, monkeypatch, learn_identity_after_capture, include_group_observation):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.delenv('SOTTO_CRITIC', raising=False)
    cr = cb._continuity()
    now = datetime(2026, 10, 6, 12)
    learned = {'person_updates': [{'person_name': 'Dana', 'identifier': 'dana@example.test',
        'facts': [{'fact': 'Dana works on clinical software.', 'memory_type': 'context',
                   'source': 'observed_message', 'source_ref': 'gmail:profile-1',
                   'evidence_refs': ['gmail:profile-1'], 'observed_date': '2026-10-05'}]}]}
    if not learn_identity_after_capture:
        ku.apply(learned, now)

    incoming = {'id': 'request-1', 'threadId': 'intro-thread', 'isSent': False,
                'from': 'Dana <dana@example.test>', 'to': 'owner@example.test',
                'date': '2026-10-05T09:00:00Z',
                'body': 'Please introduce me to Lee, and check later whether we have met.'}
    request_proof = [{'sourceType': 'email', 'sourceId': incoming['id'], 'snippet': incoming['body']}]
    base = {'type': 'reply', 'channel': 'gmail', 'contactName': 'Dana',
            'contactIdentifier': 'dana@example.test', 'created_at': '2026-10-05 09:00:00',
            'evidence': request_proof}
    actions = [{**base, 'contextSummary': 'Introduce Dana to Lee'},
               {**base, 'contextSummary': 'Check whether Dana and Lee have met', 'newObligation': True}]
    first = cr.resolve({'today': '2026-10-06', 'emails': [incoming], 'new_actions': actions},
                       now, resolve_existing=False)
    assert len(first['active']) == 2
    intro = next(row for row in first['active'] if row['summary'].startswith('Introduce'))
    meeting = next(row for row in first['active'] if row['summary'].startswith('Check'))

    if learn_identity_after_capture:
        ku.apply(learned, now)
    person_path = kg.find_person_file(identifier='dana@example.test')
    person = kg.parse_person_file(Path(person_path).read_text(encoding='utf-8'))
    facts_before = person.facts.copy()
    selected = []
    context = kq.pack_person(person, True, now, 'Dana introduction', selected_records=selected)
    assert selected and 'clinical software' in context

    sent = {'id': 'sent-intro', 'threadId': 'intro-thread', 'isSent': True, 'labelIds': ['SENT'],
            'from': 'owner@example.test', 'to': 'dana@example.test, lee@example.test',
            'date': '2026-10-06T10:00:00Z', 'body': 'Dana, meet Lee. Lee, meet Dana.'}
    group = {'id': 'sent-intro-group', 'rowid': 201,
             'handle': 'owner@example.test', 'is_from_me': True, 'is_group_chat': True,
             'chat_guid': 'iMessage;+;intro-group',
             'group_participants': ['dana@example.test', 'lee@example.test'],
             'timestamp': '2026-10-06T10:05:00Z',
             'text': 'Following up here: Dana and Lee, I introduced you by email.'}
    observations = {'imessage': [group] if include_group_observation else []}
    # Group context is not authority for a direct-person obligation. The exact sent
    # email remains the only cited proof, with the original critic/writer gates.
    update = {'loopId': intro['anchor_key'], 'loopVersion': loop_version(intro), 'status': 'resolved',
              'evidence': [{'sourceType': 'email', 'sourceId': sent['id'], 'snippet': sent['body']}]}
    calls = []

    def model(_prompt, inputs, system=None, schema=None):
        if inputs.get('_critic'):
            calls.append('critic')
            assert sent['body'] in _prompt and 'loop_update_review' in _prompt
            return json.dumps({'patches': [], 'score': 95, 'summary': 'Introduction sent; meeting unproven.',
                'loop_verdicts': [{'proposal_id': cr.critic_review_receipt(update), 'verdict': 'approve'}]})
        calls.append('extract')
        assert 'Dana works on clinical software.' in _prompt
        if include_group_observation:
            assert group['text'] in _prompt
        return json.dumps({'markdown': '# Brief\n\nThe introduction is sent; their meeting is still unconfirmed.',
                           'actionItems': [], 'loopUpdates': [update]})

    inputs = {'type': 'morning', 'now': '2026-10-06T12:00:00Z',
              'google': {'emails': [sent]}, 'local': {**observations, 'action_ledger': first['active'],
              'person_knowledge': {person.canonical_id: context}}}
    composed = cb.compose(inputs, llm=model, critic=True)
    assert calls == ['extract', 'critic']
    assert composed['loop_updates'][0]['criticReceipt']
    assert composed['_loop_projection']['resolved'] == [intro['anchor_key']]
    applied = cr.resolve({'today': '2026-10-06', 'emails': [sent], 'local': observations,
                         'loop_updates': composed['loop_updates']}, now, resolve_existing=False)
    assert [row['anchor_key'] for row in applied['resolved']] == [intro['anchor_key']]
    assert [row['anchor_key'] for row in applied['active']] == [meeting['anchor_key']]

    # Replayed source observations and the exact reviewed transition neither reopen nor relearn.
    replay = cr.resolve({'today': '2026-10-06', 'emails': [incoming, sent], 'local': observations,
                         'new_actions': actions, 'loop_updates': composed['loop_updates']},
                        now, resolve_existing=False)
    assert replay['resolved'] == []
    rows = cr._load_items()
    assert len(rows) == 2 and rows[intro['anchor_key']]['status'] == 'resolved'
    assert rows[meeting['anchor_key']]['status'] == 'open'
    assert ku.apply(learned, now)['applied']['confirmed'] == 0
    assert kg.parse_person_file(Path(person_path).read_text(encoding='utf-8')).facts == facts_before

    # The next two ordinary context paths receive only the remaining obligation. No extra
    # verifier or closure stage is introduced by the new memory projection.
    for kind in ('morning', 'digest'):
        def next_model(prompt, inputs, system=None, schema=None):
            assert 'loop_id: ' + intro['anchor_key'] + ' |' not in prompt
            if inputs.get('_critic'):
                calls.append('critic')
                return json.dumps({'patches': [], 'score': 95, 'summary': 'No completion proposed.'})
            calls.append('extract')
            assert 'Check whether Dana and Lee have met' in prompt
            return json.dumps({'markdown': '# Brief\n\nCheck whether Dana and Lee have met.', 'actionItems': []})

        cb.compose({**inputs, 'type': kind, 'local': {**inputs['local'], 'action_ledger': replay['active']}},
                   llm=next_model, critic=True)
    assert calls.count('extract') == 3 and calls.count('critic') <= 3
    assert len(cr._load_items()) == 2
