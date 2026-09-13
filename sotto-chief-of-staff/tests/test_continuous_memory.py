"""Frozen-time replay of history learning, evidence curation, source loss and owner feedback."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import context_learning as learning
import dreamer
import gather_google
import knowledge as kg
import knowledge_update as ku
import memory_cycle
import personal_context
import relevance
import usefulness_feedback

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


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


def test_same_or_overlapping_evidence_does_not_confirm_itself():
    ku.apply(fact(refs=['gmail:one', 'imessage:two']), NOW)
    for refs in (['gmail:one'], ['imessage:two'], ['gmail:one', 'imessage:two']):
        assert ku.apply(fact(refs=refs), NOW)['applied']['confirmed'] == 0
    initial = next(iter(person()[1].facts.values()))
    assert initial.seen == 1 and initial.conf == .7 and initial.last == '2026-09-06'
    assert ku.apply(fact(refs=['gmail:three']), NOW)['applied']['confirmed'] == 1
    assert next(iter(person()[1].facts.values())).seen == 2


def test_reobservation_without_new_evidence_refreshes_recency_but_confirms_nothing():
    """An extraction may omit source_ref. Re-reading the same evidence is not confirmation
    (seen/conf stay), but it IS a fresh observation: a fact restated every morning must not decay
    from the day it was first written and be pruned as if nobody had mentioned it since."""
    ku.apply(fact(refs=['gmail:one']), NOW)
    later = NOW + timedelta(days=30)
    assert ku.apply(fact(refs=[], date='2026-10-06'), later)['applied']['confirmed'] == 0
    restated = next(iter(person()[1].facts.values()))
    assert restated.seen == 1 and restated.conf == .7 and restated.last == '2026-10-06'
    import dataclasses
    unrefreshed = dataclasses.replace(restated, last='2026-09-06')
    horizon = later + timedelta(days=21)   # before either copy reaches the confidence floor
    assert kg.effective_confidence(restated, horizon) > kg.effective_confidence(unrefreshed, horizon), \
        'decay counts from the latest observation, not from the first'


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
    return {'people': [{'subject': 'alex@example.com', 'facts': [
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
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    monkeypatch.setattr(memory_cycle.prewarm_graph, 'prewarm', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.style_extract, 'extract', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.relationship_pulse, 'observe_history', lambda *a, **k: None)
    sample = records()
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a: sample)
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


@pytest.mark.parametrize('reason', ['background_budget_exhausted', 'background_provider_unsupported'])
def test_background_budget_rejection_holds_cursor_and_stops_remaining_work(tmp_path, monkeypatch, reason):
    from urllib.error import HTTPError
    monkeypatch.delenv('SOTTO_BACKGROUND_UNMETERED')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant')
    monkeypatch.setattr(memory_cycle.gemini_transport, '_background_budget_preflight', lambda: None)
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    observed = records()
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a: observed)
    fetches, curations = [], []
    def fetch(*args):
        fetches.append(True)
        return {'status': 'ok', 'rows': [], 'next_cursor': 'next', 'complete': False}
    def rejected(*args, **kwargs):
        if reason == 'background_budget_exhausted':
            raise HTTPError('https://proxy.example/native', 402, 'budget', {}, None)
        raise memory_cycle.gemini_transport.BackgroundModelHeldError(reason, 'held')
    receipt = memory_cycle.run(NOW, fetch, rejected,
                               lambda **k: curations.append(True) or {'reviewed': 0})
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(fetches) == 1 and curations == []
    assert state['sources']['imessage']['cursor'] is None
    assert 'retry_after' not in state['sources']['imessage']
    assert receipt['history'][0]['status'] == 'held'
    assert receipt['dreamer'] == {'status': 'held', 'reason': reason}


def test_feedback_is_an_explicit_bound_example_not_a_permission(tmp_path, monkeypatch):
    brief = tmp_path / 'briefs/2026-09-07_morning.json'
    brief.parent.mkdir()
    text = 'The preschool sunscreen waiver still needs completing before tomorrow.'
    brief.write_text(json.dumps({'brief_text': text}))
    brief.with_name('2026-09-07.morning.delivered').write_text('run')
    usefulness_feedback.record(brief.stem, 'useful', text, 'This is exactly what I need help remembering.')
    rendered = personal_context.render_feedback()
    assert brief.stem in rendered and '"outcome": "useful"' in rendered
    assert text not in rendered
    assert text not in relevance.judgment_prompt('another school deadline')
    assert not (tmp_path / 'preferences.json').exists()
    with pytest.raises(ValueError, match='verbatim'):
        usefulness_feedback.record(brief.stem, 'not_useful', 'invented text')
    monkeypatch.setenv('SOTTO_UNATTENDED', 'true')
    with pytest.raises(ValueError, match='owner'):
        usefulness_feedback.record(brief.stem, 'not_useful')


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
                'review_messages': learning.REVIEW_MESSAGES,
                'dream_people': dreamer.DREAM_PEOPLE, 'dream_facts': dreamer.DREAM_FACTS,
                'feedback_days': personal_context.FEEDBACK_DAYS, 'feedback_limit': personal_context.FEEDBACK_LIMIT,
                'source_retry_seconds': memory_cycle.SOURCE_RETRY_SECONDS,
                'unchanged_extraction_attempts': memory_cycle.UNCHANGED_EXTRACTION_ATTEMPTS,
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
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a: sample)
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
    assert final['sources']['imessage']['pages'] == 1  # other two bounded turns were disabled sources
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
        invalid['people'].append('bad shape')
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


@pytest.mark.parametrize('status', [400, 422, 429, 503])
def test_permanent_provider_rejection_waits_for_a_changed_contract(tmp_path, monkeypatch, status):
    from urllib.error import HTTPError
    monkeypatch.setattr(memory_cycle, 'allowed', lambda s: s == 'imessage')
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    observed = records()
    monkeypatch.setattr(learning, 'observations', lambda *a: observed)
    attempts = []
    def learn(*a, **k):
        attempts.append(True)
        raise HTTPError('https://provider.invalid/private', status, 'PRIVATE BODY', {}, None)
    fetch = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': 'next', 'complete': False}
    quiet = lambda **k: {'reviewed': 0}
    memory_cycle.run(NOW, fetch, learn, quiet)
    memory_cycle.run(NOW + timedelta(days=1), fetch, learn, quiet)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(attempts) == (1 if status in (400, 422) else 2)
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
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a: sample)
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
