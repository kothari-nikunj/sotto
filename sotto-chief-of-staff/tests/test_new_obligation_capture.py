"""Required new-obligation capture stays source bound through extraction formatting drift."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

import compose_brief as cb
import continuity_resolve as cr
import knowledge as kg
import knowledge_edit as ke
from ledger_provenance import classify_ledger_sources, digest_frontmatter, source_loop_sources
from delivery_effects import loop_version
from source_context import email_quote_found, observed_capture_quote

NOW = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
EMAIL = 'maya.capture@example.test'
PHONE = '+14155550193'


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')
    monkeypatch.delenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', raising=False)
    people = tmp_path / 'knowledge/people'
    people.mkdir(parents=True)
    kg.write_person_file(str(people / 'c_abc193.md'), kg.PersonFile(
        name='Maya Capture', canonical_id='c_abc193', identifiers=[EMAIL, PHONE]))


def promise(text='I’ll send the deck Friday.'):
    return {'id': 'capture-mail', 'threadId': 'capture-thread', 'from': 'owner@example.test',
            'to': EMAIL, 'isSent': True, 'date': '2026-10-19T08:00:00Z', 'body': text}


def action(quote="I'll send the deck Friday.", identifier=EMAIL):
    return {'type': 'follow_up', 'channel': 'email', 'contactName': 'Maya Capture',
            'contactIdentifier': identifier, 'contextSummary': 'Send the deck',
            'contextAsk': 'Send the deck Friday', 'deadlineDate': '2026-10-23',
            'helpKind': 'none', 'newObligation': True, 'emailThreadId': 'capture-thread',
            'emailMessageId': 'capture-mail',
            'evidence': [{'sourceType': 'gmail', 'sourceId': 'capture-mail', 'snippet': quote}]}


def local(mail=None):
    return {'emails': [mail or promise()], 'imessage': [{'id': 'observed-phone',
        'handle': PHONE, 'timestamp': '2026-10-18T08:00:00Z', 'is_from_me': False,
        'text': 'Thanks for the meeting notes.'}]}


@pytest.mark.parametrize('identifier', [EMAIL, PHONE])
def test_actual_composer_contract_captures_first_promise_and_retains_observed_quote(identifier):
    candidate = action(identifier=identifier)
    assert {'newObligation', 'evidence'} <= set(cb._ACTION_ITEM_SCHEMA['required'])
    response = {'markdown': f'## Should Handle Today\n**Maya Capture**<!--id:{EMAIL}|ch:email--> - Send the deck Friday.',
                'actionItems': [candidate], 'extractedKnowledge': {}}
    # Offline provider fixture exercises the real response normalizer, source
    # allowlist and composer contract; semantic model quality is evaluated elsewhere.
    out = cb.compose({'type': 'morning', 'now': NOW.isoformat(), 'local': local(),
                      'google': {'emails': [promise()], 'userEmail': 'owner@example.test'}},
                     llm=lambda *args, **kwargs: json.dumps(response), critic=False)
    assert len(out['actions']) == 1 and out['actions'][0]['newObligation'] is True
    result = cr.resolve({'today': '2026-10-19', 'local': local(), 'new_actions': out['actions']}, NOW)
    row, = result['active']
    assert row['created_at'] == '2026-10-19T08:00:00+00:00'
    assert row['canonical_id'] == 'c_abc193'
    assert row['source_refs'][0]['snippet'] == promise()['body']
    assert row['source_refs'][0]['sourceId'] == 'capture-mail'
    assert source_loop_sources(row) == {'gmail'}
    assert result['capture_outcomes'] == {'source_verified': 1, 'source_rejected': 0, 'quotes_normalized': 1}
    assert not result['resolved']
    # Delivery activity and an explicit partial/future promise never close a row.
    partial = {'id': 'partial', 'handle': PHONE, 'is_from_me': True,
        'timestamp': '2026-10-19T09:00:00Z', 'text': 'Only the first section is ready; I will send the rest Friday.'}
    later = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [partial]}}, NOW)
    assert not later['resolved'] and [it['anchor_key'] for it in later['active']] == [row['anchor_key']]


def test_typographic_recovery_preserves_part_boundaries_words_and_case():
    assert observed_capture_quote(['I’ll send the “final deck”.'], 'I\'ll send the "final deck".') == 'I’ll send the “final deck”.'
    assert observed_capture_quote(['I will not send the deck.'], 'I will send the deck.') is None
    assert observed_capture_quote(['I’ll send', 'the deck Friday.'], "I'll send the deck Friday.") is None
    assert observed_capture_quote(['I’ll send the Deck Friday.'], "I'll send the deck Friday.") is None
    # The recovery helper is capture-only; completion still requires actual source wording.
    assert not email_quote_found(promise(), "I'll send the deck Friday.")


@pytest.mark.parametrize('change', ['missing_id', 'wrong_quote', 'wrong_person', 'future', 'revoked', 'ambiguous_alias', 'malformed_refs'])
def test_unproved_first_capture_is_explicitly_rejected_without_fallback(tmp_path, monkeypatch, change):
    candidate, mail = action(identifier=PHONE), promise()
    if change == 'missing_id':
        candidate['evidence'][0]['sourceId'] = 'not-observed'
    elif change == 'wrong_quote':
        candidate['evidence'][0]['snippet'] = 'I will send the whole deck today.'
    elif change == 'wrong_person':
        mail['to'] = 'stranger@example.test'
    elif change == 'future':
        mail['date'] = '2026-10-20T08:00:00Z'
    elif change == 'revoked':
        monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', '["imessage"]')
    elif change == 'ambiguous_alias':
        kg.write_person_file(str(tmp_path / 'knowledge/people/c_abc194.md'), kg.PersonFile(
            name='Maya Capture', canonical_id='c_abc194', identifiers=[PHONE, 'other@example.test']))
    else:
        candidate['evidence'] = 42
    result = cr.resolve({'today': '2026-10-19', 'local': local(mail), 'new_actions': [candidate]}, NOW)
    assert result['active'] == [] and result['resolved'] == [] and cr._load_items() == {}
    assert result['capture_outcomes']['source_rejected'] == 1
    assert result['capture_outcomes']['source_verified'] == 0


def test_two_typographic_clauses_remain_distinct_and_replay_one_row_per_task():
    mail = promise('I’ll send the deck Friday. I’ll also send the checklist Friday.')
    deck = action()
    checklist = {**action("I'll also send the checklist Friday."),
                 'contextSummary': 'Send the checklist', 'contextAsk': 'Send the checklist Friday'}
    payload = {'today': '2026-10-19', 'local': local(mail), 'new_actions': [deck, checklist]}
    result = cr.resolve(payload, NOW)
    assert {row['summary'] for row in result['active']} == {'Send the deck', 'Send the checklist'}
    original = {row['anchor_key'] for row in result['active']}
    payload['new_actions'].reverse()
    replay = cr.resolve(payload, NOW)
    assert {row['anchor_key'] for row in replay['active']} == original
    assert len(cr._load_items()) == 2


def test_owner_origin_stays_protected_and_requires_an_audited_source_transition():
    created = ke.op_loop_add_admitted_owner_text('Send the deck', owner_text_span='Send the deck',
        owner_event_id='capture-owner-event', now=NOW)
    row = cr._load_items()[created['anchor_key']]
    # A valid owner row with a recipient remains owner-authored. This is the parity
    # boundary under review, not permission to redigest source content as owner-only.
    row['contact_identifier'] = EMAIL
    row['content_provenance']['digest'] = digest_frontmatter(row)
    cr._persist(row)
    before = deepcopy(row)
    message = {**promise('The complete deck is attached.'), 'date': '2026-10-19T11:00:00Z'}
    update = {'loopId': row['anchor_key'], 'loopVersion': loop_version(row),
              'status': 'resolved', 'evidence': [{'sourceType': 'gmail', 'sourceId': 'capture-mail',
                                                'snippet': message['body']}]}
    update['criticReceipt'] = cr.record_critic_approval(update)
    proof, reason = cr._completion_evidence_result(row, update, {'emails': [message]},
        observed_before=datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    assert proof is None and reason == 'invalid_origin_provenance'
    assert cr._load_items()[row['anchor_key']] == before
    assert classify_ledger_sources(before) == ('owner', frozenset())


@pytest.mark.parametrize('fields, expected', [
    ({'deadlineDate': '', 'contextDeadline': 'when legal approves'}, None),
    ({'deadlineDate': '', 'contextDeadline': '2 days after signing'}, None),
    ({'deadlineDate': '2026-02-30', 'contextDeadline': '2026-02-30'}, None),
    ({'deadlineDate': '', 'contextDeadline': ''}, None),
    ({'deadlineDate': '2026-10-23', 'contextDeadline': 'Friday'}, '2026-10-23'),
    ({'deadline': '2026-10-23T15:00:00Z', 'deadlineDate': ''}, '2026-10-23'),
    ({'deadlineDate': '', 'contextDeadline': '2026-10-23'}, '2026-10-23'),
    ({'deadlineDate': '', 'contextDeadline': '2026-10-23 (Fri)'}, '2026-10-23'),
])
def test_capture_stores_only_real_calendar_deadlines(fields, expected):
    candidate = {**action(), **fields}
    result = cr.resolve({'today': '2026-10-19', 'local': local(),
                         'new_actions': [candidate]}, NOW)
    row, = result['active']
    assert row['deadline'] == expected
    assert cr._load_items()[row['anchor_key']]['deadline'] == expected


@pytest.mark.parametrize('deadline', ['when legal approves', '2 days after signing', '2026-02-30'])
def test_legacy_unknown_deadline_does_not_prevent_parking_or_accelerate_chase(deadline, monkeypatch):
    monkeypatch.setenv('SOTTO_CHASE_AFTER_DAYS', '3')
    old = {'status': 'open', 'action_type': 'follow_up', 'created_at': '2026-01-01',
           'deadline': deadline}
    fresh = {**old, 'action_type': 'waiting_on', 'created_at': '2026-10-19'}
    assert cr.ledger_io.park_candidate(old, '2026-10-19')
    assert not cr.chase_due(fresh, '2026-10-19', NOW, {})
    assert old['deadline'] == fresh['deadline'] == deadline


def test_unknown_deadline_has_no_overdue_priority(monkeypatch):
    old = {'anchor_key': 'older', 'created_at': '2026-10-01', 'deadline': None}
    conditional = {'anchor_key': 'conditional', 'created_at': '2026-10-18',
                   'deadline': '2 days after signing'}
    persisted = []
    monkeypatch.setattr(cr, '_relationship_history', lambda: {})
    monkeypatch.setattr(cr, 'chase_due', lambda *args: True)
    monkeypatch.setattr(cr, '_persist', lambda row, **kwargs: persisted.append(row['anchor_key']))
    cr._stamp_chase([conditional, old], '2026-10-19', NOW.replace(tzinfo=None))
    assert persisted == ['older']


@pytest.mark.parametrize('deadline', ['2026-10-23', '2026-10-23T15:00:00Z', '2026-10-23 (Fri)'])
def test_valid_legacy_deadline_still_protects_task_and_controls_chase(deadline):
    row = {'status': 'open', 'action_type': 'follow_up', 'created_at': '2026-01-01',
           'deadline': deadline}
    assert not cr.ledger_io.park_candidate(row, '2026-10-19')
    waiting = {**row, 'action_type': 'waiting_on'}
    assert not cr.chase_due(waiting, '2026-10-19', NOW, {})
    assert cr.chase_due(waiting, '2026-10-24', NOW, {})
    assert row['deadline'] == deadline
