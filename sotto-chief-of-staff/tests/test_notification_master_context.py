"""Saved owner Procedures reach every model-written nudge, like briefs and meeting prep."""
from datetime import datetime, timezone
import json

import pytest

import compose_notification as notification
import gemini

RULE = 'Always call Dana Fictional by her nickname, Dee.'
CONTEXT_LIMIT = 48000


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'm' * 32)
    monkeypatch.setattr(notification, 'enrich', lambda items, now: items)
    monkeypatch.setattr(gemini, 'provider_key', lambda _: 'test')
    systems = []

    def once(*args, **kwargs):
        systems.append(kwargs['system'])
        item_id = json.loads(args[2])[0]['id']
        return json.dumps({'items': [{'id': item_id, 'text': 'Dee is waiting on the deck.',
                                      'draft': '', 'decline': ''}]})
    monkeypatch.setattr(gemini, '_gemini_once', once)
    return tmp_path, systems


def _save_master(root, profile=''):
    (root / 'knowledge').mkdir(parents=True, exist_ok=True)
    (root / 'knowledge/master.md').write_text('## Procedures\n- ' + RULE + '\n' + profile)


def test_saved_procedure_reaches_batch_nudge_writer(owner):
    root, systems = owner
    _save_master(root)
    notification.compose('proactive', [{'kind': 'commitment', 'key': 'x', 'decision_id': 'x',
                                        'person': 'Dana Fictional', 'title': 'Send the deck'}])
    assert len(systems) == 1 and RULE in systems[0] and 'MASTER CONTEXT' in systems[0]


def test_saved_procedure_reaches_meeting_reminder_writer(owner):
    root, systems = owner
    _save_master(root)
    item = {'kind': 'meeting_prep', 'key': 'm', 'decision_id': 'prep-1', 'person': 'Dana Fictional',
            'calendar_start': '2026-09-17T12:30:00Z', 'event': {'summary': 'Coffee', 'description': 'Deck review'}}
    notification.compose('proactive', [item], now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert len(systems) == 1 and RULE in systems[0]


def test_missing_master_file_still_writes_without_context(owner):
    _, systems = owner
    notification.compose('proactive', [{'kind': 'commitment', 'key': 'x', 'decision_id': 'x'}])
    assert len(systems) == 1 and 'MASTER CONTEXT' not in systems[0]


def test_full_master_keeps_whole_candidates_in_priority_order(owner):
    root, _ = owner
    _save_master(root, '## Profile\n' + 'Work on the café expansion. ' * 281)
    conversation = [{'ts': f'2026-09-17T09:0{i}:00Z', 'is_from_me': bool(i % 2),
                     'text': 'A café expansion detail. ' * 36 + f' Distinct ending {i}.'}
                    for i in range(8)]
    items = [{'kind': 'commitment', 'decision_id': f'candidate-{i}', 'person': 'Dana Fictional',
              'deadline': f'2026-09-{20 + i}T12:00:00Z', 'current_conversation': conversation}
             for i in reversed(range(4))]
    items.insert(1, {'kind': 'commitment', 'decision_id': 'small', 'deadline': '2026-09-24T12:00:00Z'})
    requests = []

    def write(_provider, _model, _key, prompt, *, system, schema):
        requests.append((prompt, system, schema))
        return json.dumps({'items': [{'id': 'candidate-0', 'text': 'Dee needs the deck.',
                                     'draft': '', 'decline': ''}]})

    assert notification.compose('proactive', items, llm=write) == 'Dee needs the deck.'
    [(prompt, system, schema)] = requests
    sent = json.loads(prompt)
    assert [item['id'] for item in sent] == ['candidate-0', 'candidate-1', 'candidate-2', 'small']
    assert all(item['current_conversation'] == conversation for item in sent[:-1])
    assert RULE in system
    assert len(prompt) + len(gemini.gemini_transport.writing_system(system)) + len(json.dumps(schema)) <= CONTEXT_LIMIT


@pytest.mark.parametrize('included_sources, expected_snapshot', [
    (['calendar'], {'anchor_key': 'deck', 'sources': ['calendar', 'gmail']}),
    (None, {}),
])
def test_offer_sources_follow_the_candidates_seen_by_the_writer(owner, monkeypatch, included_sources, expected_snapshot):
    _, _systems = owner
    items = [
        {'kind': 'commitment', 'decision_id': 'primary', 'anchor_key': 'deck', 'channel': 'gmail',
         'identifier': 'dana@example.test', '_composition_sources': ['gmail'],
         '_loop_snapshot': {'anchor_key': 'deck', 'sources': ['gmail']}},
        {'kind': 'commitment', 'decision_id': 'too-large', 'detail': 'a' * CONTEXT_LIMIT},
        {'kind': 'commitment', 'decision_id': 'support', '_composition_sources': included_sources},
    ]
    offers = []
    monkeypatch.setattr(notification.pending_offer, 'set_offer', lambda *_, **kw: offers.append(kw))

    def write(_provider, _model, _key, prompt, **_):
        assert [item['id'] for item in json.loads(prompt)] == ['primary', 'support']
        return json.dumps({'items': [{'id': 'primary', 'text': 'Dee needs the deck.',
                                     'draft': 'Here is the deck.', 'decline': ''}]})

    assert 'Want this in your Gmail drafts?' in notification.compose('proactive', items, llm=write)
    assert [offer['source_loop_snapshot'] for offer in offers] == [expected_snapshot]
