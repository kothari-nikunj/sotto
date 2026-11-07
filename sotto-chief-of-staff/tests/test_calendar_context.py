"""Supporting prep is evidence for one meeting, not a second calendar commitment."""
from copy import deepcopy

from calendar_context import meeting_events


def pair():
    when = {'start': '2026-09-09T12:30:00-07:00', 'end': '2026-09-09T13:00:00-07:00'}
    return [{'id': 'call', 'summary': 'Zoom | Acme <> Example Ventures', 'description': 'Join the call', **when},
            {'id': 'notes', 'summary': 'CONTEXT: Alex Smith (Acme)', 'description': 'Review the workbook', **when}]


def test_explicit_matching_context_keeps_notes_without_a_false_conflict():
    rows = pair()
    original = deepcopy(rows)
    result = meeting_events(rows)
    assert len(result) == 1 and result[0]['id'] == 'call'
    assert result[0]['supporting_context'] == [rows[1]]
    assert 'Join the call' in result[0]['description'] and 'Review the workbook' in result[0]['description']
    assert rows == original and meeting_events(result) == result


def test_real_overlap_ambiguous_subject_and_separate_prep_are_preserved():
    rows = pair()
    rows[1]['summary'] = 'Another meeting (Acme)'
    assert len(meeting_events(rows)) == 2
    rows = pair()
    rows.append({**rows[0], 'id': 'second-real-meeting'})
    assert len(meeting_events(rows)) == 3
    rows = pair()
    rows[1]['end'] = '2026-09-09T13:30:00-07:00'
    assert len(meeting_events(rows)) == 2
    rows = pair()
    rows[1]['summary'] = 'CONTEXT: Alex (Unrelated)'
    assert len(meeting_events(rows)) == 2


def test_context_needs_a_timed_meeting_and_whole_subject_match():
    rows = pair()
    rows[0]['summary'] = 'Acmeology sales'
    assert len(meeting_events(rows)) == 2
    rows = pair()
    for row in rows:
        row.update(start='2026-09-09', end='2026-09-10')
    assert len(meeting_events(rows)) == 2


def test_shared_calendar_self_is_calendar_owner_not_connected_owner():
    from calendar_context import user_participates, work_attendees
    event = {'calendar_id': 'colleague@example.com',
             'organizer': {'email': 'colleague@example.com', 'self': True},
             'attendees': [{'email': 'colleague@example.com', 'self': True, 'responseStatus': 'accepted'},
                           {'email': 'founder@startup.test'}]}
    assert not user_participates(event, 'owner@example.com')
    assert work_attendees(event, 'owner@example.com') == []
    event['attendees'].append({'email': 'owner@example.com', 'responseStatus': 'accepted'})
    assert user_participates(event, 'owner@example.com')
    assert {a['email'] for a in work_attendees(event, 'owner@example.com')} == {'founder@startup.test'}
    event['attendees'][-1]['responseStatus'] = 'declined'
    assert not user_participates(event, 'owner@example.com')


def test_invite_copies_collapse_only_exact_uid_occurrence_preserving_receipt_ids(monkeypatch):
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    own = {**pair()[0], 'id': 'own-id', 'calendar_id': 'owner@example.com', 'iCalUID': 'invite', 'my_response': 'declined'}
    other = {**own, 'id': 'shared-id', 'calendar_id': 'other@example.com', 'my_response': 'accepted'}
    rows = meeting_events([other, own])
    assert len(rows) == 1 and rows[0]['id'] == 'own-id' and rows[0]['my_response'] == 'declined'
    assert rows[0]['_calendar_copy_ids'] == ['own-id', 'shared-id']
    assert meeting_events(rows) == rows
    assert len(meeting_events([own, {**other, 'iCalUID': 'different-invite'}])) == 2
    assert len(meeting_events([own, {**other, 'start': '2026-09-10T12:30:00-07:00', 'end': '2026-09-10T13:00:00-07:00'}])) == 2


def test_explicit_personal_primary_calendar_proves_owner_solo_slot():
    from calendar_context import user_participates
    assert user_participates({'calendar_id': 'owner@example.com'}, 'owner@example.com')
    assert user_participates({'calendar_id': 'opaque-primary', '_calendar_primary': True}, 'owner@example.com')
    assert not user_participates({'calendar_id': 'colleague@example.com'}, 'owner@example.com')
    assert not user_participates({'calendar_id': 'owner@example.com', 'my_response': 'declined'}, 'owner@example.com')


def test_all_day_copies_merge_only_by_provider_uid_and_exact_date_occurrence():
    base = {'id': 'own', 'calendar_id': 'primary', 'iCalUID': 'invite',
            'summary': 'Office day', 'start': '2026-09-21', 'end': '2026-09-22'}
    copy = {**base, 'id': 'team-copy', 'calendar_id': 'team'}
    unrelated = {**base, 'id': 'unrelated', 'iCalUID': 'other'}
    following = {**base, 'id': 'next', 'start': '2026-09-22', 'end': '2026-09-23'}
    invalid = {**base, 'id': 'invalid', 'start': '2026-09-31', 'end': '2026-10-01'}
    unknown = {**base, 'id': 'unknown', 'iCalUID': ''}
    result = meeting_events([copy, base, unrelated, following, invalid, unknown], 'owner@example.com')
    assert {row['id'] for row in result} == {'own', 'unrelated', 'next', 'invalid', 'unknown'}
    assert next(row for row in result if row['id'] == 'own')['_calendar_copy_ids'] == ['own', 'team-copy']
    assert meeting_events(result, 'owner@example.com') == result


def test_busy_time_respects_owner_participation_and_provider_free_status():
    from calendar_context import blocks_time
    owner = 'owner@example.com'
    primary = {'calendar_id': 'primary'}
    assert blocks_time(primary, owner)
    assert blocks_time({**primary, 'eventType': 'focusTime'}, owner)
    assert blocks_time({**primary, 'eventType': 'outOfOffice'}, owner)
    for change in ({'transparency': 'transparent'}, {'status': 'cancelled'},
                   {'my_response': 'declined'}, {'eventType': 'workingLocation'}):
        assert not blocks_time({**primary, **change}, owner)
    shared = {'calendar_id': 'colleague@example.com',
              'attendees': [{'email': 'colleague@example.com', 'self': True}]}
    assert not blocks_time(shared, owner)
    shared['attendees'].append({'email': owner, 'responseStatus': 'accepted'})
    assert blocks_time(shared, owner)
    shared['attendees'][-1]['responseStatus'] = 'declined'
    assert not blocks_time(shared, owner)
    assert blocks_time({}, owner)  # unscoped legacy primary rows still reserve time
    assert not blocks_time({'attendees': [{'self': True, 'email': 'alias@example.com',
                                         'responseStatus': 'declined'}]}, owner)


def test_nonblocking_calendar_context_stays_visible_without_claiming_a_conflict(monkeypatch):
    from render_local import _format_calendar
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    event = {**pair()[0], 'calendar_id': 'primary', 'transparency': 'transparent'}
    rendered = _format_calendar([event])
    assert event['summary'] in rendered and event['start'] in rendered
    assert '(does not block your time)' in rendered
    assert '(does not block your time)' not in _format_calendar([{**event, 'transparency': 'opaque'}])


def test_distinct_recurring_occurrences_moved_to_same_slot_remain_distinct():
    base = {'id': 'one', 'calendar_id': 'primary', 'iCalUID': 'series', 'recurringEventId': 'series-id',
            'start': '2026-10-15T10:00:00Z', 'end': '2026-10-15T11:00:00Z',
            'originalStartTime': {'dateTime': '2026-10-14T10:00:00Z'}}
    other = {**base, 'id': 'two', 'originalStartTime': {'dateTime': '2026-10-15T10:00:00Z'}}
    copy = {**base, 'id': 'copy', 'calendar_id': 'team',
            'originalStartTime': {'dateTime': '2026-10-14T12:00:00+02:00'}}
    assert {r['id'] for r in meeting_events([base, other, copy])} == {'one', 'two'}


def test_shared_calendar_render_preserves_participation_and_availability(monkeypatch):
    from render_local import _format_calendar
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    event = {**pair()[0], 'calendar_id': 'colleague@example.com',
             '_owner_participates': False,
             'attendees': [{'email': 'colleague@example.com', 'self': True}]}
    rendered = _format_calendar([event])
    assert 'owner is not participating, not their meeting or RSVP' in rendered
    assert '(does not block your time)' in rendered
