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
