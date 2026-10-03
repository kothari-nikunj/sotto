"""Captured obligations use source integrity checks; the brief model judges fulfillment."""
from datetime import datetime
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
for part in ('_shared/lib', '_shared/scripts', 'morning-brief/scripts'):
    sys.path.insert(0, str(ROOT / part))
import continuity_resolve as cr
from delivery_effects import loop_version


def _capture(tmp_path, monkeypatch, *, ask='send Dana the deck'):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    action = {'action_type': 'follow_up', 'channel': 'email',
              'contact_identifier': 'dana@acme.com', 'contact_name': 'Dana',
              'summary': 'You committed to: ' + ask, 'ask': ask,
              'resolution_mode': 'source_grounded', 'source': 'granola',
              'created_at': '2026-09-18 17:00:00'}
    result = cr.resolve({'today': '2026-09-18', 'new_actions': [action]},
                        datetime(2026, 9, 18, 18), resolve_existing=False)
    return result['active'][0]


def _reviewed(update):
    """The receipt compose_brief stamps once a completed critic pass reviewed this closure."""
    return {**update, 'criticReceipt': cr.record_critic_approval(update)}


def _propose(row, body, *, quote=None, message_id='later-message', reviewed=True,
             recipient='dana@acme.com', sent=True, version=None, date='2026-09-19T10:00:00Z'):
    message = {'id': message_id, 'date': date, 'body': body,
               'isSent': sent, 'from': 'dana@acme.com', 'to': recipient}
    update = {'loopId': row['anchor_key'], 'loopVersion': version or loop_version(row),
              'status': 'resolved', 'evidence': [{'sourceType': 'email',
              'sourceId': message_id, 'snippet': body if quote is None else quote}]}
    if reviewed:
        update = _reviewed(update)
    return cr.resolve({'today': '2026-09-19', 'emails': [message], 'loop_updates': [update]},
                      datetime(2026, 9, 19, 12), resolve_existing=False)


@pytest.mark.parametrize('ask,body', [
    ('send Dana the deck', 'I shared the slides with Dana.'),
    ('enviar el informe a Dana', 'Ya le envié el informe a Dana.'),
])
def test_source_grounded_proposal_can_use_paraphrase_or_multilingual_evidence(
        tmp_path, monkeypatch, ask, body):
    row = _capture(tmp_path, monkeypatch, ask=ask)
    result = _propose(row, body)
    assert [item['anchor_key'] for item in result['resolved']] == [row['anchor_key']]


@pytest.mark.parametrize('change', ['wrong_quote', 'wrong_source', 'wrong_person',
                                     'wrong_direction', 'old_message', 'stale_version'])
def test_source_grounded_proposal_still_needs_integrity_and_identity(tmp_path, monkeypatch, change):
    row = _capture(tmp_path, monkeypatch)
    kwargs = {
        'wrong_quote': {'quote': 'This text does not appear in the source'},
        'wrong_source': {'message_id': 'other-message', 'quote': 'The deck is sent.'},
        'wrong_person': {'recipient': 'stranger@example.com'},
        'wrong_direction': {'sent': False},
        'old_message': {'date': '2026-09-17T10:00:00Z'},
        'stale_version': {'version': 'stale-revision'},
    }[change]
    if change == 'wrong_source':
        # Proposal cites an ID that is absent from the source bundle.
        result = cr.resolve({'today': '2026-09-19', 'emails': [{
            'id': 'later-message', 'date': '2026-09-19T10:00:00Z',
            'body': 'The deck is sent.', 'isSent': True, 'to': 'dana@acme.com'}],
            'loop_updates': [_reviewed({'loopId': row['anchor_key'],
                              'loopVersion': loop_version(row),
                              'status': 'resolved', 'evidence': [{'sourceType': 'email',
                              'sourceId': 'other-message', 'snippet': 'The deck is sent.'}]})]},
            datetime(2026, 9, 19, 12), resolve_existing=False)
    else:
        result = _propose(row, 'The deck is sent.', **kwargs)
    assert result['resolved'] == []
    assert cr._load_items()[row['anchor_key']]['status'] == 'open'


def test_one_verified_proposal_does_not_close_a_separate_obligation(tmp_path, monkeypatch):
    deck = _capture(tmp_path, monkeypatch)
    # Explicitly separate another ask from the same counterpart, as a captured commitment can be.
    memo = dict(deck, anchor_key='memo-commitment', summary='Review Dana\'s memo', ask='review memo')
    cr._persist(memo)
    result = _propose(deck, 'I shared the slides with Dana.')
    assert [item['anchor_key'] for item in result['resolved']] == [deck['anchor_key']]
    assert [item['anchor_key'] for item in result['active']] == [memo['anchor_key']]


def test_verified_model_reason_is_bounded_on_existing_loop_record(tmp_path, monkeypatch):
    row = _capture(tmp_path, monkeypatch)
    body = 'I shared the slides with Dana.'
    message = {'id': 'later-message', 'date': '2026-09-19T10:00:00Z', 'body': body,
               'isSent': True, 'to': 'dana@acme.com'}
    reason = 'The slide handoff completes this request. ' * 20
    update = {'loopId': row['anchor_key'], 'loopVersion': loop_version(row),
              'status': 'resolved', 'reason': reason, 'evidence': [{'sourceType': 'email',
              'sourceId': 'later-message', 'snippet': body}]}
    result = cr.resolve({'today': '2026-09-19', 'emails': [message],
                         'loop_updates': [_reviewed(update)]}, datetime(2026, 9, 19, 12),
                        resolve_existing=False)
    assert result['resolved'][0]['resolution_reason'] == reason[:240]


def test_closure_the_critic_did_not_review_stays_open(tmp_path, monkeypatch):
    """Code cannot read meaning: a quote inside "I haven't sent the deck yet" passes every integrity
    check, so without the critic's review receipt (skipped, held, failed, native path) it stays open."""
    row = _capture(tmp_path, monkeypatch)
    body = "I haven't sent the deck yet, will do next week."
    result = _propose(row, body, quote='sent the deck', reviewed=False)
    assert result['resolved'] == []
    assert cr._load_items()[row['anchor_key']]['status'] == 'open'
    # A receipt is bound to the exact proposal: one minted for another quote does not carry over.
    stale = _reviewed({'loopId': row['anchor_key'], 'loopVersion': loop_version(row),
                       'status': 'resolved', 'evidence': [{'sourceType': 'email',
                       'sourceId': 'later-message', 'snippet': 'will do next week'}]})
    stale['evidence'][0]['snippet'] = 'sent the deck'
    message = {'id': 'later-message', 'date': '2026-09-19T10:00:00Z', 'body': body,
               'isSent': True, 'to': 'dana@acme.com'}
    result = cr.resolve({'today': '2026-09-19', 'emails': [message], 'loop_updates': [stale]},
                        datetime(2026, 9, 19, 12), resolve_existing=False)
    assert result['resolved'] == []


@pytest.mark.parametrize('quote', ['T', 'Thanks', 'see you Tue'])
def test_trivial_quote_fragment_is_not_evidence_even_when_reviewed(tmp_path, monkeypatch, quote):
    row = _capture(tmp_path, monkeypatch)
    result = _propose(row, 'Thanks Dana, see you Tuesday.', quote=quote)
    assert result['resolved'] == []
    assert len(quote) < cr.MIN_EVIDENCE_QUOTE_CHARS
