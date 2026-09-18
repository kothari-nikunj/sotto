from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared/scripts'))
import compose_notification as notification
import gemini
from source_context import source_result


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a' * 32)
    monkeypatch.setattr(notification, 'enrich', lambda items, now: items)
    monkeypatch.setattr(gemini, 'provider_key', lambda _: 'test')
    return tmp_path


def test_empty_and_templates_make_no_model_call(isolated, monkeypatch):
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **k: pytest.fail('unexpected inference'))
    assert notification.compose('proactive', []) == 'NO_NUDGES'
    items = [{'kind': 'intention', 'key': 'x', 'decision_id': 'x', 'title': 'Call Sam', 'detail': 'About the deck'}]
    assert notification.compose('proactive', items) == 'Call Sam About the deck'
    items = [{'kind': 'meeting_prep', 'key': 'y', 'person': 'Sam', 'calendar_start': '2026-09-17T12:30:00Z'}]
    text = notification.compose('proactive', items, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert '30 minutes' in text and text.endswith('Want the full prep on Sam?')
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['effects'][0]['kind'] == 'pending_offer'
    assert not (isolated / 'pending_offer.json').exists()


def test_one_direct_call_reuses_composed_artifact(isolated, monkeypatch):
    calls = []
    def write(*args, **kw):
        calls.append(args)
        return json.dumps({'items': [{'id': 'x', 'text': 'Sam needs the deck.',
                                     'draft': 'Here is the deck.', 'decline': ''}]})
    monkeypatch.setattr(gemini, '_gemini_once', write)
    items = [{'kind': 'commitment', 'key': 'x', 'decision_id': 'x', 'person': 'Sam', 'channel': 'email',
              'identifier': 'sam@example.com', 'thread_id': 'thread-1'}]
    first = notification.compose('proactive', items)
    assert notification.compose('proactive', items) == first
    assert len(calls) == 1
    assert 'Want this in your Gmail drafts?' in first and 'mailto:' not in first
    effects = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())['effects']
    details = json.loads(effects[0]['offer']['detail'])
    assert details['to'] == 'sam@example.com' and details['thread_id'] == 'thread-1'


def test_invalid_ids_have_only_two_attempts_even_after_queue_retry(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: calls.append(1) or json.dumps({
        'items': [{'id': 'invented', 'text': 'bad', 'draft': '', 'decline': ''}]}))
    for _ in range(3):
        with pytest.raises((ValueError, notification.model_work.ModelWorkHeldError)):
            notification.compose('proactive', [{'key': 'x', 'decision_id': 'x', 'kind': 'commitment'}])
    assert len(calls) == 2


def test_oversized_context_is_held_before_inference(isolated, monkeypatch):
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: pytest.fail('oversized input sent'))
    with pytest.raises(notification.model_work.ModelWorkHeldError):
        notification.compose('proactive', [{'kind': 'commitment', 'key': 'x', 'decision_id': 'x', 'detail': 'x' * 48000}])


def test_group_draft_has_no_person_link(isolated, monkeypatch):
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: json.dumps({
        'items': [{'id': 'x', 'text': 'The team needs the deck.', 'draft': 'Here it is.', 'decline': ''}]}))
    bundle = {'events': [{'decision_id': 'x', 'class': 'urgent', 'sender': 'Sam',
                          'event': {'source': 'whatsapp', 'sender_jid': '123@s.whatsapp.net', 'is_group_chat': True}}]}
    assert notification.compose('event', bundle) == 'The team needs the deck.\nHere it is.'


def test_verified_slots_do_not_overlap_and_unknown_constraints_do_not_guess():
    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    calendar = {'all_calendars_complete': True, 'since': now.isoformat(), 'until': '2026-09-20T08:00:00Z',
                'events': [{'start': '2026-09-17T09:00:00Z', 'end': '2026-09-17T10:00:00Z'}]}
    slots = notification.slots('30 minutes Thursday?', calendar, now)
    assert slots and all('09:' not in slot for slot in slots)
    assert notification.slots('30 minutes Thursday after 3?', calendar, now) == []
    assert notification.slots('30 minutes Thursday?', None, now) == []
    assert notification.slots('Thursday or Friday?', calendar, now) == []


def test_scheduling_requires_both_choices_and_uses_code_slots(isolated, monkeypatch):
    row = {'id': 'x', 'text': 'Sam asked to meet.', 'draft': '{{slots}} works for me.', 'decline': 'I will pass, thanks.'}
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: json.dumps({'items': [row]}))
    item = {'kind': 'scheduling_ask', 'key': 'x', 'decision_id': 'x', 'channel': 'email', 'identifier': 'sam@example.com',
            'slots': ['Thu Sep 17, 10:00 AM UTC']}
    text = notification.compose('proactive', [item])
    assert 'Accept: Thu Sep 17, 10:00 AM UTC works for me.' in text
    assert 'Decline: I will pass, thanks.' in text
    assert 'Want this in your Gmail drafts?' not in text


def test_post_meeting_no_evidence_is_silent(isolated, monkeypatch):
    monkeypatch.setattr(notification, 'allowed', lambda _: False)
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: pytest.fail('no evidence'))
    assert notification.compose('event', {'events': [{'class': 'post_meeting', 'event': {'summary': 'Sync'}}]}) == 'NO_NUDGES'


def test_empty_post_meeting_yields_to_the_actionable_item_behind_it(isolated, monkeypatch):
    """A post-meeting item with no usable notes is not a reason to say nothing: the next
    candidate is tried, and the writer runs once, only when that candidate needs it."""
    monkeypatch.setattr(notification, 'allowed', lambda source: source != 'granola')
    calls = []

    def writer(items, llm):
        calls.append([i['id'] for i in items])
        return [{'id': items[0]['id'], 'text': 'Sam still needs the deck from you.', 'draft': '', 'decline': ''}]
    monkeypatch.setattr(notification, '_write', writer)
    monkeypatch.setattr(notification, 'enrich', lambda items, now: items)
    bundle = {'events': [{'class': 'post_meeting', 'event': {'summary': 'Sync'}},
                         {'class': 'chase', 'id': 'ask-1', 'person': 'Sam', 'detail': 'the deck', 'event': {}}]}
    text = notification.compose('event', bundle, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert 'Sam still needs the deck' in text and len(calls) == 1
    # …and the same ask alone gives the same answer
    alone = {'events': [{'class': 'chase', 'id': 'ask-1', 'person': 'Sam', 'detail': 'the deck', 'event': {}}]}
    assert notification.compose('event', alone, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc)) == text


def test_unrendered_candidates_do_not_earn_delivery_effects(isolated, monkeypatch):
    effects = notification.delivery_effects
    effects.stage([{'kind': 'proactive_seen', 'key': 'x', 'decision_id': 'x'}, {'kind': 'proactive_seen', 'key': 'y'},
                   {'kind': 'intention', 'id': 'unrendered'}, {'kind': 'source_permissions', 'sources': ['calendar']}])
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: '{"items":[]}')
    assert notification.compose('proactive', [{'kind': 'commitment', 'key': 'x', 'decision_id': 'x'}]) == 'NO_NUDGES'
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['notification_decision_ids'] == []
    assert doc['effects'] == [{'kind': 'source_permissions', 'sources': ['calendar']}]


def test_post_meeting_matches_once_reuses_output_and_restricts_recipient(isolated, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    event = {'summary': 'Planning', 'start': '2026-09-17T10:00:00Z',
             'attendees': [{'email': 'sam@example.com', 'name': 'Sam'}]}
    def gather(argv, **kwargs):
        Path(argv[-1]).write_text(json.dumps({'meetings': [
            {'title': 'Planning', 'start': event['start'], 'transcript': 'I will send the deck.'},
            {'title': 'Other meeting', 'start': event['start'], 'transcript': 'Private unrelated material.'}]}))
        return SimpleNamespace(returncode=0)
    calls = []
    def compose(inputs, **kwargs):
        calls.append(inputs)
        return {'drafts': [{'to_email': 'sam@example.com', 'body': 'Here is the deck.'},
                           {'to_email': 'invented@example.com', 'body': 'An invented recipient.'}]}
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    monkeypatch.setattr(notification, '_load', lambda *args: SimpleNamespace(compose=compose))
    item = {'event': event, 'title': 'Planning'}
    first = notification._post_meeting(item)
    assert notification._post_meeting(item) == first
    assert len(calls) == 1 and len(calls[0]['granola']) == 1
    assert 'Here is the deck.' in first and 'invented' not in first
    assert 'Want this in your Gmail drafts?' in first
    assert not (isolated / 'pending_offer.json').exists()


def test_calendar_permission_revocation_prevents_availability(isolated, monkeypatch):
    from types import SimpleNamespace
    consent = iter([True, False])
    monkeypatch.setattr(notification, 'allowed', lambda _: next(consent))
    def gather(argv, **kwargs):
        Path(argv[argv.index('--cal-out') + 1]).write_text('[]')
        Path(argv[-1]).write_text(json.dumps({'calendar': source_result('ok', complete=True,
            since='2026-09-17T08:00:00Z', until='2026-09-20T08:00:00Z')}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    assert notification._calendar() is None


def test_slots_must_be_inside_verified_coverage_and_constraints():
    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    calendar = {'all_calendars_complete': True, 'events': [], 'since': now.isoformat(), 'until': '2026-09-17T09:30:00Z'}
    assert notification.slots('30 minutes Thursday?', calendar, now) == ['Thu Sep 17, 09:00 AM UTC']
    assert notification.slots('30 minutes Friday?', calendar, now) == []
    assert notification.slots('30 minutes Thursday 2pm?', calendar, now) == []
    assert notification.slots('30 minutes Thursday, I can only do lunch.', calendar, now) == []


def test_partial_calendar_is_not_evidence_of_free_time(isolated, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    def gather(argv, **kwargs):
        Path(argv[argv.index('--cal-out') + 1]).write_text('[]')
        Path(argv[-1]).write_text(json.dumps({'calendar': source_result('partial', complete=False,
            since='2026-09-17T08:00:00Z', until='2026-09-20T08:00:00Z')}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    assert notification._calendar() is None


def test_complete_primary_calendar_cannot_establish_all_calendar_availability(isolated, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    def gather(argv, **kwargs):
        Path(argv[argv.index('--cal-out') + 1]).write_text('[]')
        Path(argv[-1]).write_text(json.dumps({'calendar': source_result('ok', complete=True,
            since='2026-09-17T08:00:00Z', until='2026-09-17T10:00:00Z')}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    observed = notification._calendar()
    assert observed is not None
    proposed = notification.slots('30 minutes Thursday?', observed,
                                   datetime(2026, 9, 17, 8, tzinfo=timezone.utc))
    assert proposed == []


def test_unknown_scheduling_keeps_grounded_copy_without_false_acceptance(isolated):
    row = {'id': 'x', 'text': 'Sam wants to discuss the revised launch date.',
           'draft': '', 'decline': ''}
    text = notification._render(row, {'kind': 'scheduling_ask', 'slots': []})
    assert 'revised launch date' in text and text.endswith('How would you like to respond?')
    assert 'Accept:' not in text and 'Decline:' not in text


def test_reminder_survives_oversized_writer_and_only_selected_item_is_covered(isolated):
    items = [{'decision_id': 'urgent', 'key': 'a', 'kind': 'urgent', 'detail': 'x' * 48000},
             {'decision_id': 'reminder', 'key': 'b', 'kind': 'intention', 'title': 'Call Sam'}]
    assert notification.compose('proactive', items) == 'Call Sam'
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['notification_decision_ids'] == ['reminder']


def test_scanner_fallback_identity_is_opaque(isolated):
    notification.compose('proactive', [{'key': 'birthday:Sam-Jones', 'kind': 'intention', 'title': 'Call Sam'}])
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert len(doc['notification_decision_ids'][0]) == 64
    assert 'Sam' not in json.dumps(doc['notification_decision_ids'])


def test_writer_projection_and_echo_guard(isolated):
    item = {'id': 'x', 'kind': 'chase', 'person': 'Sam', 'identifier': 'sam@example.com',
            'thread_id': 'thread-secret', 'open_loops': [{'name': 'Sam', 'what': 'Send deck',
            'identifier': '123@s.whatsapp.net', 'anchor_key': 'secret-anchor', 'chased_count': 2}]}
    prompt = json.dumps(notification._writer_item(item))
    for token in ('sam@example.com', 'thread-secret', 'secret-anchor', 'chased_count', '123@s.whatsapp.net'):
        assert token not in prompt
    for text in ('Sam: needs deck', 'The ledger needs updating', 'Sam uses 123@s.whatsapp.net', 'thread-secret', '{{slots}}', 'x' * 1201):
        with pytest.raises(ValueError):
            notification._validate(json.dumps({'items': [{'id': 'x', 'text': text, 'draft': '', 'decline': ''}]}), [item])
    # prose with a colon in it is not a labelled field
    assert notification._validate(json.dumps({'items': [{'id': 'x', 'text': 'Meeting at noon: bring the deck.',
                                                          'draft': '', 'decline': ''}]}), [item])


def test_empty_decision_is_not_cached_across_new_ticks(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: calls.append(1) or '{"items":[]}')
    for tick in ('b', 'c', 'd'):
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', tick * 32)
        assert notification.compose('proactive', [{'key': 'x', 'kind': 'commitment'}]) == 'NO_NUDGES'
    assert len(calls) == 3
    assert not list((isolated / 'events/notification-artifacts').glob('*.json'))


def test_transient_writer_failure_waits_for_queue_backoff(isolated, monkeypatch):
    from urllib.error import HTTPError
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise HTTPError('https://provider', 503, 'busy', {}, None)
    monkeypatch.setattr(gemini, '_gemini_once', fail)
    with pytest.raises(HTTPError):
        notification.compose('proactive', [{'key': 'x', 'kind': 'commitment'}])
    assert len(calls) == 1


def test_short_offer_question_survives_delivery_whitespace_normalization(isolated, monkeypatch):
    item = {'kind': 'commitment', 'identifier': 'sam@example.com', 'channel': 'email'}
    rendered = notification._render({'text': 'Sam needs the deck.', 'draft': 'Here is the deck.', 'decline': ''}, item)
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    question = doc['effects'][0]['offer']['question']
    assert question == 'Want this in your Gmail drafts?'
    assert question in ' '.join(rendered.split())


def test_one_primary_item_and_one_offer_per_tick(isolated, monkeypatch):
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: pytest.fail('deterministic primary'))
    items = [{'decision_id': 'later', 'key': 'later', 'kind': 'meeting_prep', 'person': 'Later',
              'calendar_start': '2026-09-17T12:50:00Z'},
             {'decision_id': 'soon', 'key': 'soon', 'kind': 'meeting_prep', 'person': 'Soon',
              'calendar_start': '2026-09-17T12:10:00Z'}]
    text = notification.compose('proactive', items, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert 'Soon' in text and 'Later' not in text
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['notification_decision_ids'] == ['soon']
    assert len([e for e in doc['effects'] if e['kind'] == 'pending_offer']) == 1


def test_optional_knowledge_failure_does_not_drop_candidate(isolated, monkeypatch):
    monkeypatch.setattr(notification.loops_query, 'query', lambda: {'you_owe': [], 'waiting_on_them': []})
    monkeypatch.setattr(notification.style_apply, 'apply', lambda _: {})
    monkeypatch.setattr(notification, '_read_json_script', lambda *a: (_ for _ in ()).throw(RuntimeError('offline')))
    items = [{'id': 'x', 'kind': 'commitment', 'person': 'Sam'}]
    # Fixture replaces enrich; call the original function captured by its module source.
    import importlib.util
    spec = importlib.util.spec_from_file_location('notification_real_enrich', notification.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_read_json_script', notification._read_json_script)
    assert module.enrich(items, datetime.now(timezone.utc))[0]['person_facts'] == {}
