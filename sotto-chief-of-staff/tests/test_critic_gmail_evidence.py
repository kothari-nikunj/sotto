"""Gmail evidence delivered to the brief critic."""
import json

import compose_brief as cb


def test_critic_includes_each_thread_before_adding_siblings_and_keeps_exact_messages():
    busy = [{'id': f'a{i}', 'threadId': 'a', 'body': 'a' * 150} for i in range(60)]
    other = [{'id': f'b{i}', 'threadId': 'b', 'labelIds': ['SENT'], 'body': 'from me'} for i in range(60)]
    inputs = {'google': {'emails': [busy[0], *busy, *other, {'id': 'last', 'threadId': 'c'}]}}
    evidence = cb.build_data_manifest(inputs)['email_threads']
    ids = [row['message_id'] for row in evidence]
    assert len(set(ids)) == len(ids) == cb.CRITIC_GMAIL_MESSAGE_LIMIT
    assert ids[:7] == ['a0', 'b0', 'last', 'a1', 'b1', 'a2', 'b2']
    assert evidence[0]['snippet'] == 'a' * 120 and evidence[0]['snippet_truncated']
    assert evidence[4]['sent_by_owner'] and evidence[4]['snippet'] == 'from me'
    assert not evidence[3]['sent_by_owner']


def test_hidden_gmail_siblings_stay_out_of_writer_and_critic(monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'gmail')
    prefs = {**cb.explicit_prefs(), 'mute_senders': ['muted@example.com']}
    monkeypatch.setattr(cb, 'explicit_prefs', lambda: prefs)
    allowed = {'from': 'Cedar <cedar@example.com>', 'id': 'shown', 'threadId': 'garden', 'body': 'Retain me'}
    inputs = {'google': {'emails': [
        allowed,
        {**allowed, 'id': 'muted', 'from': 'muted@example.com', 'body': 'Private sender'},
        {**allowed, 'id': 'scope', 'body': 'Private sibling', '_loop_context_sources': ['whatsapp']},
    ]}}
    for prompt in (cb.build_prompt('{{gmail}}', inputs), json.dumps(cb.build_data_manifest(inputs))):
        assert 'Retain me' in prompt and 'Private sender' not in prompt and 'Private sibling' not in prompt
    monkeypatch.setattr(source_context, 'allowed', lambda _: False)
    assert cb.build_data_manifest(inputs)['email_threads'] == []


def test_critic_keeps_message_specific_thread_read_limits():
    partial = 'Exact thread unavailable; only the current search excerpt was read.'
    history = 'Earlier thread history. ' * 100
    notes = [history, partial, history, partial, 'Thread read omitted this message; current search snippet only.']
    rows = [{'id': f'm{i}', 'threadId': 'thread', 'body': 'excerpt', 'thread_context': note}
            for i, note in enumerate(notes)]
    evidence = cb.build_data_manifest({'google': {'emails': rows}})['email_threads']
    contexts = [row['thread_context'] for row in evidence]
    assert contexts[0] == history and contexts[1] == partial
    assert 'm0' in contexts[2] and len(contexts[2]) < len(history)
    assert contexts[3] == 'Same thread context as message m1.' and contexts[1] == notes[3]
    assert contexts[4] == notes[4]
