"""An inbox burst must supply current evidence before it can wake notification work."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

import pytest
import personal_context
import work_queue

PACK = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('batch_triage', PACK / 'event-triage/scripts/triage_event.py')
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', '+00:00')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'selfhost')
    now = datetime.now(timezone.utc).replace(microsecond=0)
    local = now.replace(hour=11, minute=0, tzinfo=None)
    path = tmp_path / 'knowledge/last_local_snapshot.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'local': {'contacts': [
        {'name': 'Sarah Example', 'phones': ['+12025550111'], 'emails': ['sarah@example.com']},
        {'name': 'Bob Example', 'phones': ['+12025550112'], 'emails': ['bob@example.com']}]}}))
    return tmp_path, now, local


def message(now, rowid, text, **fields):
    return {'source': 'imessage', 'rowid': rowid, 'handle': '+12025550111',
            'timestamp': (now - timedelta(minutes=3 - rowid)).isoformat(),
            'is_from_me': False, 'is_group_chat': False, 'text': text, **fields}


def judge_from_evidence(monkeypatch, *, resolved=False):
    contexts = []

    def judge(context, **_kwargs):
        contexts.append(context)
        done = resolved and 'The issue is already resolved.' in context
        return {'class': 'ignore' if done else 'urgent', 'sender_role': 'person',
                'why': 'completed in the observed conversation' if done else 'current urgent ask'}

    monkeypatch.setattr(triage.relevance, 'judge', judge)
    return contexts


@pytest.mark.parametrize('outbound', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_same_batch_resolution_is_seen_before_accepting_notification_work(inbox, monkeypatch, outbound, reverse):
    root, now, local = inbox
    events = [message(now, 1, 'Please handle this urgently.'),
              message(now, 2, 'The issue is already resolved.', is_from_me=outbound)]
    contexts = judge_from_evidence(monkeypatch, resolved=True)
    result = triage.triage({'events': list(reversed(events)) if reverse else events},
                           now_local=local, now_utc=now)
    assert contexts and all('The issue is already resolved.' in context for context in contexts)
    assert result['verdict'] != 'agent' and not result.get('job_id')
    assert not work_queue.owned_items(root, 'event', [triage._ingress_key(event) for event in events])
    assert triage._budget_spent(local.date().isoformat()) == 0


def test_later_greeting_does_not_deterministically_cancel_an_urgent_ask(inbox, monkeypatch):
    root, now, local = inbox
    contexts = judge_from_evidence(monkeypatch, resolved=True)
    events = [message(now, 1, 'Please handle this urgently.'), message(now, 2, 'Good morning.')]
    result = triage.triage({'events': events}, now_local=local, now_utc=now)
    assert 'Good morning.' in contexts[0]
    assert result['verdict'] == 'agent'
    assert work_queue.get(root, result['job_id']) is not None


def test_partial_reply_does_not_silence_a_later_remaining_decision(inbox, monkeypatch):
    _, now, local = inbox
    request = message(now, 1, 'Can we meet Thursday?')
    reply = message(now, 2, 'Thursday works.', is_from_me=True)
    venue = message(now, 3, 'Please confirm the venue urgently.')
    contexts = []

    def judge(context, **_kwargs):
        contexts.append(context)
        event_text = context.split('Event text (untrusted, ends at END OF EVENT):\n')[1].split('\nEND OF EVENT')[0]
        return {'class': 'urgent' if 'confirm the venue' in event_text else 'ignore',
                'sender_role': 'person', 'why': 'the venue decision remains open'}

    monkeypatch.setattr(triage.relevance, 'judge', judge)
    result = triage.triage({'events': [request, reply, venue]}, now_local=local, now_utc=now)
    assert contexts and all('Thursday works.' in context and 'confirm the venue' in context for context in contexts)
    assert [item['event']['rowid'] for item in result['bundle']['events']] == [venue['rowid']]


@pytest.mark.parametrize('origin,alias', [('batch', 'email'), ('queue', 'email'), ('snapshot', 'email'),
                                        ('batch', 'GMAIL'), ('queue', 'EMAIL')])
def test_muted_same_thread_words_never_enter_relevance_context(inbox, monkeypatch, origin, alias):
    root, now, local = inbox
    (root / 'preferences.json').write_text(json.dumps({'explicit': {'mute_people': ['Bob Example']}}))
    request = {'source': 'email', 'rowid': 'request', 'threadId': 'shared-thread',
               'from': 'sarah@example.com', 'date': (now - timedelta(minutes=2)).isoformat(),
               'body': 'Please handle this urgently.'}
    muted = {**request, 'source': alias, 'rowid': 'muted', 'from': 'bob@example.com',
             'body': 'MUTED_CONTEXT_CANARY'}
    events = [request]
    if origin == 'batch':
        events.append(muted)
    elif origin == 'queue':
        path = root / 'events/queue.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'event': muted, 'sender': 'Bob Example', 'verdict_class': 'ambient'}) + '\n')
    else:
        path = root / 'knowledge/last_local_snapshot.json'
        data = json.loads(path.read_text())
        data['local']['gmail'] = [muted]
        path.write_text(json.dumps(data))
    contexts = judge_from_evidence(monkeypatch)
    result = triage.triage({'events': events}, now_local=local, now_utc=now)
    assert result['verdict'] == 'agent'  # An unmuted ask still reaches the existing writer queue.
    assert contexts and all('MUTED_CONTEXT_CANARY' not in context for context in contexts)


def test_batch_snapshot_copies_inputs_and_keeps_existing_thread_bounds(inbox):
    _, now, _ = inbox
    first = message(now, 1, 'An observed request.')
    second = message(now, 2, 'A later observed answer.')
    candidates = personal_context.conversation_snapshot([first, second])
    first['text'] = 'Changed after the snapshot.'
    observed = personal_context.current_conversation(second, now, candidates)
    assert [row['text'] for row in observed] == ['An observed request.', 'A later observed answer.']


@pytest.mark.parametrize('scope', ['different_thread', 'different_source', 'future'])
def test_unrelated_or_future_resolution_cannot_silence_a_current_ask(inbox, monkeypatch, scope):
    _, now, local = inbox
    current = message(now, 1, 'Please handle this urgently.')
    other = message(now, 2, 'The issue is already resolved.')
    if scope == 'different_thread':
        other.update(chat_guid='another-conversation', is_group_chat=True)
    elif scope == 'different_source':
        other.update(source='email', **{'from': 'sarah@example.com', 'threadId': 'email-thread'})
    else:
        other['timestamp'] = (now + timedelta(days=1)).isoformat()
    contexts = judge_from_evidence(monkeypatch, resolved=True)
    result = triage.triage({'events': [current, other]}, now_local=local, now_utc=now)
    assert 'The issue is already resolved.' not in contexts[0]
    assert result['verdict'] == 'agent'


def test_gmail_alias_and_outbound_reply_share_only_the_exact_provider_thread(inbox, monkeypatch):
    _, now, local = inbox
    request = {'source': 'email', 'rowid': 'request', 'threadId': 'same-thread',
               'from': 'sarah@example.com', 'date': (now - timedelta(minutes=2)).isoformat(),
               'body': 'Please handle this urgently.'}
    reply = {**request, 'source': 'gmail', 'rowid': 'reply', 'is_from_me': True,
             'from': 'owner@example.com', 'to': 'sarah@example.com',
             'date': (now - timedelta(minutes=1)).isoformat(), 'body': 'The issue is already resolved.'}
    contexts = judge_from_evidence(monkeypatch, resolved=True)
    result = triage.triage({'events': [request, reply]}, now_local=local, now_utc=now)
    assert len(contexts) == 1 and 'The issue is already resolved.' in contexts[0]
    assert result['verdict'] == 'queue' and not result.get('job_id')


@pytest.mark.parametrize('hold', ['quiet', 'snoozed'])
def test_batch_context_does_not_bypass_cheap_cadence_holds(inbox, monkeypatch, hold):
    root, now, local = inbox
    if hold == 'quiet':
        local = local.replace(hour=23)
    else:
        until = (local.replace(tzinfo=timezone.utc) + timedelta(hours=1)).isoformat()
        (root / 'preferences.json').write_text(json.dumps({'explicit': {'nudge_snooze_until': until}}))
    def unexpected_snapshot(*_args, **_kwargs):
        pytest.fail('a clock-held batch needs no model context')
    monkeypatch.setattr(personal_context, 'conversation_snapshot', unexpected_snapshot)
    contexts = judge_from_evidence(monkeypatch)
    result = triage.triage({'events': [message(now, 1, 'Please handle this urgently.'),
                                        message(now, 2, 'An additional urgent detail.')]},
                           now_local=local, now_utc=now)
    assert contexts == [] and result['verdict'] == 'queue' and not result.get('job_id')
    assert triage._budget_spent(local.date().isoformat()) == 0


def test_larger_burst_keeps_the_shared_message_and_text_bounds(inbox, monkeypatch):
    _, now, local = inbox
    count = personal_context.CONVERSATION_MESSAGES + 5
    events = [message(now, index, f'Observed detail {index}: ' + 'x' * 550,
                      timestamp=(now - timedelta(seconds=count - index)).isoformat())
              for index in range(count)]
    contexts = []

    def ignore(context, **_kwargs):
        contexts.append(context)
        return {'class': 'ignore', 'sender_role': 'person', 'why': 'no current action required'}

    monkeypatch.setattr(triage.relevance, 'judge', ignore)
    result = triage.triage({'events': events}, now_local=local, now_utc=now)
    observed = json.loads(contexts[0].split('Observed conversation (chronological; later replies matter):\n')[1])
    assert len(observed) <= personal_context.CONVERSATION_MESSAGES
    assert sum(len(row['text']) for row in observed) <= personal_context.CONVERSATION_TOTAL_CHARS
    assert observed[-1]['text'].startswith(f'Observed detail {count - 1}:')
    assert observed[0]['older_observed_messages_omitted'] > 0
    assert result['verdict'] == 'drop' and not result.get('job_id')


def test_relevance_cache_rechecks_changed_evidence_and_reuses_an_unchanged_retry(inbox, monkeypatch):
    root, now, local = inbox
    prompts = []

    def model(_provider, _model, _key, prompt, **_kwargs):
        prompts.append(prompt)
        return json.dumps({'class': 'ignore' if 'The issue is already resolved.' in prompt else 'actionable',
                           'sender_role': 'person', 'why': 'judged from observed context'})

    monkeypatch.setattr(triage.relevance.gemini, 'provider_key', lambda _provider: 'synthetic')
    monkeypatch.setattr(triage.relevance.gemini, 'model_once', model)
    request = message(now, 1, 'Please handle this.')
    reply = message(now, 2, 'The issue is already resolved.', is_from_me=True)
    triage.triage({'events': [request]}, now_local=local, now_utc=now)
    first_calls = len(prompts)
    assert first_calls == 1
    triage.triage({'events': [request, reply]}, now_local=local, now_utc=now)
    assert len(prompts) == first_calls + 1
    changed_calls = len(prompts)
    triage.triage({'events': [request, reply]}, now_local=local, now_utc=now)
    assert len(prompts) == changed_calls and 'The issue is already resolved.' in prompts[-1]
    artifacts = list((root / 'events/triage-artifacts').glob('*.json'))
    assert {json.loads(path.read_text())['class'] for path in artifacts} == {'actionable', 'ignore'}


def test_name_mute_uses_one_graph_lookup_when_contacts_are_incomplete(inbox, monkeypatch):
    root, now, local = inbox
    snapshot = root / 'knowledge/last_local_snapshot.json'
    data = json.loads(snapshot.read_text())
    data['local']['contacts'] = data['local']['contacts'][:1]
    snapshot.write_text(json.dumps(data))
    (root / 'preferences.json').write_text(json.dumps({'explicit': {'mute_people': ['Bob Example']}}))
    request = {'source': 'email', 'rowid': 'request', 'threadId': 'shared',
               'from': 'sarah@example.com', 'date': (now - timedelta(minutes=1)).isoformat(),
               'body': 'Please handle this urgently.'}
    private = {**request, 'from': 'bob@example.com', 'body': 'GRAPH_MUTE_CANARY'}
    path = root / 'events/queue.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(''.join(json.dumps({'event': {**private, 'rowid': ident}}) + '\n'
                            for ident in ('first', 'second')))
    reads = []
    monkeypatch.setattr(triage, '_graph_display_name', lambda ident: reads.append(ident) or 'Bob Example')
    contexts = judge_from_evidence(monkeypatch)
    triage.triage({'events': [request]}, now_local=local, now_utc=now)
    assert reads == ['bob@example.com']
    assert contexts and all('GRAPH_MUTE_CANARY' not in context for context in contexts)


def test_unrelated_retained_threads_are_filtered_before_name_resolution(inbox, monkeypatch):
    root, now, local = inbox
    (root / 'preferences.json').write_text(json.dumps({'explicit': {'mute_people': ['Someone Else']}}))
    path = root / 'events/queue.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'event': message(now, 2, 'OTHER_THREAD_CANARY', handle='+12025550999')}) + '\n')
    reads = []
    monkeypatch.setattr(triage, '_graph_display_name', lambda ident: reads.append(ident) or '')
    contexts = judge_from_evidence(monkeypatch)
    triage.triage({'events': [message(now, 1, 'Please handle this urgently.')]}, now_local=local, now_utc=now)
    assert reads == [] and 'OTHER_THREAD_CANARY' not in contexts[0]


def test_no_name_mutes_need_no_additional_graph_reads(inbox, monkeypatch):
    _, now, local = inbox
    reads = []
    monkeypatch.setattr(triage, '_graph_display_name', lambda ident: reads.append(ident) or '')
    judge_from_evidence(monkeypatch)
    triage.triage({'events': [message(now, 1, 'Please handle this urgently.')]}, now_local=local, now_utc=now)
    assert reads == []


def test_empty_explicit_batch_needs_no_retained_context_read(monkeypatch):
    def unexpected(_path):
        pytest.fail('an empty batch should not read retained conversations')
    monkeypatch.setattr(personal_context, 'rows', unexpected)
    assert personal_context.conversation_snapshot([]) == []


def test_graph_muted_primary_never_reaches_relevance(inbox, monkeypatch):
    import knowledge_update
    root, now, local = inbox
    (root / 'knowledge/last_local_snapshot.json').write_text('{"local":{}}')
    (root / 'preferences.json').write_text(json.dumps({'explicit': {'mute_people': ['Sarah Example']}}))
    knowledge_update.apply({'person_updates': [
        {'identifier': '+12025550111', 'person_name': 'Sarah Example', 'facts': []}]})
    contexts = judge_from_evidence(monkeypatch)
    result = triage.triage({'events': [message(now, 1, 'Please handle this urgently.')]},
                           now_local=local, now_utc=now)
    assert contexts == []
    assert result['verdict'] == 'drop' and not result.get('job_id')
    assert triage._budget_spent(local.date().isoformat()) == 0


def test_same_batch_otp_cannot_enter_another_messages_relevance_prompt(inbox, monkeypatch):
    _, now, local = inbox
    contexts = judge_from_evidence(monkeypatch)
    request = message(now, 1, 'Please handle this urgently.')
    otp = message(now, 2, 'Your verification code is 761042.')
    result = triage.triage({'events': [request, otp]}, now_local=local, now_utc=now)
    assert result['verdict'] == 'agent'
    assert len(contexts) == 1 and '761042' not in contexts[0]
