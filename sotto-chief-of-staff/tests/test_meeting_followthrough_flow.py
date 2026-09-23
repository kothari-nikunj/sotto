"""Offline contract from grounded meeting notes through prep and evidence-bound closure."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
for directory in ('_shared/lib', '_shared/scripts', 'followup/scripts',
                  'morning-brief/scripts', 'meeting-prep/scripts'):
    sys.path.insert(0, str(ROOT / directory))

import capture_commitments as capture
import compose_meeting_prep as prep
import continuity_resolve as resolver
from delivery_effects import loop_version


def test_notes_to_prep_to_specific_fulfillment_once(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.setattr(capture.source_context, 'allowed', lambda source: source == 'granola')
    monkeypatch.setattr(capture.apply_commitments.cr, '_now_local',
                        lambda _tz: datetime(2026, 9, 18, 18))
    meeting = {
        'meeting_id': 'granola-1', 'title': 'Dana sync',
        'start': '2026-09-18T16:00:00Z', 'end': '2026-09-18T17:00:00Z',
        'attendees': [{'email': 'nikunj@example.com', 'name': 'Nikunj Kothari'},
                      {'email': 'dana@acme.com', 'name': 'Dana'}],
        'attendee_emails': ['nikunj@example.com', 'dana@acme.com'],
        'your_notes': "Nikunj: I'll send Dana the deck.",
    }
    monkeypatch.setattr(capture.compose_followup, 'build_context',
                        lambda inputs, _hours: ('grounded', inputs['granola']))
    monkeypatch.setattr(capture.compose_followup, 'compose', lambda *_args, **_kwargs: {
        'followup_markdown': 'Send Dana the deck.', 'drafts': [],
        'commitments': [{'meeting_id': 'granola-1', 'owner': 'Nikunj',
                         'owner_is_user': True, 'what': 'send Dana the deck',
                         'to_email': 'dana@acme.com',
                         'source_snippet': "Nikunj: I'll send Dana the deck."}]})

    captured = capture.extract_apply_meeting(
        meeting, 'nikunj@example.com', data_root=str(tmp_path))
    assert captured['ledger']['written'] == 1
    anchor = captured['ledger']['recorded_anchor_keys'][0]
    row = resolver._load_items()[anchor]
    assert row['resolution_mode'] == 'source_grounded' and row['status'] == 'open'

    upcoming = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    context, _ = prep.build_context({'google': {'userEmail': 'nikunj@example.com', 'events': [{
        'id': 'next-dana', 'summary': 'Dana follow-up', 'start': upcoming,
        'attendees': [{'email': 'nikunj@example.com', 'self': True},
                      {'email': 'dana@acme.com', 'displayName': 'Dana'}]}]}})
    assert 'send Dana the deck' in context and '[you owe]' in context

    unrelated = {'id': 'sent-unrelated', 'threadId': 'dana-thread', 'isSent': True,
                 'to': 'dana@acme.com', 'date': '2026-09-19T10:00:00Z',
                 'body': 'Thanks for lunch yesterday.'}
    invalid_update = {'loopId': anchor, 'loopVersion': loop_version(row), 'status': 'resolved',
                      'evidence': [{'sourceType': 'email', 'sourceId': unrelated['id'],
                                    'snippet': unrelated['body']}]}
    still_open = resolver.resolve({'today': '2026-09-19', 'emails': [unrelated],
                                   'loop_updates': [invalid_update]},
                                  datetime(2026, 9, 19, 12), resolve_existing=False)
    assert any(item['anchor_key'] == anchor for item in still_open['active'])

    fulfilled = {'id': 'sent-deck', 'threadId': 'dana-thread', 'isSent': True,
                 'to': 'dana@acme.com', 'date': '2026-09-19T11:00:00Z',
                 'body': 'Here is the deck I promised.'}
    current = resolver._load_items()[anchor]
    update = {'loopId': anchor, 'loopVersion': loop_version(current), 'status': 'resolved',
              'evidence': [{'sourceType': 'email', 'sourceId': 'sent-deck',
                            'snippet': 'Here is the deck I promised.'}]}
    closed = resolver.resolve({'today': '2026-09-19', 'emails': [fulfilled],
                               'loop_updates': [update]},
                              datetime(2026, 9, 19, 12), resolve_existing=False)
    assert [item['anchor_key'] for item in closed['resolved']] == [anchor]
    replay = resolver.resolve({'today': '2026-09-19', 'emails': [fulfilled],
                               'loop_updates': [update]},
                              datetime(2026, 9, 19, 12), resolve_existing=False)
    assert replay['resolved'] == [] and resolver._load_items()[anchor]['status'] == 'resolved'
