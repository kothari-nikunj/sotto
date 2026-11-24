"""Bounded calendar discovery keeps useful positive observations without inventing absence."""
from google_calendar_read import read


def test_calendar_inventory_over_bound_keeps_bounded_partial_events():
    fetched = []
    rows = read(lambda page: {'items': [{'id': f'cal{i}', 'accessRole': 'reader'} for i in range(51)]},
                lambda cal, page: fetched.append(cal) or {'items': [{'id': 'event', 'start': {'dateTime': '2026-10-13T09:00:00Z'}}]})
    assert len(fetched) == 50 and len(rows) == 50 and rows.complete is False


def test_event_only_scope_fallback_preserves_primary_but_not_complete_coverage():
    def unavailable(page):
        raise PermissionError('scope')
    rows = read(unavailable, lambda cal, page: {'items': [{'id': 'event'}]})
    assert rows[0]['calendar_id'] == 'primary' and rows.complete is False
    assert rows.calendars == [{'calendar_id': '*', 'status': 'unavailable'}, {'calendar_id': 'primary', 'status': 'ok'}]


def test_chat_window_orders_across_calendars_before_output_cap():
    rows = read(lambda page: {'items': [{'id': 'later', 'accessRole': 'reader'}, {'id': 'early', 'accessRole': 'reader'}]},
                lambda cal, page: {'items': [{'id': str(i), 'start': {'dateTime': f'2026-10-{15 if cal == "later" else 13}T09:00:00Z'}} for i in range(100)]}, max_items=100)
    assert len(rows) == 200
    assert {row['calendar_id'] for row in rows[:100]} == {'early'}


def test_hidden_calendar_requires_explicit_selection():
    fetched = []
    inventory = lambda page: {'items': [{'id': 'hidden', 'hidden': True, 'accessRole': 'reader'}, {'id': 'shown', 'accessRole': 'reader'}]}
    events = lambda cal, page: fetched.append(cal) or {'items': []}
    assert read(inventory, events).complete
    assert fetched == ['shown']
    fetched.clear()
    read(inventory, events, selected={'mode': 'selected', 'calendar_ids': ['hidden']})
    assert fetched == ['hidden']


def test_next_from_never_passes_unfetched_events_of_a_capped_calendar():
    # Review of H4: a busy calendar capped at 100 rows plus a sparse later calendar. The first
    # omitted merged row belonged to the sparse one, so the busy calendar's rest was skipped.
    from datetime import datetime, timedelta, timezone
    from google_calendar_read import fill
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    def ev(i, when):
        stamp = when.strftime('%Y-%m-%dT%H:%M:%SZ')
        return {'id': f'e{i}', 'start': {'dateTime': stamp}, 'end': {'dateTime': stamp}}
    work = [ev(i, base + timedelta(hours=2 * i)) for i in range(150)]
    family = [ev(1000 + i, base + timedelta(days=20 + i)) for i in range(3)]
    rows = read(lambda page: {}, lambda cal, page: ({'items': work[:100], 'nextPageToken': 'p2'} if cal == 'work'
                                                     else {'items': family}),
                selected={'mode': 'selected', 'calendar_ids': ['family', 'work']}, max_items=100)
    out = fill(list(rows), lambda r: {'id': r['id']}, 60000, '2026-11-01T00:00:00Z', resume_by=rows.resume_by)
    assert out['truncated'] is True and out['next_from'] <= work[100]['start']['dateTime']
    # Everything merged fits, but a capped calendar still means the read is not finished.
    out = fill(list(rows)[:5], lambda r: {'id': r['id']}, 60000, '2026-11-01T00:00:00Z', resume_by=rows.resume_by)
    assert out['truncated'] is True and out['next_from'] == work[99]['start']['dateTime']


def test_a_cursor_that_cannot_advance_is_withheld_and_the_result_says_so():
    # Eight parallel sessions with 100 invitees each exceed one result, so the first omitted event
    # starts at the instant the read started from. Google returns every event still running at
    # `start`, so reading on from there would return the same page forever.
    from google_calendar_read import fill

    def ev(i, begin, end, invitees):
        return {'id': f'e{i}', 'start': {'dateTime': begin}, 'end': {'dateTime': end}, 'description': 'agenda ' * 500,
                'attendees': [{'email': f'person{n:03d}@example.com', 'responseStatus': 'needsAction'} for n in range(invitees)]}
    sessions = [ev(i, '2026-11-10T10:00:00Z', '2026-11-10T11:00:00Z', 100) for i in range(8)]
    later = ev(9, '2026-11-10T15:00:00Z', '2026-11-10T15:30:00Z', 0)
    budget = 64 * 1024 - 1024 - 60
    first = fill(sessions + [later], lambda row: row, budget, '2026-11-10T00:00:00Z')
    assert first['truncated'] is True and first['next_from'] == '2026-11-10T10:00:00Z'   # this one advances
    stuck = fill(sessions + [later], lambda row: row, budget, first['next_from'])
    assert [item['id'] for item in stuck['items']] == [item['id'] for item in first['items']]
    assert stuck['truncated'] is True and 'next_from' not in stuck
    assert stuck['next_from_unavailable'] == 'more events overlap 2026-11-10T10:00:00Z than one result holds; read on with a start after they end'
    # A capped calendar that stopped at the start instant cannot advance either.
    capped = fill(sessions[:1], lambda row: row, budget, '2026-11-10T10:00:00Z', resume_by='2026-11-10T10:00:00Z')
    assert capped['truncated'] is True and 'next_from' not in capped and 'next_from_unavailable' in capped
