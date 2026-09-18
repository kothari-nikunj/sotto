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


def test_resolved_obligation_stays_terminal_when_history_learning_reviews_the_exchange(tmp_path):
    opened = continuity_resolve.resolve({'today': '2026-09-05', 'new_actions': [{
        'type': 'reply', 'channel': 'gmail', 'contactName': 'Alex',
        'contactIdentifier': 'alex@example.com', 'contextSummary': 'Confirm Tuesday meeting time',
    }]}, NOW - timedelta(days=2))
    assert len(opened['active']) == 1
    obligation = opened['active'][0]
    closed = continuity_resolve.resolve({'today': '2026-09-07', 'emails': [{
        'id': 'b', 'from': 'Me <me@example.com>', 'to': 'Alex <alex@example.com>',
        'isSent': True, 'date': '2026-09-06T10:00:00Z', 'body': 'Confirmed, see you Tuesday.',
    }], 'loop_updates': [{
        'loopId': obligation['anchor_key'],
        'loopVersion': delivery_effects.loop_version(obligation),
        'status': 'resolved',
        'evidence': [{
            'sourceType': 'email', 'sourceId': 'b',
            'snippet': 'Confirmed, see you Tuesday.',
        }],
    }]}, NOW)
    assert len(closed['resolved']) == 1 and closed['active'] == []

    def model(prompt, config, **kwargs):
        assert 'Confirmed, see you Tuesday.' in prompt
        return json.dumps({'people': [{'subject': 'alex@example.com', 'facts': []}]})

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
    assert dreamer.run(curate, NOW)['reviewed'] == 1
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
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a: [])
    cursors = []
    def fetch(source, window):
        cursors.append(window['cursor'])
        return {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True}
    memory_cycle.run(NOW + timedelta(hours=1), fetch,
                     lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)
    assert cursors == [44]


@pytest.mark.parametrize('reason', ['background_budget_exhausted', 'background_provider_unsupported'])
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
    source_state.write_text(json.dumps({'sources': {'imessage': {'status': 'enabled'}}}))
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
    source.write_text(json.dumps({'sources': {'imessage': {'status': 'enabled'}}}))
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


def test_dreamer_transport_configuration_error_is_not_a_malformed_response(tmp_path, monkeypatch):
    """`raise ValueError('Model proxy requires HTTPS …')` is a configuration fault, not a model
    response. Counting it toward the two-attempt park would silence curation over a typo."""
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
    assert state['receipt']['dreamer'] == {'status': 'failed', 'error': 'ValueError'}


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


def _quiet_memory_inputs(monkeypatch, source='imessage'):
    monkeypatch.setattr(memory_cycle, 'allowed', lambda value: value == source)
    monkeypatch.setattr(memory_cycle, 'resolve_contact_names', lambda value: value)
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'),
                           (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.context_learning, 'observations', lambda *a: [])


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
    _quiet_memory_inputs(monkeypatch, source='gmail')
    path = Path(memory_cycle.state_path()); path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'next_source': 2, 'sources': {'gmail': {
        'since': 10, 'until': 20, 'cursor': 'expired', 'complete': False,
        'pages': 1, 'window_page_base': 0, 'rows': 10, 'reviewed': 10}}}))
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
    assert receipts[-1][0] == {'source': 'gmail', 'status': 'blocked',
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
    assert receipts[5][0]['error_code'] == 'continuation_restart_exhausted'
    assert receipts[6][0] == {'source': 'gmail', 'status': 'blocked',
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
    assert window['pages'] == 104 and window['window_page_base'] == 100
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
    _quiet_memory_inputs(monkeypatch, source='gmail')
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
        calls.append(True)
        return dreamer.run(lambda *a, **k: '{bad json', now)
    complete = lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True}
    learned = lambda *a, **k: {'reviewed': 0, 'facts': 0}
    for hour in range(3):
        memory_cycle.run(NOW + timedelta(hours=hour), complete, learned, malformed)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    assert len(calls) == memory_cycle.UNCHANGED_DREAMER_ATTEMPTS == state['dreamer_failure']['attempts'] == 2
    assert state['receipt']['dreamer']['status'] == 'blocked'

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
    good = {'subject': 'other@example.com', 'facts': [
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
                       {'subject': 'other@example.com', 'facts': []},
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
    reply['people'].append({'subject': 'invented@example.com', 'facts': []})
    def revoked(*a, **k):
        monkeypatch.setattr(learning, 'allowed', lambda _: False)
        return json.dumps(reply)
    with pytest.raises(RuntimeError, match='consent changed'):
        learning.learn(records(), revoked, NOW)
    assert not list(tmp_path.glob('knowledge/people/*.md'))


def test_mixed_extraction_advances_history_and_retains_safe_rejection_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: source == 'gmail')
    monkeypatch.setattr(memory_cycle, 'resolve_contact_names', lambda value: value)
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    observed = [*records(), {**records()[0], 'ref': 'gmail:other',
                            'subject': 'other@example.com', 'name': 'Other'}]
    monkeypatch.setattr(learning, 'observations', lambda source, rows, now: rows)
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
        people = [bad, {'subject': 'other@example.com', 'facts': []}] if len(calls) == 1 else []
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
