"""Captured promises require fulfillment of their deliverable, not contact alone."""
from datetime import datetime
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
for part in ('_shared/lib', '_shared/scripts', 'morning-brief/scripts'):
    sys.path.insert(0, str(ROOT / part))
import continuity_resolve as cr
from delivery_effects import loop_version


def _capture(tmp_path, monkeypatch, *, waiting=False, ask='send Dana the deck'):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    action = {'action_type': 'waiting_on' if waiting else 'follow_up', 'channel': 'email',
              'contact_identifier': 'dana@acme.com', 'contact_name': 'Dana',
              'summary': 'You committed to: ' + ask, 'ask': ask,
              'resolution_mode': 'source_grounded', 'source': 'granola',
              'created_at': '2026-09-18 17:00:00'}
    result = cr.resolve({'today': '2026-09-18', 'new_actions': [action]},
                        datetime(2026, 9, 18, 18), resolve_existing=False)
    return result['active'][0]


def _propose(row, body, *, quote=None, waiting=False):
    message = {'id': 'later-message', 'date': '2026-09-19T10:00:00Z', 'body': body,
               'isSent': not waiting, 'from': 'dana@acme.com', 'to': 'dana@acme.com'}
    update = {'loopId': row['anchor_key'], 'loopVersion': loop_version(row),
              'status': 'resolved', 'evidence': [{'sourceType': 'email',
              'sourceId': message['id'], 'snippet': quote or body}]}
    return cr.resolve({'today': '2026-09-19', 'emails': [message], 'loop_updates': [update]},
                      datetime(2026, 9, 19, 12), resolve_existing=False)


@pytest.mark.parametrize('body,quote', [
    ('Thanks for lunch yesterday.', None),
    ('Dana, it was good to meet.', None),
    ('I sent the invoice.', None),
    ("I'll send the deck tomorrow.", None),
    ("I haven't sent the deck yet.", 'sent the deck'),
    ('Have you sent the deck?', None),
    ('The deck looks interesting.', None),
    ('I reviewed the deck.', None),
    ('I need to have the deck sent.', None),
    ('I plan to have the deck sent next week.', None),
    ('Without having sent the deck, I asked for feedback.', 'sent the deck'),
    ('I nearly sent the deck before the meeting.', 'sent the deck'),
    ('I almost sent the deck.', 'sent the deck'),
    ('I thought I sent the deck, but I was mistaken.', 'sent the deck'),
])
def test_bad_resolution_proposals_do_not_close_captured_work(tmp_path, monkeypatch, body, quote):
    row = _capture(tmp_path, monkeypatch)
    result = _propose(row, body, quote=quote)
    assert result['resolved'] == []
    assert cr._load_items()[row['anchor_key']]['status'] == 'open'


@pytest.mark.parametrize('waiting', [False, True])
@pytest.mark.parametrize('body', ['Here is the deck I promised.', 'Sent the deck.',
                                  'The deck is attached.'])
def test_specific_fulfillment_closes_once_in_both_directions(tmp_path, monkeypatch, waiting, body):
    row = _capture(tmp_path, monkeypatch, waiting=waiting)
    result = _propose(row, body, waiting=waiting)
    assert [x['anchor_key'] for x in result['resolved']] == [row['anchor_key']]
    assert _propose(row, body, waiting=waiting)['resolved'] == []


def test_matching_generic_object_does_not_complete_a_different_deliverable(tmp_path, monkeypatch):
    row = _capture(tmp_path, monkeypatch, ask='send the Q3 investor deck')
    assert _propose(row, 'Here is the Q2 sales deck.')['resolved'] == []
    assert _propose(row, 'The Q3 investor deck is attached.')['resolved']


@pytest.mark.parametrize('modifier', ['signed', 'updated', 'reviewed'])
def test_deliverable_qualifiers_must_be_present(tmp_path, monkeypatch, modifier):
    row = _capture(tmp_path, monkeypatch, ask=f'send Dana the {modifier} agreement')
    assert _propose(row, 'Here is the agreement.')['resolved'] == []
    assert _propose(row, f'Here is the {modifier} agreement.')['resolved']


def test_object_noun_is_not_mistaken_for_a_second_action(tmp_path, monkeypatch):
    row = _capture(tmp_path, monkeypatch, ask='send Dana the review deck')
    assert _propose(row, 'Here is the deck.')['resolved'] == []
    assert _propose(row, 'Here is the review deck.')['resolved']
