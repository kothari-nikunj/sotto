"""A first capture the owner owes must bind the message it cites, when the snapshot could hold it.

Fictional people and reserved example domains only. Reproduces a task saved with a made-up quote
or a never-seen message id, and the two citations a failed bind does not disprove.
"""
from datetime import datetime

import pytest

from test_capture_structure import (  # noqa: F401 -- the fixtures apply by name
    EMAIL, NOW, PHONE, action, cr, sandbox, text)


@pytest.mark.parametrize('channel', ['gmail', 'imessage', 'whatsapp'])
@pytest.mark.parametrize('source_id, quote, saved_quote', [
    ('ask-1', 'A fabricated quote.', None),                                  # real id, invented words
    ('never-seen', 'Can you send Robin’s updated deck?', None),              # id not in the snapshot
    ('ask-1', "Can you send Robin's updated deck?", 'Can you send Robin’s updated deck?'),
    ('ask-1', '', ''),                                                       # a ref without a snippet
])
def test_first_owner_owed_capture_binds_cited_evidence(channel, source_id, quote, saved_quote):
    """Every direction: a first capture citing a message it cannot bind is withheld, never saved
    as an open task whose `source_refs` would later read as source evidence."""
    asked = 'Can you send Robin’s updated deck?'
    candidate = action('reply', quote, source_id, contextAsk='Send the updated deck',
                       deliverable='Send the updated deck', owedBy='owner', newObligation=False,
                       channel=channel, contactIdentifier=EMAIL if channel == 'gmail' else PHONE)
    candidate['evidence'][0]['sourceType'] = channel
    if not quote:
        candidate['evidence'][0].pop('snippet')
    local = ({'emails': [{'id': 'ask-1', 'from': EMAIL, 'to': 'owner@example.com', 'body': asked,
                          'date': '2026-11-10T08:00:00Z', 'isSent': False}]}
             if channel == 'gmail' else {channel: [text('ask-1', asked, False)]})
    result = cr.resolve({'today': '2026-11-10', 'new_actions': [candidate], 'local': local},
                        NOW, resolve_existing=False)
    if saved_quote is None:
        assert result['active'] == [] and cr._load_items() == {}
        assert result['capture_outcomes']['source_rejected'] == 1
        return
    row, = result['active']
    assert row['action_type'] == 'reply' and result['capture_outcomes']['source_rejected'] == 0
    assert [ref.get('snippet', '') for ref in row['source_refs']] == [saved_quote]
    assert datetime.fromisoformat(row['created_at']) == NOW.replace(hour=8)


def test_owner_owed_capture_keeps_a_meeting_note_citation():
    """A meeting note is a real source the message snapshot cannot hold; a failed bind is no disproof."""
    note = action('send', 'I will send the deck', 'note-1', contextAsk='Send the deck', owedBy='owner',
                  newObligation=False, channel='gmail', contactIdentifier=EMAIL)
    note['evidence'][0]['sourceType'] = 'granola'
    result = cr.resolve({'today': '2026-11-10', 'new_actions': [note], 'local': {'emails': []}},
                        NOW, resolve_existing=False)
    assert len(result['active']) == 1 and result['capture_outcomes']['source_rejected'] == 0


@pytest.mark.parametrize('channel', ['imessage', 'whatsapp'])
def test_first_capture_uses_bridge_instant_when_mac_and_owner_time_zones_differ(channel):
    quote = 'Can you send the updated deck?'
    candidate = action('reply', quote, 'ask-1', contextAsk='Send the updated deck', owedBy='owner',
                       newObligation=False, channel=channel)
    candidate['evidence'][0]['sourceType'] = channel
    # The Mac displays Tokyo time while the owner is configured for UTC. The scan runs at 10:00Z.
    message = dict(text('ask-1', quote, False), timestamp='2026-11-10 17:00:00',
                   timestamp_utc='2026-11-10T08:00:00Z')
    result = cr.resolve({'today': '2026-11-10', 'local': {channel: [message]}, 'new_actions': [candidate]},
                        NOW, resolve_existing=False)
    row, = result['active']
    assert datetime.fromisoformat(row['created_at']) == NOW.replace(hour=8)


@pytest.mark.parametrize('channel', ['gmail', 'imessage', 'whatsapp'])
def test_stale_gmail_id_exemption_is_specific_to_its_channel(channel):
    source_id, quote = '18fbb48e72a108d4', 'I will send the updated deck'
    stale = {'id': source_id, 'threadId': 't1', 'labelIds': ['SENT'], 'date': '2026-10-01T02:00:00Z',
             'from': 'owner@example.com', 'to': EMAIL, 'snippet': quote}
    candidate = action('send', quote, source_id, contextAsk='Send the updated deck', owedBy='owner',
                       newObligation=False, channel=channel, contactIdentifier=EMAIL if channel == 'gmail' else PHONE)
    candidate['evidence'][0]['sourceType'] = channel
    local = {'emails': [], 'imessage': [], 'whatsapp': [], 'stale_sent': [stale]}
    result = cr.resolve({'today': '2026-11-10', 'local': local, 'new_actions': [candidate]},
                        NOW, resolve_existing=False)
    assert len(result['active']) == (1 if channel == 'gmail' else 0)
    assert result['capture_outcomes']['source_rejected'] == (0 if channel == 'gmail' else 1)
