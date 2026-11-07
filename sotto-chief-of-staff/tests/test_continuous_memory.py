"""Frozen-time replay of history learning, evidence curation, source loss and owner feedback."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import context_learning as learning
import continuity_resolve
import delivery_effects
import dreamer
import gather_google
import knowledge as kg
import knowledge_update as ku
import memory_cycle
import personal_context
import relevance
import source_context
import usefulness_feedback

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def test_history_observation_counts_report_skips_limits_and_clipped_text():
    rows = [{'id': str(i), 'handle': '+14155550123', 'timestamp': NOW.isoformat(),
             'text': 'x' * (learning.MESSAGE_CHARS + 1)} for i in range(learning.REVIEW_MESSAGES + 2)]
    rows.append({'id': 'group', 'is_group_chat': True, 'text': 'private group'})
    counts = {}
    selected = learning.observations('imessage', rows, NOW, diagnostics=counts)
    assert len(selected) == learning.REVIEW_MESSAGES
    assert counts == {'rows': 163, 'eligible': 162, 'selected': 160,
                      'skipped': 1, 'omitted': 2, 'truncated': 162}
    assert 'private' not in json.dumps(counts)


def test_history_coverage_advances_only_with_success_and_resets_for_next_window(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    monkeypatch.setattr(memory_cycle.prewarm_graph, 'prewarm', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.style_extract, 'extract', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.relationship_pulse, 'observe_history', lambda *a, **k: None)
    row = {'id': 'real-row', 'handle': '+14155550123', 'timestamp': NOW.isoformat(), 'text': 'An observed fact.'}
    fetch = lambda *a: {'status': 'ok', 'rows': [row], 'next_cursor': None, 'complete': True}
    quiet = lambda **k: {'reviewed': 0}
    def fail(*a, **k):
        raise RuntimeError('synthetic extraction failure')
    memory_cycle.run(NOW, fetch, fail, quiet)
    path = Path(memory_cycle.state_path())
    assert 'observation_totals' not in json.loads(path.read_text())['sources']['imessage']
    memory_cycle.run(NOW + timedelta(hours=1), fetch, lambda *a, **k: {'reviewed': 1, 'facts': 0}, quiet)
    first = json.loads(path.read_text())['sources']['imessage']
    assert first['observation_totals']['pages'] == 1
    assert first['observation_totals']['selected'] == 1
    # A blocked initial page belongs to the backfill, never the next incremental window.
    state = json.loads(path.read_text())
    state['sources']['imessage']['excluded_pages'] = {'private-cursor': {'reason': 'SAFETY', 'since': first['since'], 'until': first['until']}}
    path.write_text(json.dumps(state))
    memory_cycle.run(NOW + timedelta(hours=2), fetch, lambda *a, **k: {'reviewed': 1, 'facts': 0}, quiet)
    next_window = json.loads(path.read_text())['sources']['imessage']
    assert next_window['pages'] == 2
    assert next_window['observation_totals']['pages'] == 1
    assert 'private-cursor' in next_window['excluded_pages']
    from history_coverage import window_coverage
    view = window_coverage(next_window)
    assert view['review_scope'] == 'reviewed_window'
    assert view['initial_review']['review_scope'] == 'partial'
    assert view['initial_review']['observation_counts']['selected'] == 1
    assert view['initial_review']['excluded_pages'] == 1
    assert 'private-cursor' not in json.dumps(view)




@pytest.mark.parametrize('bad_page', [
    {'status': 'unavailable', 'rows': [], 'complete': True},
    {'status': 'ok', 'rows': 'PRIVATE', 'complete': True},
    {'status': 'ok', 'rows': [], 'complete': False, 'next_cursor': 'next-page'},
])
def test_unavailable_or_invalid_history_page_never_advances_success(tmp_path, monkeypatch, bad_page):
    from history_coverage import learning_coverage
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: source == 'imessage')
    monkeypatch.setattr(memory_cycle.prewarm_graph, 'prewarm', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.style_extract, 'extract', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.relationship_pulse, 'observe_history', lambda *a, **k: None)
    quiet = lambda **k: {'reviewed': 0}
    memory_cycle.run(NOW, lambda *a: {'status': 'ok', 'rows': [], 'complete': False,
                                    'next_cursor': 'next-page'},
                     lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)
    before = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    memory_cycle.run(NOW + timedelta(hours=1), lambda *a: bad_page,
                     lambda *a, **k: pytest.fail('invalid fetch must not reach learning'), quiet)
    after = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    for field in ('last_read_success', 'last_success', 'cursor', 'pages', 'observation_totals'):
        assert after[field] == before[field]
    view = learning_coverage(tmp_path, ['imessage'])['sources']['imessage']
    assert view['latest_attempt']['status'] == 'failed'
    assert view['latest_attempt']['stage'] == 'fetch'
    if bad_page['status'] == 'unavailable':
        assert view['latest_attempt']['category'] == 'source_unavailable'
    assert 'PRIVATE' not in json.dumps(view)


def test_tier1_conversation_keeps_opposing_short_message_endings_from_queue_and_snapshot(
        tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    first = 'Background about the contract. ' + 'A' * 408 + ' I sent the signed contract.'
    second = 'Background about the contract. ' + 'A' * 412 + ' I have not sent the signed contract.'
    assert len(first) == 467 and len(second) == 480
    base = {'source': 'imessage', 'chat_guid': 'chat-1', 'handle': '+14155550123'}
    now = datetime.now(timezone.utc)
    queued = {**base, 'timestamp': (now - timedelta(minutes=3)).isoformat(), 'text': first}
    snap = {**base, 'timestamp': (now - timedelta(minutes=2)).isoformat(), 'text': second}
    current = {**base, 'timestamp': (now - timedelta(minutes=1)).isoformat(),
               'text': 'Did that reach you?'}
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    queue = tmp_path / 'events/queue.jsonl'
    queue.parent.mkdir(parents=True)
    queue.write_text(json.dumps({'event': queued, 'ts': queued['timestamp']}) + '\n')
    snapshot = tmp_path / 'knowledge/last_local_snapshot.json'
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({'local': {'imessage': [snap]}}))
    messages = personal_context.current_conversation(
        current, now, personal_context.conversation_snapshot())
    assert [row['text'] for row in messages] == [first, second, 'Did that reach you?']
    assert [row['source'] for row in messages] == ['imessage'] * 3
    assert [row['ts'] for row in messages] == sorted(row['ts'] for row in messages)
    assert all(not row['text_clipped'] and row['original_length'] == len(row['text'])
               for row in messages)

    spec = importlib.util.spec_from_file_location(
        'b4a_triage', Path(__file__).resolve().parent.parent / 'event-triage/scripts/triage_event.py')
    triage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(triage)
    seen = []
    monkeypatch.setattr(relevance, 'judge', lambda context, **kw: seen.append(context) or {
        'class': 'ignore', 'sender_role': 'person', 'why': 'fixture'})
    triage._classify_tier1({**current, '_sotto_conversation_rows': personal_context.conversation_snapshot()},
                           'A contact')
    assert len(seen) == 1 and first in seen[0] and second in seen[0]


def test_conversation_budget_preserves_newest_endings_and_discloses_omissions(monkeypatch):
    now = NOW
    base = {'source': 'imessage', 'chat_guid': 'chat-2', 'handle': '+14155550123'}
    rows = [({**base, 'timestamp': (now - timedelta(minutes=20 - i)).isoformat(),
              'text': f'Older {i}: ' + 'x' * 940 + f' ending {i}'}, '', '') for i in range(8)]
    current = {**base, 'timestamp': now.isoformat(), 'text': 'Current decisive ending.'}
    messages = personal_context.current_conversation(current, now, rows)
    assert messages[-1]['text'] == current['text']
    assert sum(len(row['text']) for row in messages) <= personal_context.CONVERSATION_TOTAL_CHARS
    assert messages[0]['older_observed_messages_omitted'] > 0
    assert all(row['text'].endswith(f' ending {i}') for i, row in enumerate(messages[:-1],
               start=8 - len(messages[:-1])))


def test_oversized_conversation_message_discloses_clip_and_keeps_ending():
    event = {'source': 'imessage', 'chat_guid': 'chat-3', 'timestamp': NOW.isoformat(),
             'text': 'Intro ' + 'x' * 6000 + ' I have not sent the signed contract.'}
    message, = personal_context.current_conversation(event, NOW, [])
    assert len(message['text']) <= personal_context.CONVERSATION_TOTAL_CHARS
    assert message['text'].startswith('Intro ')
    assert message['text'].endswith('I have not sent the signed contract.')
    assert message['text_clipped'] is True
    assert message['original_length'] == len(event['text'])
    assert 'omitted' in message['text']


def test_same_timestamp_opposing_messages_do_not_deduplicate_after_projection():
    base = {'source': 'imessage', 'chat_guid': 'chat-3', 'timestamp': NOW.isoformat()}
    prefix = 'Shared context ' + 'a' * 410
    rows = [({**base, 'text': prefix + ' contract was sent'}, '', ''),
            ({**base, 'text': prefix + ' contract was not sent'}, '', '')]
    messages = personal_context.current_conversation(rows[-1][0], NOW, rows)
    assert len(messages) == 2
    assert [row['text'].endswith('sent') for row in messages] == [True, True]
    assert messages[0]['text'] != messages[1]['text']


def test_conversation_message_count_discloses_observed_window_and_preview_stays_bounded():
    base = {'source': 'imessage', 'chat_guid': 'chat-5'}
    rows = [({**base, 'timestamp': (NOW - timedelta(minutes=20 - i)).isoformat(),
              'text': f'Message {i}'}, '', '') for i in range(15)]
    messages = personal_context.current_conversation(rows[-1][0], NOW, rows)
    assert len(messages) == personal_context.CONVERSATION_MESSAGES
    assert messages[0]['older_observed_messages_omitted'] == 3
    assert messages[-1]['text'] == 'Message 14'
    preview = personal_context.conversation_message({**base, 'text': 'a' * 500})
    assert preview['text'] == 'a' * personal_context.CONVERSATION_TEXT_CHARS
    assert preview['text_clipped'] and preview['original_length'] == 500


def test_revoked_conversation_source_yields_no_messages(monkeypatch):
    monkeypatch.setattr(source_context, 'allowed', lambda source: False)
    event = {'source': 'imessage', 'chat_guid': 'chat-4', 'timestamp': NOW.isoformat(),
             'text': 'A private message.'}
    assert personal_context.current_conversation(event, NOW, [(event, '', '')]) == []


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.delenv('SOTTO_UNATTENDED', raising=False)
    # Offline injected models make their unmetered intent explicit; production defaults to hold.
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true')


def fact(text='Alex builds a product design tool.', refs=None, source='observed_message', date='2026-09-06'):
    refs = ['gmail:one'] if refs is None else refs
    return {'person_updates': [{'identifier': 'alex@example.com', 'person_name': 'Alex', 'facts': [
        {'fact': text, 'confidence': .7, 'source': source, 'source_ref': refs[0] if refs else '',
         'evidence_refs': refs, 'observed_date': date, 'memory_type': 'context'}]}]}


def person():
    path = Path(kg.find_person_file(identifier='alex@example.com'))
    return path, kg.parse_person_file(path.read_text())


def test_weekly_pruning_reaches_untouched_people_without_erasing_owner_words(monkeypatch):
    ku.apply(fact(), NOW)
    path, p = person()
    old = next(iter(p.facts.values()))
    from dataclasses import replace
    p.facts['owner'] = replace(old, text='Owner correction.', source='user_edit')
    p.facts['corroborated'] = replace(old, text='Corroborated context.', seen=2)
    kg.write_person_file(str(path), p, NOW)
    later = NOW + timedelta(days=61)
    memory_cycle._prune_weekly(later, memory_cycle.state_path())
    stored = person()[1]
    archived = next(f for f in stored.facts.values() if f.text == old.text)
    assert archived.status == 'archived' and archived.archived_reason == 'stale'
    assert archived.archived_text == old.text and archived.evidence_refs == old.evidence_refs
    assert archived.last == old.last
    assert stored.facts['owner'].status == stored.facts['corroborated'].status == 'active'
    checkpoint = json.loads(Path(memory_cycle.state_path()).read_text())['pruning']
    assert checkpoint == {'status': 'completed', 'completed_at': later.timestamp(),
                          'people': 1, 'pruned': 1, 'unreadable': 0}
    # A process restart reads this checkpoint, rather than rewriting unchanged memory.
    before = path.read_bytes()
    real = memory_cycle.knowledge_update.prune_stale_memory
    monkeypatch.setattr(memory_cycle.knowledge_update, 'prune_stale_memory',
                        lambda *a: pytest.fail('must not repeat inside the week'))
    memory_cycle._prune_weekly(later + timedelta(days=6), memory_cycle.state_path())
    assert path.read_bytes() == before
    monkeypatch.setattr(memory_cycle.knowledge_update, 'prune_stale_memory', real)
    memory_cycle._prune_weekly(later + timedelta(days=7), memory_cycle.state_path())
    assert json.loads(Path(memory_cycle.state_path()).read_text())['pruning']['pruned'] == 0
    assert path.read_bytes() == before


def test_weekly_pruning_skips_an_unreadable_person_file_and_completes():
    ku.apply(fact(), NOW)
    bad = Path(kg.people_dir()) / '0000-bad.md'
    bad.write_text('---\nname: [unclosed\n---\n')
    original = bad.read_bytes()
    later = NOW + timedelta(days=61)
    memory_cycle._prune_weekly(later, memory_cycle.state_path())
    assert next(iter(person()[1].facts.values())).archived_reason == 'stale'
    checkpoint = json.loads(Path(memory_cycle.state_path()).read_text())['pruning']
    assert checkpoint == {'status': 'completed', 'completed_at': later.timestamp(),
                          'people': 1, 'pruned': 1, 'unreadable': 1}
    assert bad.read_bytes() == original  # skipped, never rewritten
    # Completed: the next 15-minute heartbeat does not rescan inside the week.
    memory_cycle._prune_weekly(later + timedelta(minutes=15), memory_cycle.state_path())
    assert json.loads(Path(memory_cycle.state_path()).read_text())['pruning'] == checkpoint


def test_weekly_pruning_failure_retries_without_advancing_completion(monkeypatch):
    ku.apply(fact(), NOW)
    later = NOW + timedelta(days=61)
    real = memory_cycle.knowledge_update.prune_stale_memory
    def fail(*args):
        raise OSError('private content must not enter the receipt')
    monkeypatch.setattr(memory_cycle.knowledge_update, 'prune_stale_memory', fail)
    memory_cycle._prune_weekly(later, memory_cycle.state_path())
    checkpoint = json.loads(Path(memory_cycle.state_path()).read_text())['pruning']
    assert checkpoint == {'status': 'failed', 'error': 'OSError'}
    assert next(iter(person()[1].facts.values())).status == 'active'
    monkeypatch.setattr(memory_cycle.knowledge_update, 'prune_stale_memory', real)
    memory_cycle._prune_weekly(later, memory_cycle.state_path())
    assert next(iter(person()[1].facts.values())).archived_reason == 'stale'


def test_weekly_pruning_runs_without_paid_learning_allowance(monkeypatch):
    ku.apply(fact(), NOW)
    monkeypatch.delenv('SOTTO_BACKGROUND_UNMETERED')
    monkeypatch.delenv('SOTTO_MODEL_PROXY_URL', raising=False)
    receipt = memory_cycle.run(NOW + timedelta(days=61),
                               lambda *a: pytest.fail('no source fetch without allowance'),
                               lambda *a, **k: pytest.fail('no extraction'),
                               lambda *a, **k: pytest.fail('no curation'))
    assert receipt['dreamer']['status'] == 'held'
    assert next(iter(person()[1].facts.values())).archived_reason == 'stale'


def test_same_or_overlapping_evidence_does_not_confirm_itself():
    ku.apply(fact(refs=['gmail:one', 'imessage:two']), NOW)
    for refs in (['gmail:one'], ['imessage:two'], ['gmail:one', 'imessage:two']):
        assert ku.apply(fact(refs=refs), NOW)['applied']['confirmed'] == 0
    initial = next(iter(person()[1].facts.values()))
    assert initial.seen == 1 and initial.conf == .7 and initial.last == '2026-09-06'
    assert ku.apply(fact(refs=['gmail:three']), NOW)['applied']['confirmed'] == 1
    assert next(iter(person()[1].facts.values())).seen == 2


def test_reobservation_without_new_evidence_does_not_refresh_recency():
    """A ref-less or repeated proposal cannot prove when the underlying fact was observed."""
    ku.apply(fact(refs=['gmail:one']), NOW)
    later = NOW + timedelta(days=30)
    assert ku.apply(fact(refs=[], date='2026-10-06'), later)['applied']['confirmed'] == 0
    restated = next(iter(person()[1].facts.values()))
    assert restated.seen == 1 and restated.conf == .7 and restated.last == '2026-09-06'


def test_repeated_evidence_at_seen_two_does_not_refresh_recency():
    ku.apply(fact(refs=['gmail:one']), NOW)
    ku.apply(fact(refs=['gmail:two'], date='2026-09-07'), NOW + timedelta(days=1))
    confirmed = next(iter(person()[1].facts.values()))
    assert confirmed.seen == 2 and confirmed.last == '2026-09-07'
    ku.apply(fact(refs=['gmail:two'], date='2026-10-06'), NOW + timedelta(days=30))
    repeated = next(iter(person()[1].facts.values()))
    assert repeated.seen == 2 and repeated.last == '2026-09-07'


def test_conflicting_history_stays_separate_and_cannot_undo_owner_correction():
    ku.apply(fact('Alex is the founder of Acme.'), NOW)
    ku.apply(fact('Alex is NOT the founder of Acme.', ['gmail:two']), NOW)
    assert len([f for f in person()[1].facts.values() if f.status == 'active']) == 2
    correction = fact('Alex leads design at Acme, per the user.', ['user-correction'], 'user_edit')
    correction['person_updates'][0]['facts'][0]['change_type'] = 'correction'
    ku.apply(correction, NOW)
    ku.apply(fact('Alex leads engineering design at Acme.', ['gmail:old'], date='2026-08-01'), NOW)
    fixed = [f for f in person()[1].facts.values() if f.source == 'user_edit'][0]
    assert fixed.status == 'active' and fixed.seen == 1
    assert kg.effective_confidence(fixed, NOW + timedelta(days=365)) == .7


def records():
    return learning.observations('gmail', [
        {'id': 'a', 'from': 'Alex <alex@example.com>', 'date': '2026-09-05T10:00:00Z',
         'body': 'Could you confirm Tuesday? We build a product design tool.'},
        {'id': 'b', 'from': 'Me <me@example.com>', 'to': 'Alex <alex@example.com>', 'isSent': True,
         'date': '2026-09-06T10:00:00Z', 'body': 'Confirmed, see you Tuesday.'}], NOW)


def extraction():
    return {'people': [{'subject_id': 'p1', 'facts': [
        {'text': 'Alex builds a product design tool.', 'refs': ['gmail:a']}]}]}


def test_history_replay_learns_only_durable_facts_and_does_not_mint_open_loops(tmp_path):
    def model(prompt, config, **kwargs):
        assert 'Confirmed, see you Tuesday.' in prompt
        assert kwargs['schema'] == learning.SCHEMA
        return json.dumps(extraction())
    for _ in range(2):
        assert learning.learn(records(), model, NOW) == {'reviewed': 2, 'facts': 1}
    assert len(person()[1].facts) == 1
    assert next(iter(person()[1].facts.values())).seen == 1
    assert not (tmp_path / 'events/queue.jsonl').exists()
    assert not (tmp_path / 'knowledge/continuity').exists()
    assert not (tmp_path / 'knowledge/episodes.json').exists()


def test_resolved_obligation_stays_terminal_when_history_learning_reviews_the_exchange(tmp_path):
    opened = continuity_resolve.resolve({'today': '2026-09-05', 'new_actions': [{
        'type': 'reply', 'channel': 'gmail', 'contactName': 'Alex',
        'contactIdentifier': 'alex@example.com', 'contextSummary': 'Confirm Tuesday meeting time',
    }]}, NOW - timedelta(days=2))
    assert len(opened['active']) == 1
    obligation = opened['active'][0]
    update = {
        'loopId': obligation['anchor_key'],
        'loopVersion': delivery_effects.loop_version(obligation),
        'status': 'resolved',
        'evidence': [{
            'sourceType': 'email', 'sourceId': 'b',
            'snippet': 'Confirmed, see you Tuesday.',
        }],
    }
    # The receipt compose_brief stamps once its critic pass reviewed this closure.
    update['criticReceipt'] = continuity_resolve.record_critic_approval(update)
    closed = continuity_resolve.resolve({'today': '2026-09-07', 'emails': [{
        'id': 'b', 'from': 'Me <me@example.com>', 'to': 'Alex <alex@example.com>',
        'isSent': True, 'date': '2026-09-06T10:00:00Z', 'body': 'Confirmed, see you Tuesday.',
    }], 'loop_updates': [update]}, NOW)
    assert len(closed['resolved']) == 1 and closed['active'] == []

    def model(prompt, config, **kwargs):
        assert 'Confirmed, see you Tuesday.' in prompt
        return json.dumps({'people': [{'subject_id': 'p1', 'facts': []}]})

    assert learning.learn(records(), model, NOW) == {'reviewed': 2, 'facts': 0}
    assert continuity_resolve.resolve({'today': '2026-09-07'}, NOW)['active'] == []
    assert all(not kg.parse_person_file(path.read_text()).facts
               for path in tmp_path.glob('knowledge/people/*.md'))


def test_unbound_or_revoked_extraction_writes_nothing(tmp_path, monkeypatch):
    invalid = extraction()
    invalid['people'][0]['facts'][0]['refs'].append('gmail:invented')
    with pytest.raises(ValueError, match='unbound'):
        learning.learn(records(), lambda *a, **k: json.dumps(invalid), NOW)
    assert not list(tmp_path.glob('knowledge/people/*.md'))
    def revoked(*args, **kwargs):
        monkeypatch.setattr(learning, 'allowed', lambda _: False)
        return json.dumps(extraction())
    with pytest.raises(RuntimeError, match='consent changed'):
        learning.learn(records(), revoked, NOW)
    assert not list(tmp_path.glob('knowledge/people/*.md'))


def test_long_conversation_provider_and_writer_share_evidence_limit(tmp_path):
    # Live iMessage history supplied 18 valid citations; the writer rejected the whole page
    # because the provider had never been told its 12-reference bound.
    observed = [{**records()[0], 'ref': f'gmail:{i}'} for i in range(18)]
    reply = extraction()
    learned_fact = reply['people'][0]['facts'][0]
    learned_fact['refs'] = [r['ref'] for r in observed]
    with pytest.raises(ValueError, match='unbound'):
        learning.learn(observed, lambda *a, **k: json.dumps(reply), NOW)
    assert not list(tmp_path.glob('knowledge/people/*.md'))

    def bounded_model(prompt, config, **kwargs):
        person_schema = kwargs['schema']['properties']['people']['items']['properties']
        refs_schema = person_schema['facts']['items']['properties']['refs']
        cap = refs_schema['maxItems']
        assert cap == learning.MAX_REFS and refs_schema['minItems'] == 1
        assert f'1–{cap}' in kwargs['system']
        learned_fact['refs'] = [r['ref'] for r in observed[-cap:]]
        return json.dumps(reply)
    assert learning.learn(observed, bounded_model, NOW)['facts'] == 1
    assert next(iter(person()[1].facts.values())).evidence_refs == sorted(learned_fact['refs'])


def test_dreamer_binds_ids_detects_races_and_does_not_refresh_evidence():
    ku.apply(fact(), NOW)
    path, before = person()
    def curate(prompt, config, **kwargs):
        candidates = json.loads(prompt)
        assert kwargs['schema'] == dreamer.SCHEMA
        return json.dumps({'people': [{'id': p['id'], 'summary_refs': list(p['facts'])[:1], 'conflicts': []}
                                      for p in candidates]})
    result = dreamer.run(curate, NOW)
    assert result['reviewed'] == 1
    after = person()[1]
    assert after.summary_refs and after.summary == 'Alex builds a product design tool.'
    assert after.facts == before.facts and after.updated_at == before.updated_at
    assert dreamer.run(lambda *a, **k: pytest.fail('unchanged memory must not call a model'), NOW)['reviewed'] == 0
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ku.apply(fact('Alex enjoys trail running.', ['imessage:new']), NOW)
    assert ku.consolidate(after.canonical_id, digest, [], [], NOW)['status'] == 'changed_during_review'


def test_dreamer_ignores_bookkeeping_but_reacts_to_provenance():
    ku.apply(fact(), NOW)
    calls = []
    def curate(prompt, config, **kwargs):
        calls.append(json.loads(prompt))
        return json.dumps({'people': [{'id': p['id'], 'summary_refs': [], 'conflicts': []}
                                      for p in calls[-1]]})
    assert dreamer.run(curate, NOW)['reviewed'] == 1
    path, p = person()
    # A writer-only timestamp change rewrites the file but does not alter Dreamer's evidence.
    p.updated_at = '2026-09-08T12:00:00Z'
    p.updated_by = 'contact_sync'
    kg.write_person_file(str(path), p, NOW + timedelta(days=1))
    assert dreamer.run(lambda *a, **k: pytest.fail('bookkeeping must not curate'), NOW)['reviewed'] == 0
    # Independent evidence provenance is an input to curation and must wake it.
    p = person()[1]
    next(iter(p.facts.values())).evidence_refs.append('imessage:two')
    kg.write_person_file(str(path), p, NOW + timedelta(days=1))
    assert dreamer.run(curate, NOW + timedelta(days=1))['reviewed'] == 1
    assert len(calls) == 2


def test_dreamer_ignores_a_repeated_proposal_but_re_dreams_changed_wording():
    ku.apply(fact(), NOW)
    calls = []
    def curate(prompt, config, **kwargs):
        calls.append(json.loads(prompt))
        return json.dumps({'people': [{'id': p['id'], 'summary_refs': [], 'conflicts': []}
                                      for p in calls[-1]]})
    assert dreamer.run(curate, NOW)['reviewed'] == 1
    later = NOW + timedelta(days=30)
    assert ku.apply(fact(refs=[], date='2026-10-06'), later)['applied']['confirmed'] == 0
    assert next(iter(person()[1].facts.values())).last == '2026-09-06'
    assert dreamer.run(lambda *a, **k: pytest.fail('a repeated proposal is not new evidence'),
                       later)['reviewed'] == 0
    path, p = person()
    next(iter(p.facts.values())).text = 'Alex builds an agent-memory tool.'
    kg.write_person_file(str(path), p, later)
    assert dreamer.run(curate, later)['reviewed'] == 1
    assert len(calls) == 2


def test_dreamer_does_not_checkpoint_a_concurrent_post_apply_change(monkeypatch):
    ku.apply(fact(), NOW)
    real = dreamer.ku.consolidate
    def concurrent(*args, **kwargs):
        result = real(*args, **kwargs)
        ku.apply(fact('Alex joined a new board.', ['gmail:concurrent']), NOW)
        return result
    monkeypatch.setattr(dreamer.ku, 'consolidate', concurrent)
    def curate(prompt, config, **kwargs):
        people = json.loads(prompt)
        return json.dumps({'people': [{'id': p['id'], 'summary_refs': [], 'conflicts': []}
                                      for p in people]})
    # A completed write that races before coverage is saved is not reviewed work.
    assert dreamer.run(curate, NOW)['reviewed'] == 0
    state = json.loads((Path(kg.data_root()) / 'knowledge/dreamer.json').read_text())
    assert person()[1].canonical_id not in state.get('reviewed', {})


def test_dreamer_checkpoints_after_consolidating_a_person_file_carrying_a_bare_cr():
    """A fact whose own text carries a lone CR reaches disk verbatim — write_person_file only
    translates newlines. Checkpointing must hash the file's BYTES: read_text() would silently fold
    that CR into an LF, the digest would never match what consolidate wrote, and the same person
    would be re-reviewed by every run forever."""
    ku.apply(fact('Alex builds a product design tool.\rIt ships in October.'), NOW)
    path, _ = person()
    assert b'\r' in path.read_bytes(), 'the CR must survive into the person file'

    def curate(prompt, config, **kwargs):
        people = json.loads(prompt)
        return json.dumps({'people': [{'id': p['id'], 'summary_refs': [], 'conflicts': []}
                                      for p in people]})

    assert dreamer.run(curate, NOW)['reviewed'] == 1
    state = json.loads((Path(kg.data_root()) / 'knowledge/dreamer.json').read_text())
    assert person()[1].canonical_id in state['reviewed']
    assert dreamer.run(lambda *a, **k: pytest.fail('checkpoint must be quiet'), NOW)['reviewed'] == 0


def test_dreamer_preserves_manual_summary_and_rejects_unknown_references():
    ku.apply(fact(), NOW)
    path, p = person()
    p.summary = 'This is the summary I wrote myself.'
    p.updated_by = 'user_edit'
    kg.write_person_file(str(path), p, NOW)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        ku.consolidate(p.canonical_id, digest, ['invented'], [], NOW)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    result = ku.consolidate(p.canonical_id, digest, list(p.facts), [], NOW)
    assert result['protected_summary'] and person()[1].summary == p.summary


def test_history_checkpoint_resumes_failed_page_and_is_quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cycle, 'PAGES_PER_RUN', 1)
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    monkeypatch.setattr(memory_cycle.prewarm_graph, 'prewarm', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.style_extract, 'extract', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.relationship_pulse, 'observe_history', lambda *a, **k: None)
    sample = records()
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a, **kw: sample)
    calls = []
    def fetch(source, window):
        calls.append((source, window['since'], window['until'], window['cursor']))
        return {'status': 'ok', 'rows': [], 'next_cursor': 80, 'complete': False}
    def broken(*a, **k):
        raise RuntimeError('provider down')
    quiet = lambda **k: {'reviewed': 0}
    memory_cycle.run(NOW, fetch, broken, quiet)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert state['sources']['imessage']['cursor'] is None
    assert state['sources']['imessage']['error_stage'] == 'memory_extraction'
    assert 'provider down' not in json.dumps(state)
    memory_cycle.run(NOW + timedelta(hours=1), fetch,
                     lambda *a, **k: {'reviewed': 2, 'facts': 0}, quiet)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert state['sources']['imessage']['cursor'] == 80
    assert 'error_stage' not in state['sources']['imessage']
    assert calls[0] == calls[1]  # frozen bounds survive retry
    assert not (tmp_path / 'events/queue.jsonl').exists()
    assert not (tmp_path / 'events/outbox.json').exists()
    memory_cycle.run(NOW + timedelta(hours=2), fetch,
                     lambda *a, **k: {'reviewed': 2, 'facts': 0}, quiet)


def test_missing_background_budget_configuration_holds_before_fetch_or_cursor_change(tmp_path, monkeypatch):
    monkeypatch.delenv('SOTTO_BACKGROUND_UNMETERED')
    monkeypatch.delenv('SOTTO_MODEL_PROXY_URL', raising=False)
    monkeypatch.delenv('SOTTO_MODEL_PROXY_TOKEN', raising=False)
    path = Path(memory_cycle.state_path())
    path.parent.mkdir(parents=True)
    original = {'next_source': 2, 'sources': {'gmail': {'cursor': 'keep-me'}}}
    path.write_text(json.dumps(original))
    receipt = memory_cycle.run(NOW, lambda *a: pytest.fail('must not fetch'),
                               lambda *a, **k: pytest.fail('must not learn'),
                               lambda **k: pytest.fail('must not curate'))
    assert receipt == {'history': [], 'dreamer': {
        'status': 'held', 'reason': 'background_budget_not_configured'}}
    state = json.loads(path.read_text())
    assert state['next_source'] == original['next_source']
    assert state['sources'] == original['sources']
    assert state['receipt'] == receipt


def test_transient_permission_denial_holds_cursor_and_resumes(tmp_path, monkeypatch):
    path = Path(memory_cycle.state_path())
    path.parent.mkdir(parents=True)
    checkpoint = {'since': 1, 'until': 2, 'cursor': 44, 'complete': False, 'pages': 3,
                  'rows': 80, 'reviewed': 70}
    path.write_text(json.dumps({'next_source': 0, 'sources': {'imessage': checkpoint}}))
    permitted = {'value': False}
    monkeypatch.setattr(memory_cycle, 'allowed',
                        lambda source: permitted['value'] and source == 'imessage')
    quiet = lambda **k: {'reviewed': 0}
    receipt = memory_cycle.run(NOW, lambda *a: pytest.fail('forbidden source must not fetch'),
                               lambda *a, **k: pytest.fail('forbidden source must not learn'), quiet)
    held = json.loads(path.read_text())
    assert held['sources']['imessage'] == checkpoint
    assert receipt['history'][0] == {'source': 'imessage', 'status': 'held',
                                     'error_code': 'source_not_allowed'}

    permitted['value'] = True
    monkeypatch.setattr(memory_cycle.prewarm_graph, 'prewarm', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.style_extract, 'extract', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.relationship_pulse, 'observe_history', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a, **kw: [])
    cursors = []
    def fetch(source, window):
        cursors.append(window['cursor'])
        return {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True}
    memory_cycle.run(NOW + timedelta(hours=1), fetch,
                     lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)
    assert cursors == [44]


@pytest.mark.parametrize('reason', ['background_budget_exhausted', 'background_provider_unsupported',
                                    'model_admission_busy'])
def test_background_budget_rejection_holds_cursor_and_stops_remaining_work(tmp_path, monkeypatch, reason):
    from urllib.error import HTTPError
    monkeypatch.delenv('SOTTO_BACKGROUND_UNMETERED')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant')
    monkeypatch.setattr(memory_cycle.gemini_transport, '_background_capability_preflight', lambda **k: None)
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    observed = records()
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a, **kw: observed)
    fetches, curations = [], []
    def fetch(*args):
        fetches.append(True)
        return {'status': 'ok', 'rows': [], 'next_cursor': 'next', 'complete': False}
    def rejected(*args, **kwargs):
        if reason == 'background_budget_exhausted':
            raise HTTPError('https://proxy.example/native', 402, 'budget', {}, None)
        if reason == 'model_admission_busy':
            raise HTTPError('https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent',
                            429, 'busy', {'X-Sotto-Admission': 'busy'}, None)
        raise memory_cycle.gemini_transport.BackgroundModelHeldError(reason, 'held')
    receipt = memory_cycle.run(NOW, fetch, rejected,
                               lambda **k: curations.append(True) or {'reviewed': 0})
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(fetches) == 1 and curations == []
    assert state['sources']['imessage']['cursor'] is None
    assert 'retry_after' not in state['sources']['imessage']
    assert receipt['history'][0]['status'] == 'held'
    assert receipt['dreamer'] == {'status': 'held', 'reason': reason}


@pytest.mark.parametrize('mode', ['self_host', 'managed'])
def test_unsupported_proxy_model_holds_before_reading_history(tmp_path, monkeypatch, mode):
    import io
    monkeypatch.delenv('SOTTO_BACKGROUND_UNMETERED')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant')
    monkeypatch.delenv('SOTTO_BRIEF_MODEL', raising=False)
    monkeypatch.setenv('SOTTO_GEMINI_MODEL', 'gemini-test-not-supported')
    response = {'version': 1, 'finite': mode != 'managed', 'can_admit': True,
                'supported_native_models': ['gemini-3.8-flash']}
    monkeypatch.setattr(memory_cycle.gemini_transport.urllib.request, 'urlopen',
                        lambda *a, **k: io.BytesIO(json.dumps(response).encode()))
    receipt = memory_cycle.run(NOW, lambda *a: pytest.fail('must not fetch'),
                               lambda *a, **k: pytest.fail('must not learn'),
                               lambda **k: pytest.fail('must not curate'))
    assert receipt == {'history': [], 'dreamer': {
        'status': 'held', 'reason': 'background_model_unsupported'}}
    assert 'sources' not in json.loads(Path(memory_cycle.state_path()).read_text())


def test_feedback_is_an_explicit_bound_example_not_a_permission(tmp_path, monkeypatch):
    brief = tmp_path / 'briefs/2026-09-07_morning.json'
    brief.parent.mkdir()
    text = 'The preschool sunscreen waiver still needs completing before tomorrow.'
    brief.write_text(json.dumps({'brief_text': text, '_source_permissions': []}))
    brief.with_name('2026-09-07.morning.delivered').write_text('run')
    usefulness_feedback.record(brief.stem, 'useful', text, 'This is exactly what I need help remembering.')
    rendered = personal_context.render_feedback()
    assert brief.stem in rendered and '"outcome": "useful"' in rendered
    assert text in rendered
    assert text in relevance.judgment_prompt('another school deadline')
    assert not (tmp_path / 'preferences.json').exists()
    with pytest.raises(ValueError, match='verbatim'):
        usefulness_feedback.record(brief.stem, 'not_useful', 'invented text')
    monkeypatch.setenv('SOTTO_UNATTENDED', 'true')
    with pytest.raises(ValueError, match='owner'):
        usefulness_feedback.record(brief.stem, 'not_useful')


def test_feedback_keeps_items_independent_and_archive_controls_retention(tmp_path):
    brief = tmp_path / 'briefs/2026-09-07_morning.json'
    brief.parent.mkdir()
    first = 'The preschool waiver is due tomorrow.'
    second = 'The quarterly tax estimate is due Friday.'
    brief.write_text(json.dumps({'brief_text': first + '\n\n' + second,
                                 '_source_permissions': []}))
    marker = brief.with_name('2026-09-07.morning.delivered')
    marker.write_text('run')
    usefulness_feedback.record(brief.stem, 'useful', first)
    usefulness_feedback.record(brief.stem, 'not_useful', second)
    examples = personal_context.feedback()
    assert [(e['outcome'], e['item']) for e in examples] == [('useful', first), ('not_useful', second)]
    # A correction replaces only the matching item in the bounded reader.
    usefulness_feedback.record(brief.stem, 'useful', second)
    examples = personal_context.feedback()
    assert [(e['outcome'], e['item']) for e in examples] == [('useful', first), ('useful', second)]
    outcomes = (tmp_path / 'outcomes.jsonl').read_text()
    assert first not in outcomes and second not in outcomes
    marker.unlink()
    assert personal_context.feedback() == []


def test_feedback_archive_requires_current_source_consent(tmp_path, monkeypatch):
    brief = tmp_path / 'briefs/2026-09-07_morning.json'
    brief.parent.mkdir()
    text = 'A private message-derived item.'
    brief.write_text(json.dumps({'brief_text': text, '_source_permissions': ['imessage']}))
    brief.with_name('2026-09-07.morning.delivered').write_text('run')
    source_state = tmp_path / 'config/source-state.json'
    source_state.parent.mkdir()
    source_state.write_text(json.dumps({'sources': {'imessage': {'status': 'ok'}}}))
    assert usefulness_feedback.items()[0]['text'] == text
    usefulness_feedback.record(brief.stem, 'useful', text)
    source_state.write_text(json.dumps({'sources': {'imessage': {'status': 'disabled'}}}))
    assert usefulness_feedback.items() == []
    assert personal_context.feedback() == []

    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant-one')
    (tmp_path / 'config/managed-capabilities.json').write_text(json.dumps({
        'tenant_id': 'tenant-one',
        'sources': {'imessage': {'consented': False, 'connected': True}}}))
    assert usefulness_feedback.items() == []

    # Legacy archives and offered drafts cannot prove source consent, so neither exposes prose.
    brief.write_text(json.dumps({'brief_text': text}))
    assert usefulness_feedback.items() == []
    drafts = tmp_path / 'events/drafts.jsonl'
    drafts.parent.mkdir(exist_ok=True)
    drafts.write_text(json.dumps({'text': text, 'channel': 'imessage'}) + '\n')
    assert usefulness_feedback.items() == []


def test_feedback_excerpt_must_identify_one_item(tmp_path):
    brief = tmp_path / 'briefs/2026-09-07_morning.json'
    brief.parent.mkdir()
    repeated = 'Same item.'
    brief.write_text(json.dumps({'brief_text': repeated + '\n' + repeated,
                                 '_source_permissions': []}))
    brief.with_name('2026-09-07.morning.delivered').write_text('run')
    with pytest.raises(ValueError, match='exactly one'):
        usefulness_feedback.record(brief.stem, 'useful', repeated)


def test_feedback_excerpt_counts_overlapping_occurrences(tmp_path):
    """A self-overlapping excerpt has two equally valid starts; a non-overlapping scan sees one."""
    brief = tmp_path / 'briefs/2026-09-07_morning.json'
    brief.parent.mkdir()
    brief.write_text(json.dumps({'brief_text': 'na na na batman.', '_source_permissions': []}))
    brief.with_name('2026-09-07.morning.delivered').write_text('run')
    with pytest.raises(ValueError, match='exactly one'):
        usefulness_feedback.record(brief.stem, 'useful', 'na na')


@pytest.mark.parametrize('lost', ['archive', 'marker', 'consent', 'provenance'])
@pytest.mark.parametrize('legacy', [False, True])
def test_whole_brief_feedback_requires_authorized_archive(tmp_path, lost, legacy):
    brief = tmp_path / 'briefs/2026-09-07_morning.json'
    brief.parent.mkdir()
    brief.write_text(json.dumps({'brief_text': 'A useful morning brief.',
                                 '_source_permissions': ['imessage']}))
    marker = brief.with_name('2026-09-07.morning.delivered')
    marker.touch()
    source = tmp_path / 'config/source-state.json'
    source.parent.mkdir()
    source.write_text(json.dumps({'sources': {'imessage': {'status': 'ok'}}}))
    usefulness_feedback.record(brief.stem, 'useful')
    if legacy:
        outcomes = tmp_path / 'outcomes.jsonl'
        row = json.loads(outcomes.read_text())
        row.pop('item_locator')
        outcomes.write_text(json.dumps(row) + '\n')
    assert len(personal_context.feedback()) == 1
    if lost == 'archive':
        brief.unlink()
    elif lost == 'marker':
        marker.unlink()
    elif lost == 'consent':
        source.write_text(json.dumps({'sources': {'imessage': {'status': 'disabled'}}}))
    else:
        brief.write_text(json.dumps({'brief_text': 'Legacy archive without provenance.'}))
    assert personal_context.feedback() == []


def test_gmail_uses_real_cursor_and_does_not_skip_failed_get(monkeypatch):
    import base64
    from types import SimpleNamespace
    invocations = []
    class Gmail:
        def users(self): return self
        def messages(self): return self
        def list(self, **params):
            invocations.append(params)
            return SimpleNamespace(execute=lambda: {'messages': [{'id': 'a'}], 'nextPageToken': 'next'})
        def get(self, **params):
            return SimpleNamespace(execute=lambda: {'id': params['id'], 'internalDate': str(int(NOW.timestamp())*1000),
                'labelIds': ['SENT'], 'payload': {'mimeType': 'text/plain', 'headers': [
                    {'name': 'From', 'value': 'Me <me@example.com>'}, {'name': 'To', 'value': 'Alex <alex@example.com>'}],
                    'body': {'data': base64.urlsafe_b64encode(b'Confirmed, see you Tuesday.').decode()}}})
    service = Gmail()
    page = gather_google.history_page(int(NOW.timestamp())-86400, int(NOW.timestamp()), 'prior', service=service, now=NOW)
    assert invocations[0]['pageToken'] == 'prior'
    assert page['next_cursor'] == 'next' and not page['complete']
    assert page['rows'][0]['isSent'] is True and page['rows'][0]['body'] == 'Confirmed, see you Tuesday.'
    monkeypatch.setattr(service, 'get', lambda **k: (_ for _ in ()).throw(RuntimeError('get failed')))
    with pytest.raises(RuntimeError, match='get failed'):
        gather_google.history_page(int(NOW.timestamp())-86400, int(NOW.timestamp()), service=service, now=NOW)


def test_gmail_adapter_rejects_its_own_impossible_window_with_the_shared_typed_error(monkeypatch):
    """The adapter's own deterministic rejection must reach the protocol latch. A frozen `since`
    ages past Gmail's 366-day bound eventually; a plain ValueError would be hot-retried hourly
    forever instead of parking with one compatibility probe a day."""
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    aged = int(NOW.timestamp()) - 400 * 86400
    with pytest.raises(source_context.HistoryProtocolError) as error:
        gather_google.history_page(aged, int(NOW.timestamp()), service=object(), now=NOW)
    assert error.value.code == 'invalid_history_window'


def test_gmail_adapter_names_a_dead_page_token_without_quoting_the_provider(monkeypatch):
    """Only a page-token request rejected as an invalid argument is a dead cursor. The same 400
    with no token resumed is a broken request, and the provider's message never leaves the
    adapter — a reason phrase can quote private content and must not reach the state file."""
    from types import SimpleNamespace
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    body = json.dumps({'error': {'code': 400, 'status': 'INVALID_ARGUMENT',
                                 'message': 'Invalid pageToken for PRIVATE SUBJECT LINE',
                                 'errors': [{'reason': 'invalidArgument'}]}}).encode()
    rejected = type('HttpError', (Exception,), {})()
    rejected.resp, rejected.content = SimpleNamespace(status=400), body

    class Gmail:
        def users(self): return self
        def messages(self): return self
        def list(self, **params): return SimpleNamespace(
            execute=lambda: (_ for _ in ()).throw(rejected))
    since, until = int(NOW.timestamp()) - 86400, int(NOW.timestamp())
    with pytest.raises(source_context.HistoryContinuationError) as error:
        gather_google.history_page(since, until, 'stale-token', service=Gmail(), now=NOW)
    assert error.value.code == 'expired_continuation_token'
    assert 'PRIVATE' not in repr(error.value) and 'PRIVATE' not in str(error.value)
    with pytest.raises(Exception) as raw:
        gather_google.history_page(since, until, '', service=Gmail(), now=NOW)
    assert not isinstance(raw.value, source_context.HistoryProtocolError)


def test_default_cycle_hands_the_dreamer_one_frozen_evidence_batch(tmp_path, monkeypatch):
    """The production handoff: memory_cycle freezes one evidence batch and passes it to the real
    dreamer.run, so a cycle prepares exactly once instead of selecting candidates twice."""
    _quiet_memory_inputs(monkeypatch)
    ku.apply(fact(), NOW)
    prepared, prompts = [], []
    real_prepare = dreamer.prepare
    def counted():
        prepared.append(real_prepare())
        return prepared[-1]
    monkeypatch.setattr(dreamer, 'prepare', counted)
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'offline-test-key')
    def model(provider, model_name, key, prompt, **kwargs):
        prompts.append(json.loads(prompt))
        return json.dumps({'people': [{'id': row['id'], 'summary_refs': [], 'conflicts': []}
                                      for row in prompts[-1]]})
    monkeypatch.setattr(dreamer.gemini, 'model_once', model)
    complete = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True}
    receipt = memory_cycle.run(NOW, complete, lambda *a, **k: {'reviewed': 0, 'facts': 0})
    assert receipt['dreamer']['reviewed'] == 1
    assert len(prepared) == 1 and prompts == [prepared[0]['candidates']]
    # Nothing changed since the checkpoint, so the next cycle prepares once and spends nothing.
    memory_cycle.run(NOW + timedelta(hours=1), complete, lambda *a, **k: {'reviewed': 0, 'facts': 0})
    assert len(prepared) == 2 and len(prompts) == 1


def test_background_hold_is_reported_even_when_dreamer_preparation_also_failed(tmp_path, monkeypatch):
    """A held cycle reports the hold. HOW-SOTTO-DECIDES promises the actual hold reason is
    retained, and a preparation failure underneath it must not overwrite that with `failed`."""
    _quiet_memory_inputs(monkeypatch)
    monkeypatch.setattr(dreamer, 'prepare', lambda: (_ for _ in ()).throw(OSError('graph unreadable')))
    def held(*args, **kwargs):
        raise memory_cycle.gemini_transport.BackgroundModelHeldError(
            'background_budget_exhausted', 'held')
    fetch = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': 'next', 'complete': False}
    receipt = memory_cycle.run(NOW, fetch, held, dreamer.run)
    assert receipt['dreamer'] == {'status': 'held', 'reason': 'background_budget_exhausted'}
    assert receipt['history'][0]['status'] == 'held'


def test_dreamer_untyped_execution_error_is_conservatively_charged(tmp_path, monkeypatch):
    """An untyped fault thrown by the injected execution function proves no admission boundary.
    Preserve conservative charges; typed preflight refusals are tested separately."""
    _quiet_memory_inputs(monkeypatch)
    ku.apply(fact(), NOW)
    attempts = []
    def misconfigured(now=None):
        attempts.append(True)
        return dreamer.run(lambda *a, **k: (_ for _ in ()).throw(
            ValueError('Model proxy requires HTTPS (except local tests)')), now)
    complete = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True}
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), complete,
                         lambda *a, **k: {'reviewed': 0, 'facts': 0}, misconfigured)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(attempts) == 3 and 'dreamer_failure' not in state
    assert state['receipt']['dreamer']['reviewed'] == 0
    assert state['receipt']['dreamer']['parked'] == 1


def test_nonadvancing_source_cursor_retries_bounded_without_model_spend(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle, 'resolve_contact_names', lambda value: value)
    fetches, learns = [], []
    def stuck(*args):
        fetches.append(True)
        return {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': False}
    learn = lambda *a, **k: learns.append(True) or {'reviewed': 0, 'facts': 0}
    quiet = lambda **k: {'reviewed': 0}
    memory_cycle.run(NOW, stuck, learn, quiet)
    memory_cycle.run(NOW + timedelta(hours=1), stuck, learn, quiet)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(fetches) == 2 and learns == []
    assert state['sources']['imessage']['error_stage'] == 'fetch'
    assert state['sources']['imessage']['cursor'] is None


def test_new_tuning_is_identical_in_both_explorable_docs():
    import re
    root = Path(__file__).resolve().parents[2]
    expected = {'history_days': memory_cycle.HISTORY_DAYS, 'pages_per_run': memory_cycle.PAGES_PER_RUN,
                'prune_interval_days': memory_cycle.PRUNE_INTERVAL_SECONDS // 86400,
                'review_messages': learning.REVIEW_MESSAGES,
                'dream_people': dreamer.DREAM_PEOPLE, 'dream_facts': dreamer.DREAM_FACTS,
                'feedback_days': personal_context.FEEDBACK_DAYS, 'feedback_limit': personal_context.FEEDBACK_LIMIT,
                'source_retry_seconds': memory_cycle.SOURCE_RETRY_SECONDS,
                'unchanged_extraction_attempts': memory_cycle.UNCHANGED_EXTRACTION_ATTEMPTS,
                'unchanged_protocol_attempts': memory_cycle.UNCHANGED_PROTOCOL_ATTEMPTS,
                'unchanged_dreamer_attempts': memory_cycle.UNCHANGED_DREAMER_ATTEMPTS,
                'protocol_recheck_seconds': memory_cycle.PROTOCOL_RECHECK_SECONDS,
                'conversation_messages': personal_context.CONVERSATION_MESSAGES,
                'conversation_text_chars': personal_context.CONVERSATION_TEXT_CHARS,
                'conversation_total_chars': personal_context.CONVERSATION_TOTAL_CHARS}
    for name in ('playground-architecture.html', 'playground-feedback-loops.html'):
        text = (root / 'docs' / name).read_text()
        rules = json.loads(re.search(r'<script type="application/json" id="sotto-rules">(.*?)</script>', text, re.S)[1])
        assert rules['learning'] == expected


def test_later_owner_correction_is_not_blocked_by_an_archived_earlier_fact():
    original = 'Alex is the founder of Acme.'
    ku.apply(fact(original), NOW)
    for text in ('Alex is NOT the founder of Acme.', original):
        update = fact(text, ['user-correction'], 'user_edit')
        update['person_updates'][0]['facts'][0]['change_type'] = 'correction'
        ku.apply(update, NOW)
    active = [f for f in person()[1].facts.values() if f.status == 'active']
    assert len(active) == 1 and active[0].text == original and active[0].source == 'user_edit'


def test_welcome_reads_personal_voice_and_avoids_optional_connection_checklist(tmp_path):
    import compose_brief
    (tmp_path / 'style.json').write_text(json.dumps({'canonical': {'personal_message': [
        {'text': 'thanks maya - dinner works. happy to meet at the cafe first.', 'quality': 1}]}}))
    assert 'thanks maya - dinner works.' in compose_brief._welcome_voice()
    note = compose_brief._first_run_note({'type': 'welcome', 'first_run': True}, {}, {}, [], [])
    assert 'full picture' not in note and 'optional' in note
    assert 'unbound yes/no' in note


def test_each_invocation_advances_bounded_history_and_unfinished_curation(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    monkeypatch.setattr(memory_cycle.prewarm_graph, 'prewarm', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.style_extract, 'extract', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.relationship_pulse, 'observe_history', lambda *a, **k: None)
    sample = records()
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a, **kw: sample)
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'day': '2026-09-07', 'dreamer_day': '2026-09-06', 'model_calls': 22}))
    legacy = tmp_path / 'knowledge/episodes.json'
    legacy.write_text(json.dumps({'episodes': {'old': {'summary': 'Do not migrate me'}}}))
    def fetch(source, window):
        return {'status': 'ok', 'rows': [], 'next_cursor': (window.get('cursor') or 0) + 1, 'complete': False}
    learned = lambda *a, **k: {'reviewed': 2, 'facts': 0}
    curated = []
    curate = lambda **k: curated.append(True) or {'reviewed': 1}
    memory_cycle.run(NOW.replace(hour=7), fetch, learned, curate)
    final = json.loads(path.read_text())
    assert len(curated) == 1
    assert all(key not in final for key in ('day', 'model_calls', 'dreamer_day'))
    assert final['sources']['imessage']['pages'] == memory_cycle.PAGES_PER_RUN
    assert not legacy.exists()


@pytest.mark.parametrize('code', ['text_length', 'cross_subject_reference', 'repeated_subject', 'invalid_person'])
def test_extraction_constraints_and_safe_rejection_diagnostics(tmp_path, code):
    observed = [*records(), {**records()[0], 'ref': 'gmail:other', 'subject': 'other@example.com'}]
    invalid = extraction()
    if code == 'text_length':
        invalid['people'][0]['facts'][0]['text'] = 'x' * (learning.MAX_TEXT_CHARS + 1)
    elif code == 'cross_subject_reference':
        invalid['people'][0]['facts'][0]['refs'] = ['gmail:other']
    elif code == 'repeated_subject':
        invalid['people'] *= 2
    else:
        invalid['people'] = ['bad shape']
    def model(prompt, config, **kwargs):
        assert kwargs['schema'] == learning.SCHEMA
        return json.dumps(invalid)
    with pytest.raises(learning.MemoryExtractionError) as error:
        learning.learn(observed, model, NOW)
    assert error.value.code == code
    assert not list(tmp_path.glob('knowledge/people/*.md'))
    assert not (tmp_path / 'knowledge/episodes.json').exists()


def test_native_history_schema_stays_constant_for_large_pages():
    # Native Gemini accepted this shape, but rejected a 100-person outer bound
    # multiplied through the nested fact / episode / evidence arrays (HTTP 400).
    observed = [{**records()[0], 'subject': f'person{i}@example.com', 'ref': f'gmail:{i}'} for i in range(100)]
    def model(prompt, config, **kwargs):
        assert len(json.loads(prompt)) == 100
        assert len(json.dumps(kwargs['schema'])) < 1000
        assert 'maxItems' not in kwargs['schema']['properties']['people']
        assert 'enum' not in json.dumps(kwargs['schema'])
        return '{"people": []}'
    assert learning.learn(observed, model, NOW)['reviewed'] == 100


@pytest.mark.parametrize('status', [400, 413, 422, 429, 503])
def test_permanent_provider_rejection_waits_for_a_changed_contract(tmp_path, monkeypatch, status):
    from urllib.error import HTTPError
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    observed = records()
    monkeypatch.setattr(learning, 'observations', lambda *a, **kw: observed)
    attempts = []
    def learn(*a, **k):
        attempts.append(True)
        raise HTTPError('https://provider.invalid/private', status, 'PRIVATE BODY', {}, None)
    fetch = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': 'next', 'complete': False}
    quiet = lambda **k: {'reviewed': 0}
    memory_cycle.run(NOW, fetch, learn, quiet)
    memory_cycle.run(NOW + timedelta(days=1), fetch, learn, quiet)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(attempts) == (1 if status in (400, 413, 422) else 2)
    assert state['sources']['imessage']['cursor'] is None
    assert state['sources']['imessage']['http_status'] == status
    assert 'PRIVATE' not in json.dumps(state) and 'provider.invalid' not in json.dumps(state)
    monkeypatch.setattr(learning, 'request_revision', lambda: 'updated-contract')
    memory_cycle.run(NOW + timedelta(days=2), fetch,
                     lambda *a, **k: {'reviewed': 2, 'facts': 0}, quiet)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert state['sources']['imessage']['cursor'] == 'next'
    assert 'rejected_request_revision' not in state['sources']['imessage']
    assert 'http_status' not in state['sources']['imessage']


def test_unchanged_invalid_extraction_retries_once_then_parks(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    monkeypatch.setattr(memory_cycle, 'resolve_contact_names', lambda value: value)
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    sample = records()
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a, **kw: sample)
    attempts = []
    def invalid(*a, **k):
        attempts.append(True)
        raise memory_cycle.context_learning.MemoryExtractionError('invalid_shape')
    fetch = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': 80, 'complete': False}
    quiet = lambda **k: {'reviewed': 0}
    for hours in range(3):
        memory_cycle.run(NOW + timedelta(hours=hours), fetch, invalid, quiet)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(attempts) == memory_cycle.UNCHANGED_EXTRACTION_ATTEMPTS == 2, state
    assert state['sources']['imessage']['rejected_request_revision'] == learning.request_revision()


def _quiet_memory_inputs(monkeypatch, source='imessage'):
    # These protocol fixtures checkpoint one page per invocation; full-budget tests run below.
    monkeypatch.setattr(memory_cycle, 'PAGES_PER_RUN', 1)
    monkeypatch.setattr(memory_cycle, 'allowed', lambda value: value == source)
    monkeypatch.setattr(memory_cycle, 'resolve_contact_names', lambda value: value)
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'),
                           (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a, **kw: [])


def test_unchanged_history_protocol_failure_parks_and_protocol_change_releases(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    calls = []
    def stuck(source, window):
        calls.append((source, window.get('cursor')))
        return {'status': 'ok', 'rows': [], 'next_cursor': window.get('cursor'), 'complete': False}
    quiet = lambda **k: {'reviewed': 0}
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), stuck, lambda *a, **k: {}, quiet)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    failure = state['sources']['imessage']['protocol_failure']
    assert len(calls) == memory_cycle.UNCHANGED_PROTOCOL_ATTEMPTS == failure['attempts'] == 2
    assert failure['code'] == 'history_cursor_did_not_advance'

    # A parked request gets one daily compatibility probe, so a fixed remote Bridge self-heals
    # even though its version is not part of the authenticated history response.
    advanced = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': 80, 'complete': False}
    memory_cycle.run(NOW + timedelta(days=1, hours=2), advanced,
                     lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)
    window = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    assert window['cursor'] == 80 and 'protocol_failure' not in window


def test_real_bridge_history_contract_error_reaches_protocol_latch(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'imessage')
    calls = []
    def invalid_bridge(name, arguments):
        calls.append((name, arguments.get('before')))
        return {'source': 'imessage', 'status': 'ok', 'rows': [],
                'next_cursor': arguments.get('before'), 'complete': False}
    monkeypatch.setattr(source_context, 'bridge_call', invalid_bridge)
    quiet = lambda **k: {'reviewed': 0}
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), memory_cycle.page,
                         lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)
    window = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    assert len(calls) == memory_cycle.UNCHANGED_PROTOCOL_ATTEMPTS == 2
    assert window['protocol_failure']['code'] == 'history_cursor_did_not_advance'


def test_history_availability_and_rate_limit_failures_never_enter_protocol_latch(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    from urllib.error import HTTPError
    attempts = []
    def unavailable(*args):
        attempts.append(True)
        if len(attempts) % 2:
            raise HTTPError('https://provider.invalid/private', 503, 'PRIVATE', {}, None)
        return {'status': 'unavailable'}
    quiet = lambda **k: {'reviewed': 0}
    for hour in range(4):
        memory_cycle.run(NOW + timedelta(hours=hour), unavailable, lambda *a, **k: {}, quiet)
    window = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    assert len(attempts) == 4 and 'protocol_failure' not in window


def test_expired_gmail_continuation_restarts_frozen_window_once_then_advances(tmp_path, monkeypatch):
    observations = memory_cycle.context_learning.observations
    _quiet_memory_inputs(monkeypatch, source='gmail')
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', observations)
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 10, 'until': 20, 'cursor': 'expired', 'complete': False,
        'pages': 1, 'window_page_base': 0, 'rows': 10, 'reviewed': 10,
        'observation_totals': {'pages': 1, 'rows': 10, 'eligible': 10, 'selected': 10,
                               'skipped': 0, 'omitted': 0, 'truncated': 0, 'rejected_people': 0}}}}))
    calls = []
    def fetch(source, window):
        calls.append(window.get('cursor'))
        if window.get('cursor') == 'expired':
            raise source_context.HistoryContinuationError()
        return {'status': 'ok', 'rows': [], 'next_cursor': 'fresh', 'complete': False}
    quiet = lambda **k: {'reviewed': 0}
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), fetch,
                         lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)
    window = json.loads(path.read_text())['sources']['gmail']
    assert calls == ['expired', 'expired', None]
    assert window['window_page_base'] == 1
    assert window['observation_totals']['rows'] == 0
    assert window['observation_totals']['pages'] == 1
    assert window['cursor'] == 'fresh' and window['since'] == 10 and window['until'] == 20
    # The marker records the depth the dead token reached, so the next restart must beat it.
    assert window['continuation_reset'] == {'at': (NOW + timedelta(hours=1)).isoformat(),
                                            'pages': 1, 'reached': 1}


def test_gmail_fetch_rejection_that_is_not_a_dead_token_never_replays_a_paid_page(tmp_path, monkeypatch):
    """A deterministic Gmail 400 that the adapter did NOT identify as an expired continuation
    token (a bad query, bad window bounds, an adapter regression) is a broken request. Restarting
    the window would re-pay pages 1..n-1 for nothing, so it parks in the protocol latch instead."""
    _quiet_memory_inputs(monkeypatch, source='gmail')
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 10, 'until': 20, 'cursor': 'paid-to-here', 'complete': False, 'pages': 4}}}))
    from urllib.error import HTTPError
    calls = []
    def broken_request(source, window):
        calls.append(window.get('cursor'))
        raise HTTPError('https://provider.invalid/private', 400, 'PRIVATE', {}, None)
    receipts = []
    for hour in range(4):
        receipts.append(memory_cycle.run(NOW + timedelta(hours=hour), broken_request,
                                         lambda *a, **k: pytest.fail('must not replay a paid page'),
                                         lambda **k: {'reviewed': 0})['history'])
    window = json.loads(path.read_text())['sources']['gmail']
    assert calls == ['paid-to-here'] * memory_cycle.UNCHANGED_PROTOCOL_ATTEMPTS
    assert window['cursor'] == 'paid-to-here' and 'continuation_reset' not in window
    assert next(row for row in receipts[-1] if row['source'] == 'gmail') == {'source': 'gmail', 'status': 'blocked',
                               'error_code': 'history_protocol_rejected',
                               'reason': 'provider_request_rejected'}
    assert 'PRIVATE' not in path.read_text() and 'provider.invalid' not in path.read_text()


def test_gmail_continuation_restart_is_earned_by_progress_not_by_an_adapter_update(tmp_path, monkeypatch):
    """One restart per attempt that got FURTHER than the attempt before the last restart did.
    A release unlocks protocol probes but earns no replay by itself; only real new progress does,
    and an attempt that dies at the same depth twice parks instead of replaying page one again."""
    _quiet_memory_inputs(monkeypatch, source='gmail')
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 10, 'until': 20, 'cursor': 'expired-one', 'complete': False,
        'pages': 3, 'window_page_base': 0}}}))
    calls = []
    def fetch(source, window):
        cursor = window.get('cursor'); calls.append(cursor)
        if cursor in ('expired-one', 'expired-two'):
            raise source_context.HistoryContinuationError()
        return {'status': 'ok', 'rows': [], 'next_cursor': 'expired-two', 'complete': False}
    quiet = lambda **k: {'reviewed': 0}
    receipts = []
    for hour in range(7):
        if hour == 4:
            monkeypatch.setattr(memory_cycle, 'history_protocol_revision', lambda: 'new-adapter')
        receipts.append(memory_cycle.run(NOW + timedelta(hours=hour), fetch,
                                         lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)['history'])
    window = json.loads(path.read_text())['sources']['gmail']
    # The restart re-paid one page and died at depth 1, short of the depth 3 it had before.
    assert calls == ['expired-one', 'expired-one', None, 'expired-two', 'expired-two', 'expired-two']
    assert window['cursor'] == 'expired-two'
    assert window['continuation_reset'] == {'at': (NOW + timedelta(hours=1)).isoformat(),
                                            'pages': 3, 'reached': 3}
    assert window['protocol_failure']['code'] == 'continuation_restart_exhausted'
    assert next(row for row in receipts[5] if row['source'] == 'gmail')['error_code'] == 'continuation_restart_exhausted'
    assert next(row for row in receipts[6] if row['source'] == 'gmail') == {'source': 'gmail', 'status': 'blocked',
                              'error_code': 'history_protocol_rejected',
                              'reason': 'continuation_restart_exhausted'}


def test_gmail_replacement_token_that_expires_after_real_progress_restarts_again(tmp_path, monkeypatch):
    """The restart is renewable, not a permanent one-shot: a window whose REPLACEMENT token later
    expires — after the window reached further than it ever had — restarts again instead of dying
    until the owner disables and re-enables Gmail."""
    _quiet_memory_inputs(monkeypatch, source='gmail')
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 10, 'until': 20, 'cursor': 'expired-one', 'complete': False,
        'pages': 1, 'window_page_base': 0}}}))
    dead, calls = {'expired-one', 'stale-later'}, []
    def fetch(source, window):
        cursor = window.get('cursor'); calls.append(cursor)
        if cursor in dead:
            raise source_context.HistoryContinuationError()
        return {'status': 'ok', 'rows': [], 'complete': False,
                'next_cursor': 'deeper' if cursor is None else 'stale-later'}
    quiet = lambda **k: {'reviewed': 0}
    for hour in range(7):
        memory_cycle.run(NOW + timedelta(hours=hour), fetch,
                         lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)
    window = json.loads(path.read_text())['sources']['gmail']
    assert calls == ['expired-one', 'expired-one', None, 'deeper',
                     'stale-later', 'stale-later', None]
    assert window['cursor'] == 'deeper' and window['continuation_reset']['reached'] == 2
    assert 'protocol_failure' not in window


def test_gmail_restart_progress_excludes_completed_windows(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch, source='gmail')
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 10, 'until': 20, 'cursor': None, 'complete': True,
        'initial_complete': True, 'pages': 100}}}))
    calls = []

    def fetch(source, window):
        cursor = window.get('cursor')
        calls.append(cursor)
        if cursor in ('dead-one', 'dead-two'):
            raise source_context.HistoryContinuationError()
        return {'status': 'ok', 'rows': [], 'complete': False,
                'next_cursor': 'dead-one' if len(calls) == 1 else
                               'deeper' if cursor is None else 'dead-two'}

    for hour in range(8):
        memory_cycle.run(NOW + timedelta(hours=hour), fetch,
                         lambda *a, **k: {'reviewed': 0, 'facts': 0},
                         lambda **k: {'reviewed': 0})
    window = json.loads(path.read_text())['sources']['gmail']
    # The old 100 pages remain in diagnostics but cannot inflate this window's one-page depth.
    # Its next attempt reached two pages, so a second expired token earns a second reset.
    assert calls == [None, 'dead-one', 'dead-one', None, 'deeper', 'dead-two', 'dead-two', None]
    assert window['pages'] == 104 and window['window_page_base'] == 103
    assert window['continuation_reset'] == {'at': (NOW + timedelta(hours=6)).isoformat(),
                                            'pages': 103, 'reached': 2}
    assert window['cursor'] == 'deeper' and 'protocol_failure' not in window


@pytest.mark.parametrize('legacy_key', [False, True])
def test_gmail_legacy_reset_marker_still_blocks_replay_after_release(tmp_path, monkeypatch, legacy_key):
    _quiet_memory_inputs(monkeypatch, source='gmail')
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True)
    marker = {'at': NOW.isoformat()}
    if legacy_key:
        marker['key'] = 'old-release-derived-key'
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 10, 'until': 20, 'cursor': 'expired-again', 'complete': False,
        'pages': 7, 'continuation_reset': marker}}}))
    def invalid(source, window):
        assert window['cursor'] == 'expired-again'
        raise source_context.HistoryContinuationError()
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), invalid,
                         lambda *a, **k: pytest.fail('Must not replay a learned page'),
                         lambda **k: {'reviewed': 0})
    window = json.loads(path.read_text())['sources']['gmail']
    assert window['cursor'] == 'expired-again' and window['continuation_reset'] == marker
    assert window['protocol_failure']['code'] == 'continuation_restart_exhausted'


def test_gmail_old_unmeasured_window_gets_only_one_restart(tmp_path, monkeypatch):
    observations = memory_cycle.context_learning.observations
    _quiet_memory_inputs(monkeypatch, source='gmail')
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', observations)
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 10, 'until': 20, 'cursor': 'expired', 'complete': False,
        'initial_complete': True, 'pages': 101}}}))
    calls = []

    def fetch(source, window):
        cursor = window.get('cursor')
        calls.append(cursor)
        if cursor is not None:
            raise source_context.HistoryContinuationError()
        return {'status': 'ok', 'rows': [], 'complete': False, 'next_cursor': 'expired-again'}

    for hour in range(6):
        memory_cycle.run(NOW + timedelta(hours=hour), fetch,
                         lambda *a, **k: {'reviewed': 0, 'facts': 0},
                         lambda **k: {'reviewed': 0})
    window = json.loads(path.read_text())['sources']['gmail']
    assert calls == ['expired', 'expired', None, 'expired-again', 'expired-again']
    assert window['cursor'] == 'expired-again'
    assert window['continuation_reset']['reached'] is None
    assert window['protocol_failure']['code'] == 'continuation_restart_exhausted'


def test_unchanged_dreamer_failure_parks_but_changed_evidence_same_person_releases(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    ku.apply(fact(), NOW)
    calls = []
    def malformed(now=None):
        def model(*args, **kwargs):
            calls.append(True)
            return '{bad json'
        return dreamer.run(model, now)
    complete = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True}
    learned = lambda *a, **k: {'reviewed': 0, 'facts': 0}
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), complete, learned, malformed)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(calls) == memory_cycle.UNCHANGED_DREAMER_ATTEMPTS == 2
    assert state['receipt']['dreamer']['parked'] == 1
    assert all(row['attempts'] == 2 for rows in dreamer.jsonstore.read(dreamer._state_path())['facts'].values()
               for row in rows.values())

    # The identity is unchanged; new durable evidence alone must release the parked request.
    ku.apply(fact('Alex is advising a climate startup.', ['gmail:new']), NOW + timedelta(hours=3))
    def valid(now=None):
        calls.append(True)
        return dreamer.run(lambda prompt, *a, **k: json.dumps({'people': [
            {'id': row['id'], 'summary_refs': [], 'conflicts': []} for row in json.loads(prompt)]}), now)
    memory_cycle.run(NOW + timedelta(hours=3), complete, learned, valid)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(calls) == 3 and 'dreamer_failure' not in state


@pytest.mark.parametrize('reply', [
    {'people': [None]},
    {'people': [{'id': [], 'summary_refs': [], 'conflicts': []}]},
    {'people': [{'id': 'person', 'summary_refs': [['nested']], 'conflicts': []}]},
    {'people': [{'id': 'person', 'summary_refs': [], 'conflicts': [[['nested'], 'other']]}]},
])
def test_dreamer_classifies_all_nested_response_shape_failures(reply):
    ku.apply(fact(), NOW)
    if isinstance(reply['people'][0], dict) and isinstance(reply['people'][0].get('id'), str):
        reply['people'][0]['id'] = person()[1].canonical_id
    with pytest.raises(dreamer.DreamerResponseError):
        dreamer.run(lambda *a, **k: json.dumps(reply), NOW)


@pytest.mark.parametrize('status', [429, 503])
def test_transient_dreamer_provider_failures_remain_retriable(tmp_path, monkeypatch, status):
    _quiet_memory_inputs(monkeypatch)
    from urllib.error import HTTPError
    attempts = []
    def transient(now=None):
        attempts.append(True)
        raise HTTPError('https://provider.invalid/private', status, 'PRIVATE', {}, None)
    complete = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True}
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), complete,
                         lambda *a, **k: {'reviewed': 0, 'facts': 0}, transient)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(attempts) == 3 and 'dreamer_failure' not in state


@pytest.mark.parametrize('reverse', [False, True])
def test_invalid_person_cannot_poison_unrelated_memory_or_write_partial_facts(tmp_path, reverse):
    observed = [*records(), {**records()[0], 'ref': 'gmail:other',
                            'subject': 'other@example.com', 'name': 'Other'}]
    bad = extraction()['people'][0]
    bad['facts'].append({'text': 'Wrong attribution must never be saved.', 'refs': ['gmail:other']})
    good = {'subject_id': 'p2', 'facts': [
        {'text': 'Other builds scheduling software.', 'refs': ['gmail:other']}]}
    people = [bad, good] if not reverse else [good, bad]
    result = learning.learn(observed, lambda *a, **k: json.dumps({'people': people}), NOW)
    assert result == {'reviewed': 3, 'facts': 1, 'rejected_people': {'cross_subject_reference': 1}}
    assert not kg.find_person_file(identifier='alex@example.com')
    path = Path(kg.find_person_file(identifier='other@example.com'))
    saved = kg.parse_person_file(path.read_text())
    assert [f.text for f in saved.facts.values()] == ['Other builds scheduling software.']
    assert not list(tmp_path.glob('knowledge/continuity/*'))


def test_duplicate_output_subjects_are_all_rejected_even_with_valid_other_person():
    observed = [*records(), {**records()[0], 'ref': 'gmail:other',
                            'subject': 'other@example.com', 'name': 'Other'}]
    reply = {'people': [extraction()['people'][0],
                       {'subject_id': 'p2', 'facts': []},
                       extraction()['people'][0]]}
    result = learning.learn(observed, lambda *a, **k: json.dumps(reply), NOW)
    assert result['rejected_people'] == {'repeated_subject': 2}
    assert result['facts'] == 0
    assert not kg.find_person_file(identifier='alex@example.com')


def test_ambiguous_input_reference_rejected_before_spending_or_writing(tmp_path):
    observed = [records()[0], {**records()[0], 'subject': 'other@example.com'}]
    with pytest.raises(learning.MemoryExtractionError) as error:
        learning.learn(observed, lambda *a, **k: pytest.fail('ambiguous input must not call a model'), NOW)
    assert error.value.code == 'ambiguous_source_reference'
    assert not list(tmp_path.glob('knowledge/people/*.md'))


def test_partial_validity_cannot_bypass_consent_recheck(tmp_path, monkeypatch):
    reply = extraction()
    reply['people'].append({'subject_id': 'invented', 'facts': []})
    def revoked(*a, **k):
        monkeypatch.setattr(learning, 'allowed', lambda _: False)
        return json.dumps(reply)
    with pytest.raises(RuntimeError, match='consent changed'):
        learning.learn(records(), revoked, NOW)
    assert not list(tmp_path.glob('knowledge/people/*.md'))


def test_mixed_extraction_advances_history_and_retains_safe_rejection_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cycle, 'PAGES_PER_RUN', 1)
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: source == 'gmail')
    monkeypatch.setattr(memory_cycle, 'resolve_contact_names', lambda value: value)
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    observed = [*records(), {**records()[0], 'ref': 'gmail:other',
                            'subject': 'other@example.com', 'name': 'Other'}]
    monkeypatch.setattr(learning, 'observations', lambda source, rows, now, **kw: rows)
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 1, 'until': 2, 'cursor': None, 'rejected_request_revision': 'old-contract',
        'retry_after': int(NOW.timestamp()) + 9999}}}))
    calls = []
    def fetch(source, window):
        calls.append(window['cursor'])
        return {'status': 'ok', 'rows': observed, 'next_cursor': 'next' if not window['cursor'] else None,
                'complete': bool(window['cursor'])}
    def learn(rows, now):
        bad = extraction()['people'][0]
        bad['facts'][0]['refs'] = ['gmail:other']
        people = [bad, {'subject_id': 'p2', 'facts': []}] if len(calls) == 1 else []
        return learning.learn(rows, lambda *a, **k: json.dumps({'people': people}), now)
    quiet = lambda **k: {'reviewed': 0}
    memory_cycle.run(NOW, fetch, learn, quiet)
    window = json.loads(path.read_text())['sources']['gmail']
    assert window['cursor'] == 'next' and window['pages'] == 1
    assert window['last_extraction']['rejected_people'] == {'cross_subject_reference': 1}
    assert 'rejected_request_revision' not in window
    memory_cycle.run(NOW + timedelta(minutes=15), fetch, learn, quiet)
    window = json.loads(path.read_text())['sources']['gmail']
    assert calls == [None, 'next'] and window['pages'] == 2 and window['initial_complete']
    assert window['rejected_people'] == {'cross_subject_reference': 1}
    assert 'rejected_people' not in window['last_extraction']
    assert 'example.com' not in path.read_text() and 'Wrong attribution' not in path.read_text()


@pytest.mark.parametrize('code,reason', [('model_no_candidates', 'OTHER'),
    ('model_no_text', 'STOP'), ('model_output_limit', 'MAX_TOKENS')])
def test_unusable_model_response_parks_across_scheduled_runs_and_contract_change_releases(tmp_path, monkeypatch, code, reason):
    _quiet_memory_inputs(monkeypatch)
    calls = []
    def unusable(*a, **k):
        calls.append(True)
        raise learning.gemini.ModelResponseError(code, reason)
    fetch = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': 80, 'complete': False}
    for hour in range(17):
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'scheduled-' + str(hour))
        memory_cycle.run(NOW + timedelta(hours=hour), fetch, unusable, lambda **k: {'reviewed': 0})
    state = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    assert len(calls) == 2
    assert state['cursor'] is None and state['pages'] == 0
    assert state['error_code'] == code and state['provider_reason'] == reason
    monkeypatch.setattr(learning, 'request_revision', lambda: 'fixed-contract')
    memory_cycle.run(NOW + timedelta(hours=17), fetch,
                     lambda *a, **k: {'reviewed': 0, 'facts': 0}, lambda **k: {'reviewed': 0})
    state = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    assert state['cursor'] == 80 and 'provider_reason' not in state
    assert 'rejected_request_revision' not in state


def test_dreamer_empty_provider_response_is_bounded(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    ku.apply(fact(), NOW)
    calls = []
    def unavailable(now=None):
        calls.append(True)
        raise learning.gemini.ModelResponseError('model_no_candidates', 'SAFETY')
    for hour in range(5):
        memory_cycle.run(NOW + timedelta(hours=hour),
            lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True},
            lambda *a, **k: {'reviewed': 0, 'facts': 0}, unavailable)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(calls) == 2 and state['receipt']['dreamer']['status'] == 'blocked'
    assert state['dreamer_failure']['code'] == 'model_no_candidates'


def test_bridge_timeout_retains_page_and_retries_without_extraction_latch(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    def unavailable(*a):
        raise source_context.BridgeReadError('bridge_timeout')
    memory_cycle.run(NOW, unavailable, lambda *a, **k: pytest.fail('no model call'), lambda **k: {'reviewed': 0})
    state = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    assert state['error_code'] == 'bridge_timeout' and state['cursor'] is None
    assert 'rejected_request_revision' not in state
    memory_cycle.run(NOW + timedelta(hours=1),
        lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': 80, 'complete': False},
        lambda *a, **k: {'reviewed': 0, 'facts': 0}, lambda **k: {'reviewed': 0})
    state = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    assert state['cursor'] == 80 and 'error_code' not in state


@pytest.mark.parametrize('reason', ['PROHIBITED_CONTENT', 'SAFETY', 'BLOCKLIST'])
def test_provider_blocked_page_is_excluded_once_and_later_history_advances(tmp_path, monkeypatch, reason):
    _quiet_memory_inputs(monkeypatch)
    monkeypatch.setattr(learning, 'observations', lambda source, rows, now, **kw: rows)
    calls = []
    def fetch(source, window):
        return {'status': 'ok', 'rows': [{'text': 'PRIVATE', 'ref': 'private-ref'}],
                'next_cursor': 80 if window.get('cursor') is None else None,
                'complete': window.get('cursor') == 80}
    def learn(rows, now):
        calls.append(True)
        if len(calls) == 1:
            raise learning.gemini.ModelResponseError('model_no_candidates', reason)
        return {'reviewed': 1, 'facts': 0}
    quiet = lambda **k: {'reviewed': 0}
    receipt = memory_cycle.run(NOW, fetch, learn, quiet)
    path = Path(memory_cycle.state_path())
    state = json.loads(path.read_text())['sources']['imessage']
    assert state['cursor'] == 80 and state['reviewed'] == 0 and 'last_success' not in state
    assert receipt['history'][0]['status'] == 'excluded'
    assert list(state['excluded_pages'].values())[0]['reason'] == reason
    assert 'PRIVATE' not in path.read_text() and 'private-ref' not in path.read_text()
    memory_cycle.run(NOW + timedelta(minutes=15), fetch, learn, quiet)
    state = json.loads(path.read_text())['sources']['imessage']
    assert len(calls) == 2 and state['reviewed'] == 1 and state['pages'] == 2
    assert state['initial_complete'] and len(state['excluded_pages']) == 1
    assert 'error' not in state and 'rejected_request_revision' not in state


def test_preupgrade_content_block_survives_fetch_failure_without_model_resubmission(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'sources': {'imessage': {
        'since': 10, 'until': 20, 'cursor': 40, 'pages': 597, 'rows': 15553, 'reviewed': 14443,
        'last_success': '2026-09-30T06:31:44Z', 'error': 'ModelResponseError',
        'error_stage': 'memory_extraction', 'provider_reason': 'PROHIBITED_CONTENT',
        'rejected_request_revision': learning.request_revision(), 'retry_after': int(NOW.timestamp()) + 999}}}))
    calls = []
    def fetch(source, window):
        calls.append(window['cursor'])
        if len(calls) == 1:
            raise source_context.BridgeReadError('bridge_offline')
        return {'status': 'ok', 'rows': [{'text': 'PRIVATE'}], 'next_cursor': 80, 'complete': False}
    learn = lambda *a, **k: pytest.fail('blocked page must not be resubmitted')
    quiet = lambda **k: {'reviewed': 0}
    memory_cycle.run(NOW, fetch, learn, quiet)
    memory_cycle.run(NOW + timedelta(minutes=15), fetch, learn, quiet)
    assert calls == [40]
    memory_cycle.run(NOW + timedelta(hours=1), fetch, learn, quiet)
    state = json.loads(path.read_text())['sources']['imessage']
    assert calls == [40, 40] and state['cursor'] == 80
    assert state['reviewed'] == 14443 and state['last_success'] == '2026-09-30T06:31:44Z'
    assert len(state['excluded_pages']) == 1 and 'pending_content_block' not in state


def test_content_block_does_not_advance_after_consent_is_revoked(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    def learn(*a, **k):
        monkeypatch.setattr(memory_cycle, 'allowed', lambda source: False)
        raise learning.gemini.ModelResponseError('model_no_candidates', 'PROHIBITED_CONTENT')
    memory_cycle.run(NOW, lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': 80, 'complete': False},
                     learn, lambda **k: {'reviewed': 0})
    state = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['imessage']
    assert state['cursor'] is None and 'excluded_pages' not in state


def test_excluded_history_page_is_not_resubmitted_after_continuation_restart(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True, exist_ok=True)
    window = {'since': 10, 'until': 20, 'cursor': 40, 'pages': 2}
    payload = {'status': 'ok', 'rows': [{'text': 'PRIVATE'}], 'next_cursor': 80, 'complete': False}
    memory_cycle._exclude_blocked_page(window, payload, 'SAFETY', NOW)
    window['cursor'] = 40  # a later continuation restart traverses this already excluded page
    path.write_text(json.dumps({'sources': {'imessage': window}}))
    memory_cycle.run(NOW, lambda *a: payload,
                     lambda *a, **k: pytest.fail('excluded page must not be resubmitted'),
                     lambda **k: {'reviewed': 0})
    saved = json.loads(path.read_text())['sources']['imessage']
    assert saved['cursor'] == 80 and len(saved['excluded_pages']) == 1
    assert 'last_success' not in saved


def test_parked_content_block_does_not_skip_on_invalid_continuation(tmp_path, monkeypatch):
    _quiet_memory_inputs(monkeypatch)
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'sources': {'imessage': {
        'since': 10, 'until': 20, 'cursor': 40, 'pages': 3,
        'pending_content_block': 'SAFETY'}}}))
    def dead(*a):
        raise source_context.HistoryContinuationError()
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), dead,
                         lambda *a, **k: pytest.fail('must not call a model'),
                         lambda **k: {'reviewed': 0})
    saved = json.loads(path.read_text())['sources']['imessage']
    assert saved['cursor'] == 40 and 'excluded_pages' not in saved
    assert saved['protocol_failure']['attempts'] == 2


@pytest.mark.parametrize('identifier', ['Alex+long-tag@Example.com', '+14155550123', '15555550123@s.whatsapp.net', 'p2'])
def test_page_ids_bind_to_exact_canonical_identity_without_mutating_input(monkeypatch, identifier):
    observed = [{**records()[0], 'subject': identifier, 'name': identifier},
                {**records()[1], 'subject': identifier, 'name': 'Alex'}]
    original = json.loads(json.dumps(observed))
    writes, calls = [], []
    monkeypatch.setattr(learning.knowledge_update, 'apply', lambda data, **k: writes.append(data))
    def model(prompt, config, **kwargs):
        rows = json.loads(prompt)
        calls.append(rows)
        assert all('subject' not in row and row['subject_id'] == 'p1' for row in rows)
        assert [row['name'] for row in rows] == ['p1', 'Alex']
        assert [row['ref'] for row in rows] == ['gmail:a', 'gmail:b']
        assert [row['text'] for row in rows] == [row['text'] for row in original]
        assert 'subject_id' in kwargs['schema']['properties']['people']['items']['required']
        return json.dumps({'people': [{'subject_id': rows[0]['subject_id'], 'facts': [
            {'text': 'Alex builds a product design tool.', 'refs': [rows[0]['ref']]}]}]})
    assert learning.learn(observed, model, NOW) == {'reviewed': 2, 'facts': 1}
    assert len(calls) == len(writes) == 1
    assert writes[0]['person_updates'][0]['identifier'] == identifier
    assert writes[0]['person_updates'][0]['facts'][0]['evidence_refs'] == ['gmail:a']
    assert observed == original


@pytest.mark.parametrize('output_id', ['Alex', 'alex@example.com', 'p999', 'P1', 'p1 ', None, [], 1])
def test_unknown_page_id_is_not_guessed_from_a_valid_reference(tmp_path, output_id):
    reply = extraction()
    reply['people'][0]['subject_id'] = output_id
    calls = []
    def model(*a, **k):
        calls.append(True)
        return json.dumps(reply)
    with pytest.raises(learning.MemoryExtractionError, match='unknown_subject'):
        learning.learn(records(), model, NOW)
    assert len(calls) == 1
    assert not list(tmp_path.glob('knowledge/people/*.md'))


def test_unknown_subject_parks_same_page_then_new_contract_resumes_without_skipping(monkeypatch):
    observed = records()
    _quiet_memory_inputs(monkeypatch, source='gmail')
    monkeypatch.setattr(learning, 'observations', lambda *a, **kw: observed)
    revision = learning.request_revision()
    monkeypatch.setattr(learning, 'request_revision', lambda: 'old-subject-contract')
    fetched, calls = [], []
    fixed = False
    def fetch(source, window):
        fetched.append(window['cursor'])
        return {'status': 'ok', 'rows': observed, 'next_cursor': 'next-page', 'complete': False}
    def model(prompt, config, **kwargs):
        calls.append(True)
        reply = extraction()
        reply['people'][0]['subject_id'] = json.loads(prompt)[0]['subject_id'] if fixed else 'Alex'
        return json.dumps(reply)
    def learn(rows, now):
        return learning.learn(rows, model, now)
    for hour in range(4):
        memory_cycle.run(NOW + timedelta(hours=hour), fetch, learn, lambda **k: {'reviewed': 0})
    path = Path(memory_cycle.state_path())
    state = json.loads(path.read_text())['sources']['gmail']
    assert len(calls) == 2 and fetched == [None, None]
    assert state['cursor'] is None and state['reviewed'] == 0
    assert state['error_code'] == 'unknown_subject'
    fixed = True
    monkeypatch.setattr(learning, 'request_revision', lambda: revision)
    memory_cycle.run(NOW + timedelta(hours=4), fetch, learn, lambda **k: {'reviewed': 0})
    state = json.loads(path.read_text())['sources']['gmail']
    assert len(calls) == 3 and fetched == [None, None, None]
    assert state['cursor'] == 'next-page' and state['reviewed'] == 2
    assert 'rejected_request_revision' not in state and 'error_code' not in state
    assert len(person()[1].facts) == 1


def test_dreamer_archives_paraphrases_without_inventing_confidence_or_losing_evidence():
    ku.apply(fact('Has children named Ari and Bo.', ['imessage:one']), NOW)
    ku.apply(fact('Has two children, Bo and Ari.', ['gmail:two']), NOW)
    path, before = person()
    ids = list(before.facts)
    assert len(ids) == 2
    def curate(prompt, config, **kwargs):
        rows = json.loads(prompt)
        return json.dumps({'people': [{'id': rows[0]['id'], 'summary_refs': ids,
            'conflicts': [], 'equivalent_groups': [ids]}]})
    result = dreamer.run(curate, NOW)
    after = person()[1]
    assert result['reviewed'] == 1
    assert after.facts[ids[1]].status == 'archived'
    assert after.facts[ids[1]].text == before.facts[ids[1]].text
    assert after.facts[ids[1]].evidence_refs == before.facts[ids[1]].evidence_refs
    assert after.facts[ids[0]] == before.facts[ids[0]]
    assert after.summary_refs == ids[:1]
    assert dreamer.run(lambda *a, **k: pytest.fail('no new evidence'), NOW)['reviewed'] == 0


def _fold_children(first_refs, second_refs):
    """Two paraphrases of one fact, folded by a curation that lists the first wording first."""
    ku.apply(fact('Has children named Ari and Bo.', first_refs[:1]), NOW)
    for ref in second_refs:
        ku.apply(fact('Has two children, Bo and Ari.', [ref]), NOW)
    ids = {f.text: fid for fid, f in person()[1].facts.items()}
    a, b = ids['Has children named Ari and Bo.'], ids['Has two children, Bo and Ari.']
    path, _ = person()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert ku.consolidate(person()[1].canonical_id, digest, [a], [], NOW,
                          equivalent_groups=[[a, b]])['status'] == 'ok'
    return a, b


def _children_alive_after_prune(day):
    ku.apply(fact('Alex runs a pottery studio.', ['gmail:nine'],
                  date=(NOW + timedelta(days=day)).date().isoformat()), NOW + timedelta(days=day))
    return [f for f in person()[1].facts.values() if f.status == 'active' and 'Ari' in f.text]


def test_equivalence_keeps_the_corroborated_copy_over_the_models_preferred_wording():
    """Wording order must not archive a seen-2 copy (never age-pruned) behind a seen-1 copy."""
    a, b = _fold_children(['imessage:one'], ['gmail:two', 'gmail:three'])
    after = person()[1]
    assert after.facts[b].status == 'active' and after.facts[b].seen == 2
    assert after.facts[a].archived_reason == 'duplicate' and after.facts[a].duplicate_of == b
    assert after.summary_refs == [b]
    assert [f.text for f in _children_alive_after_prune(61)] == ['Has two children, Bo and Ari.']


def test_new_evidence_for_a_folded_paraphrase_refreshes_the_kept_copy():
    a, b = _fold_children(['imessage:one'], ['imessage:two'])
    assert person()[1].facts[b].status == 'archived'  # equal evidence: the model's order breaks the tie
    # Replaying the folded copy's own evidence is not a new observation of the kept copy.
    assert ku.apply(fact('Has two children, Bo and Ari.', ['imessage:two'], date='2026-10-27'),
                    NOW + timedelta(days=50))['applied']['confirmed'] == 0
    assert person()[1].facts[a].seen == 1
    fresh = ku.apply(fact('Has two children, Bo and Ari.', ['imessage:fifty'], date='2026-10-27'),
                     NOW + timedelta(days=50))
    kept = person()[1].facts[a]
    assert fresh['applied']['confirmed'] == 1 and kept.seen == 2 and kept.last == '2026-10-27'
    assert 'imessage:fifty' in kept.evidence_refs and person()[1].facts[b].status == 'archived'
    assert [f.text for f in _children_alive_after_prune(61)] == ['Has children named Ari and Bo.']


def test_a_user_correction_stays_the_kept_copy_and_folded_evidence_cannot_touch_it():
    ku.apply(fact('Has two children, Bo and Ari.', ['gmail:two']), NOW)
    ku.apply(fact('Has two children, Bo and Ari.', ['gmail:three']), NOW)
    path, p = person()
    p.facts['f_user'] = kg.FactMeta(text='Alex has two kids, Ari and Bo.', type='context', seen=1,
                                    conf=1.0, source='user_edit', source_ref='user-correction',
                                    first='2026-09-01', last='2026-09-01')
    kg.write_person_file(str(path), p, NOW)
    observed = next(fid for fid in p.facts if fid != 'f_user')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    ku.consolidate(p.canonical_id, digest, [], [], NOW, equivalent_groups=[[observed, 'f_user']])
    assert person()[1].facts[observed].duplicate_of == 'f_user'
    before = person()[1].facts['f_user']
    result = ku.apply(fact('Has two children, Bo and Ari.', ['gmail:four']), NOW)
    assert result['applied']['confirmed'] == 0 and person()[1].facts['f_user'] == before


@pytest.mark.parametrize('groups, conflicts', [([['a', 'b'], ['b', 'c']], []),
    ([['a', 'missing']], []), ([['a', 'a']], []), ([['a', 'b']], [['a', 'c']])])
def test_equivalence_plan_cannot_overlap_or_hide_conflicts(groups, conflicts):
    with pytest.raises(ValueError):
        ku.validate_equivalent_groups(groups, {'a': {}, 'b': {}, 'c': {}}, conflicts)


def test_contact_note_numbers_are_not_promoted_to_facts():
    result = ku.apply(fact('74', source='apple_contacts'), NOW)
    assert result['applied']['new'] == 0


def test_dreamer_does_not_checkpoint_facts_uncovered_beyond_the_batch(monkeypatch):
    monkeypatch.setattr(dreamer, 'DREAM_FACTS', 2)
    for i, text in enumerate(['Has two children named Ari and Bo.',
                              'Has children, Bo and Ari.', 'Ari and Bo are their children.']):
        ku.apply(fact(text, [f'imessage:{i}']), NOW)
    def curate(prompt, config, **kwargs):
        rows = json.loads(prompt)
        return json.dumps({'people': [{'id': p['id'], 'summary_refs': list(p['facts'])[:1],
            'conflicts': [], 'equivalent_groups': [list(p['facts'])] if len(p['facts']) >= 2 else []} for p in rows]})
    assert dreamer.run(curate, NOW)['reviewed'] == 1
    assert dreamer.prepare()['candidates']  # newly uncovered third fact still needs review
    assert dreamer.run(curate, NOW)['reviewed'] == 1
    assert not dreamer.prepare()['candidates']


@pytest.mark.parametrize('wording', ['keeper', 'sibling', 'transitive'])
def test_a1_duplicate_family_replay_cannot_confirm_any_wording(wording):
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    path, p = person()
    p.facts['f_tail'] = kg.FactMeta(text='Ari and Bo are Alex’s children.', status='archived',
        archived_reason='duplicate', duplicate_of=b, source='observed_message',
        source_ref='gmail:three', evidence_refs=['gmail:three'], first='2026-09-06', last='2026-09-06')
    kg.write_person_file(str(path), p, NOW)
    before = person()[1].facts
    text = before[{'keeper': a, 'sibling': b, 'transitive': 'f_tail'}[wording]].text
    result = ku.apply(fact(text, ['gmail:three'], date='2026-10-06'), NOW + timedelta(days=30))
    assert result['applied']['confirmed'] == 0
    assert person()[1].facts == before


def test_a1_stale_keeper_replay_does_not_revive_but_new_evidence_does():
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    path, p = person()
    p.facts[a].status, p.facts[a].archived_reason = 'archived', 'stale'
    p.facts[a].archived_text = p.facts[a].text
    kg.write_person_file(str(path), p, NOW)
    before = person()[1].facts
    ku.apply(fact(before[a].text, ['imessage:two'], date='2026-10-06'), NOW + timedelta(days=30))
    assert person()[1].facts == before
    fresh = ku.apply(fact(before[b].text, ['gmail:new'], date='2026-10-06'), NOW + timedelta(days=30))
    assert fresh['applied']['confirmed'] == 1
    assert person()[1].facts[a].status == 'active' and person()[1].facts[a].seen == 2
    assert person()[1].facts[b] == before[b]


@pytest.mark.parametrize('bad_link', ['cycle', 'missing', 'wrong_person'])
def test_a1_invalid_duplicate_lineage_does_not_mutate_or_confirm(bad_link):
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    path, p = person()
    p.facts[a].status, p.facts[a].archived_reason = 'archived', 'duplicate'
    p.facts[a].duplicate_of = b if bad_link == 'cycle' else 'f_elsewhere'
    if bad_link == 'wrong_person':
        elsewhere = fact('Alex is an architect.', ['gmail:foreign'])
        elsewhere['person_updates'][0].update(identifier='other@example.com', person_name='Other')
        ku.apply(elsewhere, NOW)
        other_path = kg.find_person_file(identifier='other@example.com')
        other = kg.parse_person_file(Path(other_path).read_text())
        other.facts['f_elsewhere'] = kg.FactMeta(text=p.facts[a].text, source='observed_message',
            source_ref='gmail:foreign', evidence_refs=['gmail:foreign'], first='2026-09-06', last='2026-09-06')
        kg.write_person_file(other_path, other, NOW)
        other_bytes = Path(other_path).read_bytes()
    kg.write_person_file(str(path), p, NOW)
    before = person()[1].facts
    ku.apply(fact(before[b].text, ['gmail:new']), NOW)
    assert person()[1].facts == before
    if bad_link == 'wrong_person':
        assert Path(other_path).read_bytes() == other_bytes


@pytest.mark.parametrize('incomplete', ['saturated', 'legacy_missing'])
def test_a1_family_incomplete_evidence_needs_source_chronology(incomplete):
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    path, p = person()
    if incomplete == 'saturated':
        p.facts[b].evidence_refs = [f'archive:{i}' for i in range(64)]
    else:
        p.facts[b].evidence_refs = []
        p.facts[b].seen = 3
    kg.write_person_file(str(path), p, NOW)
    before = person()[1].facts[a]
    assert ku.apply(fact(before.text, ['gmail:evicted'], date='2026-09-06'), NOW)['applied']['confirmed'] == 0
    after = person()[1].facts[a]
    assert (after.seen, after.conf, after.first, after.last, after.status) == (
        before.seen, before.conf, before.first, before.last, before.status)
    assert ku.apply(fact(before.text, ['gmail:genuinely-later'], date='2026-09-07'), NOW + timedelta(days=1))['applied']['confirmed'] == 1



def test_a1_new_evidence_once_in_one_batch_preserves_sibling_provenance():
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    before = person()[1].facts
    update = fact(before[a].text, ['gmail:new'])
    update['person_updates'][0]['facts'].append(fact(before[b].text, ['gmail:new'])['person_updates'][0]['facts'][0])
    result = ku.apply(update, NOW)
    assert result['applied']['confirmed'] == 1 and person()[1].facts[a].seen == 2
    assert person()[1].facts[b] == before[b]
    assert person()[1].facts[a].source == before[a].source
    assert person()[1].facts[a].source_ref == before[a].source_ref


@pytest.mark.parametrize('last', ['2026-09-07', ''])
def test_a1_saturated_sibling_latest_or_unknown_date_fences_stale_keeper(last):
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    path, p = person()
    p.facts[a].status, p.facts[a].archived_reason = 'archived', 'stale'
    p.facts[a].archived_text = p.facts[a].text
    p.facts[b].evidence_refs = [f'archive:{i}' for i in range(64)]
    p.facts[b].last = last
    kg.write_person_file(str(path), p, NOW)
    before = person()[1].facts[a]
    ku.apply(fact(before.text, ['gmail:evicted'], date='2026-09-07'), NOW + timedelta(days=1))
    after = person()[1].facts[a]
    assert (after.status, after.archived_reason, after.seen, after.conf, after.first, after.last) == (
        before.status, before.archived_reason, before.seen, before.conf, before.first, before.last)


def test_a1_explicit_owner_confirmation_still_upgrades_authority():
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    before = person()[1].facts
    result = ku.apply(fact(before[a].text, ['owner:confirmation'], source='user_edit'), NOW)
    assert result['applied']['confirmed'] == 1
    assert person()[1].facts[a].source == 'user_edit' and person()[1].facts[a].seen == 2
    assert person()[1].facts[b] == before[b]


def test_a1_explicit_owner_archive_remains_authoritative_for_duplicate_family():
    import knowledge_edit
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    path, p = person()
    knowledge_edit.op_archive(path.stem, a, NOW)
    before = person()[1].facts
    assert before[a].archived_reason == 'user_archived'
    ku.apply(fact(before[b].text, ['gmail:new'], date='2026-10-06'), NOW + timedelta(days=30))
    assert person()[1].facts == before


def test_a1_retained_family_evidence_also_blocks_research_replay():
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    before = person()[1].facts
    ku.apply(fact(before[a].text, ['imessage:two'], source='web_research'), NOW)
    assert person()[1].facts == before



def test_a1_legacy_incompleteness_survives_repeated_calls_and_restart():
    ku.apply(fact(refs=['gmail:original']), NOW)
    path, p = person()
    fid = next(iter(p.facts))
    p.facts[fid].evidence_refs = []
    kg.write_person_file(str(path), p, NOW)
    for refs, observed, confirmed in [(['gmail:evicted-one'], '2026-09-06', 0),
                                    (['gmail:evicted-two'], '2026-09-06', 0),
                                    (['gmail:later'], '2026-09-07', 1),
                                    (['gmail:evicted-three'], '2026-09-07', 0)]:
        result = ku.apply(fact(refs=refs, date=observed), NOW + timedelta(days=1))
        assert result['applied']['confirmed'] == confirmed
        # Re-read the actual serialized record after each separate locked apply.
        saved = person()[1].facts[fid]
        assert saved.evidence_refs == [] and saved.source_ref == 'gmail:original'
    assert saved.seen == 2 and saved.last == '2026-09-07'


def test_a1_impossible_family_observation_date_is_unknown():
    a, b = _fold_children(['gmail:one'], ['imessage:two'])
    path, p = person()
    p.facts[b].evidence_refs = []
    p.facts[b].last = '2026-00-01'
    kg.write_person_file(str(path), p, NOW)
    before = person()[1].facts
    ku.apply(fact(before[a].text, ['gmail:new'], date='2026-09-07'), NOW + timedelta(days=1))
    assert person()[1].facts == before
