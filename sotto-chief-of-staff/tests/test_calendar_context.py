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
